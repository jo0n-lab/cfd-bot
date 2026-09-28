"""Ticket editing operations shared by native and Telegram interfaces."""
from copy import deepcopy
import hashlib
import json
import math
import re
import shlex
import tempfile
import uuid
from pathlib import Path

from .config import load_case, read_json
from .tickets import atomic_json, clone_document, publish_macro, ticket_lock, ticket_name
from .ui import load_ui

EVENTS = {event: load_ui().text('menus.tickets.basic.event_' + event)
          for event in ('started', 'succeeded', 'failed', 'interrupted')}
DEFAULT_CASE_ROOT = Path.home() / 'OpenFOAM' / (Path.home().name + '-dev') / 'run'
DEFAULT_SCRIPTS = {'preprocess': './Allclean', 'postprocess': './Allpost'}
TEMPLATE = {'version': 1, 'case_dir': '', 'name': '', 'cpu_policy': 'auto',
            'allow_cross_socket': True, 'watcher': {'log': 'log.solver'},
            **{stage: [{'command': [command]}] for stage, command in DEFAULT_SCRIPTS.items()}}


def case_browser_start(directory, tickets_dir):
    if directory.strip():
        current = (Path(tickets_dir) / Path(directory.strip()).expanduser()).resolve()
        if current.is_dir():
            return current
    for folder in (DEFAULT_CASE_ROOT, *DEFAULT_CASE_ROOT.parents):
        if folder.is_dir():
            return folder


def script_commands(text, previous=()):
    """One argv command per line; preserve configured timeouts when editing."""
    result = []
    for index, line in enumerate(lines(text)):
        command = shlex.split(line)
        if not command:
            raise ValueError(load_ui().text('scenarios.diagnostics.editor.script_command'))
        hook = deepcopy(previous[index]) if index < len(previous) else {}
        hook['command'] = command
        result.append(hook)
    return result


def form_values(data, tickets_dir):
    """Expose shared ticket settings, retaining legacy case-based execution."""
    watcher = data.get('watcher', {})
    failure = watcher.get('failure', {})
    case_dir = data.get('case_dir', str(tickets_dir))
    return {
        '_source': deepcopy(data),
        'task_type': data.get('task_type', 'single'),
        'role': data.get('role', 'alone'),
        'macro_ticket': data.get('macro_ticket', ''),
        'execution_source': 'ticket' if data.get('resource_source') in ('ticket', 'macro') else 'case',
        'end_time': '' if data.get('end_time') is None else str(data['end_time']),
        'cases': deepcopy(data.get('cases', [])),
        'macro_cores': str(data.get('cores', '')),
        'macro_cpu_policy': data.get('cpu_policy', 'manual' if data.get('cpu_set') else 'auto'),
        'macro_cpu_set': data.get('cpu_set', ''),
        'macro_command': shlex.join(data.get('command', ['./Allrun'])),
        'macro_cross_socket': data.get('allow_cross_socket', False),
        'case_dir': case_dir,
        'name': data.get('name', Path(case_dir).name),
        'residual_pattern': data.get('residual_pattern', ''),
        'logs': '\n'.join(watcher.get('logs', [watcher.get('log', data.get('log', 'log.solver'))])),
        'failure_patterns': '\n'.join(failure.get('patterns', [])),
        'updated_files': '\n'.join(failure.get('updated_files', [])),
        'openfoam_defaults': failure.get('include_openfoam_defaults', True),
        'events': deepcopy(data.get('notifications', {}).get('events', list(EVENTS))),
        'exports': deepcopy(data.get('exports', [])),
        **{stage: '\n'.join(shlex.join(hook['command']) for hook in data.get(stage, []))
           for stage in DEFAULT_SCRIPTS},
    }


