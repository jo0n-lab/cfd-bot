"""Ticket editing operations shared by native and Telegram interfaces."""
from . import diagnostics as _diagnostics
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
from .queueing import queue_profile_conflict
from .ui import load_ui

EVENTS = {event: load_ui().text('menus.tickets.basic.event_' + event)
          for event in ('started', 'succeeded', 'failed', 'interrupted')}
DEFAULT_CASE_ROOT = Path.home() / 'OpenFOAM' / (Path.home().name + '-dev') / 'run'
DEFAULT_SCRIPTS = {'preprocess': './Allclean', 'postprocess': './Allpost'}
DEFAULT_MONITOR_SCRIPT = './Allmonitor'
TEMPLATE = {'version': 1, 'case_dir': '', 'name': '', 'cpu_policy': 'auto',
            'allow_cross_socket': True, 'watcher': {'log': 'log.solver'},
            **{stage: [{'command': [command]}] for stage, command in DEFAULT_SCRIPTS.items()}}


@_diagnostics.trace
def case_browser_start(directory, tickets_dir):
    if directory.strip():
        if _diagnostics.detailed: _diagnostics.step('editor.case_browser_start:L28:then')
        current = (Path(tickets_dir) / Path(directory.strip()).expanduser()).resolve()
        if current.is_dir():
            if _diagnostics.detailed: _diagnostics.step('editor.case_browser_start:L30:then')
            return current
    for folder in (DEFAULT_CASE_ROOT, *DEFAULT_CASE_ROOT.parents):
        if _diagnostics.detailed: _diagnostics.step('editor.case_browser_start:L32:loop', folder=folder)
        if folder.is_dir():
            if _diagnostics.detailed: _diagnostics.step('editor.case_browser_start:L33:then')
            return folder


@_diagnostics.trace
def script_commands(text, previous=()):
    """One argv command per line; preserve configured timeouts when editing."""
    result = []
    for index, line in enumerate(lines(text)):
        if _diagnostics.detailed: _diagnostics.step('editor.script_commands:L40:loop', index=index, line=line)
        command = shlex.split(line)
        if not command:
            if _diagnostics.detailed: _diagnostics.step('editor.script_commands:L42:then')
            raise ValueError(load_ui().text('scenarios.diagnostics.editor.script_command'))
        hook = deepcopy(previous[index]) if index < len(previous) else {}
        hook['command'] = command
        result.append(hook)
    return result


