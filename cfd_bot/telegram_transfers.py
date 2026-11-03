"""Bounded interactive uploads; automatic notifications retain their outbox."""
from collections import deque
import logging
from pathlib import Path
import threading
import time
import uuid

from . import diagnostics
from .config import inside

LOG = logging.getLogger(__name__)


class TelegramTransfers:
    def __init__(self, api, remember, ui, *, workers=2, capacity=16):
        self.api, self.remember, self.ui = api, remember, ui
        self.capacity = capacity
        self._condition = threading.Condition()
        self._jobs, self._queue, self._generations = {}, deque(), {}
        self._closed = False
        self._workers = [threading.Thread(target=self._run, name=f'telegram-file-{n}',
                                         daemon=True) for n in range(workers)]
        for worker in self._workers:
            worker.start()

    def submit(self, chat, item):
        with self._condition:
            generation = self._generations.get(chat, 0)
            key = (chat, generation, str(Path(item['path']).resolve()), item.get('kind', 'document'))
            if key in self._jobs:
                job = self._jobs[key]
                if not job['started']:
                    job['item'] = dict(item)
                diagnostics.event('telegram.transfer.coalesced', transfer_id=job['id'])
                return False
            if self._closed or len(self._jobs) >= self.capacity:
                raise ValueError(self.ui.text('scenarios.data.transfer_busy'))
            job = dict(id=uuid.uuid4().hex, key=key, chat=chat, item=dict(item),
                       generation=generation, started=False, queued=time.monotonic())
            job['run'] = diagnostics.inherit_context(lambda: self._send(job))
            self._jobs[key] = job
            self._queue.append(key)
            diagnostics.event('telegram.transfer.queued', transfer_id=job['id'],
                              pending_count=len(self._jobs))
            self._condition.notify()
            return True

    def cancel_chat(self, chat):
        with self._condition:
            self._generations[chat] = self._generations.get(chat, 0) + 1
            cancelled = [key for key in self._queue if key[0] == chat]
            for key in cancelled:
                del self._jobs[key]
            self._queue = deque(key for key in self._queue if key[0] != chat)
            diagnostics.event('telegram.transfer.cancelled', count=len(cancelled), reason='clean')

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._queue or self._closed)
                if self._closed:
                    return
                key = self._queue.popleft()
                job = self._jobs[key]
                job['started'] = True
            try:
                job['run']()
            except Exception:
                # Never lose a worker because a receipt write or error notice failed.
                LOG.exception('Interactive Telegram transfer failed')
            finally:
                with self._condition:
                    self._jobs.pop(key, None)
                    self._condition.notify_all()

    def _send(self, job):
        started = time.monotonic()
        diagnostics.event('telegram.transfer.started', transfer_id=job['id'],
                          queue_ms=(started-job['queued'])*1000)
        try:
            if job['item'].get('_case_root'):
                root = Path(job['item']['_case_root'])
                # Reuse the shared path policy after waiting in the queue.
                inside(root, str(Path(job['item']['path']).relative_to(root)))
            result = self.api.file(job['chat'], job['item'])
        except Exception as exc:
            LOG.warning('Interactive Telegram file failed: %s', type(exc).__name__)
            diagnostics.event('telegram.transfer.failed', transfer_id=job['id'],
                              error_type=type(exc).__name__)
            with self._condition:
                if job['generation'] != self._generations.get(job['chat'], 0):
                    return
            result = self.api.send(job['chat'], self.ui.text(
                'scenarios.data.transfer_failed', filename=Path(job['item']['path']).name))
        # Serialize receipt publication with clean's generation change. No network
        # is performed under this lock: clean either sees this ID or we delete it.
        with self._condition:
            stale = job['generation'] != self._generations.get(job['chat'], 0)
            if not stale:
                self.remember(job['chat'], result)
        if stale:
            messages = result if isinstance(result, list) else [result]
            ids = [m['message_id'] for m in messages
                   if isinstance(m, dict) and type(m.get('message_id')) is int]
            if ids:
                try:
                    self.api.delete_messages(job['chat'], ids)
                except Exception:
                    # Keep the ID available for a later /clean if deletion failed.
                    self.remember(job['chat'], result)
                    raise
        diagnostics.event('telegram.transfer.finished', transfer_id=job['id'],
                          duration_ms=(time.monotonic()-started)*1000, cleaned=stale)

    def close(self, timeout=2):
        with self._condition:
            self._closed = True
            for key in self._queue:
                del self._jobs[key]
            cancelled = len(self._queue)
            self._queue.clear()
            self._condition.notify_all()
        deadline = time.monotonic() + timeout
        for worker in self._workers:
            worker.join(max(0, deadline-time.monotonic()))
        diagnostics.event('telegram.transfer.shutdown', cancelled=cancelled,
                          active=sum(worker.is_alive() for worker in self._workers))
