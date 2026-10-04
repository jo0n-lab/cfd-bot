"""Atomic JSON tickets, macro discovery/publication, and snapshot reconciliation."""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

from .config import load_case, read_json
from .control import control_times
from .ui import load_ui

STATES = {name: load_ui().text('scenarios.diagnostics.tickets.state_' + name)
          for name in ('waiting', 'running', 'finished')}


@contextmanager
def ticket_lock(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / '.tickets.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def atomic_json(path, data):
    path = Path(path)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, prefix='.', suffix='.tmp',
                                     encoding='utf-8', delete=False) as stream:
        tmp = Path(stream.name)
        try:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    try:
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def ticket_name(name, prefix):
    stem = Path(name).name
    if stem.endswith('.json'):
        stem = stem[:-5]
    stem = re.sub(r'^(macro|child|alone)-', '', stem)
    stem = re.sub(r'[^A-Za-z0-9._-]+', '-', stem).strip('.-') or 'case'
    return f'{prefix}-{stem}.json'


def clone_document(source, root, name=None):
    data = deepcopy(source)
    for key in list(data):
        if key.startswith('_') or key in ('queue', 'cases', 'macro_ticket'):
            data.pop(key)
    data.update(case_dir=str(Path(root).resolve()), name=name or Path(root).name,
                task_type='single', role='alone')
    return data


def _numeric(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= 0 else None
    except (TypeError, ValueError):
        return None


def checkpoint_time(root):
    """Use the last checkpoint shared by every decomposed processor directory."""
    root = Path(root)
    def latest(folder):
        return max((_numeric(p.name) for p in folder.iterdir()
                    if p.is_dir() and _numeric(p.name) is not None), default=0)
    processors = [p for p in root.iterdir() if p.is_dir() and re.fullmatch(r'processor\d+', p.name)]
    collated = [p for p in root.iterdir() if p.is_dir() and re.fullmatch(r'processors\d+(?:_\d+-\d+)?', p.name)]
    partitions = processors or collated
    if partitions:
        return min(latest(p) for p in partitions)
    return latest(root)


def has_postprocessing(root):
    return (Path(root) / 'postProcessing').is_dir()


def postprocess_time(root):
    """Read numeric output directories and the latest tabular sample time.

    A function's numeric directory may be its start time (e.g. forces/0), so
    inspect bounded file tails as well. Never mistake modification time for CFD time.
    """
    root = Path(root) / 'postProcessing'
    latest = 0.0
    if not root.is_dir():
        return None
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(folder) / d).is_symlink()]
        for name in dirs:
            value = _numeric(name)
            if value is not None:
                latest = max(latest, value)
        numeric_dirs = [d for d in dirs if _numeric(d) is not None]
        newest = max((_numeric(d) for d in numeric_dirs), default=None)
        dirs[:] = [d for d in dirs if _numeric(d) is None or _numeric(d) == newest]
        for name in files:
            path = Path(folder) / name
            if path.is_symlink() or path.suffix.lower() not in ('.dat', '.csv'):
                continue
            with path.open('rb') as stream:
                header = stream.read(4096).decode('utf-8', 'replace')
                if not re.search(r'^\s*#?\s*(?:time|iteration|iter)\b', header, re.I | re.M):
                    continue
                offset = max(0, path.stat().st_size - 16384)
                stream.seek(offset)
                lines = stream.read().decode('utf-8', 'replace').splitlines()
                for line in lines[1:] if offset else lines:
                    parts = re.split(r'[\s,;]+', line.strip())
                    value = _numeric(parts[0]) if parts else None
                    if value is not None:
                        latest = max(latest, value)
    return latest


