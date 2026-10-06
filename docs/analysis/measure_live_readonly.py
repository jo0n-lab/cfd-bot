"""Read-only timing of scanner and /stat stages; no sends, sync, or scheduling."""

def main():
    from pathlib import Path
    Path('/tmp/cfd-architecture-review').mkdir(parents=True, exist_ok=True)
    import collections
    import contextlib
    import datetime
    import fcntl
    import json
    from pathlib import Path
    import signal
    import sqlite3
    import subprocess
    import sys
    import time

    ROOT = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(ROOT))
    from cfd_bot.config import load_bot, tickets_for, cases_for
    from cfd_bot.processes import managed_scan_environment, parse_snapshot
    from cfd_bot.storage import Store
    from cfd_bot.bot import Bot

    result = {'measured_at': datetime.datetime.now().astimezone().isoformat(), 'timings_s': {}}

    def timed(name, fn):
        start = time.perf_counter()
        value = fn()
        elapsed = time.perf_counter() - start
        result['timings_s'][name] = round(elapsed, 6)
        print(json.dumps({'stage': name, 'seconds': round(elapsed, 6)}), flush=True)
        return value

    def expired(signum, frame):
        raise TimeoutError('diagnostic stage exceeded 90 seconds')

    signal.signal(signal.SIGALRM, expired)
    signal.alarm(90)
    config = load_bot(ROOT / 'bot.json')
    assert len(config['ofps_command']) == 1
    assert Path(config['ofps_command'][0]).resolve() == ROOT / 'bin/ofps'
    result['proc_count_before'] = len(list(Path('/proc').glob('[0-9]*')))
    result['poll_seconds'] = config['poll_seconds']

    raw = None
    for i in range(3):
        proc = timed('managed_ofps_' + str(i+1), lambda: subprocess.run(
            config['ofps_command'], capture_output=True, text=True, timeout=45,
            env=managed_scan_environment()))
        if proc.returncode:
            raise RuntimeError('scanner failed with exit code ' + str(proc.returncode))
        if not ('ENGINE: ' in proc.stdout or 'No active ' in proc.stdout):
            raise RuntimeError('scanner output not recognized')
        raw = proc.stdout
    snapshot = {'raw': raw, 'cases': timed('parse_snapshot', lambda: parse_snapshot(raw)), 'at': time.time()}
    result['live_cases'] = len(snapshot['cases'])
    result['solver_processes'] = sum(len(c.get('processes', [])) for c in snapshot['cases'].values())
    result['supervisor_processes'] = sum(len(c.get('supervisors', [])) for c in snapshot['cases'].values())

    original_flock = fcntl.flock
    lock_times = []
    def counted_flock(fd, operation):
        start = time.perf_counter()
        value = original_flock(fd, operation)
        if operation & fcntl.LOCK_EX:
            lock_times.append(time.perf_counter() - start)
        return value
    fcntl.flock = counted_flock
    try:
        tickets = timed('tickets_for', lambda: tickets_for(config))
    finally:
        fcntl.flock = original_flock
    result['ticket_lock_wait_s'] = round(sum(lock_times), 6)
    result['ticket_count'] = len(tickets)
    result['ticket_types'] = dict(collections.Counter(c['task_type'] + ':' + c['role'] for c in tickets))
    macros = {c['_config']: c for c in tickets if c['task_type'] == 'macro'}
    result['macro_member_counts'] = sorted([len(c['cases']) for c in macros.values()], reverse=True)
    cases = timed('cases_for_membership_1', lambda: cases_for(config, tickets))
    cases_again = timed('cases_for_membership_2', lambda: cases_for(config, tickets))
    assert [c['_config'] for c in cases] == [c['_config'] for c in cases_again]
    result['registered_cases'] = len(cases)

    # Pure read-only comparison: resolve every macro member path once per invocation.
    # This is a diagnostic prototype, not a product change or a persistent cache.
    def indexed_membership():
        membership = {
            name: {(str((Path(name).parent / row.get('ticket', '')).resolve()), row['case_dir'])
                   for row in macro['cases']}
            for name, macro in macros.items()
        }
        answer = []
        for case in tickets:
            case['_ui_dir'] = config['_ui_dir']
            if case['task_type'] == 'macro':
                continue
            if case['role'] == 'child':
                parent = str((Path(case['_config']).parent / case['macro_ticket']).resolve())
                if (case['_config'], case['_root']) not in membership.get(parent, set()):
                    continue
            answer.append(case)
        roots = [c['_root'] for c in answer]
        assert len(roots) == len(set(roots))
        return answer

    indexed = timed('membership_index_prototype', indexed_membership)
    result['prototype_same_case_order'] = [c['_config'] for c in cases] == [c['_config'] for c in indexed]

    class ReadOnlyStore(Store):
        def __init__(self, path):
            self.path = path
            self.root = path.parent
        @contextlib.contextmanager
        def connect(self):
            db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=2)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            try:
                yield db
            finally:
                db.close()

    store = ReadOnlyStore(Path(config['state_dir']) / 'state.sqlite3')
    with store.connect() as db:
        result['job_counts'] = dict(db.execute('SELECT status,count(*) FROM jobs GROUP BY status').fetchall())
        result['run_history_count'] = db.execute('SELECT count(*) FROM run_history').fetchone()[0]
    bot = Bot(config, store, None)
    runs = timed('active_runs', lambda: bot.active_runs(snapshot, cases))
    timed('status_format_and_history', lambda: bot.status(snapshot, runs, None))
    queued = timed('queued_jobs_read_and_decode', lambda: store.jobs(('queued',)))
    result['queued_jobs_read'] = len(queued)
    result['notes'] = ['Actual host read-only measurements; no Telegram API calls.',
                       'ofps invoked with CFD_BOT_OFPS_MANAGED=1: no ticket synchronization.',
                       'Does not include the running daemon SNAPSHOT_LOCK wait or Telegram request queue.',
                       'Indexed prototype only checks this sample; no product source changed.']
    signal.alarm(0)
    Path('/tmp/cfd-architecture-review/live-readonly-results.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
