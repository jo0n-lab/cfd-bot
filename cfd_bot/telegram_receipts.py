"""Recoverable Telegram bookkeeping, outside the request's SQLite critical path.

Owned by the daemon lock. The small private journal contains IDs/timestamps only;
notifications still use the existing durable outbox before sending anything.
"""
import contextlib
import json
import logging
import os
import tempfile
import threading
import time

from . import diagnostics as _diagnostics

LOG = logging.getLogger(__name__)


class TelegramReceipts:
    def __init__(self, store):
        self.store = store
        self.path = store.root / 'telegram-receipts.json'
        saved = json.loads(self.path.read_text()) if self.path.exists() else {}
        self._pending = {(chat, mid): at for chat, mid, at in saved.get('messages', [])}
        checkpoint = store.get('telegram_offset', 0)
        self._offset = max(checkpoint, saved.get('offset', 0))
        self._generation = int(bool(self._pending) or self._offset > checkpoint)
        self._committed = 0
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name='telegram-receipts', daemon=True)
        self._thread.start()
        self._wake.set()

    @property
    def offset(self):
        with self._lock:
            return self._offset

    def _journal(self, pending, offset):
        """Publish the recovery record before advancing in-memory polling state."""
        path = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', dir=self.store.root,
                                             prefix='.telegram-receipts-', delete=False) as out:
                path = out.name
                json.dump({'messages': [[*key, at] for key, at in pending.items()],
                           'offset': offset}, out, separators=(',', ':'))
                out.flush()
                os.fsync(out.fileno())
            os.replace(path, self.path)
        finally:
            if path and os.path.exists(path):
                os.unlink(path)

    @_diagnostics.trace
    def remember_message(self, chat_id, message_id, created=None):
        if type(chat_id) is not int or type(message_id) is not int:
            return
        created = created if isinstance(created, (int, float)) else time.time()
        with self._lock:
            pending = dict(self._pending)
            key = (chat_id, message_id)
            pending[key] = min(pending.get(key, created), created)
            if pending == self._pending:
                return
            self._journal(pending, self._offset)
            self._pending = pending
            self._generation += 1
            _diagnostics.event('telegram.receipts.queued', generation=self._generation,
                               pending_count=len(pending))
        self._wake.set()

    @_diagnostics.trace
    def checkpoint(self, offset):
        with self._lock:
            if offset <= self._offset:
                return
            self._journal(self._pending, offset)
            self._offset = offset
            self._generation += 1
            _diagnostics.event('telegram.receipts.queued', generation=self._generation,
                               pending_count=len(self._pending))
        self._wake.set()

    @_diagnostics.trace
    def _flush(self):
        # _flush_lock serializes SQL and /clean. Never hold _lock across SQL.
        with self._lock:
            if self._committed == self._generation:
                return
            pending, offset, generation = dict(self._pending), self._offset, self._generation
        self.store.remember_messages([[*key, at] for key, at in pending.items()], offset=offset)
        with self._lock:
            remaining = {key: at for key, at in self._pending.items()
                         if key not in pending or pending[key] != at}
            self._journal(remaining, self._offset)
            self._pending = remaining
            self._committed = generation
            _diagnostics.event('telegram.receipts.committed', generation=generation,
                               message_count=len(pending), pending_count=len(remaining))

    def flush(self):
        with self._flush_lock:
            self._flush()

    @contextlib.contextmanager
    def clean_barrier(self):
        """Flush prior IDs and exclude a stale batch until deletion completes."""
        with self._flush_lock:
            self._flush()
            yield

    def _run(self):
        while True:
            self._wake.wait()
            self._wake.clear()
            self._stop.wait(0.05)  # Coalesce incoming/outgoing IDs and offset.
            try:
                self.flush()
            except Exception as exc:
                # Retain the journal, including when the final shutdown flush fails.
                LOG.warning('Telegram receipt flush failed; journal retained: %s', exc)
                self._stop.wait(1)
                self._wake.set()
            if self._stop.is_set():
                return

    def close(self, timeout=2):
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout)
        if self._thread.is_alive():
            LOG.warning('Telegram receipt flush still pending; recovery journal retained')
