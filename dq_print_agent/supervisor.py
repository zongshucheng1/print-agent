"""One agent session with independent durable printer coordinators.

Each coordinator exclusively owns its SQLite connection. Win32 is confined to
bounded child processes; host heartbeat and printer lease renewal never use a
printing semaphore or SQLite connection.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
import hashlib
import json
import logging
import os
from pathlib import Path
from logging.handlers import RotatingFileHandler
import threading
import time
import uuid

from . import __version__
from .config import Config, HostConfig
from .credentials import load_token, secure_directory, ensure_runtime_access
from .http_client import AgentV2Api, ApiError, PrinterLease
from .process_print import IsolatedPrinterAdapter, queue_fingerprint
from .runner import Worker, _execution_configuration, configure_logging
from .store import Journal, instance_lock
from .windows_print import PrintAdapterError, PrintSettings

LOG = logging.getLogger("dq_print_agent")


def hardware_state(info):
    flags = info.get("status_flags")
    if type(flags) is not int or not 0 <= flags <= 0xFFFFFFFF:
        return "UNKNOWN"
    if flags & 0x80:
        return "OFFLINE"
    if flags & 0x10:
        return "PAPER_OUT"
    if flags & 0x1:
        return "PAUSED"
    if flags & (0x2 | 0x8 | 0x800 | 0x1000 | 0x100000 | 0x400000):
        return "ERROR"
    return "READY"  # Windows has not reported a blocking condition; not proof of physical online.


def _atomic_status(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


class PrinterRuntime:
    def __init__(self, host, profile, token, boot_id, send_slots, query_slots, host_stop,
                 *, host_ready=None, force_stop=None, api_factory=AgentV2Api, adapter_factory=IsolatedPrinterAdapter, once=False):
        self.host, self.profile = host, profile
        self.config = host.for_printer(profile)
        self.token, self.boot_id = token, boot_id
        self.send_slots, self.query_slots = send_slots, query_slots
        self.host_stop, self.retire = host_stop, threading.Event()
        self.host_ready = host_ready or threading.Event()
        self.force_stop = force_stop or threading.Event()
        self.allow_claim = threading.Event()
        if self.config.physical_printing_enabled:
            self.allow_claim.set()
        self.api_factory, self.adapter_factory, self.once = api_factory, adapter_factory, once
        self.lease = PrinterLease()
        self.status = {"printerId": profile.printer_id, "state": "STARTING", "errorCode": None,
                       "lastLeaseAtMs": None, "active": False, "observedAtMs": int(time.time() * 1000),
                       "hardwareState": "UNKNOWN", "hardwareObservedAtMs": None, "serviceAccessible": None}
        self.hardware_ready = threading.Event()
        self.hardware_checked = None
        self.hardware_error = None
        self.thread = threading.Thread(target=self.run, name="printer-" + profile.printer_id, daemon=True)

    def set_enabled(self, enabled):
        self.allow_claim.set() if enabled else self.allow_claim.clear()

    def probe(self, observer):
        now = time.monotonic()
        if self.hardware_checked is not None and now - self.hardware_checked < 15:
            return
        try:
            info = observer.diagnostics(self.config.printer_name,
                PrintSettings(self.config.printer_name) if self.hardware_checked is None else None)
            actual = info.get("queueFingerprint") or queue_fingerprint(info)
            if not self.config.queue_fingerprint or actual != self.config.queue_fingerprint:
                raise ApiError("PRINTER_QUEUE_FINGERPRINT_CHANGED")
            state = hardware_state(info)
            self.status.update(hardwareState=state, serviceAccessible=True)
            if type(info.get("status_flags")) is int and 0 <= info["status_flags"] <= 0xFFFFFFFF:
                self.status["statusFlags"] = info["status_flags"]
            self.hardware_ready.set() if state == "READY" else self.hardware_ready.clear()
            self.hardware_error = None if state == "READY" else "PRINTER_" + state
        except (ApiError, PrintAdapterError, OSError, ValueError) as error:
            self.hardware_ready.clear()
            self.status.update(hardwareState="UNKNOWN", serviceAccessible=False)
            self.status.pop("statusFlags", None)
            self.hardware_error = getattr(error, "code", "PRINTER_STATUS_UNAVAILABLE")
        finally:
            self.hardware_checked = time.monotonic()
            self.status["hardwareObservedAtMs"] = int(time.time() * 1000)

    def _renew(self, ready, end):
        api = self.api_factory(self.config, self.token, lease=self.lease)
        try:
            while not end.is_set() and not self.host_stop.is_set() and not self.retire.is_set():
                try:
                    api.renew(self.boot_id)
                    self.status["lastLeaseAtMs"] = int(time.time() * 1000)
                    ready.set()
                except ApiError as error:
                    self.status["errorCode"] = error.code
                end.wait(min(15, self.host.heartbeat_seconds))
        finally:
            api.close()

    def run(self):
        renewal_end = threading.Event()
        renewal_thread = None
        try:
            ensure_runtime_access(self.config.state_dir, self.host.state_dir)
            lock_root = Path(os.environ.get("PROGRAMDATA", str(self.host.state_dir))) / "Deqin" / "PrintAgent" / "queue-locks"
            secure_directory(lock_root)
            queue_key = hashlib.sha256(self.config.printer_name.strip().casefold().encode()).hexdigest()
            with ExitStack() as resources:
                resources.enter_context(instance_lock(self.config.state_dir / "agent.lock"))
                if not self.profile.archived:
                    resources.enter_context(instance_lock(lock_root / (queue_key + ".lock")))
                if self.config.state_dir.resolve() != self.host.state_dir.resolve():
                    handler = RotatingFileHandler(self.config.state_dir / "agent.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
                    handler.addFilter(lambda record: getattr(record, "printer_id", None) == self.profile.printer_id)
                    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
                    LOG.addHandler(handler)
                    resources.callback(handler.close)
                    resources.callback(LOG.removeHandler, handler)
                journal = Journal(self.config.state_dir / "journal.sqlite3", self.config.scope)
                resources.callback(journal.close)
                if self.profile.archived:
                    journal.bind({**self.config.host_principal, "printerId": self.profile.printer_id})
                api = self.api_factory(self.config, self.token, lease=self.lease)
                resources.callback(api.close)
                sender = self.adapter_factory(semaphore=self.send_slots, stop_event=self.force_stop,
                    submission_timeout=self.host.submission_timeout, query_timeout=self.host.query_timeout,
                    expected_fingerprint=self.config.queue_fingerprint)
                observer = self.adapter_factory(semaphore=self.query_slots, stop_event=self.force_stop,
                    submission_timeout=self.host.submission_timeout, query_timeout=self.host.query_timeout)
                worker = Worker(self.config, api, journal, sender, boot_id=self.boot_id)
                # Observation helpers have independent capacity; a busy sender cannot starve them.
                worker.observation_adapter = observer
                worker.claim_allowed = lambda: self.allow_claim.is_set() and self.host_ready.is_set() and self.hardware_ready.is_set() and not self.host_stop.is_set() and not self.retire.is_set()
                connected = False
                ready = threading.Event()
                while not self.host_stop.is_set() and not self.retire.is_set():
                    try:
                        if not connected and not self.profile.archived:
                            worker.observe_jobs()
                            worker.connect()
                            connected = True
                            renewal_thread = threading.Thread(target=self._renew, args=(ready, renewal_end),
                                name="printer-lease-" + self.profile.printer_id, daemon=True)
                            renewal_thread.start()
                            if self.once:
                                ready.wait(self.host.request_timeout + .1)
                        # Read-only observation and historical outbox recovery remain available
                        # while no new lease exists. New claims require a current lease.
                        if not self.profile.archived:
                            self.probe(observer)
                        worker.config = replace(self.config, physical_printing_enabled=ready.is_set() and self.allow_claim.is_set() and self.hardware_ready.is_set())
                        state = "PAUSED" if not self.allow_claim.is_set() else "RUNNING" if self.hardware_ready.is_set() else "ATTENTION"
                        self.status.update(state=state, errorCode=self.hardware_error, active=True, observedAtMs=int(time.time() * 1000))
                        if self.profile.archived:
                            worker.observe_jobs()
                            worker.recover()
                            worker.flush_observations()
                        else:
                            worker.tick()
                    except (ApiError, PrintAdapterError, ValueError) as error:
                        self.status.update(state="ATTENTION", errorCode=getattr(error, "code", "PRINTER_CONFIGURATION_INVALID"))
                    finally:
                        self.status["active"] = False
                        self.status["observedAtMs"] = int(time.time() * 1000)
                    if self.once or self.retire.wait(self.host.poll_seconds):
                        break
        except Exception as error:
            LOG.error("Printer coordinator stopped printer=%s type=%s", self.profile.printer_id, type(error).__name__)
            self.status.update(state="FAILED", errorCode="PRINTER_RUNTIME_FAILED", active=False)
        finally:
            renewal_end.set()
            if renewal_thread is not None:
                renewal_thread.join(self.host.request_timeout + 1)
            if self.status["state"] != "FAILED":
                self.status["state"] = "STOPPED"


class HostSupervisor:
    def __init__(self, config, config_path, stop_event=None, *, once=False,
                 api_factory=AgentV2Api, adapter_factory=IsolatedPrinterAdapter):
        self.config, self.config_path = config, Path(config_path).resolve()
        self.stop = stop_event or threading.Event()
        self.once = once
        self.api_factory, self.adapter_factory = api_factory, adapter_factory
        self.boot_id = str(uuid.uuid4())
        self.token = load_token(config.credential_file)
        self.send_slots = threading.BoundedSemaphore(config.max_concurrent)
        self.query_slots = threading.BoundedSemaphore(4)
        self.runtimes = {}
        self.last_heartbeat = None
        self.host_error = None
        self.status_write_blocked = False
        self.heartbeat_end = threading.Event()
        self.host_ready = threading.Event()
        self.force_stop = threading.Event()

    def _heartbeat(self):
        api = self.api_factory(self.config, self.token)
        try:
            while not self.heartbeat_end.is_set() and not self.stop.is_set():
                try:
                    api.heartbeat({"bootId": self.boot_id, "agentVersion": __version__,
                        "printers": self.runtime_statuses()})
                    self.last_heartbeat, self.host_error = int(time.time() * 1000), None
                    self.host_ready.set()
                except ApiError as error:
                    self.host_error = error.code
                    self.host_ready.clear()
                self.heartbeat_end.wait(self.config.heartbeat_seconds)
        finally:
            api.close()

    def runtime_statuses(self):
        active_ids = {p.printer_id for p in self.config.printers if not p.archived}
        allowed = {"printerId", "state", "errorCode", "observedAtMs", "active", "serviceAccessible",
                   "hardwareState", "hardwareObservedAtMs", "statusFlags", "lastLeaseAtMs"}
        return [{**{key: value for key, value in dict(runtime.status).items() if key in allowed},
                 "physicalPrintingEnabled": runtime.allow_claim.is_set()}
                for runtime in list(self.runtimes.values())
                if runtime.profile.printer_id in active_ids and not runtime.profile.archived]

    def _publish_status(self, state, error_code):
        path = self.config.state_dir / "runtime-status.json"
        snapshot = {"agentId": self.config.agent_id, "state": state, "errorCode": error_code,
                    "lastHeartbeatAtMs": self.last_heartbeat,
                    "printers": [dict(runtime.status) for runtime in self.runtimes.values()]}
        # A Windows reader can temporarily deny replacement of the desktop snapshot.
        # Running hosts publish again on the next tick. A stopping host has no next tick.
        delays = (0,) if state == "RUNNING" else (0, .05, .15)
        for delay in delays:
            if delay:
                time.sleep(delay)
            try:
                _atomic_status(path, snapshot)
            except PermissionError as error:
                if not self.status_write_blocked:
                    LOG.warning("Cannot update runtime-status.json; errno=%s winerror=%s",
                                error.errno, getattr(error, "winerror", None))
                    self.status_write_blocked = True
            else:
                if self.status_write_blocked:
                    LOG.info("Resumed updates to runtime-status.json")
                    self.status_write_blocked = False
                return

    def reconcile_config(self, candidate):
        immutable = ("api_url", "server_id", "tenant_id", "factory_id", "agent_id", "state_dir", "credential_file", "max_concurrent")
        if not isinstance(candidate, HostConfig) or any(getattr(candidate, key) != getattr(self.config, key) for key in immutable):
            raise ValueError("服务运行中不能更换服务器、客户端身份、目录或并发上限")
        profiles = {p.printer_id: p for p in candidate.printers}
        for printer_id, runtime in list(self.runtimes.items()):
            profile = profiles.get(printer_id)
            if profile is None:
                # Keep historical recovery available. UI deletion is archival, not evidence deletion.
                runtime.set_enabled(False)
                continue
            old = replace(runtime.profile, physical_printing_enabled=False, display_name="", location="")
            new = replace(profile, physical_printing_enabled=False, display_name="", location="")
            if old != new:
                if runtime.profile.state_dir != profile.state_dir:
                    raise ValueError("打印机日志目录与历史身份不能热切换")
                runtime.set_enabled(False)
                runtime.retire.set()
                if runtime.thread.is_alive():
                    continue
                del self.runtimes[printer_id]
            else:
                runtime.set_enabled(candidate.physical_printing_enabled and profile.physical_printing_enabled)
                runtime.profile = profile
        for profile in candidate.printers:
            if profile.archived and not (profile.state_dir / "journal.sqlite3").exists():
                continue
            if profile.printer_id not in self.runtimes:
                # A just-deleted registration can still be releasing this queue's
                # process lock. Start the replacement after that coordinator exits.
                if not profile.archived and any(
                    not runtime.profile.archived
                    and runtime.profile.printer_name.strip().casefold() == profile.printer_name.strip().casefold()
                    and runtime.thread.is_alive()
                    for runtime in self.runtimes.values()
                ):
                    continue
                runtime = PrinterRuntime(candidate, profile, self.token, self.boot_id,
                    self.send_slots, self.query_slots, self.stop, api_factory=self.api_factory,
                    adapter_factory=self.adapter_factory, once=self.once, host_ready=self.host_ready,
                    force_stop=self.force_stop)
                self.runtimes[profile.printer_id] = runtime
                runtime.thread.start()
        self.config = candidate

    def run(self):
        api = self.api_factory(self.config, self.token)
        heartbeat_thread = None
        try:
            # Offline restarts must still observe already submitted jobs.
            if not self.once:
                self.reconcile_config(self.config)
            while not self.stop.is_set():
                try:
                    remote = api.host_config_data()
                    if not isinstance(remote, dict) or remote.get("protocolVersion") != 2 or any(str(remote.get(field)) != str(getattr(self.config, attribute))
                        for field, attribute in (("agentId", "agent_id"), ("tenantId", "tenant_id"), ("factoryId", "factory_id"))):
                        raise ValueError("服务端与本地多打印机身份不一致")
                    api.heartbeat({"bootId": self.boot_id, "agentVersion": __version__, "printers": self.runtime_statuses()})
                    self.last_heartbeat = int(time.time() * 1000)
                    self.host_ready.set()
                    break
                except ApiError as error:
                    self.host_error = error.code
                    if error.status in {401, 403} or self.once:
                        raise
                    if self.stop.wait(self.config.poll_seconds):
                        return
            if self.stop.is_set():
                return
            heartbeat_thread = threading.Thread(target=self._heartbeat, name="host-heartbeat", daemon=True)
            heartbeat_thread.start()
            self.reconcile_config(self.config)
            while not self.stop.is_set():
                if not self.once:
                    try:
                        with instance_lock(self.config_path.parent / "desktop-config.lock"):
                            candidate = Config.load(self.config_path)
                        self.reconcile_config(candidate)
                    except (OSError, ValueError, RuntimeError):
                        self.host_error = "CONFIG_RELOAD_REJECTED"
                self._publish_status("RUNNING", self.host_error)
                if self.once and all(not r.thread.is_alive() for r in self.runtimes.values()):
                    return
                self.stop.wait(.5)
        finally:
            self.heartbeat_end.set()
            for runtime in self.runtimes.values():
                runtime.allow_claim.clear()
                runtime.retire.set()
            alive = []
            for runtime in self.runtimes.values():
                runtime.thread.join(self.config.submission_timeout + self.config.query_timeout + self.config.request_timeout * 4 + 5)
                if runtime.thread.is_alive():
                    self.stop.set()
                    self.force_stop.set()
                    alive.append(runtime)
            if heartbeat_thread is not None:
                heartbeat_thread.join(self.config.request_timeout + 1)
            api.close()
            try:
                self._publish_status("FAILED" if alive else "STOPPED",
                                     "PRINTER_STOP_TIMEOUT" if alive else self.host_error)
            except OSError as error:
                LOG.warning("Cannot write final runtime-status.json; errno=%s winerror=%s",
                            error.errno, getattr(error, "winerror", None))
            if alive:
                raise RuntimeError("Printer coordinator failed to stop")


def run_configured_host(config, config_path, stop_event=None, *, once=False):
    configure_logging(config.state_dir)
    return HostSupervisor(config, config_path, stop_event, once=once).run()


def run_host_service(config_path, stop_event=None, *, once=False):
    with _execution_configuration(config_path) as config:
        if not isinstance(config, HostConfig):
            raise ValueError("需要多打印机配置")
        return run_configured_host(config, config_path, stop_event, once=once)
