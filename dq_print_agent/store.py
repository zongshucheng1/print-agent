"""Durable execution journal. A possibly sent attempt is never dispatched again."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any
import uuid


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def identity(job: dict) -> tuple[str, int]:
    return str(job["jobId"]), int(job["attempt"])


def fingerprint(job: dict) -> str:
    stable = {k: job.get(k) for k in ("jobId", "attempt", "submissionKey", "printerId", "configSnapshot", "claimToken", "leaseId", "fencingToken")}
    stable["contentSha256"] = job.get("file", {}).get("contentSha256")
    return hashlib.sha256(encode(stable).encode()).hexdigest()


def enrich_observations(connection, scope: str, rows: list[dict]) -> list[dict]:
    """Read-only desktop/CLI projection of the current execution journal."""
    for row in rows:
        found = connection.execute(
            "SELECT observation_json FROM observations WHERE scope=? AND job_id=? AND attempt=?",
            (scope, str(row["job_id"]), row["attempt"])).fetchone()
        observation = json.loads(found[0]) if found else None
        if observation is not None:
            # The document name embeds the submission capability; only protocol transport uses it.
            allowed = {"sequence", "state", "observedAtMs", "receivedAtMs", "spoolJobId", "printerName",
                       "statusFlags", "pagesPrinted", "totalPages", "physicalCompletionConfirmed"}
            observation = {key: value for key, value in observation.items() if key in allowed}
        row["auto_verification"] = observation
    return rows


def pending_observation_count(connection, scope: str) -> int:
    return connection.execute("SELECT COUNT(*) FROM observations WHERE scope=? AND "
        "(sequence>acknowledged_sequence OR inflight_payload IS NOT NULL)", (scope,)).fetchone()[0]


class Journal:
    def __init__(self, path: Path, scope: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.scope = scope
        self.connection = sqlite3.connect(path, timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS attempts (
                scope TEXT NOT NULL, job_id TEXT NOT NULL, attempt INTEGER NOT NULL,
                fingerprint TEXT NOT NULL, job_json TEXT NOT NULL, stage TEXT NOT NULL,
                spool_job_id INTEGER, updated_at REAL NOT NULL,
                PRIMARY KEY(scope, job_id, attempt));
            CREATE TABLE IF NOT EXISTS reports (
                report_key TEXT PRIMARY KEY, scope TEXT NOT NULL, job_id TEXT NOT NULL,
                attempt INTEGER NOT NULL, payload TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS observations (
                scope TEXT NOT NULL, job_id TEXT NOT NULL, attempt INTEGER NOT NULL,
                sequence INTEGER NOT NULL, observation_json TEXT NOT NULL,
                latest_payload TEXT NOT NULL, inflight_payload TEXT,
                acknowledged_sequence INTEGER NOT NULL DEFAULT 0,
                first_observed_at REAL NOT NULL, checked_at REAL NOT NULL,
                next_query_at REAL NOT NULL, terminal INTEGER NOT NULL DEFAULT 0,
                report_after REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(scope, job_id, attempt));
        """)
        self.connection.commit()

    def close(self):
        self.connection.close()

    def bind(self, principal: dict):
        key = "runtimeBinding"
        value = encode({"scope": self.scope, "principal": principal})
        previous = self.connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        if previous and previous["value"] != value:
            raise RuntimeError("机器身份或打印机绑定已改变，需要先处理原客户端的任务日志")
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO metadata VALUES(?,?)", (key, value))

    def get(self, job_id: str, attempt: int):
        row = self.connection.execute("SELECT * FROM attempts WHERE scope=? AND job_id=? AND attempt=?",
                                      (self.scope, str(job_id), attempt)).fetchone()
        return dict(row) if row else None

    def remember(self, job: dict) -> dict:
        job_id, attempt = identity(job)
        previous = self.get(job_id, attempt)
        digest = fingerprint(job)
        if previous:
            if previous["fingerprint"] != digest:
                raise ValueError("同一任务尝试的冻结内容发生变化，已停止执行")
            return previous
        with self.connection:
            self.connection.execute("INSERT INTO attempts VALUES(?,?,?,?,?,'PREPARED',NULL,?)",
                                    (self.scope, job_id, attempt, digest, encode(job), time.time()))
        return self.get(job_id, attempt)

    def stage(self, job: dict, stage: str, spool_job_id=None):
        job_id, attempt = identity(job)
        with self.connection:
            changed = self.connection.execute("UPDATE attempts SET job_json=?,stage=?,spool_job_id=COALESCE(?,spool_job_id),updated_at=? WHERE scope=? AND job_id=? AND attempt=?",
                (encode(job), stage, spool_job_id, time.time(), self.scope, job_id, attempt)).rowcount
            if changed != 1:
                raise RuntimeError("本地任务证据不存在")

    def add_report(self, job: dict, payload: dict, stage: str, spool_job_id=None) -> str:
        job_id, attempt = identity(job)
        key = str(payload.get("reportKey") or uuid.uuid4())
        payload = {**payload, "reportKey": key}
        with self.connection:
            changed = self.connection.execute("UPDATE attempts SET job_json=?,stage=?,spool_job_id=COALESCE(?,spool_job_id),updated_at=? WHERE scope=? AND job_id=? AND attempt=?",
                (encode(job), stage, spool_job_id, time.time(), self.scope, job_id, attempt))
            if changed.rowcount != 1:
                raise RuntimeError("本地任务证据不存在，无法保存回执")
            self.connection.execute("INSERT INTO reports VALUES(?,?,?,?,?,0,?)",
                                    (key, self.scope, job_id, attempt, encode(payload), time.time()))
        return key

    def pending_reports(self):
        return [dict(r) for r in self.connection.execute("SELECT * FROM reports WHERE scope=? AND acknowledged=0 ORDER BY created_at,report_key", (self.scope,))]

    def acknowledge(self, report_key: str):
        with self.connection:
            self.connection.execute("UPDATE reports SET acknowledged=1 WHERE scope=? AND report_key=?", (self.scope, report_key))

    def unresolved(self):
        return [dict(r) for r in self.connection.execute("SELECT * FROM attempts WHERE scope=? AND stage IN ('INTENT','AUTHORIZED') ORDER BY updated_at", (self.scope,))]

    def unknown(self):
        return [dict(r) for r in self.connection.execute("SELECT * FROM attempts WHERE scope=? AND stage='UNKNOWN' ORDER BY updated_at", (self.scope,))]

    def observation_candidates(self, now: float, limit=8, identity_filter=None):
        sql = """SELECT a.*,o.sequence,o.observation_json,o.first_observed_at,o.checked_at,o.next_query_at
            FROM attempts a LEFT JOIN observations o
              ON o.scope=a.scope AND o.job_id=a.job_id AND o.attempt=a.attempt
            WHERE a.scope=? AND a.stage IN ('INTENT','AUTHORIZED','SUBMITTED','UNKNOWN','RESOLVED')
              AND (a.stage<>'RESOLVED' OR a.spool_job_id IS NOT NULL)
              AND (a.stage<>'INTENT' OR a.spool_job_id IS NOT NULL)
              AND (o.terminal IS NULL OR o.terminal=0)
              AND (o.next_query_at IS NULL OR o.next_query_at<=?)"""
        args: list = [self.scope, now]
        if identity_filter is not None:
            sql += " AND a.job_id=? AND a.attempt=?"
            args.extend(identity_filter)
        sql += " ORDER BY COALESCE(o.next_query_at,0),a.updated_at LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.connection.execute(sql, args)]

    def save_observation(self, job, observation: dict, payload_identity: dict, *, now: float,
                         next_query_at: float, terminal: bool, refresh_seconds=60):
        """Keep latest evidence plus at most one immutable in-flight request per attempt."""
        job_id, attempt = identity(job)
        previous = self.connection.execute(
            "SELECT * FROM observations WHERE scope=? AND job_id=? AND attempt=?",
            (self.scope, job_id, attempt)).fetchone()
        old = json.loads(previous["observation_json"]) if previous else None
        significant = lambda value: {key: val for key, val in value.items() if key not in {"sequence", "observedAtMs"}}
        changed = old is None or significant(old) != significant(observation)
        refresh = old is None or now - old["observedAtMs"] / 1000 >= refresh_seconds
        if not changed and not refresh:
            with self.connection:
                self.connection.execute("UPDATE observations SET checked_at=?,next_query_at=?,terminal=? "
                    "WHERE scope=? AND job_id=? AND attempt=?",
                    (now, next_query_at, int(terminal), self.scope, job_id, attempt))
            return
        sequence = (previous["sequence"] if previous else 0) + 1
        observation = {"observedAtMs": int(now * 1000), **observation, "sequence": sequence}
        payload = {**payload_identity, "observation": observation}
        with self.connection:
            if self.get(job_id, attempt) is None:
                raise RuntimeError("本地任务证据不存在，无法保存自动核验")
            self.connection.execute("""INSERT INTO observations
                (scope,job_id,attempt,sequence,observation_json,latest_payload,first_observed_at,checked_at,next_query_at,terminal)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(scope,job_id,attempt) DO UPDATE SET
                sequence=excluded.sequence,observation_json=excluded.observation_json,
                latest_payload=excluded.latest_payload,checked_at=excluded.checked_at,
                next_query_at=excluded.next_query_at,terminal=excluded.terminal""",
                (self.scope, job_id, attempt, sequence, encode(observation), encode(payload),
                 now, now, next_query_at, int(terminal)))

    def pending_observations(self, limit=8, now=None):
        return [dict(r) for r in self.connection.execute("""SELECT * FROM observations WHERE scope=?
            AND (sequence>acknowledged_sequence OR inflight_payload IS NOT NULL)
            AND report_after<=? ORDER BY report_after,checked_at,job_id,attempt LIMIT ?""",
            (self.scope, time.time() if now is None else now, limit))]

    def defer_observation(self, job_id: str, attempt: int, until: float):
        with self.connection:
            self.connection.execute("UPDATE observations SET report_after=? WHERE scope=? AND job_id=? AND attempt=?",
                (until, self.scope, str(job_id), attempt))

    def close_observation(self, job_id: str, attempt: int):
        """A server-confirmed pre-send failure supersedes only this attempt's provisional evidence."""
        with self.connection:
            self.connection.execute("UPDATE observations SET acknowledged_sequence=sequence,inflight_payload=NULL,terminal=1 "
                "WHERE scope=? AND job_id=? AND attempt=?", (self.scope, str(job_id), attempt))

    def begin_observation_report(self, job_id: str, attempt: int) -> dict:
        with self.connection:
            self.connection.execute("UPDATE observations SET inflight_payload=COALESCE(inflight_payload,latest_payload) "
                "WHERE scope=? AND job_id=? AND attempt=?", (self.scope, str(job_id), attempt))
            row = self.connection.execute("SELECT inflight_payload FROM observations WHERE scope=? AND job_id=? AND attempt=?",
                (self.scope, str(job_id), attempt)).fetchone()
        return json.loads(row[0])

    def acknowledge_observation(self, job_id: str, attempt: int, sequence: int):
        with self.connection:
            # Only the request actually sent is acknowledged; a newer local observation remains pending.
            self.connection.execute("UPDATE observations SET acknowledged_sequence=MAX(acknowledged_sequence,?),inflight_payload=NULL "
                "WHERE scope=? AND job_id=? AND attempt=?", (sequence, self.scope, str(job_id), attempt))

    def summary(self):
        counts = {r["stage"]: r["n"] for r in self.connection.execute("SELECT stage,COUNT(*) n FROM attempts WHERE scope=? GROUP BY stage", (self.scope,))}
        return {"attempts": counts, "pendingReports": len(self.pending_reports()),
                "pendingObservations": pending_observation_count(self.connection, self.scope)}

    def recent(self, count=20):
        rows = [dict(r) for r in self.connection.execute("SELECT job_id,attempt,stage,spool_job_id,updated_at FROM attempts WHERE scope=? ORDER BY updated_at DESC LIMIT ?", (self.scope, count))]
        return enrich_observations(self.connection, self.scope, rows)


@contextlib.contextmanager
def instance_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError("此客户端已在运行，请勿重复启动") from error
        yield
    finally:
        handle.close()
