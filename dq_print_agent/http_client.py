"""Scoped machine API and an independent, credential-free label downloader."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit
import warnings
import threading

import requests
from PIL import Image

from .config import Config


class ApiError(RuntimeError):
    def __init__(self, code: str, *, status: int | None = None):
        super().__init__(code)
        self.code = code
        self.status = status


def origin(url: str) -> str:
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{parsed.scheme}://{parsed.hostname}:{port}"


class AgentApi:
    def __init__(self, config: Config, token: str, *, session=None, download_session=None):
        self.config = config
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"Authorization": "Bearer " + token, "Accept": "application/json"})
        self.download_session = download_session or requests.Session()
        self.download_session.trust_env = False
        self.base = config.api_url + "/inout/print-agent/v2"

    def close(self):
        self.session.close()
        self.download_session.close()

    def call(self, method: str, path: str, payload=None):
        # All paths come from code, never from a remote job.
        try:
            response = self.session.request(method, self.base + path, json=payload,
                timeout=self.config.request_timeout, allow_redirects=False)
        except requests.RequestException as error:
            raise ApiError("API_UNREACHABLE") from error
        try:
            if response.status_code != 200:
                raise ApiError("API_HTTP_" + str(response.status_code), status=response.status_code)
            if len(response.content) > 1024 * 1024:
                raise ApiError("API_RESPONSE_TOO_LARGE")
            try:
                data = response.json()
            except ValueError as error:
                raise ApiError("API_INVALID_JSON") from error
            if not isinstance(data, dict) or data.get("code") != 200:
                # Don't log arbitrary server messages, URLs, credentials or business data.
                status = data.get("code") if isinstance(data, dict) else None
                raise ApiError("API_REJECTED", status=status if type(status) is int else None)
            return data.get("data")
        finally:
            response.close()

    def config_data(self):
        return self.call("GET", "/config")

    def heartbeat(self, payload):
        return self.call("POST", "/heartbeat", payload)

    @staticmethod
    def job_path(job_id):
        value = str(job_id)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
            raise ValueError("任务编号格式无效")
        return "/jobs/" + value

    def begin(self, job_id, payload):
        return self.call("POST", self.job_path(job_id) + "/begin", payload)

    def report(self, job_id, payload):
        return self.call("POST", self.job_path(job_id) + "/report", payload)

    def reconcile(self, job_id, payload):
        return self.call("POST", self.job_path(job_id) + "/reconcile", payload)

    def observe(self, job_id, payload):
        return self.call("POST", self.job_path(job_id) + "/observations", payload)

    def get_job(self, job_id):
        return self.call("GET", self.job_path(job_id))

    def download(self, file: dict) -> Path:
        digest = str(file.get("contentSha256", "")).lower()
        if not re.fullmatch("[0-9a-f]{64}", digest) or file.get("mimeType") != "image/png":
            raise ApiError("LABEL_DESCRIPTOR_INVALID")
        url = urljoin(self.config.api_url + "/", str(file.get("downloadUrl", "")))
        parsed = urlsplit(url)
        allowed = {origin(self.config.api_url), *(origin(x) for x in self.config.download_origins)}
        if parsed.username or parsed.password or parsed.fragment or origin(url) not in allowed:
            raise ApiError("LABEL_ORIGIN_REJECTED")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and
                self.config.allow_loopback_http and parsed.hostname in {"127.0.0.1", "::1"}):
            raise ApiError("LABEL_TRANSPORT_REJECTED")
        directory = self.config.state_dir / "labels"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / (digest + ".png")
        if target.exists():
            self._verify_label(target, digest)
            return target
        temporary = target.with_suffix(".part")
        try:
            with self.download_session.get(url, stream=True, allow_redirects=False,
                    timeout=self.config.request_timeout) as response:
                if response.status_code != 200:
                    raise ApiError("LABEL_HTTP_" + str(response.status_code))
                size = 0
                with temporary.open("wb") as handle:
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > 16 * 1024 * 1024:
                            raise ApiError("LABEL_TOO_LARGE")
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
            self._verify_label(temporary, digest)
            os.replace(temporary, target)
            return target
        except requests.RequestException as error:
            raise ApiError("LABEL_DOWNLOAD_FAILED") from error
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _verify_label(path: Path, digest: str):
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ApiError("LABEL_TOO_LARGE")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ApiError("LABEL_HASH_MISMATCH")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path) as image:
                    if image.format != "PNG" or image.width * image.height > 16_000_000:
                        raise ApiError("LABEL_IMAGE_INVALID")
                    image.verify()
        except (OSError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError) as error:
            raise ApiError("LABEL_IMAGE_INVALID") from error


class PrinterLease:
    def __init__(self):
        self.lock = threading.Lock()
        self.value = None

    def update(self, value):
        if not isinstance(value, dict) or not value.get("leaseId") or type(value.get("fencingToken")) is not int:
            raise ApiError("PRINTER_LEASE_INVALID")
        with self.lock:
            self.value = dict(value)

    def snapshot(self):
        with self.lock:
            if self.value is None:
                raise ApiError("PRINTER_LEASE_REQUIRED")
            return dict(self.value)


class AgentV2Api(AgentApi):
    def __init__(self, config, token, *, lease=None, **kwargs):
        super().__init__(config, token, **kwargs)
        self.lease = lease or PrinterLease()

    def host_config_data(self):
        return super().config_data()

    def config_data(self):
        remote = self.host_config_data()
        printer_id = getattr(self.config, "printer_id", None)
        if printer_id is None:
            return remote
        printers = remote.get("printers", []) if isinstance(remote, dict) else []
        match = next((p for p in printers if str(p.get("printerId")) == str(printer_id)), None)
        if match is None:
            raise ApiError("PRINTER_BINDING_NOT_FOUND")
        if match.get("queueName", match.get("printerName")) != self.config.printer_name:
            raise ApiError("PRINTER_QUEUE_CHANGED")
        if match.get("bindingVersion") != self.config.binding_version or match.get("queueFingerprint") != self.config.queue_fingerprint:
            raise ApiError("PRINTER_BINDING_CHANGED")
        return {**remote, **match}

    def printer_path(self):
        value = str(self.config.printer_id)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
            raise ValueError("打印机编号格式无效")
        return "/printers/" + value

    def renew(self, boot_id):
        value = self.call("POST", self.printer_path() + "/lease/renew", {
            "bootId": boot_id, "bindingVersion": self.config.binding_version,
            "queueFingerprint": self.config.queue_fingerprint})
        if not isinstance(value, dict) or str(value.get("printerId")) != str(self.config.printer_id) or value.get("bindingVersion") != self.config.binding_version:
            raise ApiError("PRINTER_LEASE_IDENTITY_MISMATCH")
        self.lease.update(value)
        return value

    def claim(self, boot_id):
        lease = self.lease.snapshot()
        return self.call("POST", self.printer_path() + "/jobs/claim", {
            "bootId": boot_id, "bindingVersion": self.config.binding_version,
            "queueFingerprint": self.config.queue_fingerprint,
            "leaseId": lease["leaseId"], "fencingToken": lease["fencingToken"]})

    def begin(self, job_id, payload):
        return super().begin(job_id, {**payload, "queueFingerprint": self.config.queue_fingerprint})
