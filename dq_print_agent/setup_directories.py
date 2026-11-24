"""Offline directory changes for the Windows installer, after the owned service stops."""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import sys

from . import windows_service


def _absolute(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("An absolute directory is required")
    return path


def _registered_install() -> Path:
    import winreg
    key = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
           r"\{04FDAB0A-88B7-4D3B-B70E-53613143D5E0}_is1")
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0,
                        winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as handle:
        location, kind = winreg.QueryValueEx(handle, "InstallLocation")
    if kind != winreg.REG_SZ or not Path(location).is_absolute():
        raise ValueError("Invalid previous installation")
    return Path(location)


def _open_service(stack, service, access):
    manager = service.OpenSCManager(None, None, service.SC_MANAGER_CONNECT)
    stack.callback(service.CloseServiceHandle, manager)
    try:
        handle = service.OpenService(manager, windows_service.SERVICE_NAME, access)
    except Exception as error:
        if windows_service._not_installed(error):
            return None
        raise
    stack.callback(service.CloseServiceHandle, handle)
    return handle


def _previous_config(service, handle, previous_app):
    if previous_app is None or previous_app != _registered_install():
        raise ValueError("Cannot verify the previous installation")
    executable = previous_app / "DeqinPrintAgent.exe"
    command = service.QueryServiceConfig(handle)[3]
    config = windows_service._owned_config(command, executable=executable)
    if config is None:
        raise ValueError("Service belongs to another executable")
    return config


def _verify_service(service, handle, previous_app, source, destination):
    config = _previous_config(service, handle, previous_app)
    if source != destination and (config != source / "config.json" or not config.is_file()):
        raise ValueError("Service uses an explicitly selected configuration")
    if service.QueryServiceStatus(handle)[1] != service.SERVICE_STOPPED:
        raise RuntimeError("The print service must be stopped before changing paths")
    return config


def configure_directories(source: Path, destination: Path, previous_app: Path | None, application: Path,
                          rollback_file: Path | None = None):
    from .data_migration import migrate_data_directory
    from .install_paths import initialize_data_access
    import win32service as service

    with ExitStack() as stack:
        access = service.SERVICE_QUERY_CONFIG | service.SERVICE_QUERY_STATUS | service.SERVICE_CHANGE_CONFIG
        handle = _open_service(stack, service, access)
        if handle is not None:
            _verify_service(service, handle, previous_app, source, destination)

        if source.is_dir() and any(source.iterdir()):
            if source != destination:
                migrate_data_directory(source, destination)
        else:
            initialize_data_access(destination)

        if handle is None:
            return
        if previous_app == application and source == destination:
            return
        old_config = _verify_service(service, handle, previous_app, source, destination)
        # SCM stores one command line. Replace only that field and retain the
        # existing account, recovery actions, dependencies and startup policy.
        new_config = old_config if source == destination else destination / "config.json"
        if not new_config.is_file():
            raise ValueError("The migrated service configuration is missing")
        executable = application / "DeqinPrintAgent.exe"
        command = f'"{executable}" ' + windows_service.host_arguments(new_config)
        if rollback_file is not None:
            previous = service.QueryServiceConfig(handle)[3]
            rollback_file.write_text(json.dumps([previous, command]), encoding="utf-8")
        _set_service_command(service, handle, command)


def _set_service_command(service, handle, command):
    keep = service.SERVICE_NO_CHANGE
    service.ChangeServiceConfig(handle, keep, keep, keep, command, None, 0, None, None, None, None)


def rollback_service(rollback_file: Path):
    # Inno's private helper directory lives through DeinitializeSetup.
    if rollback_file.is_file():
        import win32service as service
        previous, new = json.loads(rollback_file.read_text(encoding="utf-8"))
        with ExitStack() as stack:
            access = service.SERVICE_QUERY_CONFIG | service.SERVICE_QUERY_STATUS | service.SERVICE_CHANGE_CONFIG
            handle = _open_service(stack, service, access)
            if handle is not None and service.QueryServiceConfig(handle)[3] == new:
                if service.QueryServiceStatus(handle)[1] != service.SERVICE_STOPPED:
                    raise RuntimeError("Cannot roll back a running service")
                _set_service_command(service, handle, previous)
    return 0


def rollback_file() -> Path:
    return Path(sys.executable).parent / "setup-service-rollback.json"


def stop_previous_service(argv):
    import win32service as service
    import win32serviceutil

    parser = argparse.ArgumentParser()
    parser.add_argument("--previous-app", type=_absolute)
    args = parser.parse_args(argv)
    with ExitStack() as stack:
        access = service.SERVICE_QUERY_CONFIG | service.SERVICE_QUERY_STATUS | service.SERVICE_STOP
        handle = _open_service(stack, service, access)
        if handle is None:
            return 0
        _previous_config(service, handle, args.previous_app)
        state = service.QueryServiceStatus(handle)[1]
        if state != service.SERVICE_STOPPED:
            if state != service.SERVICE_STOP_PENDING:
                service.ControlService(handle, service.SERVICE_CONTROL_STOP)
            win32serviceutil.WaitForServiceStatus(windows_service.SERVICE_NAME, service.SERVICE_STOPPED, 60)
    return 0


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=_absolute, required=True)
    parser.add_argument("--destination", type=_absolute, required=True)
    parser.add_argument("--application", type=_absolute, required=True)
    parser.add_argument("--previous-app", type=_absolute)
    args = parser.parse_args(argv)
    if not getattr(sys, "frozen", False):
        raise RuntimeError("The installed executable is required for service relocation")
    configure_directories(args.source, args.destination, args.previous_app, args.application, rollback_file())
    return 0
