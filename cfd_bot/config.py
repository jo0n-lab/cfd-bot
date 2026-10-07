"""Validated configuration. Commands and export paths come only from local JSON."""
from . import diagnostics as _diagnostics
import json
import math
import re
from pathlib import Path

from .ui import load_ui

DEFAULT_PROGRESS_PATTERN = (r'^\s*(?:\[\d+\]\s*)?Time\s*=\s*'
                            r'(?P<value>[-+]?(?:\d+(?:\.\d*)?|\.\d+)'
                            r'(?:[eE][-+]?\d+)?)\s*s?\s*$')
DEFAULT_SUCCESS_PATTERN = r'^\s*(?:\[\d+\]\s*)?End\s*$'


class ConfigError(ValueError):
    pass


@_diagnostics.trace
def _message(key, **values):
    return load_ui().text('scenarios.diagnostics.config.' + key, **values)


@_diagnostics.trace
def read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        if _diagnostics.enabled: _diagnostics.step('config.read_json:L26:except')
        raise ConfigError(_message('read', path=path, error=exc)) from exc
    if not isinstance(value, dict):
        if _diagnostics.enabled: _diagnostics.step('config.read_json:L28:then')
        raise ConfigError(_message('json_object', path=path))
    return value


@_diagnostics.trace
def keys(value, allowed, label):
    if not isinstance(value, dict):
        if _diagnostics.enabled: _diagnostics.step('config.keys:L34:then')
        raise ConfigError(_message('object', label=label))
    unknown = set(value) - set(allowed.split())
    if unknown:
        if _diagnostics.enabled: _diagnostics.step('config.keys:L37:then')
        raise ConfigError(_message('unknown_keys', label=label, keys=', '.join(sorted(unknown))))


@_diagnostics.trace
def number(value, label, minimum=0, integer=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < minimum
            or (integer and not isinstance(value, int))):
        if _diagnostics.enabled: _diagnostics.step('config.number:L42:then')
        kind = _message('integer' if integer else 'numeric')
        raise ConfigError(_message('number', label=label, kind=kind, minimum=minimum))
    return value


@_diagnostics.trace
def boolean(value, label):
    if not isinstance(value, bool):
        if _diagnostics.enabled: _diagnostics.step('config.boolean:L51:then')
        raise ConfigError(_message('boolean', label=label))
    return value


@_diagnostics.trace
def argv(value, label):
    if not isinstance(value, list) or not value or any(
            not isinstance(v, str) or not v or '\0' in v for v in value):
        if _diagnostics.enabled: _diagnostics.step('config.argv:L57:then')
        raise ConfigError(_message('argv', label=label))
    return value


@_diagnostics.trace
def patterns(value, label):
    if not isinstance(value, list):
        if _diagnostics.enabled: _diagnostics.step('config.patterns:L64:then')
        raise ConfigError(_message('array', label=label))
    for pattern in value:
        if _diagnostics.enabled: _diagnostics.step('config.patterns:L66:loop', pattern=pattern)
        if not isinstance(pattern, str) or not pattern or len(pattern) > 512:
            if _diagnostics.enabled: _diagnostics.step('config.patterns:L67:then')
            raise ConfigError(_message('patterns', label=label))
        try:
            re.compile(pattern)
        except re.error as exc:
            if _diagnostics.enabled: _diagnostics.step('config.patterns:L71:except')
            raise ConfigError(_message('regex', label=label, error=exc)) from exc
    return value


@_diagnostics.trace
def glob_patterns(value, label):
    """Validate basename-only glob patterns used by macro discovery."""
    if not isinstance(value, list):
        if _diagnostics.enabled: _diagnostics.step('config.glob_patterns:L78:then')
        raise ConfigError(_message('array', label=label))
    for pattern in value:
        if _diagnostics.enabled: _diagnostics.step('config.glob_patterns:L80:loop', pattern=pattern)
        if (not isinstance(pattern, str) or not pattern or len(pattern) > 255
                or '\0' in pattern or '/' in pattern or '\\' in pattern):
            if _diagnostics.enabled: _diagnostics.step('config.glob_patterns:L81:then')
            raise ConfigError(_message('glob_patterns', label=label))
    if len(value) != len(set(value)):
        if _diagnostics.enabled: _diagnostics.step('config.glob_patterns:L84:then')
        raise ConfigError(_message('glob_duplicate', label=label))
    return value


