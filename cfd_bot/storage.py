"""SQLite state shared by daemon, CLI and detached job workers."""
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


def runtime_profile(case):
    watcher = case.get('watcher', {})
    return {'command': case.get('command', []),
            'logs': watcher.get('logs', [watcher.get('log', case.get('log', 'log.solver'))])}


def runtime_sample(run):
    if run.get('status') != 'succeeded':
        return None
    started = run.get('started')
    finished = run.get('solver_finished', run.get('finished'))
    if not all(isinstance(value, (int, float)) and math.isfinite(value)
               for value in (started, finished)) or finished <= started:
        return None
    return dict(id=run['id'], finished=finished, seconds=finished - started,
                cores=run.get('actual_cores'), profile=runtime_profile(run['case']))


class Store:
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
                db.executescript('''
                    DROP INDEX one_active_case;
                    CREATE UNIQUE INDEX one_active_case ON jobs(case_root)
                        WHERE status IN ('queued','starting','running','postprocessing','stopping');
                ''')
            # One-time recovery also covers old workers that know only jobs/kv.
            db.execute('BEGIN IMMEDIATE')
            if not db.execute("SELECT 1 FROM kv WHERE key='ticket_index_migrated'").fetchone():
                db.execute('INSERT INTO ticket_changes(case_root) SELECT DISTINCT case_root FROM jobs')
                db.execute("INSERT INTO ticket_changes(case_root) SELECT substr(key,10) FROM kv "
                           "WHERE substr(key,1,9)='observed:'")
                db.execute("INSERT OR IGNORE INTO observed_tracking SELECT substr(key,10) FROM kv "
                           "WHERE substr(key,1,9)='observed:'")
                db.execute("INSERT INTO kv VALUES ('ticket_index_migrated','true')")

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key, default=None):
        with self.connect() as db:
            row = db.execute('SELECT body FROM kv WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def get_many(self, keys):
        """Read many kv values through one SQLite connection."""
        keys = list(dict.fromkeys(keys))
        if not keys:
            return {}
        found = {}
        with self.connect() as db:
            # Stay comfortably below SQLite builds with the legacy 999-variable limit.
            for start in range(0, len(keys), 500):
                batch = keys[start:start + 500]
                rows = db.execute(
                    'SELECT key, body FROM kv WHERE key IN (%s)' % ','.join('?' for _ in batch),
                    batch).fetchall()
                found.update((row['key'], json.loads(row['body'])) for row in rows)
        return found

    def put(self, key, value):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)', (key, json.dumps(value)))

    def job(self, jid):
        with self.connect() as db:
            row = db.execute('SELECT body FROM jobs WHERE id=?', (jid,)).fetchone()
        if not row:
            raise ValueError(load_ui().text('scenarios.diagnostics.storage.unknown_job', job_id=jid))
        return json.loads(row[0])

    def jobs(self, statuses=None):
        with self.connect() as db:
            if statuses:
                rows = db.execute('SELECT body FROM jobs WHERE status IN (%s) ORDER BY created' %
                                  ','.join('?' for _ in statuses), tuple(statuses)).fetchall()
            else:
                rows = db.execute('SELECT body FROM jobs ORDER BY created').fetchall()
        return [json.loads(r[0]) for r in rows]

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

    def mark_terminal_published(self, jid):
        """Persist terminal handling for a managed job; ignore external/synthetic runs."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM jobs WHERE id=?', (jid,)).fetchone()
            if row is None:
                return False
            job = json.loads(row[0])
            if job['status'] not in ('succeeded', 'failed', 'interrupted'):
                return False
            job['terminal_event_published'] = True
            db.execute('UPDATE jobs SET body=? WHERE id=?', (json.dumps(job), jid))
        return True

    def jobs_for_roots(self, roots):
        roots = list(set(roots))
        result = []
        with self.connect() as db:
            for start in range(0, len(roots), 500):
                batch = roots[start:start + 500]
                result.extend(json.loads(r[0]) for r in db.execute(
                    'SELECT body FROM jobs WHERE case_root IN (%s) ORDER BY created' %
                    ','.join('?' for _ in batch), batch))
        return result

    def ticket_changes(self):
        with self.connect() as db:
            return dict(db.execute('SELECT case_root, MAX(id) FROM ticket_changes GROUP BY case_root'))

    def acknowledge_ticket_changes(self, changes):
        with self.connect() as db:
            db.executemany('DELETE FROM ticket_changes WHERE case_root=? AND id<=?', changes.items())

    def tracked_observations(self):
        with self.connect() as db:
            rows = db.execute("SELECT case_root, body FROM observed_tracking JOIN kv "
                              "ON key='observed:' || case_root")
            return {r[0]: json.loads(r[1]) for r in rows}

    def finish_observation(self, root, run_id):
        with self.connect() as db:
            db.execute("DELETE FROM observed_tracking WHERE case_root=? AND EXISTS "
                       "(SELECT 1 FROM kv WHERE key=? AND json_extract(body,'$.id')=?)",
                       (root, 'observed:' + root, run_id))

    def enqueue(self, case, request_key=None, priority='queue', queue_lane=1,
                queue_id=None, queue_cpu_set=None, dynamic_cores=None):
        if not case.get('command') and not (Path(case['_root']) / 'Allrun').is_file():
            raise ValueError(load_ui(case.get('_ui_dir')).text('scenarios.diagnostics.storage.read_only'))
        profile = queue_profile(case)
        queue_id = queue_id or (profile['id'] if profile else None)
        queue_cpu_set = queue_cpu_set or (profile.get('cpu_set') if profile else None)
        job = dict(id=uuid.uuid4().hex[:12], case=case, case_root=case['_root'], status='queued',
                   created=time.time(), reason='', telemetry={}, priority=priority,
                   queue_lane=queue_lane,
                   dynamic_cores=bool(case.get('dynamic_cores') if dynamic_cores is None else dynamic_cores))
        if queue_id:
            job['queue_id'] = queue_id
            if queue_cpu_set:
                job['queue_cpu_set'] = queue_cpu_set
        try:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if request_key:
                    row = db.execute('SELECT body FROM kv WHERE key=?', ('enqueue:' + request_key,)).fetchone()
                    if row:
                        old = db.execute('SELECT body FROM jobs WHERE id=?', (json.loads(row[0]),)).fetchone()
                        return json.loads(old[0])
                db.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',
                           (job['id'], job['case_root'], job['status'], job['created'], json.dumps(job)))
                if request_key:
                    db.execute('INSERT INTO kv VALUES (?,?)', ('enqueue:' + request_key, json.dumps(job['id'])))
        except sqlite3.IntegrityError as exc:
            raise ValueError(load_ui(case.get('_ui_dir')).text(
                'scenarios.diagnostics.storage.already_active')) from exc
        return job

    def enqueue_batch(self, cases, request, priority='queue', queue_lane=1,
                      queue_id=None, queue_cpu_set=None, dynamic_cores=None):
        """Commit every member of a macro in order, or none of them."""
        jobs = []
        try:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                for index, case in enumerate(cases):
                    key = f'enqueue:ticket:{request}:{index}'
                    row = db.execute('SELECT body FROM kv WHERE key=?', (key,)).fetchone()
                    if row:
                        old = db.execute('SELECT body FROM jobs WHERE id=?',
                                         (json.loads(row[0]),)).fetchone()
                        jobs.append(json.loads(old[0]))
                        continue
                    if not case.get('command') and not (Path(case['_root']) / 'Allrun').is_file():
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
                        job['queue_id'] = qid
                        if qcpus:
                            job['queue_cpu_set'] = qcpus
                    db.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',
                               (job['id'], job['case_root'], job['status'], job['created'], json.dumps(job)))
                    db.execute('INSERT INTO kv VALUES (?,?)', (key, json.dumps(job['id'])))
                    jobs.append(job)
        except sqlite3.IntegrityError as exc:
            root = cases[0].get('_ui_dir') if cases else None
            raise ValueError(load_ui(root).text('scenarios.diagnostics.storage.batch_conflict')) from exc
        return jobs

    def remember_run(self, run):
        """Keep successful external runs after the observed slot is replaced."""
        sample = runtime_sample(run)
        if sample is None:
            return
        root = run['case']['_root']
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO run_history VALUES (?,?,?,?)',
                       (sample['id'], root, sample['finished'], json.dumps(sample)))
            db.execute('DELETE FROM run_history WHERE case_root=? AND id NOT IN '
                       '(SELECT id FROM run_history WHERE case_root=? ORDER BY finished DESC LIMIT 20)',
                       (root, root))

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
            sample = runtime_sample(run) if run else None
            if sample:
                samples[sample['id']] = sample
        profile = runtime_profile(case)
        return sorted((sample for sample in samples.values()
                       if sample['profile'] == profile
                       and (not actual_cores or sample.get('cores') == actual_cores)),
                      key=lambda sample: sample['finished'], reverse=True)[:5]

    def update_job(self, jid, expected=None, **changes):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM jobs WHERE id=?', (jid,)).fetchone()
            if row is None:
                raise ValueError(load_ui().text('scenarios.diagnostics.storage.unknown_job', job_id=jid))
            job = json.loads(row[0])
            if expected is not None and job['status'] not in expected:
                return None
            job.update(changes)
            db.execute('UPDATE jobs SET status=?, body=? WHERE id=?',
                       (job['status'], json.dumps(job), jid))
        return job

    def cancel_queued(self, job_ids, finished=None):
        """Cancel the selected jobs still queued, atomically returning stale ids."""
        ids = list(dict.fromkeys(job_ids))
        cancelled, unavailable = [], []
        finished = time.time() if finished is None else finished
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for jid in ids:
                row = db.execute('SELECT body FROM jobs WHERE id=?', (jid,)).fetchone()
                if row is None:
                    unavailable.append(jid)
                    continue
                job = json.loads(row[0])
                if job['status'] != 'queued':
                    unavailable.append(jid)
                    continue
                job.update(status='cancelled', finished=finished)
                db.execute('UPDATE jobs SET status=?, body=? WHERE id=?',
                           (job['status'], json.dumps(job), jid))
                cancelled.append(jid)
        return dict(cancelled=cancelled, unavailable=unavailable)

    def request_interruption(self, jid, reason, requested_at=None):
        """Atomically reserve a live job while its process groups are stopped."""
        requested_at = time.time() if requested_at is None else requested_at
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM jobs WHERE id=?', (jid,)).fetchone()
            if row is None:
                return None
            job = json.loads(row[0])
            if job['status'] == 'stopping':
                return job
            if job['status'] not in STOPPABLE:
                return None
            job.update(status='stopping', reason=reason,
                       interruption_requested=requested_at)
            db.execute('UPDATE jobs SET status=?, body=? WHERE id=?',
                       (job['status'], json.dumps(job), jid))
        return job

    def event(self, key, chats, payload):
        chats = list(dict.fromkeys(chats))
        if not chats:
            return
        placeholders = ','.join('?' for _ in chats)
        with self.connect() as db:
            rows = db.execute(
                f'SELECT chat_id FROM outbox WHERE event_key=? AND chat_id IN ({placeholders})',
                (key, *chats)).fetchall()
        missing = [chat for chat in chats if chat not in {row[0] for row in rows}]
        if not missing:
            return
        body = json.dumps(payload)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # Recheck after acquiring the writer lock so concurrent producers
            # cannot insert the same recipient between the read and the write.
            for chat in missing:
                if db.execute('SELECT 1 FROM outbox WHERE event_key=? AND chat_id=?',
                              (key, chat)).fetchone() is None:
                    db.execute('INSERT INTO outbox(event_key,chat_id,body) VALUES (?,?,?)',
                               (key, chat, body))

    def pending(self, limit=10):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM outbox WHERE sent=0 AND next_attempt<=? ORDER BY id LIMIT ?',
                              (time.time(), limit)).fetchall()
        return [dict(r, body=json.loads(r['body'])) for r in rows]

    def delivered(self, oid):
        with self.connect() as db:
            db.execute('UPDATE outbox SET sent=1 WHERE id=?', (oid,))

    def retry(self, oid, delay=0):
        with self.connect() as db:
            db.execute('UPDATE outbox SET attempts=attempts+1, next_attempt=? + '
                       'MIN(300, 2 << MIN(attempts,7)) WHERE id=?', (time.time() + delay, oid))

    def save_delivery(self, oid, payload):
        with self.connect() as db:
            db.execute('UPDATE outbox SET body=? WHERE id=?', (json.dumps(payload), oid))

    def remember_message(self, chat_id, message_id, created=None):
        if type(chat_id) is not int or type(message_id) is not int:
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

    def chat_messages(self, chat_id, since=None):
        with self.connect() as db:
            if since is None:
                rows = db.execute('SELECT message_id FROM chat_messages WHERE chat_id=? '
                                  'ORDER BY message_id', (chat_id,)).fetchall()
            else:
                rows = db.execute('SELECT message_id FROM chat_messages '
                                  'WHERE chat_id=? AND created>=? ORDER BY message_id',
                                  (chat_id, since)).fetchall()
        return [row[0] for row in rows]

    def clear_messages(self, chat_id):
        with self.connect() as db:
            db.execute('DELETE FROM chat_messages WHERE chat_id=?', (chat_id,))
