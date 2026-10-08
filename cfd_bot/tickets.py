"""Atomic JSON tickets, macro discovery/publication, and snapshot reconciliation."""
from . import diagnostics as _diagnostics
from contextlib import contextmanager, nullcontext
from copy import deepcopy
import fcntl
from fnmatch import fnmatchcase
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import threading
import uuid

from .config import glob_patterns, load_case, read_json
from .control import control_times
from .processes import calculation_record
from .ui import load_ui

STATES = {name: load_ui().text('scenarios.diagnostics.tickets.state_' + name)
          for name in ('waiting', 'running', 'finished')}


_held_locks = threading.local()


@_diagnostics.trace
@contextmanager
def ticket_lock(folder):
    folder = Path(folder).resolve()
    held = getattr(_held_locks, 'folders', None)
    if held is None:
        if _diagnostics.detailed: _diagnostics.step('tickets.ticket_lock:L32:then')
        held = _held_locks.folders = set()
    if folder in held:
        if _diagnostics.detailed: _diagnostics.step('tickets.ticket_lock:L34:then')
        yield
        return
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / '.tickets.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        held.add(folder)
        try:
            yield
        finally:
            held.remove(folder)


@_diagnostics.trace
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
            if _diagnostics.enabled: _diagnostics.step('tickets.atomic_json:L57:except')
            tmp.unlink(missing_ok=True)
            raise
    try:
        tmp.replace(path)
        if _diagnostics.enabled: _diagnostics.event('ticket.file.replaced', path=path, ticket=data)
    finally:
        tmp.unlink(missing_ok=True)


@_diagnostics.trace
def ticket_name(name, prefix):
    stem = Path(name).name
    if stem.endswith('.json'):
        if _diagnostics.detailed: _diagnostics.step('tickets.ticket_name:L68:then')
        stem = stem[:-5]
    stem = re.sub(r'^(macro|child|alone)-', '', stem)
    stem = re.sub(r'[^A-Za-z0-9._-]+', '-', stem).strip('.-') or 'case'
    return f'{prefix}-{stem}.json'


@_diagnostics.trace
def clone_document(source, root, name=None):
    data = deepcopy(source)
    for key in list(data):
        if _diagnostics.detailed: _diagnostics.step('tickets.clone_document:L77:loop', key=key)
        if key.startswith('_') or key in ('queue', 'cases', 'macro_ticket'):
            if _diagnostics.detailed: _diagnostics.step('tickets.clone_document:L78:then')
            data.pop(key)
    data.update(case_dir=str(Path(root).resolve()), name=name or Path(root).name,
                task_type='single', role='alone')
    return data


