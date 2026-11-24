from __future__ import annotations
import argparse
import getpass
import json
from pathlib import Path
import sys
from .config import HostConfig
from .desktop_model import DesktopModel
from .desktop_multi import MultiDesktop
from .process_print import IsolatedPrinterAdapter
from .runner import run_service
from .windows_print import PrintSettings


def main(argv=None):
    parser = argparse.ArgumentParser(description="德勤 Windows 多打印机客户端")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="验证后台机器身份并创建多打印机配置")
    init.add_argument("--config", type=Path, required=True)
    init.add_argument("--api-url", required=True)
    init.add_argument("--allow-loopback-http", action="store_true")
    init.add_argument("--enable-printing", action="store_true")
    init.add_argument("--download-origin", action="append", default=[])
    init.add_argument("--token-stdin", action="store_true")
    run = sub.add_parser("run", help="运行多打印机监督程序")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--once", action="store_true")
    doctor = sub.add_parser("doctor", help="只读检查队列及服务器配置，不出纸")
    doctor.add_argument("--config", type=Path)
    doctor.add_argument("--printer")
    doctor.add_argument("--server", action="store_true")
    status = sub.add_parser("status", help="查看各打印机本地执行证据")
    status.add_argument("--config", type=Path, required=True)
    sub.add_parser("printers", help="列出当前命令账户的队列；服务账户列表请使用桌面")
    args = parser.parse_args(argv)
    if args.command == "init":
        if args.config.exists():
            raise ValueError("配置已存在，请通过桌面编辑")
        token = sys.stdin.readline().strip() if args.token_stdin else getpass.getpass("机器接入密钥（不回显）：")
        result = DesktopModel(args.config).save({'api_url': args.api_url,
            'allow_loopback_http': args.allow_loopback_http,
            'physical_printing_enabled': args.enable_printing,
            'download_origins': args.download_origin}, token)
    elif args.command == "run":
        run_service(args.config, once=args.once)
        return 0
    elif args.command == "printers":
        result = {'serviceVerified': False, 'printers': IsolatedPrinterAdapter().list_printers()}
    elif args.command == "doctor":
        if args.config:
            config = HostConfig.load(args.config)
            model = DesktopModel(args.config)
            names = [args.printer] if args.printer else [p.printer_name for p in config.printers]
            result = {'printers': [model.inspect_service_printer(name) for name in names]}
            if args.server:
                remote = MultiDesktop(model).remote(config)
                result['server'] = {key: remote.get(key) for key in ('agentId', 'factoryId', 'protocolVersion', 'printers')}
        else:
            if not args.printer or args.server:
                raise ValueError("无配置诊断须明确指定 --printer，服务器检查须提供 --config")
            result = {'serviceVerified': False, 'printer': IsolatedPrinterAdapter().diagnostics(args.printer, PrintSettings(args.printer, 80, 80))}
    else:
        result = DesktopModel(args.config).local_status()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
    except Exception as error:
        print("客户端执行失败：" + getattr(error, "code", type(error).__name__) + "。请检查配置和本地日志。", file=sys.stderr)
        raise SystemExit(1)
