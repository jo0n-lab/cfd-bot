"""SQLite state shared by daemon, CLI and detached job workers."""
from . import diagnostics as _diagnostics
import contextlib
import json
import math
import sqlite3
import time
import uuid
from pathlib import Path

from .ui import load_ui
from .queueing import queue_profile

STOPPABLE = ('starting', 'running', 'postprocessing')
LIVE = STOPPABLE + ('stopping',)
ACTIVE = ('queued',) + LIVE
TERMINAL = ('succeeded', 'failed', 'interrupted', 'cancelled')


@_diagnostics.trace
def runtime_profile(case):
    watcher = case.get('watcher', {})
    return {'command': case.get('command', []),
            'logs': watcher.get('logs', [watcher.get('log', case.get('log', 'log.solver'))])}


@_diagnostics.trace
def runtime_sample(run):
    if run.get('status') != 'succeeded':
        if _diagnostics.detailed: _diagnostics.step('storage.runtime_sample:L25:then')
        return None
    started = run.get('started')
    finished = run.get('solver_finished', run.get('finished'))
    if not all(isinstance(value, (int, float)) and math.isfinite(value)
               for value in (started, finished)) or finished <= started:
        if _diagnostics.detailed: _diagnostics.step('storage.runtime_sample:L29:then')
        return None
    return dict(id=run['id'], finished=finished, seconds=finished - started,
                cores=run.get('actual_cores'), profile=runtime_profile(run['case']))