@_diagnostics.trace
def inside(root, relative):
    root = Path(root).resolve()
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        if _diagnostics.enabled: _diagnostics.step('config.inside:L91:then')
        raise ConfigError(_message('relative_path'))
    result = (root / relative).resolve()
    try:
        result.relative_to(root)
    except ValueError as exc:
        if _diagnostics.enabled: _diagnostics.step('config.inside:L96:except')
        raise ConfigError(_message('path_escape', path=relative)) from exc
    return result


@_diagnostics.trace
def cpu_set(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+(-\d+)?(,\d+(-\d+)?)*", value):
        if _diagnostics.enabled: _diagnostics.step('config.cpu_set:L102:then')
        raise ConfigError(_message('cpu_format'))
    cpus = set()
    for part in value.split(','):
        if _diagnostics.enabled: _diagnostics.step('config.cpu_set:L105:loop', part=part)
        ends = [int(p) for p in part.split('-')]
        lo, hi = ends[0], ends[-1]
        if lo > hi or hi > 1048575 or hi - lo > 65536:
            if _diagnostics.enabled: _diagnostics.step('config.cpu_set:L108:then')
            raise ConfigError(_message('cpu_range'))
        cpus.update(range(lo, hi + 1))
    return cpus


@_diagnostics.trace
def load_case(path):
    path = Path(path).resolve()
    c = read_json(path)
    keys(c, "version case_dir name log residual_pattern cores cpu_set cpu_policy allow_cross_socket command expected_seconds simulation exports preprocess postprocess monitoring require_end watcher notifications task_type role macro_ticket end_time queue cases resource_source discovery execution_queue dynamic_cores", "case")
    if type(c.get("version")) is not int or c['version'] != 1:
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L118:then')
        raise ConfigError(_message('case_version'))
    case_dir = c.get('case_dir')
    if case_dir is None:
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L121:then')
        root = path.parent
    elif not isinstance(case_dir, str) or not case_dir.strip() or '\0' in case_dir:
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L123:then')
        raise ConfigError(_message('case_dir'))
    else:
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L123:else')
        root = Path(case_dir).expanduser()
        root = (path.parent / root).resolve() if not root.is_absolute() else root.resolve()
    c['case_dir'] = str(root)
    if c.setdefault('task_type', 'single') not in ('single', 'macro'):
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L129:then')
        raise ConfigError(_message('task_type'))
    if c.setdefault('role', 'alone') not in ('alone', 'child'):
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L131:then')
        raise ConfigError(_message('role'))
    if c.get('resource_source', 'case') not in ('case', 'ticket', 'macro'):
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L133:then')
        raise ConfigError(_message('resource_source'))
    if c.get('cpu_policy', 'manual') not in ('auto', 'manual'):
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L135:then')
        raise ConfigError(_message('cpu_policy'))
    automatic = c.get('cpu_policy') == 'auto'
    required = ('cores', 'command') if automatic else ('cores', 'cpu_set', 'command')
    if c.get('resource_source') in ('macro', 'ticket') and not all(key in c for key in required):
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L139:then')
        raise ConfigError(_message('execution_required', fields=', '.join(required)))
    if c['role'] == 'child':
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L141:then')
        parent = c.get('macro_ticket')
        if not isinstance(parent, str) or not parent or Path(parent).is_absolute():
            if _diagnostics.enabled: _diagnostics.step('config.load_case:L143:then')
            raise ConfigError(_message('child_macro'))
        inside(path.parent, parent)
    if c.get('end_time') is not None:
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L146:then')
        number(c['end_time'], 'end_time')
    queue = c.setdefault('queue', {})
    keys(queue, 'state result reason job_id request_id submit mode lane id cpu_set updated_at', 'queue')
    if queue.get('state', 'waiting') not in ('waiting', 'running', 'finished'):
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L150:then')
        raise ConfigError(_message('queue_state'))
    boolean(queue.get('submit', False), 'queue.submit')
    if 'request_id' in queue and (not isinstance(queue['request_id'], str) or not queue['request_id']):
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L153:then')
        raise ConfigError(_message('request_id'))
    if queue.get('mode', 'queue') not in ('run', 'queue'):
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L155:then')
        raise ConfigError(_message('queue_mode'))
    if type(queue.get('lane', 1)) is not int or not 1 <= queue.get('lane', 1) <= 3:
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L157:then')
        raise ConfigError(_message('queue_lane'))
    execution_queue = c.get('execution_queue')
    if execution_queue is not None:
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L160:then')
        keys(execution_queue, 'id cpu_set', 'execution_queue')
        if (not isinstance(execution_queue.get('id'), str)
                or not re.fullmatch(r'[A-Za-z0-9._-]{1,48}', execution_queue['id'])):
            if _diagnostics.enabled: _diagnostics.step('config.load_case:L162:then')
            raise ConfigError(_message('execution_queue_id'))
        queue_cpus = cpu_set(execution_queue['cpu_set']) if 'cpu_set' in execution_queue else set()
    else:
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L160:else')
        queue_cpus = set()
    boolean(c.setdefault('dynamic_cores', False), 'dynamic_cores')
    if c['dynamic_cores'] and c['task_type'] != 'macro' and c['role'] != 'child':
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L169:then')
        raise ConfigError(_message('dynamic_macro_only'))
    if c['dynamic_cores'] and execution_queue is None:
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L171:then')
        raise ConfigError(_message('dynamic_queue_required'))
    if c['task_type'] == 'macro':
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L173:then')
        if c['role'] == 'child' or not isinstance(c.setdefault('cases', []), list):
            if _diagnostics.enabled: _diagnostics.step('config.load_case:L174:then')
            raise ConfigError(_message('macro_shape'))
        roots = set()
        for item in c['cases']:
            if _diagnostics.enabled: _diagnostics.step('config.load_case:L177:loop', item=item)
            keys(item, 'case_dir ticket cores state result reason job_id', 'macro.cases')
            directory = item.get('case_dir')
            if not isinstance(directory, str) or not Path(directory).is_absolute():
                if _diagnostics.enabled: _diagnostics.step('config.load_case:L180:then')
                raise ConfigError(_message('macro_absolute'))
            resolved = str(Path(directory).resolve())
            if resolved in roots or Path(resolved) == root or not Path(resolved).is_relative_to(root):
                if _diagnostics.enabled: _diagnostics.step('config.load_case:L183:then')
                raise ConfigError(_message('macro_unique'))
            roots.add(resolved)
            if item.get('state', 'waiting') not in ('waiting', 'running', 'finished'):
                if _diagnostics.enabled: _diagnostics.step('config.load_case:L186:then')
                raise ConfigError(_message('macro_state'))
            if c['dynamic_cores']:
                if _diagnostics.enabled: _diagnostics.step('config.load_case:L188:then')
                number(item.get('cores'), 'macro.cases.cores', 1, True)
            elif 'cores' in item:
                if _diagnostics.enabled: _diagnostics.step('config.load_case:L190:then')
                number(item['cores'], 'macro.cases.cores', 1, True)
            if 'ticket' in item:
                if _diagnostics.enabled: _diagnostics.step('config.load_case:L192:then')
                inside(path.parent, item['ticket'])
        if 'discovery' in c:
            if _diagnostics.enabled: _diagnostics.step('config.load_case:L194:then')
            discovery = c['discovery']
            keys(discovery, 'include_patterns exclude_patterns', 'discovery')
            glob_patterns(discovery.setdefault('include_patterns', []),
                          'discovery.include_patterns')
            glob_patterns(discovery.setdefault('exclude_patterns', []),
                          'discovery.exclude_patterns')
            if not discovery['include_patterns'] and not discovery['exclude_patterns']:
                if _diagnostics.enabled: _diagnostics.step('config.load_case:L201:then')
                c.pop('discovery')
    elif 'discovery' in c:
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L203:then')
        raise ConfigError(_message('discovery_macro'))
    c.setdefault("name", root.name)
    if not isinstance(c['name'], str) or not c['name'].strip():
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L206:then')
        raise ConfigError(_message('case_name'))
    c.setdefault('log', 'log.solver')
    inside(root, c['log'])
    residual = c.setdefault('residual_pattern', '')
    if not isinstance(residual, str):
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L211:then')
        raise ConfigError(_message('residual_string'))
    if residual:
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L213:then')
        inside(root, residual)
        if not residual.lower().endswith('.png'):
            if _diagnostics.enabled: _diagnostics.step('config.load_case:L215:then')
            raise ConfigError(_message('residual_png'))
    number(c.setdefault('cores', 1), 'cores', 1, True)
    if queue_cpus and not c['dynamic_cores'] and c['cores'] > len(queue_cpus):
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L218:then')
        raise ConfigError(_message('queue_quota', cores=c['cores'], quota=len(queue_cpus)))
    if 'cpu_set' in c:
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L220:then')
        cpus = cpu_set(c['cpu_set'])
        if not automatic and c['cores'] > len(cpus):
            if _diagnostics.enabled: _diagnostics.step('config.load_case:L222:then')
            raise ConfigError(_message('cores'))
    if 'command' in c:
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L224:then')
        argv(c['command'], 'command')
        if 'cpu_set' not in c and not automatic:
            if _diagnostics.enabled: _diagnostics.step('config.load_case:L226:then')
            raise ConfigError(_message('command_cpu'))
    boolean(c.setdefault('allow_cross_socket', automatic), 'allow_cross_socket')
    if automatic:
        # Auto policy now spans sockets; old auto tickets may still store false.
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L229:then')
        c['allow_cross_socket'] = True
    boolean(c.setdefault('require_end', True), 'require_end')
    expected = c.get('expected_seconds')
    if expected is None or (isinstance(expected, str) and not expected.strip()):
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L234:then')
        c.pop('expected_seconds', None)
    else:
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L234:else')
        number(expected, 'expected_seconds', 1)
    if 'simulation' in c:
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L238:then')
        s = c['simulation']
        keys(s, 'start end', 'simulation')
        number(s.get('start', 0), 'simulation.start')
        number(s.get('end'), 'simulation.end')
        s.setdefault('start', 0)
        if s['end'] <= s['start']:
            if _diagnostics.enabled: _diagnostics.step('config.load_case:L244:then')
            raise ConfigError(_message('simulation'))
    watcher = c.setdefault('watcher', {})
    keys(watcher, 'log logs progress success failure incomplete_status missing_polls', 'watcher')
    if 'log' in watcher and 'logs' in watcher:
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L248:then')
        raise ConfigError(_message('watcher_logs_choice'))
    if 'logs' in watcher:
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L250:then')
        logs = watcher['logs']
        if (not isinstance(logs, list) or not logs
                or any(not isinstance(value, str) or not value for value in logs)):
            if _diagnostics.enabled: _diagnostics.step('config.load_case:L252:then')
            raise ConfigError(_message('watcher_logs'))
        if len(logs) != len(set(logs)):
            if _diagnostics.enabled: _diagnostics.step('config.load_case:L255:then')
            raise ConfigError(_message('watcher_logs_duplicate'))
    else:
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L250:else')
        logs = [watcher.setdefault('log', c['log'])]
    for value in logs:
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L259:loop', value=value)
        inside(root, value)
    watcher['logs'] = logs
    c['log'] = logs[0]
    progress = watcher.setdefault('progress', {})
    keys(progress, 'pattern group start end', 'watcher.progress')
    progress.setdefault('pattern', DEFAULT_PROGRESS_PATTERN)
    progress.setdefault('group', 'value')
    progress.setdefault('start', c.get('simulation', {}).get('start', 0))
    if 'end' not in progress and 'simulation' in c:
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L268:then')
        progress['end'] = c['simulation']['end']
    compiled_progress = re.compile(patterns([progress['pattern']], 'watcher.progress.pattern')[0])
    group = progress['group']
    if isinstance(group, str):
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L272:then')
        if group not in compiled_progress.groupindex:
            if _diagnostics.enabled: _diagnostics.step('config.load_case:L273:then')
            raise ConfigError(_message('progress_group_missing', group=group))
    elif type(group) is int:
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L275:then')
        if group < 1 or group > compiled_progress.groups:
            if _diagnostics.enabled: _diagnostics.step('config.load_case:L276:then')
            raise ConfigError(_message('progress_group_range'))
    else:
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L275:else')
        raise ConfigError(_message('progress_group_type'))
    number(progress['start'], 'watcher.progress.start')
    if 'end' in progress:
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L281:then')
        number(progress['end'], 'watcher.progress.end')
        if progress['end'] <= progress['start']:
            if _diagnostics.enabled: _diagnostics.step('config.load_case:L283:then')
            raise ConfigError(_message('progress_end'))
        c['simulation'] = {'start': progress['start'], 'end': progress['end']}
    success = watcher.setdefault('success', {})
    keys(success, 'patterns match required_files require_progress_end', 'watcher.success')
    patterns(success.setdefault('patterns', [DEFAULT_SUCCESS_PATTERN]
                                if c['require_end'] else []),
             'watcher.success.patterns')
    if success.setdefault('match', 'all') not in ('all', 'any'):
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L291:then')
        raise ConfigError(_message('success_match'))
    boolean(success.setdefault('require_progress_end', True),
            'watcher.success.require_progress_end')
    if not isinstance(success.setdefault('required_files', []), list):
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L295:then')
        raise ConfigError(_message('required_files'))
    for value in success['required_files']:
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L297:loop', value=value)
        inside(root, value)
    failure = watcher.setdefault('failure', {})
    keys(failure, 'patterns include_openfoam_defaults updated_files', 'watcher.failure')
    if not isinstance(failure.setdefault('updated_files', []), list):
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L301:then')
        raise ConfigError(_message('updated_files'))
    for value in failure['updated_files']:
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L303:loop', value=value)
        inside(root, value)
    patterns(failure.setdefault('patterns', []), 'watcher.failure.patterns')
    boolean(failure.setdefault('include_openfoam_defaults', True),
            'watcher.failure.include_openfoam_defaults')
    if ('incomplete_status' in watcher and
            watcher['incomplete_status'] not in ('failed', 'interrupted')):
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L308:then')
        raise ConfigError(_message('incomplete_status'))
    if 'missing_polls' in watcher:
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L311:then')
        number(watcher['missing_polls'], 'watcher.missing_polls', 1, True)
    notifications = c.setdefault('notifications', {})
    keys(notifications, 'events', 'notifications')
    events = notifications.setdefault('events', ['started', 'succeeded', 'failed', 'interrupted'])
    allowed_events = {'started', 'succeeded', 'failed', 'interrupted'}
    if (not isinstance(events, list) or any(e not in allowed_events for e in events)
            or len(events) != len(set(events))):
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L317:then')
        raise ConfigError(_message('events'))
    for key in ('exports', 'preprocess', 'postprocess'):
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L320:loop', key=key)
        if not isinstance(c.setdefault(key, []), list):
            if _diagnostics.enabled: _diagnostics.step('config.load_case:L321:then')
            raise ConfigError(_message('named_array', name=key))
    seen = set()
    for export in c['exports']:
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L324:loop', export=export)
        keys(export, 'name pattern kind max_files on_complete on', 'export')
        name = export.get('name')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', name) or name in seen:
            if _diagnostics.enabled: _diagnostics.step('config.load_case:L327:then')
            raise ConfigError(_message('export_name'))
        seen.add(name)
        inside(root, export.get('pattern'))
        if export.setdefault('kind', 'document') not in ('photo', 'document'):
            if _diagnostics.enabled: _diagnostics.step('config.load_case:L331:then')
            raise ConfigError(_message('export_kind'))
        number(export.setdefault('max_files', 1), 'export.max_files', 1, True)
        if export['max_files'] > 10:
            if _diagnostics.enabled: _diagnostics.step('config.load_case:L334:then')
            raise ConfigError(_message('export_count'))
        boolean(export.setdefault('on_complete', True), 'export.on_complete')
        if 'on' not in export:
            if _diagnostics.enabled: _diagnostics.step('config.load_case:L337:then')
            export['on'] = (['succeeded', 'failed', 'interrupted']
                            if export['on_complete'] else [])
        allowed_export_events = {'succeeded', 'failed', 'interrupted'}
        if (not isinstance(export['on'], list)
                or any(event not in allowed_export_events for event in export['on'])
                or len(export['on']) != len(set(export['on']))):
            if _diagnostics.enabled: _diagnostics.step('config.load_case:L341:then')
            raise ConfigError(_message('export_events'))
    for stage in ('preprocess', 'postprocess'):
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L345:loop', stage=stage)
        for hook in c[stage]:
            if _diagnostics.enabled: _diagnostics.step('config.load_case:L346:loop', hook=hook)
            keys(hook, 'command timeout_seconds', stage)
            argv(hook.get('command'), stage + '.command')
            number(hook.setdefault('timeout_seconds', 120), stage + '.timeout_seconds', 1)
    if 'monitoring' in c:
        if _diagnostics.enabled: _diagnostics.step('config.load_case:L350:then')
        monitoring = c['monitoring']
        keys(monitoring, 'allocate_cpu command', 'monitoring')
        boolean(monitoring.get('allocate_cpu'), 'monitoring.allocate_cpu')
        argv(monitoring.get('command'), 'monitoring.command')
        if not monitoring['allocate_cpu']:
            if _diagnostics.enabled: _diagnostics.step('config.load_case:L355:then')
            c.pop('monitoring')
    c['_root'] = str(root)
    c['_config'] = str(path)
    return c


@_diagnostics.trace
def load_bot(path):
    path = Path(path).resolve()
    b = read_json(path)
    keys(b, 'version state_dir text_file ui_dir cases case_globs ofps_command poll_seconds missing_polls telegram scheduler diagnostic_logging', 'bot')
    if type(b.get('version')) is not int or b['version'] != 1:
        if _diagnostics.enabled: _diagnostics.step('config.load_bot:L366:then')
        raise ConfigError(_message('bot_version'))
    b['_path'] = str(path)
    if not isinstance(b.get('state_dir', 'state'), str) or not b.get('state_dir', 'state'):
        if _diagnostics.enabled: _diagnostics.step('config.load_bot:L369:then')
        raise ConfigError(_message('state_dir'))
    b['state_dir'] = str((path.parent / b.get('state_dir', 'state')).resolve())
    _diagnostics.configure(b)
    if _diagnostics.enabled:
        _diagnostics.event('config.loaded', path=path, diagnostic_logging=_diagnostics.settings())
    text_file = b.get('text_file')
    if text_file is not None:
        if _diagnostics.enabled: _diagnostics.step('config.load_bot:L373:then')
        if not isinstance(text_file, str) or not text_file or '\0' in text_file:
            if _diagnostics.enabled: _diagnostics.step('config.load_bot:L374:then')
            raise ConfigError(_message('text_file'))
        b['_text_file'] = str((path.parent / text_file).resolve())
    else:
        if _diagnostics.enabled: _diagnostics.step('config.load_bot:L373:else')
        b['_text_file'] = None
    ui_dir = b.get('ui_dir')
    if ui_dir is not None:
        if _diagnostics.enabled: _diagnostics.step('config.load_bot:L380:then')
        if not isinstance(ui_dir, str) or not ui_dir or '\0' in ui_dir:
            if _diagnostics.enabled: _diagnostics.step('config.load_bot:L381:then')
            raise ConfigError(_message('ui_dir'))
        b['_ui_dir'] = str((path.parent / ui_dir).resolve())
    else:
        if _diagnostics.enabled: _diagnostics.step('config.load_bot:L380:else')
        from .ui import DEFAULT_UI_DIR
        b['_ui_dir'] = str(DEFAULT_UI_DIR)
    from .ui import load_ui
    load_ui(b['_ui_dir'])
    b.setdefault('ofps_command', [str(Path.home() / '.local/bin/ofps')])
    argv(b['ofps_command'], 'ofps_command')
    number(b.setdefault('poll_seconds', 5), 'poll_seconds', 1)
    number(b.setdefault('missing_polls', 2), 'missing_polls', 1, True)
    for key in ('cases', 'case_globs'):
        if _diagnostics.enabled: _diagnostics.step('config.load_bot:L393:loop', key=key)
        if not isinstance(b.setdefault(key, []), list) or any(not isinstance(x, str) for x in b[key]):
            if _diagnostics.enabled: _diagnostics.step('config.load_bot:L394:then')
            raise ConfigError(_message('string_array', name=key))
        b[key] = [str(path.parent / p) for p in b[key]]
    t = b.setdefault('telegram', {})
    keys(t, 'token_env allowed_user_ids chat_ids', 'telegram')
    t.setdefault('token_env', 'TELEGRAM_BOT_TOKEN')
    if not isinstance(t['token_env'], str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', t['token_env']):
        if _diagnostics.enabled: _diagnostics.step('config.load_bot:L400:then')
        raise ConfigError(_message('token_env'))
    for key in ('allowed_user_ids', 'chat_ids'):
        if _diagnostics.enabled: _diagnostics.step('config.load_bot:L402:loop', key=key)
        if not isinstance(t.setdefault(key, []), list) or any(type(x) is not int or x == 0 for x in t[key]):
            if _diagnostics.enabled: _diagnostics.step('config.load_bot:L403:then')
            raise ConfigError(_message('id_array', name=key))
        t[key] = list(dict.fromkeys(t[key]))
    s = b.setdefault('scheduler', {})
    keys(s, 'enabled max_parallel cpu_capacity openfoam_bashrc', 'scheduler')
    boolean(s.setdefault('enabled', False), 'scheduler.enabled')
    number(s.setdefault('max_parallel', 1), 'scheduler.max_parallel', 1, True)
    if s.setdefault('cpu_capacity', None) is not None:
        if _diagnostics.enabled: _diagnostics.step('config.load_bot:L410:then')
        number(s['cpu_capacity'], 'scheduler.cpu_capacity', 1, True)
    bashrc = s.setdefault('openfoam_bashrc', None)
    if bashrc is not None:
        if _diagnostics.enabled: _diagnostics.step('config.load_bot:L413:then')
        if not isinstance(bashrc, str) or not bashrc.strip() or '\0' in bashrc:
            if _diagnostics.enabled: _diagnostics.step('config.load_bot:L414:then')
            raise ConfigError(_message('bashrc'))
        s['openfoam_bashrc'] = str((path.parent / Path(bashrc).expanduser()).resolve())
    return b


@_diagnostics.trace
def cases_for(bot, tickets=None, *, force=False):
    from .catalog import active_cases, ticket_index
    if tickets is not None:
        if _diagnostics.enabled: _diagnostics.step('config.cases_for:L422:then')
        return active_cases(tickets, bot.get('_ui_dir'))
    index = ticket_index(bot, force=force)
    if force:
        if _diagnostics.enabled: _diagnostics.step('config.cases_for:L425:then')
        from .cpu_allocation import managed_cpus
        from .queueing import queue_profile_conflict
        top = [ticket for ticket in index.tickets() if ticket.get('role', 'alone') != 'child']
        pool = managed_cpus(bot)
        for ticket in top:
            if _diagnostics.enabled: _diagnostics.step('config.cases_for:L430:loop', ticket=ticket)
            profile = ticket.get('execution_queue')
            if profile and profile.get('cpu_set') and (outside := cpu_set(profile['cpu_set']) - pool):
                if _diagnostics.enabled: _diagnostics.step('config.cases_for:L432:then')
                raise ConfigError(_message(
                    'execution_queue_outside_pool', queue=profile['id'],
                    cpus=','.join(map(str, sorted(outside)))))
        for position, ticket in enumerate(top):
            if _diagnostics.enabled: _diagnostics.step('config.cases_for:L436:loop', position=position, ticket=ticket)
            conflict = queue_profile_conflict(ticket, top[:position])
            if conflict:
                if _diagnostics.enabled: _diagnostics.step('config.cases_for:L438:then')
                kind, queue_id, detail = conflict
                raise ConfigError(_message('execution_queue_' + kind,
                                           queue=queue_id, detail=detail))
    return index.cases()


@_diagnostics.trace
def tickets_for(bot, *, force=False):
    from .catalog import ticket_index
    return ticket_index(bot, force=force).tickets()