def discover_cases(root, observed, end_time=None):
    ui = load_ui()
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(ui.text('scenarios.diagnostics.tickets.root_missing'))
    rows, skipped = [], []
    for case in sorted(root.iterdir(), key=lambda path: path.name):
        if (not case.is_dir() or case.is_symlink() or case.name.startswith('.')
                or case.name.endswith('-template') or not (case / 'Allrun').is_file()):
            continue
        if str(case) in observed:
            skipped.append((str(case), ui.text('scenarios.diagnostics.tickets.discovery_running')))
            continue
        control = control_times({'_root': str(case), 'end_time': end_time})
        target = control.get('end') if control else None
        post = postprocess_time(case)
        checkpoint = checkpoint_time(case)
        if post is None:
            reason = ui.text('scenarios.diagnostics.tickets.never_run')
        elif target is None:
            reason = ui.text('scenarios.diagnostics.tickets.end_unknown',
                             checkpoint=f'{checkpoint:g}', post=f'{post:g}')
        elif checkpoint >= target and post >= target:
            reason = ui.text('scenarios.diagnostics.tickets.complete', checkpoint=f'{checkpoint:g}',
                             post=f'{post:g}', target=f'{target:g}')
        else:
            reason = ui.text('scenarios.diagnostics.tickets.incomplete', checkpoint=f'{checkpoint:g}',
                             post=f'{post:g}', target=f'{target:g}')
        rows.append(dict(case_dir=str(case), state='waiting', reason=reason))
    return rows, skipped


