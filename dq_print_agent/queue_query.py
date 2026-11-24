"""Bound the caller's wait for synchronous Windows spooler/network queries.

Windows does not guarantee a GetJob deadline. One daemon query is permitted at a
time; a stalled driver cannot accumulate threads or prevent sending heartbeats,
processing receipts, or claiming unrelated work. No journal or API is accessed
from the query thread.
"""
from __future__ import annotations

import threading
import time

from .windows_print import PrintAdapterError


class QueueQuery:
    def __init__(self, adapter, wait_seconds=0.25, *, clock=None, completed_limit=16):
        self.adapter = adapter
        self.wait_seconds = wait_seconds
        self.clock = clock or time.time
        self.completed_limit = completed_limit
        self.pending = None
        self.completed = {}

    @staticmethod
    def _result(pending, job_id):
        if "error" in pending:
            error = pending["error"]
            if not isinstance(error, PrintAdapterError):
                error = PrintAdapterError("JOB_QUERY_FAILED", "Windows spooler query failed",
                    phase="MAY_HAVE_SENT", job_id=job_id)
            error.observed_at = pending["observed_at"]
            raise error
        result = pending["result"]
        if not isinstance(result, dict):
            raise PrintAdapterError("JOB_QUERY_FAILED", "Windows spooler query returned invalid evidence",
                phase="MAY_HAVE_SENT", job_id=job_id)
        return {**result, "_observed_at": pending["observed_at"]}

    def query_job(self, printer_name, job_id, *, expected_document_name=None):
        key = (printer_name, job_id, expected_document_name)
        if key in self.completed:
            return self._result(self.completed.pop(key), job_id)
        pending = self.pending
        if pending is not None and pending["done"].is_set() and pending["key"] != key:
            # A different job must not erase a late PRINTED result. At capacity,
            # retain the pending evidence and stop new queries until it is consumed.
            if len(self.completed) >= self.completed_limit:
                raise PrintAdapterError("JOB_QUERY_BACKLOG", "Completed queue evidence is waiting to be recorded",
                    phase="MAY_HAVE_SENT", job_id=job_id)
            self.completed[pending["key"]] = pending
            self.pending = pending = None
        if pending is None:
            pending = {"key": key, "done": threading.Event()}
            self.pending = pending

            def run():
                try:
                    pending["result"] = self.adapter.query_job(printer_name, job_id,
                        expected_document_name=expected_document_name)
                except Exception as error:
                    pending["error"] = error
                finally:
                    pending["observed_at"] = self.clock()
                    pending["done"].set()

            threading.Thread(target=run, name="agent-queue-query", daemon=True).start()
        if pending["key"] != key or not pending["done"].wait(self.wait_seconds):
            raise PrintAdapterError("JOB_QUERY_PENDING", "Windows spooler query did not respond in time",
                phase="MAY_HAVE_SENT", job_id=job_id)
        self.pending = None
        return self._result(pending, job_id)
