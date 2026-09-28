"""SQLite state shared by daemon, CLI and detached job workers."""
import contextlib
import json
import math
import sqlite3
import time
import uuid
from pathlib import Path

from .ui import load_ui

ACTIVE = ('queued', 'starting', 'running', 'postprocessing')
LIVE = ('starting', 'running', 'postprocessing')
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
                    WHERE status IN ('queued','starting','running','postprocessing');
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
            ''')

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

    def enqueue(self, case, request_key=None):
        if not case.get('command') and not (Path(case['_root']) / 'Allrun').is_file():
            raise ValueError(load_ui(case.get('_ui_dir')).text('scenarios.diagnostics.storage.read_only'))
        job = dict(id=uuid.uuid4().hex[:12], case=case, case_root=case['_root'], status='queued',
                   created=time.time(), reason='', telemetry={})
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

    def enqueue_batch(self, cases, request):
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
                    job = dict(id=uuid.uuid4().hex[:12], case=case, case_root=case['_root'],
                               status='queued', created=time.time(), reason='', telemetry={},
                               batch=request, batch_index=index)
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

    def event(self, key, chats, payload):
        with self.connect() as db:
            for chat in chats:
                db.execute('INSERT OR IGNORE INTO outbox(event_key,chat_id,body) VALUES (?,?,?)',
                           (key, chat, json.dumps(payload)))

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
