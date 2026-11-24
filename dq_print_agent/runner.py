"""Durable execution of one printer runtime in a multi-printer host."""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import time
import uuid

from .config import Config, HostConfig
from .credentials import secure_directory
from .http_client import ApiError
from .queue_query import QueueQuery
from .process_print import IsolatedPrinterAdapter
from .store import Journal, instance_lock
from .windows_print import PrintAdapterError, PrintSettings

LOG = logging.getLogger("dq_print_agent")

OBSERVATION_STATES = {"QUEUED", "SPOOLING", "PRINTING", "PAUSED", "ATTENTION_REQUIRED",
    "SENT_TO_PRINTER", "SPOOLER_REPORTED_PRINTED", "DELETED", "NOT_FOUND", "IDENTITY_MISMATCH",
    "QUERY_FAILED", "TIMED_OUT", "NO_SPOOL_ID"}
TERMINAL_OBSERVATIONS = {"SPOOLER_REPORTED_PRINTED", "DELETED", "NOT_FOUND", "IDENTITY_MISMATCH", "NO_SPOOL_ID"}
OBSERVATION_TIMEOUT_SECONDS = 600


def attempt_payload(job: dict) -> dict:
    return {"bootId": job["_bootId"], "attempt": job["attempt"],
            "claimToken": job["claimToken"], "submissionKey": job["submissionKey"],
            "leaseId": job["leaseId"], "fencingToken": job["fencingToken"],
            "expectedVersion": job["version"]}


def resolved_attempt(original: dict, current) -> bool:
    same_attempt = isinstance(current, dict) and str(current.get("attempt")) == str(original["attempt"]) \
        and current.get("submissionKey") == original["submissionKey"]
    return same_attempt and (current.get("status") in {"SUBMITTED", "COMPLETED", "CANCELLED", "FAILED"}
                             or current.get("verificationResult") in {"PRINTED", "NOT_PRINTED"})


