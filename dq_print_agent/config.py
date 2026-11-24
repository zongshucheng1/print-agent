from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Config:
    """Internal view of one printer; persisted configuration is HostConfig only."""
    api_url: str
    printer_name: str
    state_dir: Path
    credential_file: Path
    poll_seconds: float = 3.0
    heartbeat_seconds: float = 15.0
    request_timeout: float = 15.0
    download_origins: tuple[str, ...] = ()
    allow_loopback_http: bool = False
    physical_printing_enabled: bool = False
    paper_width_mm: float = 80.0
    paper_height_mm: float = 80.0
    printer_id: str | None = None
    server_id: str = ""
    binding_version: int = 1
    queue_fingerprint: str = ""
    host_principal: dict | None = None

    @property
    def scope(self) -> str:
        principal = self.host_principal or {}
        if not self.printer_id or not all(principal.get(key) for key in ("agentId", "tenantId", "factoryId")):
            raise ValueError("打印运行配置缺少已验证的客户端和打印机身份")
        return stable_scope("v2-printer", self.server_id or self.api_url, principal["tenantId"],
            principal["factoryId"], principal["agentId"], self.printer_id)

    def validate(self) -> None:
        if type(self.allow_loopback_http) is not bool or type(self.physical_printing_enabled) is not bool:
            raise ValueError("启用开关必须为 JSON 布尔值")
        endpoint = urlsplit(self.api_url)
        if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise ValueError("API 地址不能包含账号、查询参数或片段")
        loopback = endpoint.hostname in {"127.0.0.1", "::1"}
        if not endpoint.hostname or not (endpoint.scheme == "https" or (
            endpoint.scheme == "http" and self.allow_loopback_http and loopback
        )):
            raise ValueError("API 必须使用 HTTPS；仅显式启用的本机开发环境可用 HTTP")
        if not self.printer_name.strip() or len(self.printer_name) > 200:
            raise ValueError("需要固定的 Windows 打印队列名称")
        if not 0.2 <= self.poll_seconds <= 300 or not 1 <= self.heartbeat_seconds <= 60:
            raise ValueError("轮询或心跳间隔超出范围")
        if not 1 <= self.request_timeout <= 60:
            raise ValueError("请求超时超出范围")
        if not all(10 <= x <= 300 for x in (self.paper_width_mm, self.paper_height_mm)):
            raise ValueError("纸张尺寸超出范围")
        for origin in self.download_origins:
            parsed = urlsplit(origin)
            if not parsed.hostname or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username or parsed.password:
                raise ValueError("文件来源白名单必须为 origin，不含路径和凭据")
            if not (parsed.scheme == "https" or (self.allow_loopback_http and parsed.scheme == "http"
                    and parsed.hostname in {"127.0.0.1", "::1"})):
                raise ValueError("文件来源必须使用 HTTPS")

    @classmethod
    def load(cls, path: str | Path) -> HostConfig:
        path = Path(path).resolve()
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict) or raw.get("schemaVersion") != 2:
            raise ValueError("仅支持 schemaVersion=2 的多打印机配置，请重新配置客户端")
        return HostConfig.from_dict(raw, path)


def stable_scope(*identity: str) -> str:
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class PrinterConfig:
    printer_id: str
    printer_name: str
    state_dir: Path
    physical_printing_enabled: bool = False
    binding_version: int = 1
    queue_fingerprint: str = ""
    display_name: str = ""
    location: str = ""
    archived: bool = False