def publish_macro(path, data, previous=None, *, request_id=None, locked=False):
    """Publish children first and commit their macro last; rollback on error."""
    ui = load_ui()
    path = Path(path)
    rows = data.get('cases', [])
    if not rows:
        raise ValueError(ui.text('scenarios.diagnostics.tickets.macro_empty'))
    with nullcontext() if locked else ticket_lock(path.parent):
        saved = read_json(previous) if previous and Path(previous).exists() else None
        editing = saved is not None and saved.get('task_type') == 'macro'
        reconfigured = False
        if editing:
            if Path(previous) != path:
                raise ValueError(ui.text('scenarios.diagnostics.tickets.macro_filename'))
            if [r['case_dir'] for r in saved['cases']] != [r['case_dir'] for r in rows]:
                queue = saved.get('queue', {})
                if (queue.get('submit') or queue.get('state') == 'running'
                        or (queue.get('state') == 'waiting' and queue.get('request_id'))):
                    raise ValueError(ui.text('scenarios.diagnostics.tickets.macro_order_busy'))
                reconfigured = True
        existing = [load_case(p) for p in path.parent.glob('*.json') if p != path]
        by_root = {c['_root']: c for c in existing if c['task_type'] == 'single'}
        saved_rows = {str(Path(r['case_dir']).resolve()): r for r in saved['cases']} if editing else {}
        selected_roots = {str(Path(row['case_dir']).resolve()) for row in rows}
        staged, removed = {}, []
        if reconfigured:
            affected = set(saved_rows) | selected_roots
            for root in affected:
                queue = by_root.get(root, {}).get('queue', {})
                if (queue.get('submit') or queue.get('state') == 'running'
                        or (queue.get('state') == 'waiting' and queue.get('job_id'))):
                    raise ValueError(ui.text('scenarios.diagnostics.tickets.membership_busy', root=root))
            # Removing a row removes batch membership, not its monitoring ticket or results.
            for root in set(saved_rows) - selected_roots:
                old = by_root.get(root)
                if old is None:
                    continue
                original = Path(old['_config'])
                if old['role'] != 'child' or (original.parent / old['macro_ticket']).resolve() != path.resolve():
                    raise ValueError(ui.text('scenarios.diagnostics.tickets.child_invalid', root=root))
                detached = read_json(original)
                detached['role'] = 'alone'
                detached.pop('macro_ticket', None)
                target = path.parent / ticket_name(original.name, 'alone')
                if target != original and target.exists():
                    raise ValueError(ui.text('scenarios.diagnostics.tickets.standalone_exists', name=target.name))
                staged[target] = detached
                if target != original:
                    removed.append(original)
        request = saved['queue'].get('request_id') if editing else request_id or uuid.uuid4().hex
        macro = deepcopy(data)
        macro.update(task_type='macro', role='alone', cases=[],
                     queue=dict(state='waiting', submit=True, request_id=request))
        if editing:
            macro['queue'] = saved['queue']
        if reconfigured:
            # No submission until the user explicitly executes the revised batch.
            macro['queue'] = dict(state='waiting', submit=False)
        for row in rows:
            root = str(Path(row['case_dir']).resolve())
            old = by_root.get(root)
            old_data = read_json(old['_config']) if old else {}
            if old and old['role'] == 'child':
                parent = (Path(old['_config']).parent / old['macro_ticket']).resolve()
                if parent != path.resolve():
                    raise ValueError(ui.text('scenarios.diagnostics.tickets.other_macro', root=root))
            suffix = hashlib.sha256(root.encode()).hexdigest()[:8]
            # Editing a batch must retain the paths used by its queued/running jobs.
            name = (Path(old['_config']).name if editing and old and old['role'] == 'child' else
                    ticket_name(Path(root).name + '-' + suffix, 'child'))
            child = clone_document(data, root)
            if data.get('resource_source') != 'macro':
                for key in ('command', 'cores', 'cpu_set', 'cpu_policy'):
                    child.pop(key, None)
                    if key in old_data:
                        child[key] = old_data[key]
            child.update(role='child', macro_ticket=path.name, queue=dict(state='waiting'))
            if request:
                child['queue']['request_id'] = request
            if old and old.get('queue', {}).get('state') == 'running':
                if not editing or old['role'] != 'child':
                    raise ValueError(ui.text('scenarios.diagnostics.tickets.adopt_running', root=root))
                # Request data and watcher settings do not launch or reconfigure a
                # solver. Protect execution and monitor-process settings instead.
                protected = {
                    'command': (ui.text('scenarios.diagnostics.tickets.protected_command'), None),
                    'cores': (ui.text('scenarios.diagnostics.tickets.protected_cores'), 1),
                    'cpu_set': (ui.text('scenarios.diagnostics.tickets.protected_cpu_set'), None),
                    'resource_source': (ui.text('scenarios.diagnostics.tickets.protected_source'), 'case'),
                    'cpu_policy': (ui.text('scenarios.diagnostics.tickets.protected_policy'), 'manual'),
                    'allow_cross_socket': (ui.text('scenarios.diagnostics.tickets.protected_socket'), False),
                    'preprocess': (ui.text('scenarios.diagnostics.tickets.protected_pre'), []),
                    'postprocess': (ui.text('scenarios.diagnostics.tickets.protected_post'), []),
                    'monitoring': (ui.text('scenarios.diagnostics.tickets.protected_monitoring'), None),
                }
                changed = [label for key, (label, default) in protected.items()
                           if child.get(key, default) != old_data.get(key, default)]
                if changed:
                    raise ValueError(ui.text('scenarios.diagnostics.tickets.running_change',
                                             fields=', '.join(changed), root=root))
            if editing and old:
                child['queue'] = old.get('queue', child['queue'])
            if reconfigured:
                child['queue'] = dict(state='waiting')
            staged[path.parent / name] = child
            saved_row = deepcopy(saved_rows.get(root, {})) if not reconfigured else {}
            saved_row.update(case_dir=root, ticket=name, state=child['queue']['state'])
            macro['cases'].append(saved_row)
            if old and Path(old['_config']) != path.parent / name:
                removed.append(Path(old['_config']))
        staged[path] = macro
        if previous and Path(previous) != path:
            removed.append(Path(previous))
        backups = {p: p.read_bytes() if p.exists() else None for p in set(staged) | set(removed)}
        try:
            for target, document in staged.items():
                atomic_json(target, document)
                load_case(target)
            for target in removed:
                target.unlink(missing_ok=True)
        except BaseException:
            for target, before in backups.items():
                if before is None:
                    target.unlink(missing_ok=True)
                else:
                    target.write_bytes(before)
            raise
    return macro


