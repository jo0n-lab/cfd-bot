"""921-member synthetic benchmark. No operational JSON/DB, network or solver."""
from copy import deepcopy
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from cfd_bot.catalog import ticket_index
from cfd_bot.config import cases_for, load_bot, load_case
from cfd_bot.storage import Store
from cfd_bot.tickets import sync_ticket_states


def old_membership(tickets):
    macros = {c['_config']: c for c in tickets if c['task_type'] == 'macro'}
    cases = []
    for case in tickets:
        if case['task_type'] == 'macro':
            continue
        if case['role'] == 'child':
            parent_path = str((Path(case['_config']).parent / case['macro_ticket']).resolve())
            parent = macros.get(parent_path)
            if parent is None or not any(
                    str((Path(parent_path).parent / item['ticket']).resolve()) == case['_config']
                    and item['case_dir'] == case['_root'] for item in parent['cases']):
                continue
        cases.append(case)
    return cases


def measured(operation, repeats=1):
    elapsed = []
    calls = []
    for _ in range(repeats):
        with patch('cfd_bot.catalog.load_case', wraps=load_case) as load:
            started = time.perf_counter()
            operation()
            elapsed.append(time.perf_counter() - started)
            calls.append(load.call_count)
    return dict(seconds_median=round(statistics.median(elapsed), 6),
                load_case_calls=calls, samples=repeats)


def main():
    with tempfile.TemporaryDirectory(prefix='cfd-index-benchmark-') as directory:
        root = Path(directory)
        tickets_dir = root / 'tickets'
        tickets_dir.mkdir()
        parent = root / 'batch'
        parent.mkdir()
        rows = []
        for i in range(921):
            case_root = parent / f'case-{i:04}'
            case_root.mkdir()
            name = f'child-{i:04}.json'
            rows.append(dict(case_dir=str(case_root), ticket=name, state='waiting'))
            (tickets_dir / name).write_text(json.dumps(dict(
                version=1, case_dir=str(case_root), role='child', macro_ticket='macro.json',
                queue=dict(state='waiting'))))
        (tickets_dir / 'macro.json').write_text(json.dumps(dict(
            version=1, task_type='macro', case_dir=str(parent), cases=rows,
            queue=dict(state='waiting', submit=False))))
        config_path = root / 'bot.json'
        config_path.write_text(json.dumps(dict(version=1, case_globs=['tickets/*.json'],
                                              ofps_command=['NEVER-EXECUTE'], scheduler={'enabled': False})))
        config = load_bot(config_path)
        store = Store(config['state_dir'])
        result = dict(members=921, tickets=922, fixture='synthetic temporary files only')
        with patch('subprocess.Popen', side_effect=AssertionError('no subprocess')), \
                patch('urllib.request.urlopen', side_effect=AssertionError('no network')):
            result['cold_catalog'] = measured(lambda: cases_for(config))
            tickets = ticket_index(config).tickets()
            result['old_membership_only'] = measured(lambda: old_membership(tickets))
            result['new_membership_only'] = measured(lambda: cases_for(config, tickets))
            assert [c['_config'] for c in old_membership(tickets)] == [c['_config'] for c in cases_for(config, tickets)]
            result['warm_catalog'] = measured(lambda: cases_for(config), 5)
            active = {rows[0]['case_dir']: {}}
            result['warm_one_case_lookup'] = measured(lambda: ticket_index(config).cases(active), 5)
            path = tickets_dir / rows[0]['ticket']
            doc = json.loads(path.read_text())
            doc['name'] = 'external-edit'
            path.write_text(json.dumps(doc))
            result['one_changed_ticket'] = measured(lambda: ticket_index(config).cases(active))
            for _ in range(3):
                sync_ticket_states(config, store, {'cases': active})
            result['warm_sync_one_active_case'] = measured(lambda: sync_ticket_states(config, store, {'cases': active}), 5)
        output = Path(__file__).with_name('ticket-index-results.json')
        output.write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
