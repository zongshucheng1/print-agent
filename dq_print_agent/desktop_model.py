"""Desktop operations for the current multi-printer model; diagnostics never print."""
from __future__ import annotations
from contextlib import contextmanager
from functools import wraps
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from typing import Mapping
from .config import Config, HostConfig
from .http_client import ApiError
from .log_records import redact_log_line
from .store import instance_lock, pending_observation_count
from .windows_print import PrintAdapterError

class DesktopError(RuntimeError):
    """An intentionally user-safe error; never include arbitrary exceptions."""

def _installed_data_dir() -> Path | None:
    if os.name != "nt":
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Deqin\PrintAgent", 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            value, kind = winreg.QueryValueEx(key, "DataDir")
    except FileNotFoundError:
        return None
    directory = Path(value) if kind == winreg.REG_SZ else None
    if not directory or not directory.is_absolute() or len(directory.drive) != 2 or directory == directory.parent:
        raise DesktopError("安装记录中的数据目录无效，请重新运行安装程序选择目录。")
    return directory

def default_config_path() -> Path:
    directory = _installed_data_dir()
    if directory is not None:
        return directory / "config.json"
    root = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    return root / "Deqin" / "PrintAgent" / "runtime" / "config.json"

def _safe_operation(function):
    @wraps(function)
    def run(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except DesktopError:
            raise
        except ApiError as error:
            if error.status in (401, 403):
                message = "服务器拒绝此接入密钥，请检查是否已撤销或重新配发。"
            elif error.code == "API_UNREACHABLE":
                message = "无法连接后端，请检查后端地址、网络与服务状态。"
            else:
                message = "服务器未返回有效的客户端配置，请检查后端日志与接口版本。"
            raise DesktopError(message) from None
        except PrintAdapterError:
            raise DesktopError("打印机检查未通过，请确认 Windows 队列、驱动和 80×80 纸张设置。") from None
        except (PermissionError, OSError):
            raise DesktopError("无法访问本机配置或打印设备，请检查目录权限和驱动；服务管理可能需要管理员权限。") from None
        except sqlite3.Error:
            raise DesktopError("无法读取本地任务记录，请保留数据目录并检查客户端日志。") from None
        except Exception:
            raise DesktopError("操作未完成，请检查配置和本地日志；原任务记录已保留。") from None
    return run

def _atomic_json(path: Path, value: dict) -> None:
    """Replace a known application-owned file only after a flushed temp write."""
    handle, temporary = tempfile.mkstemp(prefix=".desktop-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def _principal(value) -> dict[str, str]:
    if not isinstance(value, dict):
        raise DesktopError("服务器机器身份格式异常，请核对后台配发的客户端。")
    result = {}
    for key in ("agentId", "printerId", "tenantId", "factoryId"):
        item = value.get(key)
        pattern = r"[A-Za-z0-9_-]{1,64}" if key == "tenantId" else r"[1-9][0-9]{0,18}"
        if not isinstance(item, str) or not re.fullmatch(pattern, item):
            raise DesktopError("服务器机器身份格式异常，请核对后台配发的客户端。")
        result[key] = item
    return result

def _valid_token(token: str) -> str:
    if not isinstance(token, str):
        raise DesktopError("请粘贴后台配发的机器接入密钥。")
    value = token.strip()
    if not re.fullmatch(r"pa1\.[1-9][0-9]{0,18}\.[A-Za-z0-9_-]{43}", value):
        raise DesktopError("机器接入密钥格式不正确，请复制后台本次配发的完整密钥。")
    return value

class DesktopModel:
    def __init__(self, config_path: str | Path | None = None, *, status_reader=None):
        self.config_path = Path(config_path or default_config_path()).resolve()
        self.status_reader = status_reader

    @contextmanager
    def _configuration_lock(self):
        try:
            guard = instance_lock(self.config_path.parent / "desktop-config.lock")
            guard.__enter__()
        except RuntimeError:
            raise DesktopError("其他窗口正在保存配置或客户端正在启动，请稍后重新读取配置。") from None
        try:
            yield
        finally:
            guard.__exit__(None, None, None)

    def _require_stopped(self):
        if self.status_reader is None:
            from .windows_service import service_status
            status = service_status()
        else:
            status = self.status_reader()
        if status.get("installed"):
            if not status.get("owned") or not status.get("configPath") or Path(status["configPath"]).resolve() != self.config_path:
                raise DesktopError("检测到其他安装目录或配置的打印服务，不能切换当前服务器。")
            if status.get("state") != "STOPPED":
                raise DesktopError("请先在“运行状态”停止打印服务，确认已停止后再切换服务器。")
        elif status.get("state") != "NOT_INSTALLED":
            raise DesktopError("无法确认打印服务已停止，请刷新服务状态后再切换。")

    @staticmethod
    def _open_journal(config: Config):
        path = config.state_dir / "journal.sqlite3"
        if not path.exists():
            return None
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    @staticmethod
    def _switch_evidence(config: Config) -> dict:
        connection = DesktopModel._open_journal(config)
        if connection is None:
            return {"pendingReports": 0, "pendingObservations": 0, "unfinishedAttempts": 0, "unknownAttempts": 0, "preparedAttempts": 0}
        try:
            return {
                "pendingReports": connection.execute("SELECT COUNT(*) FROM reports WHERE scope=? AND acknowledged=0", (config.scope,)).fetchone()[0],
                "pendingObservations": pending_observation_count(connection, config.scope),
                "unfinishedAttempts": connection.execute("SELECT COUNT(*) FROM attempts WHERE scope=? AND stage IN ('INTENT','AUTHORIZED')", (config.scope,)).fetchone()[0],
                "unknownAttempts": connection.execute("SELECT COUNT(*) FROM attempts WHERE scope=? AND stage='UNKNOWN'", (config.scope,)).fetchone()[0],
                "preparedAttempts": connection.execute("SELECT COUNT(*) FROM attempts WHERE scope=? AND stage='PREPARED'", (config.scope,)).fetchone()[0],
            }
        finally:
            connection.close()

    @_safe_operation
    def log_tail(self, lines: int = 100) -> str:
        config = self._load_config()
        path = (config.state_dir if config else self.config_path.parent) / "agent.log"
        if not path.is_file():
            return "尚无客户端运行日志。"
        with path.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 256 * 1024))
            content = stream.read().decode("utf-8", errors="replace")
        tail = content.splitlines()[-max(1, min(int(lines), 500)):]
        return "\n".join(redact_log_line(line) for line in tail)

    @_safe_operation
    def inspect_service_printer(self, printer_name: str) -> dict:
        from .desktop_multi import MultiDesktop
        return MultiDesktop(self).service_inspect(printer_name)

    @_safe_operation
    def sync_printers(self) -> dict:
        from .desktop_multi import MultiDesktop
        return MultiDesktop(self).synchronize()

    @_safe_operation
    def save_printer(self, fields: Mapping, printer_id=None) -> dict:
        from .desktop_multi import MultiDesktop
        return MultiDesktop(self).upsert(fields, printer_id)

    @_safe_operation
    def delete_printer(self, printer_id) -> dict:
        from .desktop_multi import MultiDesktop
        return MultiDesktop(self).remove(printer_id)

    @_safe_operation
    def set_printer_enabled(self, printer_id, enabled: bool) -> dict:
        from .desktop_multi import MultiDesktop
        return MultiDesktop(self).pause(printer_id, enabled)

    def _load_config(self) -> HostConfig | None:
        if not self.config_path.exists():
            return None
        try:
            return HostConfig.load(self.config_path)
        except Exception:
            raise DesktopError('配置必须是当前多打印机格式（schemaVersion: 2），请核对配置文件。') from None

    @staticmethod
    def _settings(config, config_path):
        from .desktop_multi import settings
        if config is not None:
            return settings(config, config_path)
        return {'configured': False, 'schema_version': 2, 'config_path': str(config_path),
                'state_dir': str(config_path.parent), 'credential_saved': False,
                'api_url': 'http://127.0.0.1:8080', 'download_origins': [],
                'allow_loopback_http': True, 'physical_printing_enabled': False, 'printers': []}

    @_safe_operation
    def load(self):
        return self._settings(self._load_config(), self.config_path)

    def _saved_principal(self, config: Config):
        connection = self._open_journal(config)
        if connection is None:
            return None
        try:
            row = connection.execute("SELECT value FROM metadata WHERE key='runtimeBinding'").fetchone()
            if row is None:
                raise DesktopError('任务记录缺少机器身份，请保留记录并核对。')
            value = json.loads(row['value'])
            expected = {**config.host_principal, 'printerId': config.printer_id}
            principal = _principal(value.get('principal'))
            if value.get('scope') != config.scope or principal != expected:
                raise DesktopError('任务记录属于其他客户端或打印机，不能重新初始化。')
            return principal
        finally:
            connection.close()

    @_safe_operation
    def inspect_server(self, fields=None, token=''):
        from .desktop_multi import MultiDesktop
        return MultiDesktop(self).inspect(fields, token)

    @_safe_operation
    def save(self, fields, token=''):
        from .desktop_multi import MultiDesktop
        return MultiDesktop(self).save(fields, token)

    @_safe_operation
    def inspect_server_switch(self, fields, token):
        from .desktop_multi import MultiDesktop
        return MultiDesktop(self).switch(fields, token, inspect_only=True)

    @_safe_operation
    def switch_server(self, fields, token):
        from .desktop_multi import MultiDesktop
        return MultiDesktop(self).switch(fields, token)

    @_safe_operation
    def registered_queue_metadata(self, registered):
        """Read connection details for display when the service account is unavailable."""
        from .process_print import IsolatedPrinterAdapter
        names = {row['queueName'].casefold() for row in registered}
        return [{'name': row['name'], 'port': row.get('port'), 'port_target': row.get('port_target'),
                 'serviceVerified': False}
                for row in IsolatedPrinterAdapter().list_printers()
                if row.get('name') and row['name'].casefold() in names]

    @_safe_operation
    def printers(self):
        from .service_rpc import request
        from .desktop_multi import MultiDesktop
        config = self._load_config()
        if config is None:
            raise DesktopError('请先保存连接配置并启动服务，再读取服务账户打印队列。')
        status = self.status_reader() if self.status_reader else MultiDesktop._service_status()
        if not (status.get('owned') and status.get('state') == 'RUNNING' and Path(status.get('configPath') or '').resolve() == self.config_path):
            raise DesktopError('请先启动当前配置的打印服务，再刷新服务账户可访问的队列。')
        try:
            result = request(config.state_dir, 'list', timeout=8)
            return [{**row, 'serviceVerified': True} for row in result['printers']]
        except RuntimeError:
            raise DesktopError('服务账户队列查询未完成，请检查服务状态后重试。') from None

    @_safe_operation
    def inspect_printer(self, printer_name):
        return self.inspect_service_printer(printer_name)

    @_safe_operation
    def network_inventory(self):
        from . import network_setup
        try:
            return network_setup.inventory()
        except network_setup.NetworkSetupError as error:
            raise DesktopError(str(error)) from None

    @_safe_operation
    def discover_network(self, interface, network, cancel, ip=None):
        from . import network_setup, network_discovery
        try:
            started = time.monotonic()
            def stopped(timed_out=False):
                return {'devices': [], 'scanned': 0, 'total': 0, 'cancelled': bool(cancel and cancel.is_set()),
                        'timedOut': timed_out, 'network': network, 'serviceVerified': False}
            if cancel is not None and cancel.is_set():
                return stopped()
            current = network_setup.inventory()
            if cancel is not None and cancel.is_set():
                return stopped()
            remaining = 20 - (time.monotonic() - started)
            if remaining <= 0:
                return stopped(True)
            selected = network_setup.resolve_interface(interface, current)
            result = network_discovery.discover(selected, network, cancel, ip=ip,
                local_addresses=[item['address'] for item in current['interfaces']], timeout_seconds=remaining)
            for device in result['devices']:
                matches = [q for q in current['queues'] if q['ip'] == device['ip'] and q['port'] in device.get('ports', [])]
                device['queueNames'] = [q['name'] for q in matches]
                device['rawQueue'] = next((q for q in matches if q['port'] == 9100), None)
            result['drivers'] = current['drivers']
            result['serviceVerified'] = False
            return result
        except (network_setup.NetworkSetupError, ValueError) as error:
            raise DesktopError(str(error)) from None

    @_safe_operation
    def add_network_queue(self, fields):
        from . import network_setup
        try:
            # Serialize this application's desktop mutations. Does not start a
            # service, write host config, register a backend target or print.
            with self._configuration_lock():
                return network_setup.add_queue(fields)
        except network_setup.NetworkSetupError as error:
            raise DesktopError(str(error)) from None

    @_safe_operation
    def local_status(self):
        from .desktop_multi import MultiDesktop
        config = self._load_config()
        if config is None:
            return {'configured': False, 'started': False, 'pendingReports': 0, 'pendingObservations': 0, 'recent': [], 'printers': []}
        return MultiDesktop(self).local_status(config)