def numeric(text, label, *, integer=False, minimum=0):
    try:
        value = int(text) if integer else float(text)
        if not math.isfinite(value) or value < minimum:
            raise ValueError
        return value
    except (ValueError, OverflowError):
        ui = load_ui()
        kind = ui.text('scenarios.diagnostics.editor.integer' if integer
                       else 'scenarios.diagnostics.editor.number')
        raise ValueError(ui.text('scenarios.diagnostics.editor.numeric', label=label,
                                 minimum=minimum, kind=kind)) from None


def lines(text, *, strip=True):
    return [line.strip() if strip else line for line in text.splitlines() if line.strip()]


def form_document(values):
    """Save shared execution intent and monitoring fields for all interfaces."""
    case_dir = values['case_dir'].strip()
    if not case_dir:
        raise ValueError(load_ui().text('scenarios.diagnostics.editor.case_required'))
    logs = lines(values['logs'])
    if not logs:
        raise ValueError(load_ui().text('scenarios.diagnostics.editor.logs_required'))
    data = deepcopy(values.get('_source', TEMPLATE))
    data.update(version=1, case_dir=case_dir)
    data['task_type'] = values.get('task_type', 'single')
    data['role'] = values.get('role', 'alone') if data['task_type'] == 'single' else 'alone'
    end = values.get('end_time', '').strip()
    data['end_time'] = numeric(end, 'End Time / Iteration') if end else None
    if data['role'] == 'child':
        data['macro_ticket'] = values.get('macro_ticket', '').strip()
    else:
        data.pop('macro_ticket', None)
    if data['task_type'] == 'macro':
        data['cases'] = deepcopy(values.get('cases', []))
    else:
        data.pop('cases', None)
    execution_source = values.get('execution_source',
                                  'ticket' if data.get('resource_source') in ('ticket', 'macro') else 'case')
    if execution_source not in ('case', 'ticket'):
        raise ValueError(load_ui().text('scenarios.diagnostics.config.resource_source'))
    explicit = data['task_type'] == 'macro' or (data['role'] == 'alone' and execution_source == 'ticket')
    if explicit:
        data.update(resource_source='macro' if data['task_type'] == 'macro' else 'ticket',
                    cores=numeric(values.get('macro_cores', ''),
                                  load_ui().text('scenarios.diagnostics.editor.macro_cores' if data['task_type'] == 'macro'
                                                 else 'scenarios.diagnostics.editor.cores'),
                                  integer=True, minimum=1),
                    cpu_policy=values.get('macro_cpu_policy', data.get('cpu_policy', 'manual' if data.get('cpu_set') else 'auto')),
                    command=shlex.split(values.get('macro_command', './Allrun')),
                    allow_cross_socket=values.get('macro_cross_socket', False))
        if data['cpu_policy'] == 'auto':
            data.pop('cpu_set', None)
            data['allow_cross_socket'] = True
        else:
            data['cpu_set'] = values.get('macro_cpu_set', '').strip()
    elif data['role'] == 'alone' and data.get('resource_source') in ('ticket', 'macro'):
        data['resource_source'] = 'case'
        for key in ('cores', 'command', 'cpu_set'):
            data.pop(key, None)
        data.update(cpu_policy='auto', allow_cross_socket=True)
    data['residual_pattern'] = values['residual_pattern'].strip()
    data.pop('name', None)
    if values['name'].strip():
        data['name'] = values['name'].strip()
    # Case-derived execution and duration remain unchanged on metadata-only saves.
    data.pop('simulation', None)
    watcher = data.setdefault('watcher', {})
    watcher.pop('log', None)
    watcher['logs'] = logs
    progress = watcher.get('progress', {})
    progress.pop('start', None)
    progress.pop('end', None)
    if not progress:
        watcher.pop('progress', None)
    # Completion is derived from system/controlDict and the final logged Time.
    # Remove legacy success gates when a ticket is saved through the editor.
    watcher.pop('success', None)
    watcher.pop('incomplete_status', None)
    watcher.setdefault('failure', {}).update(
        patterns=lines(values['failure_patterns'], strip=False),
        updated_files=lines(values['updated_files']),
        include_openfoam_defaults=values['openfoam_defaults'])
    data['notifications'] = {'events': list(values['events'])}
    for stage in DEFAULT_SCRIPTS:
        if stage in values:
            data[stage] = script_commands(values[stage], data.get(stage, []))
    data['exports'] = deepcopy(values['exports'])
    # The editor defines data available on request, never automatic attachments.
    for item in data['exports']:
        item.update(on=[], on_complete=False)
    return data