class Store:
    @_diagnostics.trace
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / 'state.sqlite3'
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, case_root TEXT NOT NULL,
                    status TEXT NOT NULL, created REAL NOT NULL, body TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_case ON jobs(case_root)
                    WHERE status IN ('queued','starting','running','postprocessing','stopping');
                CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL,
                    chat_id INTEGER NOT NULL, body TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                    UNIQUE(event_key, chat_id)
                );
                CREATE TABLE IF NOT EXISTS chat_messages (
                    chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                    created REAL NOT NULL, PRIMARY KEY(chat_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS run_history (
                    id TEXT PRIMARY KEY, case_root TEXT NOT NULL,
                    finished REAL NOT NULL, body TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS history_by_case ON run_history(case_root, finished);
                CREATE INDEX IF NOT EXISTS jobs_by_case ON jobs(case_root, created);
                CREATE TABLE IF NOT EXISTS ticket_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, case_root TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ticket_changes_by_root ON ticket_changes(case_root, id);
                CREATE TABLE IF NOT EXISTS observed_tracking (case_root TEXT PRIMARY KEY);
                CREATE TRIGGER IF NOT EXISTS ticket_job_insert AFTER INSERT ON jobs BEGIN
                    INSERT INTO ticket_changes(case_root) VALUES (NEW.case_root);
                END;
                CREATE TRIGGER IF NOT EXISTS ticket_job_update AFTER UPDATE ON jobs
                WHEN OLD.status != NEW.status OR
                     json_extract(OLD.body, '$.reason') IS NOT json_extract(NEW.body, '$.reason') BEGIN
                    INSERT INTO ticket_changes(case_root) VALUES (NEW.case_root);
                END;
                CREATE TRIGGER IF NOT EXISTS ticket_observed_insert AFTER INSERT ON kv
                WHEN substr(NEW.key, 1, 9) = 'observed:' BEGIN
                    INSERT INTO ticket_changes(case_root) VALUES (substr(NEW.key, 10));
                    INSERT OR IGNORE INTO observed_tracking VALUES (substr(NEW.key, 10));
                END;
            ''')
            active_index = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name='one_active_case'"
            ).fetchone()
            if active_index and "'stopping'" not in (active_index[0] or ''):
                if _diagnostics.detailed: _diagnostics.step('storage.Store.__init__:M96:then')
                db.executescript('''
                    DROP INDEX one_active_case;
                    CREATE UNIQUE INDEX one_active_case ON jobs(case_root)
                        WHERE status IN ('queued','starting','running','postprocessing','stopping');
                ''')
            # One-time recovery also covers old workers that know only jobs/kv.
            db.execute('BEGIN IMMEDIATE')
            if not db.execute("SELECT 1 FROM kv WHERE key='ticket_index_migrated'").fetchone():
                if _diagnostics.detailed: _diagnostics.step('storage.Store.__init__:L88:then')
                db.execute('INSERT INTO ticket_changes(case_root) SELECT DISTINCT case_root FROM jobs')
                db.execute("INSERT INTO ticket_changes(case_root) SELECT substr(key,10) FROM kv "
                           "WHERE substr(key,1,9)='observed:'")
                db.execute("INSERT OR IGNORE INTO observed_tracking SELECT substr(key,10) FROM kv "
                           "WHERE substr(key,1,9)='observed:'")
                db.execute("INSERT INTO kv VALUES ('ticket_index_migrated','true')")

    @contextlib.contextmanager
    @_diagnostics.trace
    def connect(self):
        db = _diagnostics.connect_sqlite(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @_diagnostics.trace
    def get(self, key, default=None):
        with self.connect() as db:
            row = db.execute('SELECT body FROM kv WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @_diagnostics.trace
    def get_many(self, keys):
        """Read many kv values through one SQLite connection."""
        keys = list(dict.fromkeys(keys))
        if not keys:
            if _diagnostics.detailed: _diagnostics.step('storage.Store.get_many:L114:then')
            return {}
        found = {}
        with self.connect() as db:
            # Stay comfortably below SQLite builds with the legacy 999-variable limit.
            for start in range(0, len(keys), 500):
                if _diagnostics.detailed: _diagnostics.step('storage.Store.get_many:L119:loop', start=start)
                batch = keys[start:start + 500]
                rows = db.execute(
                    'SELECT key, body FROM kv WHERE key IN (%s)' % ','.join('?' for _ in batch),
                    batch).fetchall()
                found.update((row['key'], json.loads(row['body'])) for row in rows)
        return found

    @_diagnostics.trace
    def put(self, key, value):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)', (key, json.dumps(value)))

    @_diagnostics.trace
    def job(self, jid):
        with self.connect() as db:
            row = db.execute('SELECT body FROM jobs WHERE id=?', (jid,)).fetchone()
        if not row:
            if _diagnostics.detailed: _diagnostics.step('storage.Store.job:L134:then')
            raise ValueError(load_ui().text('scenarios.diagnostics.storage.unknown_job', job_id=jid))
        return json.loads(row[0])

    @_diagnostics.trace
    def jobs(self, statuses=None):
        with self.connect() as db:
            if statuses:
                if _diagnostics.detailed: _diagnostics.step('storage.Store.jobs:L140:then')
                rows = db.execute('SELECT body FROM jobs WHERE status IN (%s) ORDER BY created' %
                                  ','.join('?' for _ in statuses), tuple(statuses)).fetchall()
            else:
                if _diagnostics.detailed: _diagnostics.step('storage.Store.jobs:L140:else')
                rows = db.execute('SELECT body FROM jobs ORDER BY created').fetchall()
        return [json.loads(r[0]) for r in rows]

    @_diagnostics.trace
    def unpublished_terminal_jobs(self):
        """Terminal managed jobs whose notification decision has not been persisted."""
        with self.connect() as db:
            rows = db.execute('''
                SELECT job.body FROM jobs AS job
                WHERE status IN ('succeeded','failed','interrupted')
                  AND COALESCE(json_extract(job.body, '$.terminal_event_published'), 0) != 1
                  AND NOT EXISTS (
                      SELECT 1 FROM outbox
                      WHERE event_key = job.id || ':terminal'
                  )
                ORDER BY created
            ''').fetchall()
        return [json.loads(row[0]) for row in rows]

    @_diagnostics.trace
    def mark_terminal_published(self, jid):
        """Persist terminal handling for a managed job; ignore external/synthetic runs."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM jobs WHERE id=?', (jid,)).fetchone()
            if row is None:
                if _diagnostics.detailed: _diagnostics.step('storage.Store.mark_terminal_published:L167:then')
                return False
            job = json.loads(row[0])
            if job['status'] not in ('succeeded', 'failed', 'interrupted'):
                if _diagnostics.detailed: _diagnostics.step('storage.Store.mark_terminal_published:L170:then')
                return False
            job['terminal_event_published'] = True
            db.execute('UPDATE jobs SET body=? WHERE id=?', (json.dumps(job), jid))
        return True

    @_diagnostics.trace
    def jobs_for_roots(self, roots):
        roots = list(set(roots))
        result = []
        with self.connect() as db:
            for start in range(0, len(roots), 500):
                if _diagnostics.detailed: _diagnostics.step('storage.Store.jobs_for_roots:L180:loop', start=start)
                batch = roots[start:start + 500]
                result.extend(json.loads(r[0]) for r in db.execute(
                    'SELECT body FROM jobs WHERE case_root IN (%s) ORDER BY created' %
                    ','.join('?' for _ in batch), batch))
        return result

    @_diagnostics.trace
    def ticket_changes(self):
        with self.connect() as db:
            return dict(db.execute('SELECT case_root, MAX(id) FROM ticket_changes GROUP BY case_root'))

    @_diagnostics.trace
    def acknowledge_ticket_changes(self, changes):
        with self.connect() as db:
            db.executemany('DELETE FROM ticket_changes WHERE case_root=? AND id<=?', changes.items())

    @_diagnostics.trace
    def tracked_observations(self):
        with self.connect() as db:
            rows = db.execute("SELECT case_root, body FROM observed_tracking JOIN kv "
                              "ON key='observed:' || case_root")
            return {r[0]: json.loads(r[1]) for r in rows}

    @_diagnostics.trace
    def finish_observation(self, root, run_id):
        with self.connect() as db:
            db.execute("DELETE FROM observed_tracking WHERE case_root=? AND EXISTS "
                       "(SELECT 1 FROM kv WHERE key=? AND json_extract(body,'$.id')=?)",
                       (root, 'observed:' + root, run_id))

    @_diagnostics.trace
    def enqueue(self, case, request_key=None, priority='queue', queue_lane=1,
                queue_id=None, queue_cpu_set=None, dynamic_cores=None):
        if not case.get('command') and not (Path(case['_root']) / 'Allrun').is_file():
            if _diagnostics.detailed: _diagnostics.step('storage.Store.enqueue:L209:then')
            raise ValueError(load_ui(case.get('_ui_dir')).text('scenarios.diagnostics.storage.read_only'))
        profile = queue_profile(case)
        queue_id = queue_id or (profile['id'] if profile else None)
        queue_cpu_set = queue_cpu_set or (profile.get('cpu_set') if profile else None)
        job = dict(id=uuid.uuid4().hex[:12], case=case, case_root=case['_root'], status='queued',
                   created=time.time(), reason='', telemetry={}, priority=priority,
                   queue_lane=queue_lane,
                   dynamic_cores=bool(case.get('dynamic_cores') if dynamic_cores is None else dynamic_cores))
        if queue_id:
            if _diagnostics.detailed: _diagnostics.step('storage.Store.enqueue:L218:then')
            job['queue_id'] = queue_id
            if queue_cpu_set:
                if _diagnostics.detailed: _diagnostics.step('storage.Store.enqueue:L220:then')
                job['queue_cpu_set'] = queue_cpu_set
        try:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if request_key:
                    if _diagnostics.detailed: _diagnostics.step('storage.Store.enqueue:L225:then')
                    row = db.execute('SELECT body FROM kv WHERE key=?', ('enqueue:' + request_key,)).fetchone()
                    if row:
                        if _diagnostics.detailed: _diagnostics.step('storage.Store.enqueue:L227:then')
                        old = db.execute('SELECT body FROM jobs WHERE id=?', (json.loads(row[0]),)).fetchone()
                        return json.loads(old[0])
                db.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',
                           (job['id'], job['case_root'], job['status'], job['created'], json.dumps(job)))
                if request_key:
                    if _diagnostics.detailed: _diagnostics.step('storage.Store.enqueue:L232:then')
                    db.execute('INSERT INTO kv VALUES (?,?)', ('enqueue:' + request_key, json.dumps(job['id'])))
        except sqlite3.IntegrityError as exc:
            if _diagnostics.enabled: _diagnostics.step('storage.Store.enqueue:L234:except')
            raise ValueError(load_ui(case.get('_ui_dir')).text(
                'scenarios.diagnostics.storage.already_active')) from exc
        if _diagnostics.enabled: _diagnostics.event('job.persisted', job_id=job['id'], job=job)
        return job

    @_diagnostics.trace
    def enqueue_batch(self, cases, request, priority='queue', queue_lane=1,
                      queue_id=None, queue_cpu_set=None, dynamic_cores=None):
        """Commit every member of a macro in order, or none of them."""
        jobs = []
        try:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                for index, case in enumerate(cases):
                    if _diagnostics.detailed: _diagnostics.step('storage.Store.enqueue_batch:L246:loop', index=index, case=case)
                    key = f'enqueue:ticket:{request}:{index}'
                    row = db.execute('SELECT body FROM kv WHERE key=?', (key,)).fetchone()
                    if row:
                        if _diagnostics.detailed: _diagnostics.step('storage.Store.enqueue_batch:L249:then')
                        old = db.execute('SELECT body FROM jobs WHERE id=?',
                                         (json.loads(row[0]),)).fetchone()
                        jobs.append(json.loads(old[0]))
                        continue
                    if not case.get('command') and not (Path(case['_root']) / 'Allrun').is_file():
                        if _diagnostics.detailed: _diagnostics.step('storage.Store.enqueue_batch:L254:then')
                        raise ValueError(load_ui(case.get('_ui_dir')).text(
                            'scenarios.diagnostics.storage.missing_command', name=case['name']))
                    profile = queue_profile(case)
                    qid = queue_id or (profile['id'] if profile else None)
                    qcpus = queue_cpu_set or (profile.get('cpu_set') if profile else None)
                    job = dict(id=uuid.uuid4().hex[:12], case=case, case_root=case['_root'],
                               status='queued', created=time.time(), reason='', telemetry={},
                               batch=request, batch_index=index, priority=priority,
                               queue_lane=queue_lane,
                               dynamic_cores=bool(case.get('dynamic_cores')
                                                  if dynamic_cores is None else dynamic_cores))
                    if qid:
                        if _diagnostics.detailed: _diagnostics.step('storage.Store.enqueue_batch:L266:then')
                        job['queue_id'] = qid
                        if qcpus:
                            if _diagnostics.detailed: _diagnostics.step('storage.Store.enqueue_batch:L268:then')
                            job['queue_cpu_set'] = qcpus
                    db.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',
                               (job['id'], job['case_root'], job['status'], job['created'], json.dumps(job)))
                    db.execute('INSERT INTO kv VALUES (?,?)', (key, json.dumps(job['id'])))
                    if _diagnostics.enabled: _diagnostics.event('job.inserted', job_id=job['id'], job=job)
                    jobs.append(job)
        except sqlite3.IntegrityError as exc:
            if _diagnostics.enabled: _diagnostics.step('storage.Store.enqueue_batch:L274:except')
            root = cases[0].get('_ui_dir') if cases else None
            raise ValueError(load_ui(root).text('scenarios.diagnostics.storage.batch_conflict')) from exc
        if _diagnostics.enabled: _diagnostics.event('job.batch.committed', batch=request, count=len(jobs))
        return jobs

    @_diagnostics.trace
    def remember_run(self, run):
        """Keep successful external runs after the observed slot is replaced."""
        sample = runtime_sample(run)
        if sample is None:
            if _diagnostics.detailed: _diagnostics.step('storage.Store.remember_run:L282:then')
            return
        root = run['case']['_root']
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO run_history VALUES (?,?,?,?)',
                       (sample['id'], root, sample['finished'], json.dumps(sample)))
            db.execute('DELETE FROM run_history WHERE case_root=? AND id NOT IN '
                       '(SELECT id FROM run_history WHERE case_root=? ORDER BY finished DESC LIMIT 20)',
                       (root, root))

    @_diagnostics.trace
    def runtime_history(self, case, actual_cores=None):
        """Latest comparable successes, including runs saved before history existed."""
        root = case['_root']
        with self.connect() as db:
            rows = db.execute('SELECT body FROM run_history WHERE case_root=? ORDER BY finished DESC',
                              (root,)).fetchall()
            jobs = db.execute("SELECT body FROM jobs WHERE case_root=? AND status='succeeded' "
                              'ORDER BY created DESC LIMIT 20', (root,)).fetchall()
        samples = {sample['id']: sample for sample in (json.loads(row[0]) for row in rows)}
        for run in [*(json.loads(row[0]) for row in jobs), self.get('observed:' + root)]:
            if _diagnostics.detailed: _diagnostics.step('storage.Store.runtime_history:L302:loop', run=run)
            sample = runtime_sample(run) if run else None
            if sample:
                if _diagnostics.detailed: _diagnostics.step('storage.Store.runtime_history:L304:then')
                samples[sample['id']] = sample
        profile = runtime_profile(case)
        return sorted((sample for sample in samples.values()
                       if sample['profile'] == profile
                       and (not actual_cores or sample.get('cores') == actual_cores)),
                      key=lambda sample: sample['finished'], reverse=True)[:5]

    @_diagnostics.trace
    def update_job(self, jid, expected=None, **changes):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM jobs WHERE id=?', (jid,)).fetchone()
            if row is None:
                if _diagnostics.detailed: _diagnostics.step('storage.Store.update_job:L316:then')
                raise ValueError(load_ui().text('scenarios.diagnostics.storage.unknown_job', job_id=jid))
            job = json.loads(row[0])
            if expected is not None and job['status'] not in expected:
                if _diagnostics.detailed: _diagnostics.step('storage.Store.update_job:L319:then')
                return None
            if _diagnostics.enabled:
                _diagnostics.event('job.update.request', job_id=jid, before=job, changes=changes)
            job.update(changes)
            db.execute('UPDATE jobs SET status=?, body=? WHERE id=?',
                       (job['status'], json.dumps(job), jid))
        if _diagnostics.enabled: _diagnostics.event('job.persisted', job_id=job['id'], job=job)
        return job

    @_diagnostics.trace
    def cancel_queued(self, job_ids, finished=None):
        """Cancel the selected jobs still queued, atomically returning stale ids."""
        ids = list(dict.fromkeys(job_ids))
        cancelled, unavailable = [], []
        finished = time.time() if finished is None else finished
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for jid in ids:
                if _diagnostics.detailed: _diagnostics.step('storage.Store.cancel_queued:L333:loop', jid=jid)
                row = db.execute('SELECT body FROM jobs WHERE id=?', (jid,)).fetchone()
                if row is None:
                    if _diagnostics.detailed: _diagnostics.step('storage.Store.cancel_queued:L335:then')
                    unavailable.append(jid)
                    continue
                job = json.loads(row[0])
                if job['status'] != 'queued':
                    if _diagnostics.detailed: _diagnostics.step('storage.Store.cancel_queued:L339:then')
                    unavailable.append(jid)
                    continue
                job.update(status='cancelled', finished=finished)
                db.execute('UPDATE jobs SET status=?, body=? WHERE id=?',
                           (job['status'], json.dumps(job), jid))
                if _diagnostics.enabled: _diagnostics.event('job.cancelled.pending_commit', job_id=jid, status=job['status'])
                cancelled.append(jid)
        return dict(cancelled=cancelled, unavailable=unavailable)

    @_diagnostics.trace
    def request_interruption(self, jid, reason, requested_at=None):
        """Atomically reserve a live job while its process groups are stopped."""
        requested_at = time.time() if requested_at is None else requested_at
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM jobs WHERE id=?', (jid,)).fetchone()
            if row is None:
                if _diagnostics.detailed: _diagnostics.step('storage.Store.request_interruption:M426:then')
                return None
            job = json.loads(row[0])
            if job['status'] == 'stopping':
                if _diagnostics.detailed: _diagnostics.step('storage.Store.request_interruption:M429:then')
                return job
            if job['status'] not in STOPPABLE:
                if _diagnostics.detailed: _diagnostics.step('storage.Store.request_interruption:M431:then')
                return None
            job.update(status='stopping', reason=reason,
                       interruption_requested=requested_at)
            db.execute('UPDATE jobs SET status=?, body=? WHERE id=?',
                       (job['status'], json.dumps(job), jid))
        return job

    @_diagnostics.trace
    def event(self, key, chats, payload):
        chats = list(dict.fromkeys(chats))
        if not chats:
            if _diagnostics.detailed: _diagnostics.step('storage.Store.event:L350:then')
            return
        placeholders = ','.join('?' for _ in chats)
        with self.connect() as db:
            rows = db.execute(
                f'SELECT chat_id FROM outbox WHERE event_key=? AND chat_id IN ({placeholders})',
                (key, *chats)).fetchall()
        missing = [chat for chat in chats if chat not in {row[0] for row in rows}]
        if not missing:
            if _diagnostics.detailed: _diagnostics.step('storage.Store.event:L358:then')
            return
        body = json.dumps(payload)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # Recheck after acquiring the writer lock so concurrent producers
            # cannot insert the same recipient between the read and the write.
            for chat in missing:
                if _diagnostics.detailed: _diagnostics.step('storage.Store.event:L365:loop', chat=chat)
                if db.execute('SELECT 1 FROM outbox WHERE event_key=? AND chat_id=?',
                              (key, chat)).fetchone() is None:
                    if _diagnostics.detailed: _diagnostics.step('storage.Store.event:L366:then')
                    db.execute('INSERT INTO outbox(event_key,chat_id,body) VALUES (?,?,?)',
                               (key, chat, body))

    @_diagnostics.trace
    def pending(self, limit=10):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM outbox WHERE sent=0 AND next_attempt<=? ORDER BY id LIMIT ?',
                              (time.time(), limit)).fetchall()
        return [dict(r, body=json.loads(r['body'])) for r in rows]

    @_diagnostics.trace
    def delivered(self, oid):
        with self.connect() as db:
            db.execute('UPDATE outbox SET sent=1 WHERE id=?', (oid,))

    @_diagnostics.trace
    def retry(self, oid, delay=0):
        with self.connect() as db:
            db.execute('UPDATE outbox SET attempts=attempts+1, next_attempt=? + '
                       'MIN(300, 2 << MIN(attempts,7)) WHERE id=?', (time.time() + delay, oid))

    @_diagnostics.trace
    def save_delivery(self, oid, payload):
        with self.connect() as db:
            db.execute('UPDATE outbox SET body=? WHERE id=?', (json.dumps(payload), oid))

    @_diagnostics.trace
    def remember_message(self, chat_id, message_id, created=None):
        if type(chat_id) is not int or type(message_id) is not int:
            if _diagnostics.detailed: _diagnostics.step('storage.Store.remember_message:L391:then')
            return
        created = created if isinstance(created, (int, float)) else time.time()
        with self.connect() as db:
            db.execute('INSERT INTO chat_messages VALUES (?,?,?) '
                       'ON CONFLICT(chat_id,message_id) DO UPDATE SET '
                       'created=MIN(chat_messages.created,excluded.created)',
                       (chat_id, message_id, created))
            db.execute('DELETE FROM chat_messages WHERE chat_id=? AND message_id NOT IN '
                       '(SELECT message_id FROM chat_messages WHERE chat_id=? '
                       'ORDER BY created DESC LIMIT 1000)', (chat_id, chat_id))

    @_diagnostics.trace
    def chat_messages(self, chat_id, since=None):
        with self.connect() as db:
            if since is None:
                if _diagnostics.detailed: _diagnostics.step('storage.Store.chat_messages:L405:then')
                rows = db.execute('SELECT message_id FROM chat_messages WHERE chat_id=? '
                                  'ORDER BY message_id', (chat_id,)).fetchall()
            else:
                if _diagnostics.detailed: _diagnostics.step('storage.Store.chat_messages:L405:else')
                rows = db.execute('SELECT message_id FROM chat_messages '
                                  'WHERE chat_id=? AND created>=? ORDER BY message_id',
                                  (chat_id, since)).fetchall()
        return [row[0] for row in rows]

    @_diagnostics.trace
    def clear_messages(self, chat_id):
        with self.connect() as db:
            db.execute('DELETE FROM chat_messages WHERE chat_id=?', (chat_id,))
