"""Validated configuration. Commands and export paths come only from local JSON."""
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


def _message(key, **values):
    return load_ui().text('scenarios.diagnostics.config.' + key, **values)


def read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(_message('read', path=path, error=exc)) from exc
    if not isinstance(value, dict):
        raise ConfigError(_message('json_object', path=path))
    return value


def keys(value, allowed, label):
    if not isinstance(value, dict):
        raise ConfigError(_message('object', label=label))
    unknown = set(value) - set(allowed.split())
    if unknown:
        raise ConfigError(_message('unknown_keys', label=label, keys=', '.join(sorted(unknown))))


def number(value, label, minimum=0, integer=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < minimum
            or (integer and not isinstance(value, int))):
        kind = _message('integer' if integer else 'numeric')
        raise ConfigError(_message('number', label=label, kind=kind, minimum=minimum))
    return value


def boolean(value, label):
    if not isinstance(value, bool):
        raise ConfigError(_message('boolean', label=label))
    return value


def argv(value, label):
    if not isinstance(value, list) or not value or any(
            not isinstance(v, str) or not v or '\0' in v for v in value):
        raise ConfigError(_message('argv', label=label))
    return value


def patterns(value, label):
    if not isinstance(value, list):
        raise ConfigError(_message('array', label=label))
    for pattern in value:
        if not isinstance(pattern, str) or not pattern or len(pattern) > 512:
            raise ConfigError(_message('patterns', label=label))
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(_message('regex', label=label, error=exc)) from exc
    return value


def glob_patterns(value, label):
    """Validate basename-only glob patterns used by macro discovery."""
    if not isinstance(value, list):
        raise ConfigError(_message('array', label=label))
    for pattern in value:
        if (not isinstance(pattern, str) or not pattern or len(pattern) > 255
                or '\0' in pattern or '/' in pattern or '\\' in pattern):
            raise ConfigError(_message('glob_patterns', label=label))
    if len(value) != len(set(value)):
        raise ConfigError(_message('glob_duplicate', label=label))
    return value


def inside(root, relative):
    root = Path(root).resolve()
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ConfigError(_message('relative_path'))
    result = (root / relative).resolve()
    try:
        result.relative_to(root)
    except ValueError as exc:
        raise ConfigError(_message('path_escape', path=relative)) from exc
    return result


