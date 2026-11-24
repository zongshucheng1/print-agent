"""Bounded, read-only diagnostics executed by the installed service identity.

The mailbox is inside the existing ACL-protected runtime directory. Messages
can only enumerate or inspect queues: they cannot print, execute commands,
change configuration, or select arbitrary filesystem paths.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid


def _write(path: Path, value: dict):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _read(path: Path):
    if path.is_symlink() or path.stat().st_size > 256 * 1024:
        raise ValueError('Invalid diagnostic message')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Invalid diagnostic message')
    return value


def request(state_dir: Path, operation: str, printer_name: str = '', *, timeout=15.0):
    if operation not in {'list', 'diagnose'}:
        raise ValueError('Unsupported service diagnostic')
    folder = Path(state_dir) / 'service-rpc'
    if not folder.is_dir():
        raise RuntimeError('SERVICE_DIAGNOSTICS_UNAVAILABLE')
    key = uuid.uuid4().hex
    query, response = folder / (key + '.request'), folder / (key + '.response')
    _write(query, {'id': key, 'operation': operation, 'printerName': printer_name,
                   'expiresAt': time.time() + timeout})
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if response.is_file():
                value = _read(response)
                if value.get('id') != key or value.get('ok') is not True:
                    raise RuntimeError('SERVICE_DIAGNOSTICS_FAILED')
                return value['result']
            time.sleep(0.05)
        raise RuntimeError('SERVICE_DIAGNOSTICS_TIMEOUT')
    finally:
        query.unlink(missing_ok=True)
        response.unlink(missing_ok=True)


class DiagnosticMailbox:
    def __init__(self, folder: Path, adapter_factory=None):
        self.folder = folder
        self.adapter_factory = adapter_factory
        self.pending = {}

    def handle(self, query: Path):
        key = query.stem
        if not re.fullmatch('[a-f0-9]{32}', key):
            return
        try:
            message = _read(query)
            if message.get('id') != key or not time.time() < float(message['expiresAt']) <= time.time() + 60:
                return
            operation = message.get('operation')
            if self.adapter_factory is None:
                from .process_print import IsolatedPrinterAdapter
                adapter = IsolatedPrinterAdapter()
            else:
                adapter = self.adapter_factory()
            if operation == 'list':
                result = {'printers': adapter.list_printers(), 'serviceVerified': True}
            elif operation == 'diagnose':
                name = message.get('printerName')
                if not isinstance(name, str) or not name.strip() or len(name) > 200 or any(ord(c) < 32 for c in name):
                    raise ValueError('Invalid queue')
                from .windows_print import PrintSettings
                result = adapter.diagnostics(name, PrintSettings(name, 80, 80))
                result = {**result, 'serviceVerified': True, 'checkedAt': time.time()}
            else:
                raise ValueError('Unsupported diagnostic')
            value = {'id': key, 'ok': True, 'result': result}
        except Exception:
            value = {'id': key, 'ok': False, 'error': 'SERVICE_DIAGNOSTICS_FAILED'}
        finally:
            query.unlink(missing_ok=True)
        _write(self.folder / (key + '.response'), value)

    def run(self, stop: threading.Event):
        self.folder.mkdir(parents=True, exist_ok=True)
        executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='service-diagnostics')
        try:
            while not stop.wait(0.1):
                self.pending = {path: future for path, future in self.pending.items() if not future.done()}
                for path in self.folder.glob('*.request'):
                    if len(self.pending) >= 2:
                        break
                    if path not in self.pending:
                        self.pending[path] = executor.submit(self.handle, path)
                for path in self.folder.glob('*.response'):
                    try:
                        if path.stat().st_mtime < time.time() - 120:
                            path.unlink(missing_ok=True)
                    except OSError:
                        pass
        finally:
            executor.shutdown(wait=False, cancel_futures=True)


@contextmanager
def serve_diagnostics(config_path):
    from .config import Config
    config = Config.load(config_path)
    stop = threading.Event()
    mailbox = DiagnosticMailbox(config.state_dir / 'service-rpc')
    thread = threading.Thread(target=mailbox.run, args=(stop,), name='service-diagnostic-mailbox', daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=2)