def validate_document(text, tickets_dir):
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(load_ui().text('scenarios.diagnostics.editor.json_object'))
    raw_dir = data.get('case_dir')
    if not isinstance(raw_dir, str) or not raw_dir.strip():
        raise ValueError(load_ui().text('scenarios.diagnostics.editor.case_required'))
    case_dir = Path(raw_dir).expanduser()
    if not case_dir.is_absolute():
        case_dir = (Path(tickets_dir) / case_dir).resolve()
    if not case_dir.is_dir():
        raise ValueError(load_ui().text('scenarios.diagnostics.editor.case_missing', path=case_dir))
    if not data.get('name'):
        data['name'] = case_dir.resolve().name or 'case'
    tickets_dir = Path(tickets_dir)
    tickets_dir.mkdir(parents=True, exist_ok=True)
    # Validation drafts must never appear in the monitor's *.json ticket scan.
    temp = tempfile.NamedTemporaryFile('w', suffix='.tmp', dir=tickets_dir,
                                       encoding='utf-8', delete=False)
    try:
        with temp:
            json.dump(data, temp, ensure_ascii=False, indent=2)
            temp.write('\n')
        load_case(temp.name)
    finally:
        Path(temp.name).unlink(missing_ok=True)
    return data


class TicketService:
    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        self.folder.mkdir(parents=True, exist_ok=True)

    def path(self, name):
        if not isinstance(name, str) or Path(name).name != name or not name.endswith('.json'):
            raise ValueError(load_ui().text('scenarios.diagnostics.editor.ticket_filename'))
        path = self.folder / name
        if path.resolve().parent != self.folder:
            raise ValueError(load_ui().text('scenarios.diagnostics.editor.ticket_escape'))
        return path

    def listing(self):
        with ticket_lock(self.folder):
            return [p.name for p in sorted(self.folder.glob('*.json')) if not p.is_symlink()]

    def revision(self, name):
        data = read_json(self.path(name))
        data.pop('queue', None)
        for row in data.get('cases', []):
            for key in ('state', 'result', 'reason', 'job_id'):
                row.pop(key, None)
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def open(self, name):
        with ticket_lock(self.folder):
            path = self.path(name)
            load_case(path)
            data = read_json(path)
            return dict(values=form_values(data, self.folder), filename=name,
                        current=name, revision=self.revision(name), dirty=False)

    def new(self, kind='single'):
        if kind not in ('single', 'macro'):
            raise ValueError(load_ui().text('scenarios.diagnostics.editor.task_type'))
        data = deepcopy(TEMPLATE)
        data['task_type'] = kind
        return dict(values=form_values(data, self.folder), filename='',
                    current=None, revision=None, dirty=False)

    def duplicate(self, name):
        with ticket_lock(self.folder):
            path = self.path(name)
            case = load_case(path)
            data = clone_document(read_json(path), case['_root'], load_ui().text(
                'scenarios.diagnostics.editor.copy_suffix', name=case['name']))
            if case['task_type'] == 'macro':
                data.update(task_type='macro', cases=[])
            stem = re.sub(r'[^A-Za-z0-9._-]+', '-', path.stem).strip('.-') or 'case'
            prefix = 'macro' if data['task_type'] == 'macro' else 'alone'
            filename = ticket_name(f'{stem}-copy.json', prefix)
            number = 2
            while self.path(filename).exists():
                filename = ticket_name(f'{stem}-copy-{number}.json', prefix)
                number += 1
            return dict(values=form_values(data, self.folder), filename=filename,
                        current=None, revision=None, dirty=True)

    def filename(self, values, name=''):
        prefix = 'macro' if values['task_type'] == 'macro' else values['role']
        name = name.strip() or Path(values['case_dir'].strip()).name + '.json'
        result = ticket_name(name, prefix)
        if not re.fullmatch(r'[A-Za-z0-9._-]+\.json', result):
            raise ValueError(load_ui().text('scenarios.diagnostics.editor.filename_chars'))
        return result

    def validate(self, values, name='', current=None):
        data = validate_document(json.dumps(form_document(values)), self.folder)
        if data['task_type'] == 'macro' and not data['cases']:
            raise ValueError(load_ui().text('scenarios.diagnostics.editor.macro_cases'))
        destination = self.path(self.filename(values, name))
        original = self.path(current) if current else None
        root = (self.folder / Path(data['case_dir']).expanduser()).resolve()
        for path in self.folder.glob('*.json'):
            if path in (original, destination):
                continue
            try:
                other = load_case(path)
            except (ValueError, OSError):
                continue
            if other['task_type'] == data['task_type'] and Path(other['_root']) == root:
                raise ValueError(load_ui().text('scenarios.diagnostics.editor.duplicate_case',
                                                ticket=path.name))
        return data

    def save(self, values, name='', current=None, *, submit=False, overwrite=False,
             expected_revision=None, request_id=None):
        name = self.filename(values, name)
        destination = self.path(name)
        original = self.path(current) if current else None
        with ticket_lock(self.folder):
            if request_id and destination.exists():
                saved = read_json(destination)
                if saved.get('queue', {}).get('request_id') == request_id:
                    return name, saved
            if current and expected_revision and self.revision(current) != expected_revision:
                raise ValueError(load_ui().text('scenarios.diagnostics.editor.changed'))
            data = self.validate(values, name, current)
            if destination != original and destination.exists() and not overwrite:
                raise FileExistsError(load_ui().text('scenarios.diagnostics.editor.exists', name=name))
            if data['task_type'] == 'macro':
                return name, publish_macro(destination, data, original,
                                           request_id=request_id, locked=True)
            if original and original.exists():
                saved = read_json(original)
                data['queue'] = saved.get('queue', {})
                if data['queue'].get('state') == 'running' and any(
                        data.get(stage, []) != saved.get(stage, []) for stage in DEFAULT_SCRIPTS):
                    raise ValueError(load_ui().text('scenarios.diagnostics.editor.running_scripts'))
                from .execution import execution_settings
                if data['queue'].get('state') == 'running' and execution_settings(data) != execution_settings(saved):
                    raise ValueError(load_ui().text('scenarios.diagnostics.editor.running_execution'))
            if submit:
                data['queue'] = dict(state='waiting', submit=True, request_id=request_id or uuid.uuid4().hex)
            else:
                data.setdefault('queue', {}).setdefault('state', 'waiting')
            parent_path, parent = None, None
            if data['role'] == 'child':
                parent_path = self.path((self.folder / data['macro_ticket']).resolve().name)
                parent = read_json(parent_path)
                if parent.get('task_type') != 'macro':
                    raise ValueError(load_ui().text('scenarios.diagnostics.editor.macro_pointer'))
                from .execution import execution_settings
                if parent.get('resource_source') == 'macro' and execution_settings(data) != execution_settings(parent):
                    raise ValueError(load_ui().text('scenarios.diagnostics.editor.child_execution'))
                row = next((r for r in parent['cases'] if r['ticket'] == current), None)
                if row is None:
                    raise ValueError(load_ui().text('scenarios.diagnostics.editor.child_from_macro'))
                if (self.folder / Path(data['case_dir']).expanduser()).resolve() != Path(row['case_dir']).resolve():
                    raise ValueError(load_ui().text('scenarios.diagnostics.editor.child_path'))
                row['ticket'] = name
            atomic_json(destination, data)
            if parent_path:
                atomic_json(parent_path, parent)
            if original and original != destination:
                original.unlink(missing_ok=True)
        return name, data

    def delete(self, name, expected_revision=None):
        return self.delete_many([name], {name: expected_revision} if expected_revision else None)

    def _deletion_plan(self, names, expected_revisions=None):
        """Caller holds the ticket lock; expand macros before checking any file."""
        documents = {name: read_json(self.path(name)) for name in dict.fromkeys(names)}
        if not documents:
            raise ValueError(load_ui().text('scenarios.diagnostics.editor.delete_selection'))
        for name, data in list(documents.items()):
            if data.get('task_type') != 'macro':
                continue
            if any(row.get('state') != 'finished' for row in data['cases']):
                raise ValueError(load_ui().text('scenarios.diagnostics.editor.delete_busy_macro', name=name))
            for row in data['cases']:
                child = self.path(row['ticket'])
                if not child.exists():
                    continue
                child_data = read_json(child)
                parent = (self.folder / child_data.get('macro_ticket', '')).resolve()
                if child_data.get('role') != 'child' or parent != self.path(name):
                    raise ValueError(load_ui().text('scenarios.diagnostics.editor.child_membership_changed',
                                                    name=child.name))
                documents[child.name] = child_data
        for name, data in documents.items():
            revision = (expected_revisions or {}).get(name)
            if revision and self.revision(name) != revision:
                raise ValueError(load_ui().text('scenarios.diagnostics.editor.delete_changed', name=name))
            queue = data.get('queue', {})
            if queue.get('submit') or queue.get('state') == 'running' or (
                    queue.get('job_id') and queue.get('state') == 'waiting'):
                raise ValueError(load_ui().text('scenarios.diagnostics.editor.delete_queued', name=name))
            if data.get('role') == 'child':
                parent = (self.folder / data['macro_ticket']).resolve()
                if parent.parent != self.folder or documents.get(parent.name, {}).get('task_type') != 'macro':
                    raise ValueError(load_ui().text('scenarios.diagnostics.editor.delete_child', name=name))
                if not any(row['ticket'] == name for row in documents[parent.name]['cases']):
                    raise ValueError(load_ui().text('scenarios.diagnostics.editor.macro_mismatch', name=name))
        return documents

    def deletion_preview(self, names, expected_revisions=None):
        with ticket_lock(self.folder):
            documents = self._deletion_plan(names, expected_revisions)
            return dict(names=list(documents), revisions={name: self.revision(name) for name in documents})

    def delete_many(self, names, expected_revisions=None):
        with ticket_lock(self.folder):
            documents = self._deletion_plan(names, expected_revisions)
            backups = {self.path(name): self.path(name).read_bytes() for name in documents}
            removed = []
            try:
                for path in backups:
                    path.unlink()
                    removed.append(path)
            except OSError:
                for path in removed:
                    path.write_bytes(backups[path])
                raise
            return list(documents)


def validate_export(item, others, root, index=None):
    from .config import inside
    result = deepcopy(item)
    name = result.get('name', '').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', name):
        raise ValueError(load_ui().text('scenarios.diagnostics.editor.export_name'))
    if any(other['name'] == name for i, other in enumerate(others) if i != index):
        raise ValueError(load_ui().text('scenarios.diagnostics.editor.export_duplicate'))
    result['name'] = name
    result['pattern'] = result.get('pattern', '').strip()
    inside(root, result['pattern'])
    if result.setdefault('kind', 'document') not in ('photo', 'document'):
        raise ValueError(load_ui().text('scenarios.diagnostics.editor.export_kind'))
    result['max_files'] = numeric(str(result.get('max_files', 1)),
                                  load_ui().text('scenarios.diagnostics.editor.max_files'),
                                  integer=True, minimum=1)
    if result['max_files'] > 10:
        raise ValueError(load_ui().text('scenarios.diagnostics.editor.max_files_limit'))
    result.update(on=[], on_complete=False)
    return result