@_diagnostics.trace
def _numeric(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= 0 else None
    except (TypeError, ValueError):
        if _diagnostics.enabled: _diagnostics.step('tickets._numeric:L89:except')
        return None


@_diagnostics.trace
def checkpoint_time(root):
    """Use the last checkpoint shared by every decomposed processor directory."""
    root = Path(root)
    @_diagnostics.trace
    def latest(folder):
        return max((_numeric(p.name) for p in folder.iterdir()
                    if p.is_dir() and _numeric(p.name) is not None), default=0)
    processors = [p for p in root.iterdir() if p.is_dir() and re.fullmatch(r'processor\d+', p.name)]
    collated = [p for p in root.iterdir() if p.is_dir() and re.fullmatch(r'processors\d+(?:_\d+-\d+)?', p.name)]
    partitions = processors or collated
    if partitions:
        if _diagnostics.detailed: _diagnostics.step('tickets.checkpoint_time:L102:then')
        return min(latest(p) for p in partitions)
    return latest(root)


@_diagnostics.trace
def has_postprocessing(root):
    return (Path(root) / 'postProcessing').is_dir()


@_diagnostics.trace
def postprocess_time(root):
    """Read numeric output directories and the latest tabular sample time.

    A function's numeric directory may be its start time (e.g. forces/0), so
    inspect bounded file tails as well. Never mistake modification time for CFD time.
    """
    root = Path(root) / 'postProcessing'
    latest = 0.0
    if not root.is_dir():
        if _diagnostics.detailed: _diagnostics.step('tickets.postprocess_time:L119:then')
        return None
    for folder, dirs, files in os.walk(root, followlinks=False):
        if _diagnostics.detailed: _diagnostics.step('tickets.postprocess_time:L121:loop', folder=folder, dirs=dirs, files=files)
        dirs[:] = [d for d in dirs if not (Path(folder) / d).is_symlink()]
        for name in dirs:
            if _diagnostics.detailed: _diagnostics.step('tickets.postprocess_time:L123:loop', name=name)
            value = _numeric(name)
            if value is not None:
                if _diagnostics.detailed: _diagnostics.step('tickets.postprocess_time:L125:then')
                latest = max(latest, value)
        numeric_dirs = [d for d in dirs if _numeric(d) is not None]
        newest = max((_numeric(d) for d in numeric_dirs), default=None)
        dirs[:] = [d for d in dirs if _numeric(d) is None or _numeric(d) == newest]
        for name in files:
            if _diagnostics.detailed: _diagnostics.step('tickets.postprocess_time:L130:loop', name=name)
            path = Path(folder) / name
            if path.is_symlink() or path.suffix.lower() not in ('.dat', '.csv'):
                if _diagnostics.detailed: _diagnostics.step('tickets.postprocess_time:L132:then')
                continue
            with path.open('rb') as stream:
                header = stream.read(4096).decode('utf-8', 'replace')
                if not re.search(r'^\s*#?\s*(?:time|iteration|iter)\b', header, re.I | re.M):
                    if _diagnostics.detailed: _diagnostics.step('tickets.postprocess_time:L136:then')
                    continue
                offset = max(0, path.stat().st_size - 16384)
                stream.seek(offset)
                lines = stream.read().decode('utf-8', 'replace').splitlines()
                for line in lines[1:] if offset else lines:
                    if _diagnostics.detailed: _diagnostics.step('tickets.postprocess_time:L141:loop', line=line)
                    parts = re.split(r'[\s,;]+', line.strip())
                    value = _numeric(parts[0]) if parts else None
                    if value is not None:
                        if _diagnostics.detailed: _diagnostics.step('tickets.postprocess_time:L144:then')
                        latest = max(latest, value)
    return latest


@_diagnostics.trace
def discover_cases(root, observed, end_time=None, include_patterns=None, exclude_patterns=None):
    """Find direct child cases and apply basename glob filters.

    Empty include patterns admit every existing candidate. Any exclude match wins.
    """
    ui = load_ui()
    include_patterns = [] if include_patterns is None else include_patterns
    exclude_patterns = [] if exclude_patterns is None else exclude_patterns
    glob_patterns(include_patterns, 'discovery.include_patterns')
    glob_patterns(exclude_patterns, 'discovery.exclude_patterns')
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        if _diagnostics.detailed: _diagnostics.step('tickets.discover_cases:L160:then')
        raise ValueError(ui.text('scenarios.diagnostics.tickets.root_missing'))
    rows, skipped = [], []
    for case in sorted(root.iterdir(), key=lambda path: path.name):
        if _diagnostics.detailed: _diagnostics.step('tickets.discover_cases:L163:loop', case=case)
        if (not case.is_dir() or case.is_symlink() or case.name.startswith('.')
                or case.name.endswith('-template') or not (case / 'Allrun').is_file()):
            if _diagnostics.detailed: _diagnostics.step('tickets.discover_cases:L164:then')
            continue
        if any(fnmatchcase(case.name, pattern) for pattern in exclude_patterns):
            if _diagnostics.detailed: _diagnostics.step('tickets.discover_cases:L167:then')
            skipped.append((str(case), ui.text(
                'scenarios.diagnostics.tickets.discovery_excluded')))
            continue
        if include_patterns and not any(
                fnmatchcase(case.name, pattern) for pattern in include_patterns):
            if _diagnostics.detailed: _diagnostics.step('tickets.discover_cases:L171:then')
            skipped.append((str(case), ui.text(
                'scenarios.diagnostics.tickets.discovery_not_included')))
            continue
        if str(case) in observed:
            if _diagnostics.detailed: _diagnostics.step('tickets.discover_cases:L176:then')
            skipped.append((str(case), ui.text('scenarios.diagnostics.tickets.discovery_running')))
            continue
        control = control_times({'_root': str(case), 'end_time': end_time})
        target = control.get('end') if control else None
        post = postprocess_time(case)
        checkpoint = checkpoint_time(case)
        if post is None:
            if _diagnostics.detailed: _diagnostics.step('tickets.discover_cases:L183:then')
            reason = ui.text('scenarios.diagnostics.tickets.never_run')
        elif target is None:
            if _diagnostics.detailed: _diagnostics.step('tickets.discover_cases:L185:then')
            reason = ui.text('scenarios.diagnostics.tickets.end_unknown',
                             checkpoint=f'{checkpoint:g}', post=f'{post:g}')
        elif checkpoint >= target and post >= target:
            if _diagnostics.detailed: _diagnostics.step('tickets.discover_cases:L188:then')
            reason = ui.text('scenarios.diagnostics.tickets.complete', checkpoint=f'{checkpoint:g}',
                             post=f'{post:g}', target=f'{target:g}')
        else:
            if _diagnostics.detailed: _diagnostics.step('tickets.discover_cases:L188:else')
            reason = ui.text('scenarios.diagnostics.tickets.incomplete', checkpoint=f'{checkpoint:g}',
                             post=f'{post:g}', target=f'{target:g}')
        rows.append(dict(case_dir=str(case), state='waiting', reason=reason))
    return rows, skipped


@_diagnostics.trace
def publish_macro(path, data, previous=None, *, request_id=None, submit=True, locked=False):
    """Publish children first and commit their macro last; rollback on error."""
    ui = load_ui()
    path = Path(path)
    rows = data.get('cases', [])
    if not rows:
        if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L203:then')
        raise ValueError(ui.text('scenarios.diagnostics.tickets.macro_empty'))
    with nullcontext() if locked else ticket_lock(path.parent):
        saved = read_json(previous) if previous and Path(previous).exists() else None
        editing = saved is not None and saved.get('task_type') == 'macro'
        reconfigured = False
        if editing:
            if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L209:then')
            if Path(previous) != path:
                if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L210:then')
                raise ValueError(ui.text('scenarios.diagnostics.tickets.macro_filename'))
            if [r['case_dir'] for r in saved['cases']] != [r['case_dir'] for r in rows]:
                if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L212:then')
                queue = saved.get('queue', {})
                if (queue.get('submit') or queue.get('state') == 'running'
                        or (queue.get('state') == 'waiting' and queue.get('request_id'))):
                    if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L214:then')
                    raise ValueError(ui.text('scenarios.diagnostics.tickets.macro_order_busy'))
                reconfigured = True
        from .catalog import folder_index
        existing = [c for c in folder_index(path.parent).tickets() if c['_config'] != str(path)]
        by_root = {c['_root']: c for c in existing if c['task_type'] == 'single'}
        saved_rows = {str(Path(r['case_dir']).resolve()): r for r in saved['cases']} if editing else {}
        selected_roots = {str(Path(row['case_dir']).resolve()) for row in rows}
        staged, removed = {}, []
        if reconfigured:
            if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L224:then')
            affected = set(saved_rows) | selected_roots
            for root in affected:
                if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L226:loop', root=root)
                queue = by_root.get(root, {}).get('queue', {})
                if (queue.get('submit') or queue.get('state') == 'running'
                        or (queue.get('state') == 'waiting' and queue.get('job_id'))):
                    if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L228:then')
                    raise ValueError(ui.text('scenarios.diagnostics.tickets.membership_busy', root=root))
            # Removing a row removes batch membership, not its monitoring ticket or results.
            for root in set(saved_rows) - selected_roots:
                if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L232:loop', root=root)
                old = by_root.get(root)
                if old is None:
                    if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L234:then')
                    continue
                original = Path(old['_config'])
                if old['role'] != 'child' or (original.parent / old['macro_ticket']).resolve() != path.resolve():
                    if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L237:then')
                    raise ValueError(ui.text('scenarios.diagnostics.tickets.child_invalid', root=root))
                detached = read_json(original)
                detached['role'] = 'alone'
                detached.pop('macro_ticket', None)
                detached.pop('execution_queue', None)
                detached['dynamic_cores'] = False
                target = path.parent / ticket_name(original.name, 'alone')
                if target != original and target.exists():
                    if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L245:then')
                    raise ValueError(ui.text('scenarios.diagnostics.tickets.standalone_exists', name=target.name))
                staged[target] = detached
                if target != original:
                    if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L248:then')
                    removed.append(original)
        request = (saved['queue'].get('request_id') if editing else
                   (request_id or uuid.uuid4().hex) if submit else None)
        macro = deepcopy(data)
        queue = dict(state='waiting', submit=bool(submit))
        if request:
            if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L254:then')
            queue['request_id'] = request
        macro.update(task_type='macro', role='alone', cases=[], queue=queue)
        if editing:
            if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L257:then')
            macro['queue'] = saved['queue']
        if reconfigured:
            # No submission until the user explicitly executes the revised batch.
            if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L259:then')
            macro['queue'] = dict(state='waiting', submit=False)
        for row in rows:
            if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L262:loop', row=row)
            root = str(Path(row['case_dir']).resolve())
            old = by_root.get(root)
            old_data = read_json(old['_config']) if old else {}
            if old and old['role'] == 'child':
                if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L266:then')
                parent = (Path(old['_config']).parent / old['macro_ticket']).resolve()
                if parent != path.resolve():
                    if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L268:then')
                    raise ValueError(ui.text('scenarios.diagnostics.tickets.other_macro', root=root))
            suffix = hashlib.sha256(root.encode()).hexdigest()[:8]
            # Editing a batch must retain the paths used by its queued/running jobs.
            name = (Path(old['_config']).name if editing and old and old['role'] == 'child' else
                    ticket_name(Path(root).name + '-' + suffix, 'child'))
            child = clone_document(data, root)
            child.pop('discovery', None)
            if data.get('dynamic_cores'):
                if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L276:then')
                child['cores'] = row['cores']
                child['cpu_policy'] = 'auto'
                child.pop('cpu_set', None)
            if data.get('resource_source') != 'macro':
                if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L280:then')
                for key in ('command', 'cores', 'cpu_set', 'cpu_policy'):
                    if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L281:loop', key=key)
                    child.pop(key, None)
                    if key in old_data:
                        if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L283:then')
                        child[key] = old_data[key]
            child.update(role='child', macro_ticket=path.name, queue=dict(state='waiting'))
            if request:
                if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L286:then')
                child['queue']['request_id'] = request
            if old and old.get('queue', {}).get('state') == 'running':
                if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L288:then')
                if not editing or old['role'] != 'child':
                    if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L289:then')
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
                    'execution_queue': (ui.text('scenarios.diagnostics.tickets.protected_queue'), None),
                    'dynamic_cores': (ui.text('scenarios.diagnostics.tickets.protected_dynamic'), False),
                }
                changed = [label for key, (label, default) in protected.items()
                           if child.get(key, default) != old_data.get(key, default)]
                if changed:
                    if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L308:then')
                    raise ValueError(ui.text('scenarios.diagnostics.tickets.running_change',
                                             fields=', '.join(changed), root=root))
            if editing and old:
                if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L311:then')
                child['queue'] = old.get('queue', child['queue'])
            if reconfigured:
                if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L313:then')
                child['queue'] = dict(state='waiting')
            staged[path.parent / name] = child
            saved_row = deepcopy(saved_rows.get(root, {})) if not reconfigured else {}
            saved_row.update(case_dir=root, ticket=name, state=child['queue']['state'])
            if data.get('dynamic_cores'):
                if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L318:then')
                saved_row['cores'] = row['cores']
            else:
                if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L318:else')
                saved_row.pop('cores', None)
            macro['cases'].append(saved_row)
            if old and Path(old['_config']) != path.parent / name:
                if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L323:then')
                removed.append(Path(old['_config']))
        staged[path] = macro
        if previous and Path(previous) != path:
            if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L326:then')
            removed.append(Path(previous))
        backups = {p: p.read_bytes() if p.exists() else None for p in set(staged) | set(removed)}
        try:
            for target, document in staged.items():
                if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L330:loop', target=target, document=document)
                atomic_json(target, document)
                load_case(target)
            for target in removed:
                if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L333:loop', target=target)
                target.unlink(missing_ok=True)
        except BaseException:
            if _diagnostics.enabled: _diagnostics.step('tickets.publish_macro:L335:except')
            for target, before in backups.items():
                if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L336:loop', target=target, before=before)
                if before is None:
                    if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L337:then')
                    target.unlink(missing_ok=True)
                else:
                    if _diagnostics.detailed: _diagnostics.step('tickets.publish_macro:L337:else')
                    target.write_bytes(before)
            raise
    return macro


