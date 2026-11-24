"""Desktop operations for host-level identities and independently bound queues."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import replace
import json
from pathlib import Path
import re
import uuid

from .config import HostConfig, PrinterConfig
from .credentials import load_token, save_token, secure_directory
from .http_client import AgentApi, ApiError
from .store import enrich_observations, instance_lock, pending_observation_count
from .desktop_model import DesktopError, _atomic_json, _valid_token


def host_principal(value):
    result = {}
    for key in ('agentId', 'tenantId', 'factoryId'):
        item = value.get(key) if isinstance(value, dict) else None
        pattern = r'[A-Za-z0-9_-]{1,64}' if key == 'tenantId' else r'[1-9][0-9]{0,18}'
        if not isinstance(item, str) or not re.fullmatch(pattern, item):
            raise DesktopError('服务器机器身份无效，请核对登记信息。')
        result[key] = item
    return result


def settings(config: HostConfig, path: Path):
    return {'configured': True, 'schema_version': 2, 
            'config_path': str(path), 'state_dir': str(config.state_dir),
            'credential_saved': config.credential_file.is_file(), 'api_url': config.api_url,
            'download_origins': list(config.download_origins),
            'allow_loopback_http': config.allow_loopback_http,
            'physical_printing_enabled': config.physical_printing_enabled,
            'paper_width_mm': 80, 'paper_height_mm': 80, 'max_concurrent': config.max_concurrent,
            'printers': [{'printerId': p.printer_id, 'queueName': p.printer_name,
                          'printerName': p.display_name or p.printer_name, 'location': p.location,
                          'bindingVersion': p.binding_version, 'queueFingerprint': p.queue_fingerprint,
                          'acceptNewJobs': p.physical_printing_enabled} for p in config.printers if not p.archived]}


class MultiDesktop:
    def __init__(self, model):
        self.model = model

    @staticmethod
    def api(config, token=''):
        api = AgentApi(config, token or load_token(config.credential_file))
        api.base = config.api_url + '/inout/print-agent/v2'
        return api

    def remote(self, config, token=''):
        api = self.api(config, token)
        try:
            remote = api.config_data()
        finally:
            api.close()
        host_principal(remote)
        if not isinstance(remote.get('printers'), list):
            raise DesktopError('后台尚未提供多打印机配置，请先更新后端。')
        return remote

    def require_host(self):
        config = self.model._load_config()
        if not isinstance(config, HostConfig):
            raise DesktopError('请先保存客户端连接配置。')
        return config

    @contextmanager
    def execution_guards(self, *hosts):
        paths = set()
        for host in hosts:
            paths.add(host.state_dir / 'host.lock')
            paths.update(p.state_dir / 'agent.lock' for p in host.printers)
        with ExitStack() as guards:
            try:
                for path in sorted(paths, key=str):
                    guards.enter_context(instance_lock(path))
            except RuntimeError:
                raise DesktopError('客户端或打印进程仍在运行，请停止后再修改机器配置。') from None
            yield

    def validate_profile_layout(self, target, origin):
        base = self.model.config_path.parent
        canonical = base / 'servers' / target.scope
        inherited = origin is not None and target.scope == origin.scope
        expected_dir = origin.state_dir if inherited else canonical
        expected_credential = origin.credential_file if inherited else canonical / 'credential.dpapi'
        if target.state_dir.resolve() != expected_dir.resolve() or target.credential_file.resolve() != expected_credential.resolve():
            raise DesktopError('目标配置的数据目录不符合原登记，请保留证据，不能转用空目录。')
        for profile in target.printers:
            standard = (target.state_dir / 'printers' / profile.printer_id).resolve()
            if profile.state_dir.resolve() != standard:
                raise DesktopError('目标打印机任务目录或历史身份已变化，不能重新初始化。')

    @staticmethod
    def verify_identity(config, remote):
        expected = {'agentId': config.agent_id, 'tenantId': config.tenant_id, 'factoryId': config.factory_id}
        if host_principal(remote) != expected:
            raise DesktopError('此密钥属于另一台电脑或工厂，原配置不能被覆盖。')

    @staticmethod
    def profiles(config, remote):
        result = []
        previous = {p.printer_id: p for p in config.printers}
        for row in remote['printers']:
            printer_id = str(row.get('printerId', ''))
            if not re.fullmatch(r'[1-9][0-9]{0,18}', printer_id):
                raise DesktopError('后台打印机编号无效。')
            old = previous.pop(printer_id, None)
            snapshot = row.get('configSnapshot') or {}
            name = row.get('queueName') or snapshot.get('printerName')
            fingerprint = row.get('queueFingerprint') or ''
            version = int(row.get('bindingVersion', 0))
            if not isinstance(name, str) or not name.strip() or version < 1:
                raise DesktopError('后台打印机配置不完整。')
            if old:
                changed = old.printer_name != name or old.binding_version != version or old.queue_fingerprint != fingerprint
                result.append(replace(old, printer_name=name, binding_version=version, queue_fingerprint=fingerprint,
                                      display_name=str(row.get('printerName') or name), location=str(row.get('location') or ''),
                                      physical_printing_enabled=old.physical_printing_enabled and not changed and row.get('enabled') in (True, 1)))
            else:
                result.append(PrinterConfig(printer_id=printer_id, printer_name=name,
                    state_dir=config.state_dir / 'printers' / printer_id, binding_version=version,
                    display_name=str(row.get('printerName') or name), location=str(row.get('location') or ''),
                    queue_fingerprint=fingerprint, physical_printing_enabled=False))
        # Archived bindings disappear from the active server list. Keep their journals for receipts.
        supports_archive = remote.get('supportsPrinterArchive') is True
        result.extend(replace(old, archived=old.archived or supports_archive, physical_printing_enabled=False)
                      for old in previous.values())
        return tuple(result)

    def candidate(self, fields, previous=None, *, remote=None):
        api_url = str(fields.get('api_url', previous.api_url if previous else '')).strip().rstrip('/')
        if previous and api_url != previous.api_url:
            raise DesktopError('请使用“切换服务器”更换后端，原任务记录必须保留。')
        origins = fields.get('download_origins', previous.download_origins if previous else ())
        if isinstance(origins, str):
            origins = [x.strip() for x in origins.splitlines() if x.strip()]
        if not isinstance(origins, (list, tuple)) or any(not isinstance(x, str) for x in origins):
            raise DesktopError('标签来源格式无效。')
        base = previous or HostConfig(api_url=api_url, state_dir=self.model.config_path.parent,
            credential_file=self.model.config_path.parent / 'credential.dpapi', server_id=api_url,
            tenant_id='pending', factory_id='1', agent_id='1', printers=())
        config = replace(base, api_url=api_url, download_origins=tuple(origins),
            physical_printing_enabled=fields.get('physical_printing_enabled', base.physical_printing_enabled),
            allow_loopback_http=fields.get('allow_loopback_http', base.allow_loopback_http))
        if remote:
            principal = host_principal(remote)
            config = replace(config, agent_id=principal['agentId'], tenant_id=principal['tenantId'], factory_id=principal['factoryId'])
        config.validate()
        return config

    def inspect(self, fields=None, token=''):
        previous = self.model._load_config()
        if previous is None and not token:
            raise DesktopError('首次连接需要后台配发的机器接入密钥。')
        config = self.candidate(fields or {}, previous)
        remote = self.remote(config, _valid_token(token) if token else '')
        if previous is not None:
            self.verify_identity(previous, remote)
        if remote.get('protocolVersion') != 2:
            raise DesktopError('后台未提供当前多打印机协议，请核对服务版本。')
        return remote

    def save(self, fields, token=''):
        with self.model._configuration_lock():
            previous = self.model._load_config()
            config = self.candidate(fields, previous)
            self.model._require_stopped()
            if previous is None:
                if any((config.state_dir / x).exists() for x in ('journal.sqlite3', 'credential.dpapi', 'host-identity.json')):
                    raise DesktopError('目录已有客户端数据，请使用原配置，不可重新初始化。')
                if not token:
                    raise DesktopError('首次配置需要后台配发的机器接入密钥。')
            secure_directory(config.state_dir)
            with self.execution_guards(config):
                supplied = _valid_token(token) if token else load_token(config.credential_file)
                remote = self.remote(config, supplied)
                if remote.get('protocolVersion') != 2:
                    raise DesktopError('后台未提供当前多打印机协议，请核对服务版本。')
                if previous:
                    self.verify_identity(previous, remote)
                config = self.candidate(fields, previous, remote=remote)
                config = replace(config, printers=self.profiles(config, remote))
                config.validate()
                if token:
                    try:
                        save_token(config.credential_file, supplied)
                        _atomic_json(config.state_dir / 'host-identity.json', {'scope': config.scope, 'principal': host_principal(remote)})
                        _atomic_json(self.model.config_path, config.to_dict())
                    except Exception:
                        if previous is None and not self.model.config_path.exists():
                            config.credential_file.unlink(missing_ok=True)
                            (config.state_dir / 'host-identity.json').unlink(missing_ok=True)
                        raise
                else:
                    _atomic_json(self.model.config_path, config.to_dict())
                origin_path = self.model.config_path.parent / 'host-origin.json'
                if previous is None and not origin_path.exists():
                    _atomic_json(origin_path, config.to_dict())
                return settings(config, self.model.config_path)

    def service_inspect(self, name):
        from .service_rpc import request
        config = self.model._load_config()
        if config is None:
            raise DesktopError('请先保存连接配置并启动服务，然后检查服务账户可用的打印机。')
        status = self.model.status_reader() if self.model.status_reader else self._service_status()
        if status.get('state') != 'RUNNING' or not status.get('owned') or Path(status.get('configPath') or '').resolve() != self.model.config_path:
            raise DesktopError('请先启动当前配置的打印服务；检查仅访问驱动，不发送纸张。')
        try:
            result = request(config.state_dir, 'diagnose', name)
        except RuntimeError:
            raise DesktopError('服务账户无法检查该打印机，请核对队列权限、驱动和服务状态。') from None
        if result.get('serviceVerified') is not True or not re.fullmatch(r'[0-9a-f]{64}', result.get('queueFingerprint', '')):
            raise DesktopError('服务未返回有效的队列核验结果。')
        _atomic_json(config.state_dir / 'last-service-check.json', result)
        return result

    @staticmethod
    def _service_status():
        from .windows_service import service_status
        return service_status()

    def synchronize(self):
        with self.model._configuration_lock():
            config = self.require_host()
            pending_path = config.state_dir / 'pending-printer-mutation.json'
            remote = self.remote(config)
            self.verify_identity(config, remote)
            if pending_path.exists():
                pending = json.loads(pending_path.read_text(encoding='utf-8'))
                return self.apply_pending(config, pending, pending_path)
            updated = replace(config, printers=self.profiles(config, remote))
            updated.validate()
            _atomic_json(self.model.config_path, updated.to_dict())
            return settings(updated, self.model.config_path)

    def apply_pending(self, config, pending, pending_path):
        method, path = pending['method'], pending['path']
        editing = re.fullmatch(r'/printers/([1-9][0-9]{0,18})', path) if method == 'PATCH' else None
        removing = re.fullmatch(r'/printers/([1-9][0-9]{0,18})/(archive|disable)', path) if method == 'POST' else None
        if not (editing or removing or (method == 'POST' and path == '/printers/register')):
            raise DesktopError('待确认配置记录无效，请保留文件并核对。')
        api = self.api(config)
        try:
            try:
                api.call(method, path, {**pending['payload'], 'requestKey': pending['requestKey']})
            except ApiError as error:
                # A confirmed validation/conflict response did not commit. Keep uncertain
                # transport failures for idempotent replay using the same request key.
                if error.status in (400, 404, 409, 422):
                    pending_path.unlink(missing_ok=True)
                    if removing and error.status == 409:
                        raise DesktopError('后台拒绝删除。请先处理此打印机已启用的仓库绑定，等待在途打印结束，再同步打印机后重试。') from None
                    if error.status == 409:
                        raise DesktopError('后台登记发生冲突。请同步打印机，并检查队列是否已登记或仍有在途任务。') from None
                raise
            remote = api.config_data()
        finally:
            api.close()
        self.verify_identity(config, remote)
        profiles = self.profiles(config, remote)
        if removing or editing:
            target_id = (removing or editing).group(1)
            profiles = tuple(replace(p, archived=bool(removing), physical_printing_enabled=False)
                             if p.printer_id == target_id and (removing or p.archived) else p for p in profiles)
        updated = replace(config, printers=profiles)
        _atomic_json(self.model.config_path, updated.to_dict())
        pending_path.unlink(missing_ok=True)
        return settings(updated, self.model.config_path)

    def mutate(self, config, method, path, payload):
        pending_path = config.state_dir / 'pending-printer-mutation.json'
        pending = {'path': path, 'method': method, 'payload': payload}
        if pending_path.exists():
            previous = json.loads(pending_path.read_text(encoding='utf-8'))
            if any(previous.get(k) != value for k, value in pending.items()):
                raise DesktopError('有待确认的打印机修改，请先点击“同步打印机”恢复结果。')
            pending = previous
        else:
            pending['requestKey'] = str(uuid.uuid4())
            _atomic_json(pending_path, pending)
        return self.apply_pending(config, pending, pending_path)

    def current_row(self, config, printer, current=None):
        current = current if current is not None else self.remote(config)
        self.verify_identity(config, current)
        row = next((p for p in current['printers'] if str(p['printerId']) == printer.printer_id), None)
        if row is None or row.get('queueFingerprint') != printer.queue_fingerprint or int(row.get('bindingVersion', 0)) != printer.binding_version:
            raise DesktopError('后台绑定已变化，请同步列表后重新检查。')
        return row

    def remove(self, printer_id):
        with self.model._configuration_lock():
            config = self.require_host()
            target = next((p for p in config.printers if not p.archived and p.printer_id == str(printer_id)), None)
            if target is None:
                raise DesktopError('请选择要删除的打印机。')
            current = self.remote(config)
            self.verify_identity(config, current)
            if current.get('supportsPrinterArchive') is not True:
                raise DesktopError('当前后台版本不支持删除打印机，请先更新后台。')
            row = self.current_row(config, target, current)
            return self.mutate(config, 'POST', f'/printers/{target.printer_id}/archive', {'expectedVersion': row['version']})

    def upsert(self, fields, printer_id=None):
        config = self.require_host()
        name = str(fields.get('queueName', '')).strip()
        diagnostic = self.service_inspect(name)
        with self.model._configuration_lock():
            config = self.require_host()
            old = next((p for p in config.printers if p.printer_id == str(printer_id)), None)
            if printer_id and (old is None or old.archived):
                raise DesktopError('打印机已变化，请刷新列表。')
            if not printer_id:
                # Older clients could hide a disabled server registration locally. Only restore
                # such an ID if it is still active on the server.
                archived = [p for p in config.printers if p.archived and p.printer_name.strip().casefold() == name.casefold()]
                if archived:
                    current = self.remote(config)
                    self.verify_identity(config, current)
                    visible_ids = {str(p.get('printerId')) for p in current['printers']}
                    old = next((p for p in archived if p.printer_id in visible_ids), None)
            if (old is None or old.archived) and sum(not p.archived for p in config.printers) >= 10:
                raise DesktopError('一个客户端最多配置10台打印机。')
            if any(p != old and not p.archived and p.printer_name.strip().casefold() == name.casefold() for p in config.printers):
                raise DesktopError('此 Windows 队列已添加，不能重复登记。')
            payload = {'queueName': name, 'queueFingerprint': diagnostic['queueFingerprint'],
                'printerName': str(fields.get('printerName') or name).strip(), 'location': str(fields.get('location') or '').strip(),
                'capabilities': {'driverName': diagnostic.get('driver'), 'portName': diagnostic.get('port')}}
            path = '/printers/' + old.printer_id if old else '/printers/register'
            method = 'PATCH' if old else 'POST'
            if old:
                payload['expectedVersion'] = self.current_row(config, old)['version']
            return self.mutate(config, method, path, payload)

    def pause(self, printer_id, enabled):
        if type(enabled) is not bool:
            raise DesktopError('打印开关无效。')
        config = self.require_host()
        target = next((p for p in config.printers if p.printer_id == str(printer_id)), None)
        if target is None or target.archived:
            raise DesktopError('请先选择一台打印机。')
        if enabled:
            checked = self.service_inspect(target.printer_name)
            if checked['queueFingerprint'] != target.queue_fingerprint:
                raise DesktopError('队列配置已变化，请编辑并重新测试后启用。')
        with self.model._configuration_lock():
            latest = self.require_host()
            current = next((p for p in latest.printers if p.printer_id == str(printer_id)), None)
            if current != target:
                raise DesktopError('打印机配置已变化，请刷新后重试。')
            config = replace(latest, printers=tuple(replace(p, physical_printing_enabled=enabled)
                if p.printer_id == str(printer_id) else p for p in latest.printers))
            _atomic_json(self.model.config_path, config.to_dict())
            return settings(config, self.model.config_path)


    def switch(self, fields, token, *, inspect_only=False):
        with self.model._configuration_lock():
            previous = self.require_host()
            self.model._require_stopped()
            if fields.get('expected_current_api_url', previous.api_url) != previous.api_url or fields.get('expected_state_dir', str(previous.state_dir)) != str(previous.state_dir):
                raise DesktopError('配置已被其他窗口修改，请重新读取。')
            supplied = _valid_token(token)
            probe = self.candidate({**fields, 'physical_printing_enabled': False})
            if probe.api_url == previous.api_url:
                raise DesktopError('服务器地址没有变化；同一服务器更换密钥请保存配置。')
            remote = self.remote(probe, supplied)
            if remote.get('protocolVersion') != 2:
                raise DesktopError('目标服务器必须登记多打印机客户端。')
            target = self.candidate({**fields, 'physical_printing_enabled': False}, remote=remote)
            profile_dir = self.model.config_path.parent / 'host-profiles'
            profile = profile_dir / (target.scope + '.json')
            origin_path = self.model.config_path.parent / 'host-origin.json'
            origin = HostConfig.load(origin_path) if origin_path.exists() else previous
            restored = profile.is_file()
            if restored:
                target = HostConfig.load(profile)
                self.verify_identity(target, remote)
                if target.api_url != probe.api_url:
                    raise DesktopError('目标配置地址不一致，请保留原配置核对。')
                self.validate_profile_layout(target, origin)
                target = replace(target, physical_printing_enabled=False, download_origins=probe.download_origins)
            else:
                folder = self.model.config_path.parent / 'servers' / target.scope
                if folder.exists() and any(folder.iterdir()):
                    raise DesktopError('目标服务器目录已有未登记记录，不能创建空配置接管。')
                target = replace(target, state_dir=folder, credential_file=folder / 'credential.dpapi')
            target = replace(target, printers=self.profiles(target, remote))
            target.validate()
            with self.execution_guards(previous, *([target] if restored else [])):
                for host in (previous, target):
                    if (host.state_dir / 'pending-printer-mutation.json').exists():
                        raise DesktopError('打印机配置仍有待确认操作，请先同步恢复后切换。')
                    for printer in host.printers:
                        view = host.for_printer(printer)
                        self.model._saved_principal(view)
                        evidence = self.model._switch_evidence(view)
                        if any(evidence[key] for key in ('pendingReports', 'pendingObservations', 'unfinishedAttempts')):
                            raise DesktopError('原环境存在待补报或尚未收尾的任务，请处理后再切换。')
                if inspect_only:
                    return remote
                secure_directory(target.state_dir)
                profile_dir.mkdir(exist_ok=True)
                if not origin_path.exists():
                    _atomic_json(origin_path, origin.to_dict())
                _atomic_json(profile_dir / (previous.scope + '.json'), previous.to_dict())
                # Register the immutable target before writing credentials; a crash can resume.
                _atomic_json(profile, target.to_dict())
                with ExitStack() as target_guard:
                    if not restored:
                        target_guard.enter_context(self.execution_guards(target))
                    save_token(target.credential_file, supplied)
                    _atomic_json(target.state_dir / 'host-identity.json', {'scope': target.scope, 'principal': host_principal(remote)})
                    _atomic_json(self.model.config_path, target.to_dict())
            return {**settings(target, self.model.config_path), 'restoredExistingServer': restored}

    def local_status(self, config):
        result = {'configured': True, 'started': False, 'attempts': {}, 'pendingReports': 0, 'pendingObservations': 0, 'recent': [], 'printers': []}
        runtime_path = config.state_dir / 'runtime-status.json'
        if runtime_path.is_file() and runtime_path.stat().st_size < 256 * 1024:
            try:
                result['runtime'] = json.loads(runtime_path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                pass
        for profile in config.printers:
            view = config.for_printer(profile)
            self.model._saved_principal(view)
            row = {'printerId': profile.printer_id, 'queueName': profile.printer_name, 'acceptNewJobs': profile.physical_printing_enabled,
                   'pendingReports': 0, 'pendingObservations': 0}
            connection = self.model._open_journal(view)
            if connection:
                try:
                    result['started'] = True
                    row['pendingReports'] = connection.execute('SELECT COUNT(*) FROM reports WHERE scope=? AND acknowledged=0', (view.scope,)).fetchone()[0]
                    row['pendingObservations'] = pending_observation_count(connection, view.scope)
                    records = [dict(x) for x in connection.execute('SELECT job_id,attempt,stage,spool_job_id,updated_at FROM attempts WHERE scope=? ORDER BY updated_at DESC LIMIT 30', (view.scope,))]
                    result['recent'].extend({**x, 'printerId': profile.printer_id, 'queueName': profile.printer_name}
                                            for x in enrich_observations(connection, view.scope, records))
                finally:
                    connection.close()
            result['pendingReports'] += row['pendingReports']
            result['pendingObservations'] += row['pendingObservations']
            if not profile.archived:
                result['printers'].append(row)
        result['recent'] = sorted(result['recent'], key=lambda row: row.get('updated_at', 0), reverse=True)[:100]
        return result
