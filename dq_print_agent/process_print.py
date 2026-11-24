"""Bounded Win32 helpers. Only the parent owns credentials, network and evidence.

Each helper waits for its request until the Windows kill-on-close Job Object is
installed. Parent death therefore cannot leave a detached sender behind.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import multiprocessing
import os
import threading
import time
import uuid

from .windows_print import PrintAdapterError, PrintSettings, PrintSubmission, WindowsPrinterAdapter


def queue_fingerprint(info: dict) -> str:
    value = [str(info.get(key, "") or "").strip().casefold() for key in ("name", "driver", "port")]
    target = info.get("port_target") or {"kind": "unverified"}
    value.append({key: str(target[key]).strip().casefold() for key in sorted(target)
                  if key in {"kind", "host", "address", "protocol", "port", "queue"}})
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _helper(connection, adapter_factory=None):
    try:
        request = connection.recv()
        adapter = (adapter_factory or WindowsPrinterAdapter)()
        operation = request["operation"]
        def event(name, value=None):
            connection.send({"event": name, "value": value})
            if connection.recv() != {"ack": name}:
                raise RuntimeError("Parent did not acknowledge durable event")
        if operation == "submit":
            settings = PrintSettings(**request["settings"])
            def authorize():
                expected = request.get("queue_fingerprint")
                if expected:
                    info = adapter.diagnostics(settings.printer_name)
                    if queue_fingerprint(info) != expected:
                        raise PrintAdapterError("PRINTER_QUEUE_FINGERPRINT_CHANGED", "Windows printer target changed")
                    flags = info.get("status_flags")
                    if type(flags) is not int or not 0 <= flags <= 0xFFFFFFFF:
                        raise PrintAdapterError("PRINTER_STATUS_UNKNOWN", "Windows printer status is unavailable")
                    if flags & (0x1 | 0x2 | 0x8 | 0x10 | 0x80 | 0x800 | 0x1000 | 0x100000 | 0x400000):
                        raise PrintAdapterError("PRINTER_NOT_READY", "Windows printer reports a blocking condition")
                event("authorize")
            adapter.submit_png(request["path"], settings,
                before_submit=authorize,
                on_job_created=lambda value: event("job_created", value),
                on_submitted=lambda value: event("submitted", asdict(value)))
        elif operation == "query":
            value = adapter.query_job(request["printer_name"], request["job_id"],
                expected_document_name=request.get("document_name"))
            connection.send({"event": "result", "value": value})
        elif operation == "list":
            connection.send({"event": "result", "value": adapter.list_printers()})
        elif operation == "diagnostics":
            settings = PrintSettings(**request["settings"]) if request.get("settings") else None
            value = adapter.diagnostics(request["printer_name"], settings)
            value["queueFingerprint"] = queue_fingerprint(value)
            connection.send({"event": "result", "value": value})
        else:
            raise ValueError("Unknown helper operation")
    except Exception as error:
        try:
            connection.send({"event": "error", "code": getattr(error, "code", "PRINT_HELPER_FAILED"),
                "phase": getattr(error, "phase", "BEFORE_SEND"), "job_id": getattr(error, "job_id", None)})
        except (OSError, EOFError, BrokenPipeError):
            pass
    finally:
        connection.close()


def _protect_process(process):
    if os.name != "nt":
        return None
    import win32api
    import win32job
    job = win32job.CreateJobObject(None, "Local\\DeqinPrinterHelper-" + str(uuid.uuid4()))
    try:
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        handle = win32api.OpenProcess(0x0001 | 0x0100, False, process.pid)
        try:
            win32job.AssignProcessToJobObject(job, handle)
        finally:
            handle.Close()
        return job
    except Exception:
        job.Close()
        raise


class IsolatedPrinterAdapter:
    supports_accepted_callback = True
    bounded_queries = True

    def __init__(self, *, semaphore=None, stop_event=None, submission_timeout=60,
                 query_timeout=5, adapter_factory=None, context=None, expected_fingerprint=None):
        self.semaphore = semaphore or threading.BoundedSemaphore(4)
        self.stop_event = stop_event or threading.Event()
        self.submission_timeout, self.query_timeout = submission_timeout, query_timeout
        self.adapter_factory = adapter_factory
        self.expected_fingerprint = expected_fingerprint
        self.context = context or multiprocessing.get_context("spawn")

    def _request(self, request, *, before_submit=None, on_job_created=None, on_submitted=None):
        deadline = time.monotonic() + (self.submission_timeout if request["operation"] == "submit" else self.query_timeout)
        acquired = False
        parent = child = process = job_object = None
        authorized, spool_id = False, None
        try:
            while not acquired:
                if self.stop_event.is_set() or time.monotonic() >= deadline:
                    raise PrintAdapterError("PRINT_HELPER_UNAVAILABLE", "No isolated driver slot available")
                acquired = self.semaphore.acquire(timeout=min(.05, max(.001, deadline - time.monotonic())))
            parent, child = self.context.Pipe()
            process = self.context.Process(target=_helper, args=(child, self.adapter_factory), daemon=True)
            process.start()
            child.close()
            job_object = _protect_process(process)
            parent.send(request)
            while True:
                if self.stop_event.is_set() or time.monotonic() >= deadline:
                    raise PrintAdapterError("PRINT_HELPER_TIMEOUT", "Isolated printer operation did not finish",
                        phase="MAY_HAVE_SENT" if authorized else "BEFORE_SEND", job_id=spool_id)
                if not parent.poll(.05):
                    if not process.is_alive():
                        raise EOFError("Printer helper exited")
                    continue
                message = parent.recv()
                event = message.get("event")
                if event == "authorize":
                    if authorized or not callable(before_submit):
                        raise ValueError("Unexpected authorization request")
                    before_submit()
                    if self.stop_event.is_set() or time.monotonic() >= deadline:
                        raise PrintAdapterError("PRINT_AUTHORIZATION_EXPIRED", "Printer authorization expired before dispatch")
                    # Once an ACK may reach the helper, it may call StartDoc.
                    authorized = True
                    parent.send({"ack": event})
                elif event == "job_created":
                    if not authorized or spool_id is not None or type(message.get("value")) is not int:
                        raise ValueError("Unexpected spool identifier")
                    spool_id = message["value"]
                    if spool_id <= 0:
                        raise ValueError("Invalid spool identifier")
                    if on_job_created is not None:
                        on_job_created(spool_id)
                    parent.send({"ack": event})
                elif event == "submitted":
                    submission = PrintSubmission(**message["value"])
                    settings = request["settings"]
                    if not authorized or submission.job_id != spool_id or submission.printer_name != settings["printer_name"] or submission.document_name != settings["document_name"]:
                        raise ValueError("Submission identity mismatch")
                    if on_submitted is not None:
                        on_submitted(submission)
                    try:
                        parent.send({"ack": event})
                    except (OSError, EOFError):
                        pass  # Accepted evidence was already persisted.
                    return submission
                elif event == "result" and request["operation"] != "submit":
                    return message["value"]
                elif event == "error":
                    raise PrintAdapterError(message.get("code", "PRINT_HELPER_FAILED"), "Isolated printer operation failed",
                        phase="MAY_HAVE_SENT" if authorized else "BEFORE_SEND", job_id=spool_id)
                else:
                    raise ValueError("Invalid printer helper event")
        except PrintAdapterError:
            raise
        except Exception as error:
            raise PrintAdapterError("PRINT_HELPER_FAILED", "Isolated printer operation failed",
                phase="MAY_HAVE_SENT" if authorized else "BEFORE_SEND", job_id=spool_id) from error
        finally:
            if parent is not None:
                parent.close()
            if child is not None:
                child.close()
            if process is not None and process.pid is not None:
                process.join(.1)
                if process.is_alive():
                    process.terminate()
                    process.join(2)
                if process.is_alive():
                    process.kill()
                    process.join(2)
                if process.is_alive():
                    # Do not start another helper until the old sender is proven dead.
                    self.stop_event.set()
                    raise RuntimeError("Unable to stop printer helper; executor quarantined")
                process.close()
            if job_object is not None:
                job_object.Close()
            if acquired:
                self.semaphore.release()

    def submit_png(self, path, settings, *, before_submit, on_job_created=None, on_submitted=None):
        return self._request({"operation": "submit", "path": str(path), "settings": asdict(settings),
            "queue_fingerprint": self.expected_fingerprint},
            before_submit=before_submit, on_job_created=on_job_created, on_submitted=on_submitted)

    def query_job(self, printer_name, job_id, *, expected_document_name=None):
        return self._request({"operation": "query", "printer_name": printer_name, "job_id": job_id,
            "document_name": expected_document_name})

    def list_printers(self):
        return self._request({"operation": "list"})

    def diagnostics(self, printer_name, settings=None):
        return self._request({"operation": "diagnostics", "printer_name": printer_name,
            "settings": asdict(settings) if settings else None})