def cpu_set(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+(-\d+)?(,\d+(-\d+)?)*", value):
        raise ConfigError(_message('cpu_format'))
    cpus = set()
    for part in value.split(','):
        ends = [int(p) for p in part.split('-')]
        lo, hi = ends[0], ends[-1]
        if lo > hi or hi > 1048575 or hi - lo > 65536:
            raise ConfigError(_message('cpu_range'))
        cpus.update(range(lo, hi + 1))
    return cpus


def load_case(path):
    path = Path(path).resolve()
    c = read_json(path)
    keys(c, "version case_dir name log residual_pattern cores cpu_set cpu_policy allow_cross_socket command expected_seconds simulation exports preprocess postprocess monitoring require_end watcher notifications task_type role macro_ticket end_time queue cases resource_source discovery", "case")
    if type(c.get("version")) is not int or c['version'] != 1:
        raise ConfigError(_message('case_version'))
    case_dir = c.get('case_dir')
    if case_dir is None:
        root = path.parent
    elif not isinstance(case_dir, str) or not case_dir.strip() or '\0' in case_dir:
        raise ConfigError(_message('case_dir'))
    else:
        root = Path(case_dir).expanduser()
        root = (path.parent / root).resolve() if not root.is_absolute() else root.resolve()
    c['case_dir'] = str(root)
    if c.setdefault('task_type', 'single') not in ('single', 'macro'):
        raise ConfigError(_message('task_type'))
    if c.setdefault('role', 'alone') not in ('alone', 'child'):
        raise ConfigError(_message('role'))
    if c.get('resource_source', 'case') not in ('case', 'ticket', 'macro'):
        raise ConfigError(_message('resource_source'))
    if c.get('cpu_policy', 'manual') not in ('auto', 'manual'):
        raise ConfigError(_message('cpu_policy'))
    automatic = c.get('cpu_policy') == 'auto'
    required = ('cores', 'command') if automatic else ('cores', 'cpu_set', 'command')
    if c.get('resource_source') in ('macro', 'ticket') and not all(key in c for key in required):
        raise ConfigError(_message('execution_required', fields=', '.join(required)))
    if c['role'] == 'child':
        parent = c.get('macro_ticket')
        if not isinstance(parent, str) or not parent or Path(parent).is_absolute():
            raise ConfigError(_message('child_macro'))
        inside(path.parent, parent)
    if c.get('end_time') is not None:
        number(c['end_time'], 'end_time')
    queue = c.setdefault('queue', {})
    keys(queue, 'state result reason job_id request_id submit mode lane updated_at', 'queue')
    if queue.get('state', 'waiting') not in ('waiting', 'running', 'finished'):
        raise ConfigError(_message('queue_state'))
    boolean(queue.get('submit', False), 'queue.submit')
    if 'request_id' in queue and (not isinstance(queue['request_id'], str) or not queue['request_id']):
        raise ConfigError(_message('request_id'))
    if queue.get('mode', 'queue') not in ('run', 'queue'):
        raise ConfigError(_message('queue_mode'))
    if type(queue.get('lane', 1)) is not int or not 1 <= queue.get('lane', 1) <= 3:
        raise ConfigError(_message('queue_lane'))
    if c['task_type'] == 'macro':
        if c['role'] == 'child' or not isinstance(c.setdefault('cases', []), list):
            raise ConfigError(_message('macro_shape'))
        roots = set()
        for item in c['cases']:
            keys(item, 'case_dir ticket state result reason job_id', 'macro.cases')
            directory = item.get('case_dir')
            if not isinstance(directory, str) or not Path(directory).is_absolute():
                raise ConfigError(_message('macro_absolute'))
            resolved = str(Path(directory).resolve())
            if resolved in roots or Path(resolved) == root or not Path(resolved).is_relative_to(root):
                raise ConfigError(_message('macro_unique'))
            roots.add(resolved)
            if item.get('state', 'waiting') not in ('waiting', 'running', 'finished'):
                raise ConfigError(_message('macro_state'))
            if 'ticket' in item:
                inside(path.parent, item['ticket'])
        if 'discovery' in c:
            discovery = c['discovery']
            keys(discovery, 'include_patterns exclude_patterns', 'discovery')
            glob_patterns(discovery.setdefault('include_patterns', []),
                          'discovery.include_patterns')
            glob_patterns(discovery.setdefault('exclude_patterns', []),
                          'discovery.exclude_patterns')
            if not discovery['include_patterns'] and not discovery['exclude_patterns']:
                c.pop('discovery')
    elif 'discovery' in c:
        raise ConfigError(_message('discovery_macro'))
    c.setdefault("name", root.name)
    if not isinstance(c['name'], str) or not c['name'].strip():
        raise ConfigError(_message('case_name'))
    c.setdefault('log', 'log.solver')
    inside(root, c['log'])
    residual = c.setdefault('residual_pattern', '')
    if not isinstance(residual, str):
        raise ConfigError(_message('residual_string'))
    if residual:
        inside(root, residual)
        if not residual.lower().endswith('.png'):
            raise ConfigError(_message('residual_png'))
    number(c.setdefault('cores', 1), 'cores', 1, True)
    if 'cpu_set' in c:
        cpus = cpu_set(c['cpu_set'])
        if not automatic and c['cores'] > len(cpus):
            raise ConfigError(_message('cores'))
    if 'command' in c:
        argv(c['command'], 'command')
        if 'cpu_set' not in c and not automatic:
            raise ConfigError(_message('command_cpu'))
    boolean(c.setdefault('allow_cross_socket', automatic), 'allow_cross_socket')
    if automatic:
        # Auto policy now spans sockets; old auto tickets may still store false.
        c['allow_cross_socket'] = True
    boolean(c.setdefault('require_end', True), 'require_end')
    expected = c.get('expected_seconds')
    if expected is None or (isinstance(expected, str) and not expected.strip()):
        c.pop('expected_seconds', None)
    else:
        number(expected, 'expected_seconds', 1)
    if 'simulation' in c:
        s = c['simulation']
        keys(s, 'start end', 'simulation')
        number(s.get('start', 0), 'simulation.start')
        number(s.get('end'), 'simulation.end')
        s.setdefault('start', 0)
        if s['end'] <= s['start']:
            raise ConfigError(_message('simulation'))
    watcher = c.setdefault('watcher', {})
    keys(watcher, 'log logs progress success failure incomplete_status missing_polls', 'watcher')
    if 'log' in watcher and 'logs' in watcher:
        raise ConfigError(_message('watcher_logs_choice'))
    if 'logs' in watcher:
        logs = watcher['logs']
        if (not isinstance(logs, list) or not logs
                or any(not isinstance(value, str) or not value for value in logs)):
            raise ConfigError(_message('watcher_logs'))
        if len(logs) != len(set(logs)):
            raise ConfigError(_message('watcher_logs_duplicate'))
    else:
        logs = [watcher.setdefault('log', c['log'])]
    for value in logs:
        inside(root, value)
    watcher['logs'] = logs
    c['log'] = logs[0]
    progress = watcher.setdefault('progress', {})
    keys(progress, 'pattern group start end', 'watcher.progress')
    progress.setdefault('pattern', DEFAULT_PROGRESS_PATTERN)
    progress.setdefault('group', 'value')
    progress.setdefault('start', c.get('simulation', {}).get('start', 0))
    if 'end' not in progress and 'simulation' in c:
        progress['end'] = c['simulation']['end']
    compiled_progress = re.compile(patterns([progress['pattern']], 'watcher.progress.pattern')[0])
    group = progress['group']
    if isinstance(group, str):
        if group not in compiled_progress.groupindex:
            raise ConfigError(_message('progress_group_missing', group=group))
    elif type(group) is int:
        if group < 1 or group > compiled_progress.groups:
            raise ConfigError(_message('progress_group_range'))
    else:
        raise ConfigError(_message('progress_group_type'))
    number(progress['start'], 'watcher.progress.start')
    if 'end' in progress:
        number(progress['end'], 'watcher.progress.end')
        if progress['end'] <= progress['start']:
            raise ConfigError(_message('progress_end'))
        c['simulation'] = {'start': progress['start'], 'end': progress['end']}
    success = watcher.setdefault('success', {})
    keys(success, 'patterns match required_files require_progress_end', 'watcher.success')
    patterns(success.setdefault('patterns', [DEFAULT_SUCCESS_PATTERN]
                                if c['require_end'] else []),
             'watcher.success.patterns')
    if success.setdefault('match', 'all') not in ('all', 'any'):
        raise ConfigError(_message('success_match'))
    boolean(success.setdefault('require_progress_end', True),
            'watcher.success.require_progress_end')
    if not isinstance(success.setdefault('required_files', []), list):
        raise ConfigError(_message('required_files'))
    for value in success['required_files']:
        inside(root, value)
    failure = watcher.setdefault('failure', {})
    keys(failure, 'patterns include_openfoam_defaults updated_files', 'watcher.failure')
    if not isinstance(failure.setdefault('updated_files', []), list):
        raise ConfigError(_message('updated_files'))
    for value in failure['updated_files']:
        inside(root, value)
    patterns(failure.setdefault('patterns', []), 'watcher.failure.patterns')
    boolean(failure.setdefault('include_openfoam_defaults', True),
            'watcher.failure.include_openfoam_defaults')
    if ('incomplete_status' in watcher and
            watcher['incomplete_status'] not in ('failed', 'interrupted')):
        raise ConfigError(_message('incomplete_status'))
    if 'missing_polls' in watcher:
        number(watcher['missing_polls'], 'watcher.missing_polls', 1, True)
    notifications = c.setdefault('notifications', {})
    keys(notifications, 'events', 'notifications')
    events = notifications.setdefault('events', ['started', 'succeeded', 'failed', 'interrupted'])
    allowed_events = {'started', 'succeeded', 'failed', 'interrupted'}
    if (not isinstance(events, list) or any(e not in allowed_events for e in events)
            or len(events) != len(set(events))):
        raise ConfigError(_message('events'))
    for key in ('exports', 'preprocess', 'postprocess'):
        if not isinstance(c.setdefault(key, []), list):
            raise ConfigError(_message('named_array', name=key))
    seen = set()
    for export in c['exports']:
        keys(export, 'name pattern kind max_files on_complete on', 'export')
        name = export.get('name')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', name) or name in seen:
            raise ConfigError(_message('export_name'))
        seen.add(name)
        inside(root, export.get('pattern'))
        if export.setdefault('kind', 'document') not in ('photo', 'document'):
            raise ConfigError(_message('export_kind'))
        number(export.setdefault('max_files', 1), 'export.max_files', 1, True)
        if export['max_files'] > 10:
            raise ConfigError(_message('export_count'))
        boolean(export.setdefault('on_complete', True), 'export.on_complete')
        if 'on' not in export:
            export['on'] = (['succeeded', 'failed', 'interrupted']
                            if export['on_complete'] else [])
        allowed_export_events = {'succeeded', 'failed', 'interrupted'}
        if (not isinstance(export['on'], list)
                or any(event not in allowed_export_events for event in export['on'])
                or len(export['on']) != len(set(export['on']))):
            raise ConfigError(_message('export_events'))
    for stage in ('preprocess', 'postprocess'):
        for hook in c[stage]:
            keys(hook, 'command timeout_seconds', stage)
            argv(hook.get('command'), stage + '.command')
            number(hook.setdefault('timeout_seconds', 120), stage + '.timeout_seconds', 1)
    if 'monitoring' in c:
        monitoring = c['monitoring']
        keys(monitoring, 'allocate_cpu command', 'monitoring')
        boolean(monitoring.get('allocate_cpu'), 'monitoring.allocate_cpu')
        argv(monitoring.get('command'), 'monitoring.command')
        if not monitoring['allocate_cpu']:
            c.pop('monitoring')
    c['_root'] = str(root)
    c['_config'] = str(path)
    return c


def load_bot(path):
    path = Path(path).resolve()
    b = read_json(path)
    keys(b, 'version state_dir text_file ui_dir cases case_globs ofps_command poll_seconds missing_polls telegram scheduler', 'bot')
    if type(b.get('version')) is not int or b['version'] != 1:
        raise ConfigError(_message('bot_version'))
    b['_path'] = str(path)
    if not isinstance(b.get('state_dir', 'state'), str) or not b.get('state_dir', 'state'):
        raise ConfigError(_message('state_dir'))
    b['state_dir'] = str((path.parent / b.get('state_dir', 'state')).resolve())
    text_file = b.get('text_file')
    if text_file is not None:
        if not isinstance(text_file, str) or not text_file or '\0' in text_file:
            raise ConfigError(_message('text_file'))
        b['_text_file'] = str((path.parent / text_file).resolve())
    else:
        b['_text_file'] = None
    ui_dir = b.get('ui_dir')
    if ui_dir is not None:
        if not isinstance(ui_dir, str) or not ui_dir or '\0' in ui_dir:
            raise ConfigError(_message('ui_dir'))
        b['_ui_dir'] = str((path.parent / ui_dir).resolve())
    else:
        from .ui import DEFAULT_UI_DIR
        b['_ui_dir'] = str(DEFAULT_UI_DIR)
    from .ui import load_ui
    load_ui(b['_ui_dir'])
    b.setdefault('ofps_command', [str(Path.home() / '.local/bin/ofps')])
    argv(b['ofps_command'], 'ofps_command')
    number(b.setdefault('poll_seconds', 5), 'poll_seconds', 1)
    number(b.setdefault('missing_polls', 2), 'missing_polls', 1, True)
    for key in ('cases', 'case_globs'):
        if not isinstance(b.setdefault(key, []), list) or any(not isinstance(x, str) for x in b[key]):
            raise ConfigError(_message('string_array', name=key))
        b[key] = [str(path.parent / p) for p in b[key]]
    t = b.setdefault('telegram', {})
    keys(t, 'token_env allowed_user_ids chat_ids', 'telegram')
    t.setdefault('token_env', 'TELEGRAM_BOT_TOKEN')
    if not isinstance(t['token_env'], str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', t['token_env']):
        raise ConfigError(_message('token_env'))
    for key in ('allowed_user_ids', 'chat_ids'):
        if not isinstance(t.setdefault(key, []), list) or any(type(x) is not int or x == 0 for x in t[key]):
            raise ConfigError(_message('id_array', name=key))
        t[key] = list(dict.fromkeys(t[key]))
    s = b.setdefault('scheduler', {})
    keys(s, 'enabled max_parallel cpu_capacity openfoam_bashrc', 'scheduler')
    boolean(s.setdefault('enabled', False), 'scheduler.enabled')
    number(s.setdefault('max_parallel', 1), 'scheduler.max_parallel', 1, True)
    if s.setdefault('cpu_capacity', None) is not None:
        number(s['cpu_capacity'], 'scheduler.cpu_capacity', 1, True)
    bashrc = s.setdefault('openfoam_bashrc', None)
    if bashrc is not None:
        if not isinstance(bashrc, str) or not bashrc.strip() or '\0' in bashrc:
            raise ConfigError(_message('bashrc'))
        s['openfoam_bashrc'] = str((path.parent / Path(bashrc).expanduser()).resolve())
    return b


def cases_for(bot, tickets=None, *, force=False):
    from .catalog import active_cases, ticket_index
    if tickets is not None:
        return active_cases(tickets, bot.get('_ui_dir'))
    return ticket_index(bot, force=force).cases()


def tickets_for(bot, *, force=False):
    from .catalog import ticket_index
    return ticket_index(bot, force=force).tickets()