@_diagnostics.trace
def form_values(data, tickets_dir):
    """Expose shared ticket settings, retaining legacy case-based execution."""
    watcher = data.get('watcher', {})
    failure = watcher.get('failure', {})
    discovery = data.get('discovery', {})
    case_dir = data.get('case_dir', str(tickets_dir))
    return {
        '_source': deepcopy(data),
        'task_type': data.get('task_type', 'single'),
        'role': data.get('role', 'alone'),
        'macro_ticket': data.get('macro_ticket', ''),
        'execution_source': 'ticket' if data.get('resource_source') in ('ticket', 'macro') else 'case',
        'end_time': '' if data.get('end_time') is None else str(data['end_time']),
        'cases': deepcopy(data.get('cases', [])),
        'case_include_patterns': '\n'.join(discovery.get('include_patterns', [])),
        'case_exclude_patterns': '\n'.join(discovery.get('exclude_patterns', [])),
        'macro_cores': str(data.get('cores', '')),
        'macro_cpu_policy': data.get('cpu_policy', 'manual' if data.get('cpu_set') else 'auto'),
        'macro_cpu_set': data.get('cpu_set', ''),
        'macro_command': shlex.join(data.get('command', ['./Allrun'])),
        'macro_cross_socket': data.get('allow_cross_socket', False),
        'queue_id': data.get('execution_queue', {}).get('id', ''),
        'dynamic_cores': data.get('dynamic_cores', False),
        'monitoring_cpu': data.get('monitoring', {}).get('allocate_cpu', False),
        'monitoring_command': shlex.join(data.get('monitoring', {}).get(
            'command', [DEFAULT_MONITOR_SCRIPT])),
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


@_diagnostics.trace
def numeric(text, label, *, integer=False, minimum=0):
    try:
        value = int(text) if integer else float(text)
        if not math.isfinite(value) or value < minimum:
            if _diagnostics.detailed: _diagnostics.step('editor.numeric:L93:then')
            raise ValueError
        return value
    except (ValueError, OverflowError):
        if _diagnostics.enabled: _diagnostics.step('editor.numeric:L96:except')
        ui = load_ui()
        kind = ui.text('scenarios.diagnostics.editor.integer' if integer
                       else 'scenarios.diagnostics.editor.number')
        raise ValueError(ui.text('scenarios.diagnostics.editor.numeric', label=label,
                                 minimum=minimum, kind=kind)) from None


@_diagnostics.trace
def lines(text, *, strip=True):
    return [line.strip() if strip else line for line in text.splitlines() if line.strip()]


@_diagnostics.trace
def form_document(values):
    """Save shared execution intent and monitoring fields for all interfaces."""
    case_dir = values['case_dir'].strip()
    if not case_dir:
        if _diagnostics.detailed: _diagnostics.step('editor.form_document:L111:then')
        raise ValueError(load_ui().text('scenarios.diagnostics.editor.case_required'))
    logs = lines(values['logs'])
    if not logs:
        if _diagnostics.detailed: _diagnostics.step('editor.form_document:L114:then')
        raise ValueError(load_ui().text('scenarios.diagnostics.editor.logs_required'))
    data = deepcopy(values.get('_source', TEMPLATE))
    data.update(version=1, case_dir=case_dir)
    data['task_type'] = values.get('task_type', 'single')
    data['role'] = values.get('role', 'alone') if data['task_type'] == 'single' else 'alone'
    end = values.get('end_time', '').strip()
    data['end_time'] = numeric(end, 'End Time / Iteration') if end else None
    if data['role'] == 'child':
        if _diagnostics.detailed: _diagnostics.step('editor.form_document:L122:then')
        data['macro_ticket'] = values.get('macro_ticket', '').strip()
    else:
        if _diagnostics.detailed: _diagnostics.step('editor.form_document:L122:else')
        data.pop('macro_ticket', None)
    if data['task_type'] == 'macro':
        if _diagnostics.detailed: _diagnostics.step('editor.form_document:L126:then')
        data['cases'] = deepcopy(values.get('cases', []))
        include = lines(values.get('case_include_patterns', ''))
        exclude = lines(values.get('case_exclude_patterns', ''))
        if include or exclude:
            if _diagnostics.detailed: _diagnostics.step('editor.form_document:L130:then')
            data['discovery'] = {
                'include_patterns': include,
                'exclude_patterns': exclude,
            }
        else:
            if _diagnostics.detailed: _diagnostics.step('editor.form_document:L130:else')
            data.pop('discovery', None)
    else:
        if _diagnostics.detailed: _diagnostics.step('editor.form_document:L126:else')
        data.pop('cases', None)
        data.pop('discovery', None)
    queue_id = values.get('queue_id', '').strip()
    if queue_id:
        if _diagnostics.detailed: _diagnostics.step('editor.form_document:L141:then')
        data['execution_queue'] = {'id': queue_id}
    else:
        if _diagnostics.detailed: _diagnostics.step('editor.form_document:L141:else')
        data.pop('execution_queue', None)
    data['dynamic_cores'] = bool(values.get('dynamic_cores', False)) if data['task_type'] == 'macro' else False
    execution_source = values.get('execution_source',
                                  'ticket' if data.get('resource_source') in ('ticket', 'macro') else 'case')
    if execution_source not in ('case', 'ticket'):
        if _diagnostics.detailed: _diagnostics.step('editor.form_document:L148:then')
        raise ValueError(load_ui().text('scenarios.diagnostics.config.resource_source'))
    explicit = data['task_type'] == 'macro' or (data['role'] == 'alone' and execution_source == 'ticket')
    if explicit:
        if _diagnostics.detailed: _diagnostics.step('editor.form_document:L151:then')
        data.update(resource_source='macro' if data['task_type'] == 'macro' else 'ticket',
                    cores=numeric(values.get('macro_cores', ''),
                                  load_ui().text('scenarios.diagnostics.editor.macro_cores' if data['task_type'] == 'macro'
                                                 else 'scenarios.diagnostics.editor.cores'),
                                  integer=True, minimum=1),
                    cpu_policy=values.get('macro_cpu_policy', data.get('cpu_policy', 'manual' if data.get('cpu_set') else 'auto')),
                    command=shlex.split(values.get('macro_command', './Allrun')),
                    allow_cross_socket=values.get('macro_cross_socket', False))
        if data['cpu_policy'] == 'auto':
            if _diagnostics.detailed: _diagnostics.step('editor.form_document:L160:then')
            data.pop('cpu_set', None)
            data['allow_cross_socket'] = True
        else:
            if _diagnostics.detailed: _diagnostics.step('editor.form_document:L160:else')
            data['cpu_set'] = values.get('macro_cpu_set', '').strip()
    elif data['role'] == 'alone' and data.get('resource_source') in ('ticket', 'macro'):
        if _diagnostics.detailed: _diagnostics.step('editor.form_document:L165:then')
        data['resource_source'] = 'case'
        for key in ('cores', 'command', 'cpu_set'):
            if _diagnostics.detailed: _diagnostics.step('editor.form_document:L167:loop', key=key)
            data.pop(key, None)
        data.update(cpu_policy='auto', allow_cross_socket=True)
    if data.get('dynamic_cores'):
        if _diagnostics.detailed: _diagnostics.step('editor.form_document:L170:then')
        default_cores = numeric(values.get('macro_cores', ''),
                                load_ui().text('scenarios.diagnostics.editor.macro_cores'),
                                integer=True, minimum=1)
        for row in data['cases']:
            if _diagnostics.detailed: _diagnostics.step('editor.form_document:L174:loop', row=row)
            row['cores'] = numeric(row.get('cores', default_cores),
                                   load_ui().text('scenarios.diagnostics.editor.member_cores'),
                                   integer=True, minimum=1)
    elif data['task_type'] == 'macro':
        if _diagnostics.detailed: _diagnostics.step('editor.form_document:L178:then')
        for row in data['cases']:
            if _diagnostics.detailed: _diagnostics.step('editor.form_document:L179:loop', row=row)
            row.pop('cores', None)
    if values.get('monitoring_cpu', False):
        if _diagnostics.detailed: _diagnostics.step('editor.form_document:L181:then')
        command = shlex.split(values.get('monitoring_command', DEFAULT_MONITOR_SCRIPT))
        if not command:
            if _diagnostics.detailed: _diagnostics.step('editor.form_document:L183:then')
            raise ValueError(load_ui().text('scenarios.diagnostics.editor.monitor_command'))
        data['monitoring'] = {'allocate_cpu': True, 'command': command}
    else:
        if _diagnostics.detailed: _diagnostics.step('editor.form_document:L181:else')
        data.pop('monitoring', None)
    data['residual_pattern'] = values['residual_pattern'].strip()
    data.pop('name', None)
    if values['name'].strip():
        if _diagnostics.detailed: _diagnostics.step('editor.form_document:L190:then')
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
        if _diagnostics.detailed: _diagnostics.step('editor.form_document:L200:then')
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
        if _diagnostics.detailed: _diagnostics.step('editor.form_document:L211:loop', stage=stage)
        if stage in values:
            if _diagnostics.detailed: _diagnostics.step('editor.form_document:L212:then')
            data[stage] = script_commands(values[stage], data.get(stage, []))
    data['exports'] = deepcopy(values['exports'])
    # The editor defines data available on request, never automatic attachments.
    for item in data['exports']:
        if _diagnostics.detailed: _diagnostics.step('editor.form_document:L216:loop', item=item)
        item.update(on=[], on_complete=False)
    return data


@_diagnostics.trace
def validate_document(text, tickets_dir):
    data = json.loads(text)
    if not isinstance(data, dict):
        if _diagnostics.detailed: _diagnostics.step('editor.validate_document:L223:then')
        raise ValueError(load_ui().text('scenarios.diagnostics.editor.json_object'))
    raw_dir = data.get('case_dir')
    if not isinstance(raw_dir, str) or not raw_dir.strip():
        if _diagnostics.detailed: _diagnostics.step('editor.validate_document:L226:then')
        raise ValueError(load_ui().text('scenarios.diagnostics.editor.case_required'))
    case_dir = Path(raw_dir).expanduser()
    if not case_dir.is_absolute():
        if _diagnostics.detailed: _diagnostics.step('editor.validate_document:L229:then')
        case_dir = (Path(tickets_dir) / case_dir).resolve()
    if not case_dir.is_dir():
        if _diagnostics.detailed: _diagnostics.step('editor.validate_document:L231:then')
        raise ValueError(load_ui().text('scenarios.diagnostics.editor.case_missing', path=case_dir))
    if not data.get('name'):
        if _diagnostics.detailed: _diagnostics.step('editor.validate_document:L233:then')
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
    @_diagnostics.trace
    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        self.folder.mkdir(parents=True, exist_ok=True)
        self._save_requests = {}

    @_diagnostics.trace
    def _remember_save(self, request_id, name, data):
        if not request_id:
            if _diagnostics.detailed: _diagnostics.step('editor.TicketService._remember_save:L257:then')
            return
        self._save_requests[request_id] = (name, deepcopy(data))
        while len(self._save_requests) > 256:
            if _diagnostics.detailed: _diagnostics.step('editor.TicketService._remember_save:L260:loop')
            self._save_requests.pop(next(iter(self._save_requests)))

    @_diagnostics.trace
    def path(self, name):
        if not isinstance(name, str) or Path(name).name != name or not name.endswith('.json'):
            if _diagnostics.detailed: _diagnostics.step('editor.TicketService.path:L264:then')
            raise ValueError(load_ui().text('scenarios.diagnostics.editor.ticket_filename'))
        path = self.folder / name
        if path.resolve().parent != self.folder:
            if _diagnostics.detailed: _diagnostics.step('editor.TicketService.path:L267:then')
            raise ValueError(load_ui().text('scenarios.diagnostics.editor.ticket_escape'))
        return path

    @_diagnostics.trace
    def listing(self):
        with ticket_lock(self.folder):
            return [p.name for p in sorted(self.folder.glob('*.json')) if not p.is_symlink()]

    @_diagnostics.trace
    def revision(self, name, *, data=None):
        data = read_json(self.path(name)) if data is None else deepcopy(data)
        data.pop('queue', None)
        for row in data.get('cases', []):
            if _diagnostics.detailed: _diagnostics.step('editor.TicketService.revision:L278:loop', row=row)
            for key in ('state', 'result', 'reason', 'job_id'):
                if _diagnostics.detailed: _diagnostics.step('editor.TicketService.revision:L279:loop', key=key)
                row.pop(key, None)
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    @_diagnostics.trace
    def open(self, name):
        with ticket_lock(self.folder):
            path = self.path(name)
            load_case(path)
            data = read_json(path)
            return dict(values=form_values(data, self.folder), filename=name,
                        current=name, revision=self.revision(name), dirty=False)

    @_diagnostics.trace
    def new(self, kind='single'):
        if kind not in ('single', 'macro'):
            if _diagnostics.detailed: _diagnostics.step('editor.TicketService.new:L292:then')
            raise ValueError(load_ui().text('scenarios.diagnostics.editor.task_type'))
        data = deepcopy(TEMPLATE)
        data['task_type'] = kind
        return dict(values=form_values(data, self.folder), filename='',
                    current=None, revision=None, dirty=False)

    @_diagnostics.trace
    def duplicate(self, name):
        with ticket_lock(self.folder):
            path = self.path(name)
            case = load_case(path)
            data = clone_document(read_json(path), case['_root'], load_ui().text(
                'scenarios.diagnostics.editor.copy_suffix', name=case['name']))
            if case['task_type'] == 'macro':
                if _diagnostics.detailed: _diagnostics.step('editor.TicketService.duplicate:L305:then')
                data.update(task_type='macro', cases=[])
            stem = re.sub(r'[^A-Za-z0-9._-]+', '-', path.stem).strip('.-') or 'case'
            prefix = 'macro' if data['task_type'] == 'macro' else 'alone'
            filename = ticket_name(f'{stem}-copy.json', prefix)
            number = 2
            while self.path(filename).exists():
                if _diagnostics.detailed: _diagnostics.step('editor.TicketService.duplicate:L311:loop')
                filename = ticket_name(f'{stem}-copy-{number}.json', prefix)
                number += 1
            return dict(values=form_values(data, self.folder), filename=filename,
                        current=None, revision=None, dirty=True)

    @_diagnostics.trace
    def filename(self, values, name=''):
        prefix = 'macro' if values['task_type'] == 'macro' else values['role']
        name = name.strip() or Path(values['case_dir'].strip()).name + '.json'
        result = ticket_name(name, prefix)
        if not re.fullmatch(r'[A-Za-z0-9._-]+\.json', result):
            if _diagnostics.detailed: _diagnostics.step('editor.TicketService.filename:L321:then')
            raise ValueError(load_ui().text('scenarios.diagnostics.editor.filename_chars'))
        return result

    @_diagnostics.trace
    def validate(self, values, name='', current=None):
        data = validate_document(json.dumps(form_document(values)), self.folder)
        if data['task_type'] == 'macro' and not data['cases']:
            if _diagnostics.detailed: _diagnostics.step('editor.TicketService.validate:L327:then')
            raise ValueError(load_ui().text('scenarios.diagnostics.editor.macro_cases'))
        destination = self.path(self.filename(values, name))
        original = self.path(current) if current else None
        root = (self.folder / Path(data['case_dir']).expanduser()).resolve()
        from .catalog import folder_index
        for other in folder_index(self.folder).tickets():
            if _diagnostics.detailed: _diagnostics.step('editor.TicketService.validate:L333:loop', other=other)
            path = Path(other['_config'])
            if path in (original, destination):
                if _diagnostics.detailed: _diagnostics.step('editor.TicketService.validate:L335:then')
                continue
            if other['task_type'] == data['task_type'] and Path(other['_root']) == root:
                if _diagnostics.detailed: _diagnostics.step('editor.TicketService.validate:L337:then')
                raise ValueError(load_ui().text('scenarios.diagnostics.editor.duplicate_case',
                                                ticket=path.name))
        others = [other for other in folder_index(self.folder).tickets()
                  if Path(other['_config']) not in (original, destination)]
        conflict = queue_profile_conflict(data, others)
        if conflict:
            if _diagnostics.detailed: _diagnostics.step('editor.TicketService.validate:L343:then')
            kind, queue_id, detail = conflict
            raise ValueError(load_ui().text('scenarios.diagnostics.editor.queue_' + kind,
                                            queue=queue_id, detail=detail))
        return data

    @_diagnostics.trace
    def save(self, values, name='', current=None, *, submit=False, overwrite=False,
             expected_revision=None, request_id=None):
        name = self.filename(values, name)
        destination = self.path(name)
        original = self.path(current) if current else None
        with ticket_lock(self.folder):
            if request_id in self._save_requests:
                if _diagnostics.detailed: _diagnostics.step('editor.TicketService.save:L355:then')
                saved_name, saved = self._save_requests[request_id]
                return saved_name, deepcopy(saved)
            if request_id and destination.exists():
                if _diagnostics.detailed: _diagnostics.step('editor.TicketService.save:L358:then')
                saved = read_json(destination)
                if saved.get('queue', {}).get('request_id') == request_id:
                    if _diagnostics.detailed: _diagnostics.step('editor.TicketService.save:L360:then')
                    self._remember_save(request_id, name, saved)
                    return name, saved
            if current and expected_revision and self.revision(current) != expected_revision:
                if _diagnostics.detailed: _diagnostics.step('editor.TicketService.save:L363:then')
                raise ValueError(load_ui().text('scenarios.diagnostics.editor.changed'))
            data = self.validate(values, name, current)
            if destination != original and destination.exists() and not overwrite:
                if _diagnostics.detailed: _diagnostics.step('editor.TicketService.save:L366:then')
                raise FileExistsError(load_ui().text('scenarios.diagnostics.editor.exists', name=name))
            if data['task_type'] == 'macro':
                if _diagnostics.detailed: _diagnostics.step('editor.TicketService.save:L368:then')
                saved = publish_macro(destination, data, original,
                                      request_id=request_id, submit=submit, locked=True)
                self._remember_save(request_id, name, saved)
                return name, saved
            if original and original.exists():
                if _diagnostics.detailed: _diagnostics.step('editor.TicketService.save:L373:then')
                saved = read_json(original)
                data['queue'] = saved.get('queue', {})
                if data['queue'].get('state') == 'running' and any(
                        data.get(stage, []) != saved.get(stage, []) for stage in DEFAULT_SCRIPTS):
                    if _diagnostics.detailed: _diagnostics.step('editor.TicketService.save:L376:then')
                    raise ValueError(load_ui().text('scenarios.diagnostics.editor.running_scripts'))
                from .execution import execution_settings
                if data['queue'].get('state') == 'running' and execution_settings(data) != execution_settings(saved):
                    if _diagnostics.detailed: _diagnostics.step('editor.TicketService.save:L380:then')
                    raise ValueError(load_ui().text('scenarios.diagnostics.editor.running_execution'))
            if submit:
                if _diagnostics.detailed: _diagnostics.step('editor.TicketService.save:L382:then')
                data['queue'] = dict(state='waiting', submit=True,
                                     request_id=request_id or uuid.uuid4().hex)
            else:
                if _diagnostics.detailed: _diagnostics.step('editor.TicketService.save:L382:else')
                data.setdefault('queue', {}).setdefault('state', 'waiting')
            parent_path, parent = None, None
            if data['role'] == 'child':
                if _diagnostics.detailed: _diagnostics.step('editor.TicketService.save:L388:then')
                parent_path = self.path((self.folder / data['macro_ticket']).resolve().name)
                parent = read_json(parent_path)
                if parent.get('task_type') != 'macro':
                    if _diagnostics.detailed: _diagnostics.step('editor.TicketService.save:L391:then')
                    raise ValueError(load_ui().text('scenarios.diagnostics.editor.macro_pointer'))
                from .execution import execution_settings
                if parent.get('resource_source') == 'macro' and execution_settings(data) != execution_settings(parent):
                    if _diagnostics.detailed: _diagnostics.step('editor.TicketService.save:L394:then')
                    raise ValueError(load_ui().text('scenarios.diagnostics.editor.child_execution'))
                row = next((r for r in parent['cases'] if r['ticket'] == current), None)
                if row is None:
                    if _diagnostics.detailed: _diagnostics.step('editor.TicketService.save:L397:then')
                    raise ValueError(load_ui().text('scenarios.diagnostics.editor.child_from_macro'))
                if (self.folder / Path(data['case_dir']).expanduser()).resolve() != Path(row['case_dir']).resolve():
                    if _diagnostics.detailed: _diagnostics.step('editor.TicketService.save:L399:then')
                    raise ValueError(load_ui().text('scenarios.diagnostics.editor.child_path'))
                row['ticket'] = name
            atomic_json(destination, data)
            if parent_path:
                if _diagnostics.detailed: _diagnostics.step('editor.TicketService.save:L403:then')
                atomic_json(parent_path, parent)
            if original and original != destination:
                if _diagnostics.detailed: _diagnostics.step('editor.TicketService.save:L405:then')
                original.unlink(missing_ok=True)
            self._remember_save(request_id, name, data)
        return name, data

    @_diagnostics.trace
    def delete(self, name, expected_revision=None):
        return self.delete_many([name], {name: expected_revision} if expected_revision else None)

    @_diagnostics.trace
    def _deletion_plan(self, names, expected_revisions=None):
        """Caller holds the ticket lock; expand macros before checking any file."""
        documents = {name: read_json(self.path(name)) for name in dict.fromkeys(names)}
        if not documents:
            if _diagnostics.detailed: _diagnostics.step('editor.TicketService._deletion_plan:L416:then')
            raise ValueError(load_ui().text('scenarios.diagnostics.editor.delete_selection'))
        for name, data in list(documents.items()):
            if _diagnostics.detailed: _diagnostics.step('editor.TicketService._deletion_plan:L418:loop', name=name, data=data)
            if data.get('task_type') != 'macro':
                if _diagnostics.detailed: _diagnostics.step('editor.TicketService._deletion_plan:L419:then')
                continue
            if any(row.get('state') != 'finished' for row in data['cases']):
                if _diagnostics.detailed: _diagnostics.step('editor.TicketService._deletion_plan:L421:then')
                raise ValueError(load_ui().text('scenarios.diagnostics.editor.delete_busy_macro', name=name))
            for row in data['cases']:
                if _diagnostics.detailed: _diagnostics.step('editor.TicketService._deletion_plan:L423:loop', row=row)
                child = self.path(row['ticket'])
                if not child.exists():
                    if _diagnostics.detailed: _diagnostics.step('editor.TicketService._deletion_plan:L425:then')
                    continue
                child_data = read_json(child)
                parent = (self.folder / child_data.get('macro_ticket', '')).resolve()
                if child_data.get('role') != 'child' or parent != self.path(name):
                    if _diagnostics.detailed: _diagnostics.step('editor.TicketService._deletion_plan:L429:then')
                    raise ValueError(load_ui().text('scenarios.diagnostics.editor.child_membership_changed',
                                                    name=child.name))
                documents[child.name] = child_data
        for name, data in documents.items():
            if _diagnostics.detailed: _diagnostics.step('editor.TicketService._deletion_plan:L433:loop', name=name, data=data)
            revision = (expected_revisions or {}).get(name)
            if revision and self.revision(name) != revision:
                if _diagnostics.detailed: _diagnostics.step('editor.TicketService._deletion_plan:L435:then')
                raise ValueError(load_ui().text('scenarios.diagnostics.editor.delete_changed', name=name))
            queue = data.get('queue', {})
            if queue.get('submit') or queue.get('state') == 'running' or (
                    queue.get('job_id') and queue.get('state') == 'waiting'):
                if _diagnostics.detailed: _diagnostics.step('editor.TicketService._deletion_plan:L438:then')
                raise ValueError(load_ui().text('scenarios.diagnostics.editor.delete_queued', name=name))
            if data.get('role') == 'child':
                if _diagnostics.detailed: _diagnostics.step('editor.TicketService._deletion_plan:L441:then')
                parent = (self.folder / data['macro_ticket']).resolve()
                if parent.parent != self.folder or documents.get(parent.name, {}).get('task_type') != 'macro':
                    if _diagnostics.detailed: _diagnostics.step('editor.TicketService._deletion_plan:L443:then')
                    raise ValueError(load_ui().text('scenarios.diagnostics.editor.delete_child', name=name))
                if not any(row['ticket'] == name for row in documents[parent.name]['cases']):
                    if _diagnostics.detailed: _diagnostics.step('editor.TicketService._deletion_plan:L445:then')
                    raise ValueError(load_ui().text('scenarios.diagnostics.editor.macro_mismatch', name=name))
        return documents

    @_diagnostics.trace
    def deletion_preview(self, names, expected_revisions=None):
        with ticket_lock(self.folder):
            documents = self._deletion_plan(names, expected_revisions)
            return dict(names=list(documents), revisions={name: self.revision(name) for name in documents})

    @_diagnostics.trace
    def delete_many(self, names, expected_revisions=None):
        with ticket_lock(self.folder):
            documents = self._deletion_plan(names, expected_revisions)
            backups = {self.path(name): self.path(name).read_bytes() for name in documents}
            removed = []
            try:
                for path in backups:
                    if _diagnostics.detailed: _diagnostics.step('editor.TicketService.delete_many:L460:loop', path=path)
                    path.unlink()
                    removed.append(path)
            except OSError:
                if _diagnostics.enabled: _diagnostics.step('editor.TicketService.delete_many:L463:except')
                for path in removed:
                    if _diagnostics.detailed: _diagnostics.step('editor.TicketService.delete_many:L464:loop', path=path)
                    path.write_bytes(backups[path])
                raise
            return list(documents)


@_diagnostics.trace
def validate_export(item, others, root, index=None):
    from .config import inside
    result = deepcopy(item)
    name = result.get('name', '').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', name):
        if _diagnostics.detailed: _diagnostics.step('editor.validate_export:L474:then')
        raise ValueError(load_ui().text('scenarios.diagnostics.editor.export_name'))
    if any(other['name'] == name for i, other in enumerate(others) if i != index):
        if _diagnostics.detailed: _diagnostics.step('editor.validate_export:L476:then')
        raise ValueError(load_ui().text('scenarios.diagnostics.editor.export_duplicate'))
    result['name'] = name
    result['pattern'] = result.get('pattern', '').strip()
    inside(root, result['pattern'])
    if result.setdefault('kind', 'document') not in ('photo', 'document'):
        if _diagnostics.detailed: _diagnostics.step('editor.validate_export:L481:then')
        raise ValueError(load_ui().text('scenarios.diagnostics.editor.export_kind'))
    result['max_files'] = numeric(str(result.get('max_files', 1)),
                                  load_ui().text('scenarios.diagnostics.editor.max_files'),
                                  integer=True, minimum=1)
    if result['max_files'] > 10:
        if _diagnostics.detailed: _diagnostics.step('editor.validate_export:L486:then')
        raise ValueError(load_ui().text('scenarios.diagnostics.editor.max_files_limit'))
    result.update(on=[], on_complete=False)
    return result