class Worker:
    def __init__(self, config: Config, api, journal: Journal, adapter=None, boot_id=None, *, clock=None):
        self.config, self.api, self.journal = config, api, journal
        self.adapter = adapter or IsolatedPrinterAdapter()
        self.clock = clock or time.time
        self.queue_query = QueueQuery(self.adapter, clock=self.clock)
        self.boot_id = boot_id or str(uuid.uuid4())
        self.remote_config = None
        self.observation_report_after = 0
        self.active_attempt = None
        self.claim_allowed = lambda: True
        self.log = logging.LoggerAdapter(LOG, {"printer_id": config.printer_id or config.printer_name})

    def connect(self):
        remote = self.api.config_data()
        if not isinstance(remote, dict) or not remote.get("agentId") or not remote.get("printerId"):
            raise ValueError("服务器未提供有效的机器与打印机身份")
        settings = remote.get("configSnapshot", {})
        if settings.get("printerName") != self.config.printer_name:
            raise ValueError("服务器绑定的 Windows 队列与本机配置不一致")
        if remote.get("protocolVersion") != 2 or str(remote["printerId"]) != str(self.config.printer_id):
            raise ValueError("服务器打印机身份或协议不一致")
        if not self.config.host_principal or any(str(remote.get(key, "")) != value for key, value in self.config.host_principal.items()):
            raise ValueError("服务器客户端、租户或工厂身份不一致")
        self.journal.bind({key: str(remote.get(key, "")) for key in ("agentId", "printerId", "tenantId", "factoryId")})
        self.remote_config = remote

    def settings(self, job):
        if self.remote_config is None or str(job.get("printerId")) != str(self.remote_config["printerId"]):
            raise ValueError("任务不属于当前绑定的打印机")
        value = job["configSnapshot"]
        if value.get("profile") != "WINDOWS_SPOOLER" or value.get("executionMode") != "PC_AGENT":
            raise ValueError("任务未使用 Windows 执行客户端配置")
        if value.get("printerName") != self.config.printer_name:
            raise ValueError("任务指定的打印队列与客户端绑定不一致")
        if (value.get("protocolVersion") != 2 or
                value.get("queueFingerprint") != self.config.queue_fingerprint or
                value.get("queueName", value.get("printerName")) != self.config.printer_name):
            raise ValueError("任务冻结的打印队列身份与客户端绑定不一致")
        width, height = float(value["paperWidthMm"]), float(value["paperHeightMm"])
        if abs(width - self.config.paper_width_mm) > .01 or abs(height - self.config.paper_height_mm) > .01:
            raise ValueError("任务纸张与当前装载的纸张尺寸不一致")
        settings = PrintSettings(printer_name=self.config.printer_name,
            paper_width_mm=width, paper_height_mm=height, copies=value.get("copies", 1),
            orientation=value.get("orientation", "portrait"),
            offset_x_mm=float(value.get("offsetXmm", 0)), offset_y_mm=float(value.get("offsetYmm", 0)),
            dpi=int(value["dpi"]) if value.get("dpi") else None,
            document_name="DQ-" + str(job["jobId"]) + "-" + str(job["submissionKey"]))
        settings.validate()
        return settings

    def report_payload(self, job, status, phase, *, job_id=None, error_code=None):
        payload = {**attempt_payload(job), "status": status, "phase": phase,
                   "reportKey": str(uuid.uuid4())}
        if job_id is not None:
            payload["receipt"] = {"deliveryKind": "WINDOWS_SPOOL", "spoolJobId": str(job_id),
                "printerName": job["configSnapshot"]["printerName"], "contentSha256": job["file"]["contentSha256"],
                "physicalCompletionConfirmed": False}
        if error_code:
            payload["errorCode"] = error_code
            payload["errorMessage"] = error_code
        return payload

    def flush_reports(self) -> bool:
        for row in self.journal.pending_reports():
            payload = json.loads(row["payload"])
            if payload["status"] == "UNKNOWN":
                current = self.api.reconcile(row["job_id"], payload)
                # The server can settle a historical pre-send attempt even if
                # an administrator has already created a newer attempt.
                if resolved_attempt(payload, current):
                    attempt = self.journal.get(row["job_id"], row["attempt"])
                    self.journal.stage(json.loads(attempt["job_json"]), "RESOLVED", attempt["spool_job_id"])
                    if current.get("status") == "FAILED" and current.get("phase") == "BEFORE_SEND":
                        self.journal.close_observation(row["job_id"], row["attempt"])
            else:
                self.api.report(row["job_id"], payload)
            self.journal.acknowledge(row["report_key"])
            self.log.info("回执已确认 job=%s attempt=%s status=%s", row["job_id"], row["attempt"], payload["status"])
        return True

    def recover(self):
        for row in self.journal.unresolved():
            job = json.loads(row["job_json"])
            if self.active_attempt == (str(job["jobId"]), int(job["attempt"])):
                continue
            # An ID saved after StartDoc proves neither EndDoc nor physical output.
            payload = self.report_payload(job, "UNKNOWN", "MAY_HAVE_SENT",
                job_id=row["spool_job_id"], error_code="PROCESS_INTERRUPTED")
            self.journal.add_report(job, payload, "UNKNOWN", row["spool_job_id"])
        self.flush_reports()
        for row in self.journal.unknown():
            original = json.loads(row["job_json"])
            current = self.api.get_job(row["job_id"])
            if resolved_attempt(original, current):
                self.journal.stage(original, "RESOLVED", row["spool_job_id"])
                if current.get("status") == "FAILED":
                    self.journal.close_observation(row["job_id"], row["attempt"])
        return not self.journal.unknown()

    def observe_jobs(self, identity_filter=None):
        """Read local Windows evidence even when the backend is offline or claims are disabled.

        This never invokes StartDoc, modifies a Windows job, or changes an execution stage.
        Frozen queue and document identity are usable before a network reconnect.
        """
        now = self.clock()
        for row in self.journal.observation_candidates(now, identity_filter=identity_filter):
            job = json.loads(row["job_json"])
            printer = job["configSnapshot"]["printerName"]
            if str(job.get("printerId")) != str(self.config.printer_id):
                continue
            document = "DQ-" + str(job["jobId"]) + "-" + str(job["submissionKey"])
            observation = {"printerName": printer, "documentName": document,
                           "physicalCompletionConfirmed": False}
            spool_id = row["spool_job_id"]
            if spool_id is None:
                observation["state"] = "NO_SPOOL_ID"
            else:
                observation["spoolJobId"] = str(spool_id)
                try:
                    observer = getattr(self, "observation_adapter", self.adapter)
                    if getattr(observer, "bounded_queries", False):
                        evidence = {**observer.query_job(printer, spool_id, expected_document_name=document),
                                    "_observed_at": self.clock()}
                    else:
                        evidence = self.queue_query.query_job(printer, spool_id, expected_document_name=document)
                    if not isinstance(evidence, dict) or evidence.get("state") not in OBSERVATION_STATES:
                        raise ValueError("Invalid queue observation")
                    observation["state"] = evidence["state"]
                    observation["observedAtMs"] = int(evidence["_observed_at"] * 1000)
                    for source, target in (("status_flags", "statusFlags"), ("pages_printed", "pagesPrinted"), ("total_pages", "totalPages")):
                        value = evidence.get(source)
                        if type(value) is int and 0 <= value <= 4294967295:
                            observation[target] = value
                except (PrintAdapterError, OSError, ValueError) as error:
                    observation["state"] = "QUERY_FAILED"
                    observation["observedAtMs"] = int(getattr(error, "observed_at", self.clock()) * 1000)
            first_seen = row["first_observed_at"] if row["first_observed_at"] is not None else row["updated_at"]
            expired = now - first_seen >= OBSERVATION_TIMEOUT_SECONDS
            if expired and observation["state"] in {"QUEUED", "SPOOLING", "PRINTING", "SENT_TO_PRINTER"}:
                observation["state"] = "TIMED_OUT"
            interval = 60 if expired else max(1, self.config.poll_seconds)
            self.journal.save_observation(job, observation, attempt_payload(job), now=now,
                next_query_at=now + interval, terminal=observation["state"] in TERMINAL_OBSERVATIONS)

    def flush_observations(self):
        """Observations are an independent, bounded outbox; failures never prevent new claims."""
        now = self.clock()
        if now < self.observation_report_after:
            return
        for row in self.journal.pending_observations(now=now):
            payload = self.journal.begin_observation_report(row["job_id"], row["attempt"])
            try:
                result = self.api.observe(row["job_id"], payload)
                sent_sequence = payload["observation"]["sequence"]
                if not isinstance(result, dict) or type(result.get("sequence")) is not int or result["sequence"] < sent_sequence:
                    raise ApiError("OBSERVATION_RESPONSE_INVALID")
                self.journal.acknowledge_observation(row["job_id"], row["attempt"], sent_sequence)
            except ApiError as error:
                self.log.warning("自动核验结果待补报 job=%s code=%s", row["job_id"], error.code)
                # Retry the exact persisted payload later; retain the newest local evidence separately.
                if error.status in {400, 404, 409, 422}:
                    self.journal.defer_observation(row["job_id"], row["attempt"], now + 60)
                    continue
                self.observation_report_after = now + max(3, self.config.poll_seconds)
                break

    def execute(self, job: dict):
        job = {**job, "_bootId": self.boot_id}
        existing = self.journal.remember(job)
        if existing["stage"] != "PREPARED":
            self.log.warning("重复任务已拦截 job=%s attempt=%s stage=%s", job["jobId"], job["attempt"], existing["stage"])
            return
        # A different boot must claim a new attempt, not reuse an old capability.
        old_job = json.loads(existing["job_json"])
        if old_job.get("_bootId") != self.boot_id:
            raise RuntimeError("服务器向新进程返回了旧进程的任务领取身份")
        try:
            settings = self.settings(job)
            path = self.api.download(job["file"])
        except (ValueError, KeyError, ApiError, PrintAdapterError) as error:
            code = getattr(error, "code", "INVALID_FROZEN_JOB")
            self.journal.add_report(job, self.report_payload(job, "FAILED", "BEFORE_SEND", error_code=code), "FAILED")
            return

        def authorize():
            if not self.claim_allowed():
                raise ApiError("PRINTING_PAUSED")
            self.journal.stage(job, "INTENT")
            begun = self.api.begin(job["jobId"], attempt_payload(job))
            if not isinstance(begun, dict) or type(begun.get("version")) is not int or begun.get("status") != "SENDING":
                raise ApiError("BEGIN_RESPONSE_INVALID")
            job["version"] = begun["version"]
            self.journal.stage(job, "AUTHORIZED")

        def spool_created(spool_job_id):
            self.journal.stage(job, "AUTHORIZED", spool_job_id)

        accepted = False
        def submitted(submission):
            nonlocal accepted
            self.journal.add_report(job, self.report_payload(job, "SUBMITTED", "ACCEPTED",
                job_id=submission.job_id), "SUBMITTED", submission.job_id)
            accepted = True

        self.active_attempt = (str(job["jobId"]), int(job["attempt"]))
        try:
            extra = {"on_submitted": submitted} if getattr(self.adapter, "supports_accepted_callback", False) else {}
            submission = self.adapter.submit_png(path, settings,
                before_submit=authorize, on_job_created=spool_created, **extra)
        except PrintAdapterError as error:
            if accepted:
                return
            row = self.journal.get(str(job["jobId"]), int(job["attempt"]))
            uncertain = error.phase == "MAY_HAVE_SENT" or row["stage"] in {"INTENT", "AUTHORIZED"}
            status = "UNKNOWN" if uncertain else "FAILED"
            spool_id = error.job_id or row["spool_job_id"]
            self.journal.add_report(job, self.report_payload(job, status,
                "MAY_HAVE_SENT" if uncertain else "BEFORE_SEND", job_id=spool_id, error_code=error.code), status, spool_id)
            self.log.warning("打印尝试结束 job=%s status=%s code=%s", job["jobId"], status, error.code)
            return
        finally:
            self.active_attempt = None
        # Persist a receipt before any network call. If this disk commit fails,
        # stop the process; recovery must treat AUTHORIZED as uncertain.
        if not accepted:
            submitted(submission)
        self.log.info("Windows 已接收 job=%s spool=%s", job["jobId"], submission.job_id)

    def tick(self):
        self.observe_jobs()
        if self.remote_config is None:
            self.connect()
        self.recover()
        self.flush_observations()
        if not self.config.physical_printing_enabled or not self.claim_allowed():
            return
        job = self.api.claim(self.boot_id)
        if job:
            self.execute(job)
            self.observe_jobs((str(job["jobId"]), int(job["attempt"])))
            self.flush_reports()
            self.flush_observations()


def configure_logging(directory: Path):
    LOG.setLevel(logging.INFO)
    if not LOG.handlers:
        handler = RotatingFileHandler(directory / "agent.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s printer=%(printer_id)s %(message)s", defaults={"printer_id": "host"}))
        LOG.addHandler(handler)
        LOG.addHandler(logging.StreamHandler())


@contextmanager
def _execution_configuration(config_path):
    """Select a configuration and acquire its execution lock atomically.

    The desktop takes these locks in the same order when saving or switching
    servers. Retain the state lock for the whole worker lifetime so a stopped
    UI snapshot cannot race an independently started service or CLI process.
    """
    config_path = Path(config_path).resolve()
    with ExitStack() as executing:
        with instance_lock(config_path.parent / "desktop-config.lock"):
            config = Config.load(config_path)
            secure_directory(config.state_dir)
            executing.enter_context(instance_lock(config.state_dir / "host.lock"))
        yield config


def run_service(config_path, stop_event=None, *, once=False):
    with _execution_configuration(config_path) as config:
        from .supervisor import run_configured_host
        return run_configured_host(config, config_path, stop_event=stop_event, once=once)