@_diagnostics.trace
def sync_ticket_states(config, store, snapshot, tickets=None):
    """Publish known states after a successful scan; absence alone is not success."""
    # Serialize whole publications, not just each file: an older publisher must
    # not overwrite a newer child/parent pair after its journal was acknowledged.
    with ticket_lock(store.root):
        _sync_ticket_states(config, store, snapshot, tickets)


@_diagnostics.trace
def _sync_ticket_states(config, store, snapshot, tickets=None):
    ui = load_ui(config.get('_ui_dir'))
    from .config import cases_for
    from .catalog import ticket_index
    from .storage import LIVE
    index = ticket_index(config) if tickets is None else None
    changes = index.changes() if index else {}
    pending_changes = store.ticket_changes()
    roots = set(snapshot['cases']) | set(changes) | set(pending_changes)
    cases = index.cases(roots) if index else cases_for(config, tickets)
    macros = index.related_macros(roots) if index else [t for t in tickets if t['task_type'] == 'macro']
    jobs = store.jobs_for_roots(c['_root'] for c in cases)
    skipped = set()
    jobs_by_root = {}
    for job in jobs:
        if _diagnostics.detailed: _diagnostics.step('tickets._sync_ticket_states:L367:loop', job=job)
        jobs_by_root.setdefault(job['case_root'], []).append(job)
    observed = store.get_many('observed:' + case['_root'] for case in cases)
    states = {}
    for case in cases:
        if _diagnostics.detailed: _diagnostics.step('tickets._sync_ticket_states:L371:loop', case=case)
        path = Path(case['_config'])
        runs = list(jobs_by_root.get(case['_root'], ()))
        external = observed.get('observed:' + case['_root'])
        if external:
            if _diagnostics.detailed: _diagnostics.step('tickets._sync_ticket_states:L375:then')
            runs.append(external)
        pending = [r for r in runs if r['status'] in (*LIVE, 'queued')]
        run = max(pending or runs, key=lambda r: r['created']) if runs else None
        queue = dict(case.get('queue', {}))
        if calculation_record(snapshot['cases'].get(case['_root'])) is not None:
            if _diagnostics.detailed: _diagnostics.step('tickets._sync_ticket_states:L380:then')
            queue.update(state='running', result=None,
                         reason=ui.text('scenarios.diagnostics.tickets.ofps_running'))
        elif queue.get('submit'):
            if _diagnostics.detailed: _diagnostics.step('tickets._sync_ticket_states:L383:then')
            queue.update(state='waiting', result=None)
        elif (queue.get('request_id') and not queue.get('job_id')
              and not any(r.get('batch') == queue['request_id'] for r in runs)):
            if _diagnostics.detailed: _diagnostics.step('tickets._sync_ticket_states:L385:then')
            queue.update(state='waiting', result=None)
        elif run:
            if _diagnostics.detailed: _diagnostics.step('tickets._sync_ticket_states:L388:then')
            status = run['status']
            queue.update(state='waiting' if status == 'queued' else 'running' if status in LIVE else 'finished',
                         result=status if status not in LIVE and status != 'queued' else None,
                         reason=run.get('reason', ''), job_id=run['id'])
        else:
            if _diagnostics.detailed: _diagnostics.step('tickets._sync_ticket_states:L388:else')
            queue.setdefault('state', 'waiting')
        states[case['_root']] = queue
        with ticket_lock(path.parent):
            if not path.exists():
                if _diagnostics.detailed: _diagnostics.step('tickets._sync_ticket_states:L397:then')
                continue
            data = read_json(path)
            # Preserve an explicit new submission written since the scan started.
            old = data.get('queue', {})
            if old.get('request_id') != case.get('queue', {}).get('request_id'):
                if _diagnostics.detailed: _diagnostics.step('tickets._sync_ticket_states:L402:then')
                skipped.add(case['_root'])
                states.pop(case['_root'], None)
                continue
            if queue != old:
                if _diagnostics.detailed: _diagnostics.step('tickets._sync_ticket_states:L406:then')
                data['queue'] = dict(queue, updated_at=time.time())
                atomic_json(path, data)
    for macro in macros:
        if _diagnostics.detailed: _diagnostics.step('tickets._sync_ticket_states:L409:loop', macro=macro)
        path = Path(macro['_config'])
        with ticket_lock(path.parent):
            if not path.exists():
                if _diagnostics.detailed: _diagnostics.step('tickets._sync_ticket_states:L412:then')
                continue
            data = read_json(path)
            if data.get('queue', {}).get('request_id') != macro.get('queue', {}).get('request_id'):
                if _diagnostics.detailed: _diagnostics.step('tickets._sync_ticket_states:L415:then')
                skipped.update(row['case_dir'] for row in macro['cases'])
                skipped.add(macro['_root'])
                continue
            changed = False
            for row in data['cases']:
                if _diagnostics.detailed: _diagnostics.step('tickets._sync_ticket_states:L420:loop', row=row)
                state = states.get(row['case_dir'])
                if not state:
                    if _diagnostics.detailed: _diagnostics.step('tickets._sync_ticket_states:L422:then')
                    continue
                updated = {key: state[key] for key in ('state', 'result', 'reason', 'job_id') if key in state}
                if any(row.get(k) != v for k, v in updated.items()):
                    if _diagnostics.detailed: _diagnostics.step('tickets._sync_ticket_states:L425:then')
                    row.update(updated)
                    changed = True
            values = [r['state'] for r in data['cases']]
            state = ('running' if 'running' in values else 'finished'
                     if values and all(s == 'finished' for s in values) else 'waiting')
            if data['queue'].get('state') != state:
                if _diagnostics.detailed: _diagnostics.step('tickets._sync_ticket_states:L431:then')
                data['queue']['state'] = state
                changed = True
            if changed:
                if _diagnostics.detailed: _diagnostics.step('tickets._sync_ticket_states:L434:then')
                data['queue']['updated_at'] = time.time()
                atomic_json(path, data)

    # A crash before this point retries both the child and its parent. A newer
    # DB event survives the watermark even if it arrives during JSON publication.
    store.acknowledge_ticket_changes({r: v for r, v in pending_changes.items() if r not in skipped})
    if index:
        if _diagnostics.detailed: _diagnostics.step('tickets._sync_ticket_states:L441:then')
        index.acknowledge({r: v for r, v in changes.items() if r not in skipped})


