"""Explicit Windows service wrapper; importing this module changes nothing.

The host uses the project's Python executable directly, avoiding pywin32's
post-install operation and modifications to a global Python installation.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import ctypes
from ctypes import wintypes
import json
import logging
from pathlib import Path
import re
import stat
import subprocess
import sys
import threading
import traceback

SERVICE_NAME = "DeqinPrintAgent"
SERVICE_DISPLAY_NAME = "Deqin Printer Agent"
SERVICE_DESCRIPTION = "Deqin multi-printer cloud task client; retains per-printer execution evidence."


def host_arguments(config_path: str | Path) -> str:
    return subprocess.list2cmdline([*_host_prefix(), "--config", str(Path(config_path).resolve())])


def _host_prefix() -> list[str]:
    if getattr(sys, "frozen", False):
        return ["service", "host"]
    return [str(Path(__file__).resolve()), "host"]


def owned_command_prefix() -> str:
    return f'"{Path(sys.executable).resolve()}" ' + subprocess.list2cmdline([*_host_prefix(), "--config"]) + " "


def _split_windows_command(command: str) -> list[str]:
    """Use the same argument grammar as a Windows executable, not shell parsing."""
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    count = ctypes.c_int()
    values = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not values:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return [values[index] for index in range(count.value)]
    finally:
        kernel.LocalFree(ctypes.cast(values, ctypes.c_void_p))


def _owned_config(command: str, *, executable: Path | None = None) -> Path | None:
    """Accept exactly this executable/module and one absolute configuration path."""
    try:
        arguments = _split_windows_command(command)
        expected_executable = (executable or Path(sys.executable)).resolve()
        prefix = [str(expected_executable), *_host_prefix(), "--config"]
        if len(arguments) != len(prefix) + 1:
            return None
        if not Path(arguments[0]).is_absolute() or Path(arguments[0]).resolve() != expected_executable:
            return None
        if getattr(sys, "frozen", False):
            if arguments[1:len(prefix)] != prefix[1:]:
                return None
        elif not Path(arguments[1]).is_absolute() or Path(arguments[1]).resolve() != Path(__file__).resolve() or arguments[2:len(prefix)] != prefix[2:]:
            return None
        config = Path(arguments[-1])
        return config.resolve() if config.is_absolute() else None
    except (OSError, ValueError, AttributeError):
        return None


def _not_installed(error: Exception) -> bool:
    return getattr(error, "winerror", None) == 1060 or bool(error.args and error.args[0] == 1060)


def _inspect(service) -> dict:
    manager = service.OpenSCManager(None, None, service.SC_MANAGER_CONNECT)
    handle = None
    try:
        try:
            handle = service.OpenService(manager, SERVICE_NAME, service.SERVICE_QUERY_CONFIG | service.SERVICE_QUERY_STATUS)
        except Exception as error:
            if _not_installed(error):
                return {"installed": False, "owned": False, "state": "NOT_INSTALLED", "configPath": None}
            raise
        command = service.QueryServiceConfig(handle)[3]
        config = _owned_config(command)
        state = service.QueryServiceStatus(handle)[1]
        names = {1: "STOPPED", 2: "START_PENDING", 3: "STOP_PENDING", 4: "RUNNING", 5: "CONTINUE_PENDING", 6: "PAUSE_PENDING", 7: "PAUSED"}
        return {"installed": True, "owned": config is not None, "state": names.get(state, "UNKNOWN"),
                "configPath": str(config) if config else None}
    finally:
        if handle is not None:
            service.CloseServiceHandle(handle)
        service.CloseServiceHandle(manager)


def service_status() -> dict:
    """Read-only SCM snapshot. No raw service command or credential is exposed."""
    if sys.platform != "win32":
        return {"installed": False, "owned": False, "state": "UNSUPPORTED", "configPath": None}
    _, service, _ = _modules()
    try:
        return _inspect(service)
    except Exception:
        raise RuntimeError("无法读取打印服务状态，请检查 Windows 服务访问权限") from None


def _modules():
    try:
        import servicemanager
        import win32service
        import win32serviceutil
    except ImportError as error:
        raise RuntimeError("Windows service support requires pywin32 in the agent environment") from error
    return servicemanager, win32service, win32serviceutil


def _assert_owned(service, util) -> None:
    manager = service.OpenSCManager(None, None, service.SC_MANAGER_CONNECT)
    handle = None
    try:
        handle = service.OpenService(manager, SERVICE_NAME, service.SERVICE_QUERY_CONFIG)
        command = service.QueryServiceConfig(handle)[3]
        if _owned_config(command) is None:
            raise RuntimeError("Existing service is not owned by this Python executable and agent directory")
    finally:
        if handle is not None:
            service.CloseServiceHandle(handle)
        service.CloseServiceHandle(manager)


def _service_class(service, util, config_path: Path):
    class DeqinPrintAgentService(util.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _svc_description_ = SERVICE_DESCRIPTION

        def __init__(self, args):
            super().__init__(args)
            self.stop_event = threading.Event()

        def SvcStop(self):
            self.ReportServiceStatus(service.SERVICE_STOP_PENDING, waitHint=60000)
            self.stop_event.set()

        def SvcShutdown(self):
            self.SvcStop()

        def SvcDoRun(self):
            try:
                from dq_print_agent.runner import run_service
                from dq_print_agent.service_rpc import serve_diagnostics
                with serve_diagnostics(config_path):
                    run_service(str(config_path), stop_event=self.stop_event)
                if not self.stop_event.is_set():
                    raise RuntimeError("Printer worker exited without a service stop request")
            except Exception as error:
                # Record our last call site, without exception text, locals or machine paths.
                location = "unknown"
                for frame, line in traceback.walk_tb(error.__traceback__):
                    module = frame.f_globals.get("__name__", "")
                    if module.startswith("dq_print_agent."):
                        location = f"{module}.{frame.f_code.co_name}:{line}"
                logging.getLogger("dq_print_agent.windows_service").error(
                    "Printer service stopped: %s at %s; errno=%s winerror=%s",
                    type(error).__name__, location, getattr(error, "errno", None),
                    getattr(error, "winerror", None))
                # ServiceFramework reports failure to SCM when this escapes.
                # Do not expose the original error in its Windows Event Log.
                raise RuntimeError("Printer worker failed; inspect the protected agent log") from None

    return DeqinPrintAgentService


def _write_result(value: dict) -> None:
    # PyInstaller --windowed has no stdout. GUI reads status via service_status().
    if sys.stdout is not None:
        print(json.dumps(value, ensure_ascii=False))


def _stop_owned(service, util) -> None:
    # Re-check command ownership immediately before control; never stop a same-name foreign service.
    _assert_owned(service, util)
    state = util.QueryServiceStatus(SERVICE_NAME)[1]
    if state == service.SERVICE_STOPPED:
        return
    if state != service.SERVICE_STOP_PENDING:
        util.StopService(SERVICE_NAME)
    util.WaitForServiceStatus(SERVICE_NAME, service.SERVICE_STOPPED, 60)


def _repair_path(path: Path) -> Path:
    """Reject links before resolving an elevated repair target, including its ancestors."""
    path = path.absolute()
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or stat.S_ISREG(info.st_mode) and info.st_nlink > 1):
            raise RuntimeError("目录权限修复不能经过链接或联接目录")
    return path.resolve()


def _repair_runtime_permissions(service, expected_config: Path) -> None:
    """Repair only an owned, stopped host's canonical printer directories; never execute jobs."""
    from dq_print_agent.config import HostConfig
    from dq_print_agent.credentials import ensure_runtime_access
    from dq_print_agent.store import instance_lock

    # _inspect returns a resolved path. Re-read the registered spelling so a
    # junction cannot disappear through resolve() before our link checks.
    manager = service.OpenSCManager(None, None, service.SC_MANAGER_CONNECT)
    handle = None
    try:
        handle = service.OpenService(manager, SERVICE_NAME, service.SERVICE_QUERY_CONFIG)
        command = service.QueryServiceConfig(handle)[3]
        if _owned_config(command) != expected_config:
            raise RuntimeError("服务配置已变化，不能修复当前目录")
        config_path = _repair_path(Path(_split_windows_command(command)[-1]))
    finally:
        if handle is not None:
            service.CloseServiceHandle(handle)
        service.CloseServiceHandle(manager)

    with ExitStack() as guards:
        _repair_path(config_path.parent / "desktop-config.lock")
        guards.enter_context(instance_lock(config_path.parent / "desktop-config.lock"))
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
        # Validate lexical paths before HostConfig normalizes them. The config
        # contains no authority to change ACLs outside its registered data root.
        def local_path(value):
            candidate = Path(value)
            return _repair_path(candidate if candidate.is_absolute() else config_path.parent / candidate)
        state_dir = local_path(raw["state_dir"])
        credential_file = local_path(raw["credential_file"])
        printer_dirs = [local_path(row["state_dir"]) for row in raw["printers"]]
        current = HostConfig.from_dict(raw, config_path)
        allowed = {config_path.parent, config_path.parent / "servers" / current.scope}
        if state_dir not in allowed or not state_dir.is_dir() or credential_file != state_dir / "credential.dpapi":
            raise RuntimeError("数据目录不属于已登记的客户端，不能修复权限")
        for printer, directory in zip(current.printers, printer_dirs):
            if not re.fullmatch(r"[1-9][0-9]{0,18}", printer.printer_id) or directory != state_dir / "printers" / printer.printer_id:
                raise RuntimeError("打印机目录不属于已登记的数据目录，不能修复权限")
        _repair_path(state_dir / "host.lock")
        guards.enter_context(instance_lock(state_dir / "host.lock"))
        status = _inspect(service)
        if not status["owned"] or status["state"] != "STOPPED" or status["configPath"] != str(config_path):
            raise RuntimeError("请先停止本客户端打印服务，再修复目录权限")
        for directory in printer_dirs:
            ensure_runtime_access(directory, state_dir)


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Manage only this checkout's Deqin printer service")
    parser.add_argument("action", choices=("install", "remove", "start", "stop", "host", "status", "prepare-upgrade", "repair-permissions", "uninstall"))
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        parser.error("Windows services are only available on Windows")
    config = args.config.resolve() if args.config else None
    if args.action in ("install", "host") and (config is None or not config.is_file()):
        parser.error("An existing absolute --config file is required")
    if args.action == "status":
        _write_result(service_status())
        return 0
    manager, service, util = _modules()
    if args.action == "host":
        manager.Initialize()
        manager.PrepareToHostSingle(_service_class(service, util, config))
        manager.StartServiceCtrlDispatcher()
        return 0
    if args.action == "install":
        # A duplicate name fails in CreateService; it never overwrites a
        # different checkout's service, account, or executable.
        util.InstallService(None, SERVICE_NAME, SERVICE_DISPLAY_NAME,
                            startType=service.SERVICE_AUTO_START,
                            exeName=str(Path(sys.executable).resolve()),
                            exeArgs=host_arguments(config),
                            description=SERVICE_DESCRIPTION,
                            serviceDeps=["Spooler"], delayedstart=True)
        scm = service.OpenSCManager(None, None, service.SC_MANAGER_CONNECT)
        handle = None
        try:
            handle = service.OpenService(scm, SERVICE_NAME, service.SERVICE_CHANGE_CONFIG)
            service.ChangeServiceConfig2(handle, service.SERVICE_CONFIG_FAILURE_ACTIONS,
                {"ResetPeriod": 86400, "RebootMsg": "", "Command": "",
                 "Actions": [(service.SC_ACTION_RESTART, 10000), (service.SC_ACTION_RESTART, 30000), (service.SC_ACTION_RESTART, 60000)]})
            service.ChangeServiceConfig2(handle, service.SERVICE_CONFIG_FAILURE_ACTIONS_FLAG, True)
        finally:
            if handle is not None:
                service.CloseServiceHandle(handle)
            service.CloseServiceHandle(scm)
        _write_result({"ok": True, "action": "install", "started": False})
        return 0
    status = _inspect(service)
    if not status["installed"]:
        if args.action in ("stop", "remove", "prepare-upgrade", "repair-permissions", "uninstall"):
            _write_result({"ok": True, "action": args.action, "installed": False})
            return 0
        raise RuntimeError("打印服务尚未安装")
    if not status["owned"]:
        raise RuntimeError("同名服务属于其他安装目录，已拒绝操作")
    if config is not None and config != Path(status["configPath"]):
        raise RuntimeError("服务配置与当前页面不一致，请先核对原客户端数据")
    _assert_owned(service, util)
    if args.action == "start":
        # Configuration belongs to the SCM registration; a new GUI path cannot redirect an existing service.
        from dq_print_agent.config import Config
        current = Config.load(status["configPath"])
        if not current.credential_file.is_file():
            raise RuntimeError("尚未保存机器接入密钥")
        if util.QueryServiceStatus(SERVICE_NAME)[1] != service.SERVICE_RUNNING:
            util.StartService(SERVICE_NAME)
            util.WaitForServiceStatus(SERVICE_NAME, service.SERVICE_RUNNING, 60)
    elif args.action == "stop":
        _stop_owned(service, util)
    elif args.action == "prepare-upgrade":
        _stop_owned(service, util)
    elif args.action == "repair-permissions":
        if status["state"] != "STOPPED":
            raise RuntimeError("请先停止本客户端打印服务，再修复目录权限")
        _repair_runtime_permissions(service, Path(status["configPath"]))
    elif args.action == "uninstall":
        _stop_owned(service, util)
        _assert_owned(service, util)
        util.RemoveService(SERVICE_NAME)
    elif args.action == "remove":
        status = util.QueryServiceStatus(SERVICE_NAME)[1]
        if status != service.SERVICE_STOPPED:
            raise RuntimeError("Stop this agent service before removing it; execution records are preserved")
        util.RemoveService(SERVICE_NAME)
    _write_result({"ok": True, "action": args.action, "dataPreserved": True})
    return 0


def main(argv=None) -> int:
    try:
        return _main(argv)
    except Exception as error:
        code = "ACCESS_DENIED" if getattr(error, "winerror", None) == 5 or bool(error.args and error.args[0] == 5) else "SERVICE_ACTION_FAILED"
        _write_result({"ok": False, "code": code, "message": "服务操作未完成，请检查管理员权限、安装目录和本地服务状态"})
        return 1


if __name__ == "__main__":
    # Direct service hosting starts with this script's directory on sys.path,
    # rather than the package parent. Do not rely on a service working directory.
    project_root = str(Path(__file__).resolve().parents[1])
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    raise SystemExit(main())