@dataclass(frozen=True)
class HostConfig:
    api_url: str
    state_dir: Path
    credential_file: Path
    server_id: str
    tenant_id: str
    factory_id: str
    agent_id: str
    printers: tuple[PrinterConfig, ...]
    physical_printing_enabled: bool = False
    max_concurrent: int = 4
    poll_seconds: float = 3.0
    heartbeat_seconds: float = 15.0
    request_timeout: float = 15.0
    download_origins: tuple[str, ...] = ()
    allow_loopback_http: bool = False
    submission_timeout: float = 60.0
    query_timeout: float = 5.0

    @property
    def scope(self) -> str:
        return stable_scope("v2-host", self.server_id, self.tenant_id, self.factory_id, self.agent_id)

    def for_printer(self, printer: PrinterConfig) -> Config:
        return Config(self.api_url, printer.printer_name, printer.state_dir, self.credential_file,
            poll_seconds=self.poll_seconds, heartbeat_seconds=self.heartbeat_seconds,
            request_timeout=self.request_timeout, download_origins=self.download_origins,
            allow_loopback_http=self.allow_loopback_http,
            physical_printing_enabled=self.physical_printing_enabled and printer.physical_printing_enabled,
            printer_id=printer.printer_id, server_id=self.server_id,
            binding_version=printer.binding_version, queue_fingerprint=printer.queue_fingerprint,
            host_principal={"agentId": self.agent_id, "tenantId": self.tenant_id, "factoryId": self.factory_id})

    def validate(self):
        if not all(isinstance(value, str) and value.strip() for value in
                   (self.server_id, self.tenant_id, self.factory_id, self.agent_id)):
            raise ValueError("多打印机配置需要已验证的服务器、租户、工厂和客户端身份")
        if sum(not p.archived for p in self.printers) > 10 or type(self.max_concurrent) is not int or not 1 <= self.max_concurrent <= 4:
            raise ValueError("最多配置 10 台打印机，同时执行上限为 1 至 4")
        if not 1 <= self.submission_timeout <= 300 or not 0.1 <= self.query_timeout <= 30:
            raise ValueError("打印或查询超时超出范围")
        if type(self.physical_printing_enabled) is not bool:
            raise ValueError("启用开关必须为 JSON 布尔值")
        Config(self.api_url, "validation", self.state_dir, self.credential_file,
            poll_seconds=self.poll_seconds, heartbeat_seconds=self.heartbeat_seconds,
            request_timeout=self.request_timeout, download_origins=self.download_origins,
            allow_loopback_http=self.allow_loopback_http).validate()
        ids, queues, directories, scopes = set(), set(), set(), set()
        for printer in self.printers:
            if not isinstance(printer.printer_id, str) or not printer.printer_id.strip():
                raise ValueError("打印机必须具有服务端身份")
            if type(printer.binding_version) is not int or printer.binding_version < 1:
                raise ValueError("打印机绑定版本无效")
            if type(printer.archived) is not bool or (printer.archived and printer.physical_printing_enabled):
                raise ValueError("已删除的打印机不能领取任务")
            if type(printer.physical_printing_enabled) is not bool:
                raise ValueError("打印机开关必须为 JSON 布尔值")
            runtime = self.for_printer(printer)
            runtime.validate()
            directory = str(printer.state_dir.resolve()).casefold()
            queue = printer.printer_name.strip().casefold()
            if printer.printer_id in ids or (not printer.archived and queue in queues) or directory in directories or runtime.scope in scopes:
                raise ValueError("打印机身份、Windows 队列、数据目录和日志范围不能重复")
            ids.add(printer.printer_id)
            if not printer.archived:
                queues.add(queue)
            directories.add(directory)
            scopes.add(runtime.scope)

    @classmethod
    def from_dict(cls, raw: dict, path: Path):
        if not isinstance(raw, dict) or raw.get("schemaVersion") != 2:
            raise ValueError("仅支持 schemaVersion=2 的多打印机配置，请重新配置客户端")
        raw = dict(raw)
        raw.pop("schemaVersion", None)
        extra = set(raw) - set(cls.__dataclass_fields__)
        if extra:
            raise ValueError("未知多打印机配置字段：" + ", ".join(sorted(extra)))
        def resolved(value):
            candidate = Path(value)
            return (path.parent / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        raw["state_dir"] = resolved(raw["state_dir"])
        raw["credential_file"] = resolved(raw["credential_file"])
        raw["api_url"] = raw["api_url"].rstrip("/")
        raw["download_origins"] = tuple(raw.get("download_origins", ()))
        raw["printers"] = tuple(PrinterConfig(**{**p, "state_dir": resolved(p["state_dir"])}) for p in raw["printers"])
        result = cls(**raw)
        result.validate()
        return result

    @classmethod
    def load(cls, path):
        result = Config.load(path)
        if not isinstance(result, cls):
            raise ValueError("需要多打印机配置")
        return result

    def save(self, path):
        import os
        value = self.to_dict()
        target = Path(path)
        temporary = target.with_suffix(target.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)

    def to_dict(self) -> dict:
        from dataclasses import asdict
        self.validate()
        value = asdict(self)
        value["schemaVersion"] = 2
        value["state_dir"], value["credential_file"] = str(self.state_dir), str(self.credential_file)
        for printer in value["printers"]:
            printer["state_dir"] = str(printer["state_dir"])
            if not printer["archived"]:
                del printer["archived"]
        return value

load_config = Config.load