def sync_ticket_states(config, store, snapshot, tickets=None):
    """Publish known states after a successful scan; absence alone is not success."""
    ui = load_ui(config.get('_ui_dir'))
    from .config import cases_for, tickets_for
    from .storage import LIVE
    tickets = tickets_for(config) if tickets is None else tickets
    cases = cases_for(config, tickets)
    jobs = store.jobs()
    jobs_by_root = {}
    for job in jobs:
        jobs_by_root.setdefault(job['case_root'], []).append(job)
    observed = store.get_many('observed:' + case['_root'] for case in cases)
    states = {}
    for case in cases:
        path = Path(case['_config'])
        runs = list(jobs_by_root.get(case['_root'], ()))
        external = observed.get('observed:' + case['_root'])
        if external:
            runs.append(external)
        pending = [r for r in runs if r['status'] in (*LIVE, 'queued')]
        run = max(pending or runs, key=lambda r: r['created']) if runs else None
        queue = dict(case.get('queue', {}))
        if case['_root'] in snapshot['cases']:
            queue.update(state='running', result=None,
                         reason=ui.text('scenarios.diagnostics.tickets.ofps_running'))
        elif queue.get('submit'):
            queue.update(state='waiting', result=None)
        elif (queue.get('request_id') and not queue.get('job_id')
              and not any(r.get('batch') == queue['request_id'] for r in runs)):
            queue.update(state='waiting', result=None)
        elif run:
            status = run['status']
            queue.update(state='waiting' if status == 'queued' else 'running' if status in LIVE else 'finished',
                         result=status if status not in LIVE and status != 'queued' else None,
                         reason=run.get('reason', ''), job_id=run['id'])
        else:
            queue.setdefault('state', 'waiting')
        states[case['_root']] = queue
        with ticket_lock(path.parent):
            if not path.exists():
                continue
            data = read_json(path)
            # Preserve an explicit new submission written since the scan started.
            old = data.get('queue', {})
            if old.get('request_id') != case.get('queue', {}).get('request_id'):
                continue
            if queue != old:
                data['queue'] = dict(queue, updated_at=time.time())
                atomic_json(path, data)
    for macro in (ticket for ticket in tickets if ticket['task_type'] == 'macro'):
        path = Path(macro['_config'])
        with ticket_lock(path.parent):
            data = read_json(path)
            changed = False
            for row in data['cases']:
                state = states.get(row['case_dir'])
                if not state:
                    continue
                updated = {key: state[key] for key in ('state', 'result', 'reason', 'job_id') if key in state}
                if any(row.get(k) != v for k, v in updated.items()):
                    row.update(updated)
                    changed = True
            values = [r['state'] for r in data['cases']]
            state = ('running' if 'running' in values else 'finished'
                     if values and all(s == 'finished' for s in values) else 'waiting')
            if data['queue'].get('state') != state:
                data['queue']['state'] = state
                changed = True
            if changed:
                data['queue']['updated_at'] = time.time()
                atomic_json(path, data)


def accept_submissions(config, store):
    ui = load_ui(config.get('_ui_dir'))
    from .config import cases_for, tickets_for
    by_path = {c['_config']: c for c in cases_for(config)}
    for ticket in tickets_for(config):
        queue = ticket.get('queue', {})
        if not queue.get('submit'):
            continue
        request = queue.get('request_id')
        if not request:
            continue
        if ticket['task_type'] == 'macro':
            children = [by_path.get(str((Path(ticket['_config']).parent / r['ticket']).resolve()))
                        for r in ticket['cases']]
        else:
            children = [ticket]
        if not children or any(c is None for c in children):
            raise ValueError(ui.text('scenarios.diagnostics.tickets.children_missing', name=ticket['name']))
        path = Path(ticket['_config'])
        try:
            store.enqueue_batch(children, request)
        except ValueError as exc:
            with ticket_lock(path.parent):
                data = read_json(path)
                if data.get('queue', {}).get('request_id') == request:
                    data['queue']['reason'] = str(exc)
                    atomic_json(path, data)
            store.event('submission-blocked:' + request, config['telegram']['chat_ids'],
                        dict(kind='text', text=ui.text('scenarios.notifications.waiting',
                                                      case_name=ticket['name'], reason=exc)))
            continue
        with ticket_lock(path.parent):
            data = read_json(path)
            if data.get('queue', {}).get('request_id') == request:
                data['queue']['submit'] = False
                atomic_json(path, data)
        store.event('submission:' + request, config['telegram']['chat_ids'],
                    dict(kind='text', text=ui.text('scenarios.diagnostics.tickets.batch_registered',
                                                  name=ticket['name'], count=len(children))))
