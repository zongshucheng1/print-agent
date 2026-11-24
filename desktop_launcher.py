"""Packaged entry point. GUI by default; explicit commands never start a GUI."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "setup-rollback-service":
        from dq_print_agent.setup_directories import rollback_file, rollback_service
        return rollback_service(rollback_file())
    if args and args[0] == "setup-protect-app":
        from dq_print_agent.install_app import protect_application
        parser = argparse.ArgumentParser()
        parser.add_argument("--application", required=True, type=Path)
        parser.add_argument("--previous-app", type=Path)
        options = parser.parse_args(args[1:])
        protect_application(options.application, options.previous_app)
        return 0
    if args and args[0] == "setup-stop-service":
        from dq_print_agent.setup_directories import stop_previous_service
        return stop_previous_service(args[1:])
    if args and args[0] == "setup-directories":
        from dq_print_agent.setup_directories import main as configure_directories
        return configure_directories(args[1:])
    if args and args[0] == "setup-data-access":
        from dq_print_agent.install_paths import initialize_data_access
        parser = argparse.ArgumentParser()
        parser.add_argument("--destination", required=True, type=Path)
        initialize_data_access(parser.parse_args(args[1:]).destination)
        return 0
    if args and args[0] == "service":
        from dq_print_agent import windows_service
        if len(args) > 1 and args[1] == "enable":
            parser = argparse.ArgumentParser()
            parser.add_argument("--config", type=Path, required=True)
            options = parser.parse_args(args[2:])
            status = windows_service.service_status()
            if status["installed"] and not status["owned"]:
                return 3
            if not status["installed"]:
                result = windows_service.main(["install", "--config", str(options.config)])
                if result:
                    return result
            return windows_service.main(["start", "--config", str(options.config)])
        return windows_service.main(args[1:])
    parser = argparse.ArgumentParser(description="德勤打印客户端")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--self-check", action="store_true", help="只检查打包依赖，不访问服务器或打印机")
    parser.add_argument("--smoke-ui", action="store_true", help="离线界面自检，不发送任务")
    parser.add_argument("--smoke-discovery-ui", action="store_true", help="离线局域网搜索界面自检，不扫描或打印")
    parser.add_argument("--network-inventory", action="store_true", help="只读列出网卡、驱动与已有网络队列，不扫描或打印")
    parser.add_argument("--check-printer", help="只读检查指定打印机，不出纸")
    parser.add_argument("--report", type=Path)
    options = parser.parse_args(args)
    if options.self_check or options.check_printer or options.network_inventory:
        import tkinter
        import PIL.Image
        import requests
        import win32print
        import win32ui
        import win32com.shell.shell
        import win32service
        import servicemanager
        from dq_print_agent import __version__
        from dq_print_agent.desktop_model import default_config_path
        report = {"ok": True, "version": __version__, "frozen": bool(getattr(sys, "frozen", False)),
                  "tk": tkinter.TkVersion, "physicalPrinting": False,
                  "configPath": str(options.config or default_config_path())}
        from dq_print_agent.package_check import check_spawn
        report["subprocessSpawn"] = check_spawn()
        if getattr(sys, "frozen", False):
            bundle_root = Path(sys._MEIPASS).resolve()
            for module in (requests, PIL.Image, win32print, win32service, win32ui, servicemanager):
                if not Path(module.__file__).resolve().is_relative_to(bundle_root):
                    raise RuntimeError("Unexpected external dependency")
            report["dependenciesFromBundle"] = True
        if options.check_printer:
            from dq_print_agent.process_print import IsolatedPrinterAdapter
            from dq_print_agent.windows_print import PrintSettings
            report["printer"] = IsolatedPrinterAdapter().diagnostics(options.check_printer, PrintSettings(options.check_printer, 80, 80))
        if options.network_inventory:
            from dq_print_agent.network_setup import inventory
            report["networkInventory"] = inventory()
        if options.report:
            options.report.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        if sys.stdout is not None:
            print(json.dumps(report, ensure_ascii=False))
        return 0
    from dq_print_agent.desktop import run_gui, smoke_ui
    if options.smoke_discovery_ui:
        from dq_print_agent.desktop_discovery import smoke_discovery_ui
        return smoke_discovery_ui(options.report)
    if options.smoke_ui:
        return smoke_ui(options.report)
    return run_gui(options.config)


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    try:
        raise SystemExit(main())
    except Exception as error:
        # Do not expose credential-bearing exceptions from requests/Windows in an error dialog.
        if len(sys.argv) > 1 and (sys.argv[1] == "service" or sys.argv[1].startswith("setup-")):
            raise SystemExit(2)
        if any(flag in sys.argv for flag in ("--self-check", "--smoke-ui", "--smoke-discovery-ui", "--network-inventory", "--check-printer")):
            if "--report" in sys.argv:
                try:
                    Path(sys.argv[sys.argv.index("--report") + 1]).write_text(
                        json.dumps({"ok": False, "errorType": type(error).__name__}), encoding="utf-8")
                except Exception:
                    pass
            raise SystemExit(1)
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, "启动失败，请检查安装是否完整，或查看客户端日志。\n错误类型：" + type(error).__name__,
                                       "德勤打印客户端", 0x10)
        raise SystemExit(1)