@_diagnostics.trace
def accept_submissions(config, store):
    ui = load_ui(config.get('_ui_dir'))
    from .catalog import ticket_index
    index = ticket_index(config)
    submissions = index.tickets(submit_only=True)
    if not submissions:
        if _diagnostics.detailed: _diagnostics.step('tickets.accept_submissions:L450:then')
        return
    by_path = {c['_config']: c for c in index.cases()}
    for ticket in submissions:
        if _diagnostics.detailed: _diagnostics.step('tickets.accept_submissions:L453:loop', ticket=ticket)
        queue = ticket.get('queue', {})
        if not queue.get('submit'):
            if _diagnostics.detailed: _diagnostics.step('tickets.accept_submissions:L455:then')
            continue
        request = queue.get('request_id')
        if not request:
            if _diagnostics.detailed: _diagnostics.step('tickets.accept_submissions:L458:then')
            continue
        if ticket['task_type'] == 'macro':
            if _diagnostics.detailed: _diagnostics.step('tickets.accept_submissions:L460:then')
            children = [by_path.get(str((Path(ticket['_config']).parent / r['ticket']).resolve()))
                        for r in ticket['cases']]
        else:
            if _diagnostics.detailed: _diagnostics.step('tickets.accept_submissions:L460:else')
            children = [ticket]
        if not children or any(c is None for c in children):
            if _diagnostics.detailed: _diagnostics.step('tickets.accept_submissions:L465:then')
            raise ValueError(ui.text('scenarios.diagnostics.tickets.children_missing', name=ticket['name']))
        path = Path(ticket['_config'])
        try:
            store.enqueue_batch(children, request, priority=queue.get('mode', 'queue'),
                                queue_lane=queue.get('lane', 1))
        except ValueError as exc:
            if _diagnostics.enabled: _diagnostics.step('tickets.accept_submissions:L471:except')
            with ticket_lock(path.parent):
                data = read_json(path)
                if data.get('queue', {}).get('request_id') == request:
                    if _diagnostics.detailed: _diagnostics.step('tickets.accept_submissions:L474:then')
                    data['queue']['reason'] = str(exc)
                    atomic_json(path, data)
            store.event('submission-blocked:' + request, config['telegram']['chat_ids'],
                        dict(kind='text', text=ui.text('scenarios.notifications.waiting',
                                                      case_name=ticket['name'], reason=exc)))
            continue
        with ticket_lock(path.parent):
            data = read_json(path)
            if data.get('queue', {}).get('request_id') == request:
                if _diagnostics.detailed: _diagnostics.step('tickets.accept_submissions:L483:then')
                data['queue']['submit'] = False
                atomic_json(path, data)
        store.event('submission:' + request, config['telegram']['chat_ids'],
                    dict(kind='text', text=ui.text('scenarios.diagnostics.tickets.batch_registered',
                                                  name=ticket['name'], count=len(children))))
