"""Retrospective execution records; never a source of business decisions.

No profiler, database writes, network collector, retry or execution policy lives
here. Each process owns a buffered JSONL file. OFF returns before summarising.
"""
import atexit
import contextvars
import functools
import hashlib
import inspect
import itertools
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import warnings

from . import diagnostic_codec as _codec

_POPEN_CLASS = subprocess.Popen


enabled = False
_sink = None
_settings = None
_component = 'python'
_counter = itertools.count(1)
_current = contextvars.ContextVar('cfd_diagnostic_call', default=None)
_actor = contextvars.ContextVar('cfd_diagnostic_actor', default=None)
_definitions = {}
_step_numbers = {}
_sequence_functions = set()
_IDENTIFIERS = ('_config', '_root', 'id', 'name', 'case_dir', 'status', 'phase',
                'cpu_set', 'actual_cpu_list', 'cores', 'time', 'offset', 'ended',
                'request_id', 'queue_id', 'revision', 'reason')
_IDENTIFIERS += ('state', 'error', 'can_run', 'capacity', 'used_cores', 'free_cores',
                 'required_cores', 'queue_possible', 'queue_required', 'monitor_cpu',
                 'monitoring_command', 'task_type', 'role', 'resource_source', 'cpu_policy',
                 'queue_cpu_set', 'backlog', 'missing', 'outcome', 'returncode')
_IDENTIFIER_SET = frozenset(_IDENTIFIERS)
_secrets = set()
_secret_epoch = 0
_warning_handler = warnings.showwarning
_thread_exception_handler = threading.excepthook
_exception_handler = sys.excepthook
_logging_handler = None
_SECRET_KEY = re.compile(r'token|password|passwd|secret|authorization|cookie|csrf|private.?key', re.I)
_TOKEN = re.compile(r'(?:https?://api\.telegram\.org/bot)?\d{6,}:[A-Za-z0-9_-]{20,}')
_CREDENTIAL = re.compile(r'(?i)((?:password|passwd|token|secret|authorization|cookie|csrf)\s*[=:]\s*)[^\s,;]+')
_TEXT_KEYS = {'raw', 'text', 'body', 'content', 'caption', 'message', 'html', 'source', 'stdout', 'stderr'}
_IDENTITY_KEYS = {'chat', 'chat_id', 'user', 'user_id', 'actor', 'allowed_user_ids', 'chat_ids'}
_ENV_KEYS = {'env', 'environ', 'environment', 'headers', '_base'}
_LARGE_DETAILS = {'tail', 'history', 'rate_samples', 'residuals', 'success_matches', 'checkpoint', 'partial'}
_SAFE_STRINGS = {
    'name', 'filename', 'path', 'root', '_root', '_config', 'case_root', 'case_dir',
    'kind', 'type', 'status', 'state', 'phase', 'mode', 'action', 'op', 'key', 'view',
    'method', 'pattern', 'cpu_set', 'actual_cpu_list', 'resource_source', 'cpu_policy',
    'queue_id', 'id', 'jid', 'job_id', 'run_id', 'ticket', 'macro_ticket', 'request_id',
    'revision', 'expected_revision', 'current', 'label', 'reason', 'reason_code',
    'command', 'module', 'function', 'caller', 'event', 'target', 'operation',
    'flow_id', 'sequence_step_id', 'statement', 'signal', 'log_path', 'log',
    'basis', 'outcome', 'field', 'stage', 'identity', 'returncode', 'call_id',
    'pid', 'ppid', 'cpu_list', 'sockets', 'numa', 'elapsed', 'engines',
    'request_key', 'batch', 'event_key', 'worker_identity', 'solver_identity',
    'task_type', 'role', 'execution_source', 'queue_cpu_set', 'monitor_cpu',
    'macro_cpu_set', 'macro_cpu_policy', 'macro_cores', 'monitoring_command',
}


def remember_secret(value):
    global _secret_epoch
    if isinstance(value, str) and value and value not in _secrets:
        _secrets.add(value)
        _secret_epoch += 1
        _argument_summary.cache_clear()
        _collection_summary.cache_clear()
        _redact_cached.cache_clear()
        _scalar_summary.cache_clear()


def _redact_text(value):
    for secret in _secrets:
        if secret in value:
            value = value.replace(secret, '[redacted]')
    value = _TOKEN.sub('[redacted]', value)
    value = _CREDENTIAL.sub(r'\1[redacted]', value)
    return re.sub(r'-----BEGIN [\s\S]*?PRIVATE KEY-----[\s\S]*?-----END [\s\S]*?PRIVATE KEY-----',
                  '[redacted private key]', value)


_redact_cached = functools.lru_cache(maxsize=8192)(_redact_text)


def redact(value):
    value = str(value)
    return _redact_cached(value) if len(value) <= 2048 else _redact_text(value)


def actor_id(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:16]


@functools.lru_cache(maxsize=8192, typed=True)
def _scalar_summary(value, key):
    if _SECRET_KEY.search(key) or key in _ENV_KEYS:
        return '[redacted]'
    if key in _IDENTITY_KEYS:
        return {'actor_ref': actor_id(value)}
    if type(value) is float and not math.isfinite(value):
        return {'nonfinite': str(value)}
    if value is None or type(value) in (bool, int, float):
        return value
    if isinstance(value, str):
        if key in _TEXT_KEYS or (key not in _SAFE_STRINGS and key != 'error'):
            return {'type': 'str', 'length': len(value)}
        if value.startswith('te:'):
            parts = value.split(':', 2)
            return 'te:[redacted]:' + (parts[2] if len(parts) > 2 else '')
        if value.startswith(('ticket-editor:', 'queue-selection:')):
            return value.split(':', 1)[0] + ':' + actor_id(value)
        text = redact(value)
        return text if len(text) <= 512 else {'value': text[:512], 'length': len(text), 'truncated': True}


def summary(value, key='', depth=0):
    """Bounded, side-effect-free values: no repr(), getters or arbitrary iterators."""
    key = str(key)
    if type(value) in (str, bool, int, float, type(None)):
        if type(value) is str and len(value) > 512:
            return _scalar_summary.__wrapped__(value, key)
        return _scalar_summary(value, key)
    if _SECRET_KEY.search(key) or key in _ENV_KEYS:
        return '[redacted]'
    if key in _IDENTITY_KEYS:
        return {'actor_ref': actor_id(value)}
    if isinstance(value, Path):
        return redact(str(value))
    if isinstance(value, _POPEN_CLASS):
        return {'type': 'Popen', 'pid': value.pid, 'returncode': value.returncode}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {'type': type(value).__name__, 'bytes': len(value)}
    if key in _LARGE_DETAILS and type(value) in (dict, list, tuple, str):
        return {'type': type(value).__name__, 'count': len(value)}
    if depth >= 1:
        return {'type': type(value).__name__, 'count': len(value) if type(value) in (dict, list, tuple, set) else None}
    if type(value) is dict:
        if key == 'state' and 'offset' in value:
            return {k: value[k] for k in ('offset', 'time', 'clock', 'ended', 'backlog', 'missing') if k in value}
        items = list(itertools.islice(value.items(), 32))
        result = {redact(str(k))[:128]: summary(v, str(k), depth + 1) for k, v in items}
        if len(value) > len(items):
            result['_truncated_items'] = len(value) - len(items)
        return result
    if type(value) in (list, tuple, set, frozenset):
        items = list(itertools.islice(iter(value), 12))
        if all(type(v) in (str, bool, int, float, type(None)) for v in items):
            return _collection_summary(key, tuple((type(v), v) for v in items), len(value), _secret_epoch)
        return {'count': len(value), 'items': [summary(v, key, depth + 1) for v in items],
                'truncated': len(value) > len(items)}
    return {'type': type(value).__name__}


def call_value(value, key, detailed):
    if key in _IDENTITY_KEYS:
        return summary(value, key)
    if type(value) is dict:
        if key == 'state' and 'offset' in value:
            return summary(value, key)
        if _SECRET_KEY.search(key) or key in _ENV_KEYS:
            return '[redacted]'
        items = value.items() if len(value) <= len(_IDENTIFIERS) else ((name, value[name]) for name in _IDENTIFIERS if name in value)
        return {'type': 'dict', 'count': len(value),
                'identity': {name: summary(item, name) for name, item in items if name in _IDENTIFIER_SET}}
    return summary(value, key)


@functools.lru_cache(maxsize=4096)
def _argument_summary(keys, values, epoch):
    return {key: summary(value, key) for key, value in zip(keys, values)}


@functools.lru_cache(maxsize=4096)
def _collection_summary(key, values, count, epoch):
    return {'count': count, 'items': [summary(v[1], key, 1) for v in values],
            'truncated': count > len(values)}


@functools.lru_cache(maxsize=4096)
def _caller_definition(module, code, line):
    return {'module': module,
            'function': code.co_name, 'line': line}


@functools.lru_cache(maxsize=4096)
def _trace_frame(code, line, epoch):
    return {'file': redact(code.co_filename), 'line': line, 'function': code.co_name}


def exception_info(exc):
    if exc is None:
        return None
    current = _current.get()
    identities = current['errors'] if current is not None else {}
    chain, seen = [], set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        identity = identities.get(id(exc))
        if identity is None:
            # Long-lived serve/CLI frames must not retain every past traceback.
            # A later observation after eviction gets a fresh, complete definition.
            if len(identities) >= 256:
                identities.clear()
            identity = (exc, f'{_sink.instance if _sink else ""}:error:{next(_counter)}')
            identities[id(exc)] = identity
        frames, tb = [], exc.__traceback__
        while tb is not None:
            frames.append(_trace_frame(tb.tb_frame.f_code, tb.tb_lineno, _secret_epoch))
            tb = tb.tb_next
        chain.append({'error_id': identity[1], 'type': type(exc).__name__, 'message': redact(str(exc))[:2048],
                      'frames': frames[-24:],
                      'sqlite_errorcode': getattr(exc, 'sqlite_errorcode', None),
                      'sqlite_errorname': getattr(exc, 'sqlite_errorname', None)})
        exc = exc.__cause__ or exc.__context__
    return chain


class _Sink:
    def __init__(self, directory, max_bytes, backups, component):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.pid = os.getpid()
        self.instance = f'{self.pid}-{time.time_ns():x}'
        self.path = self.directory / f'{component}-{self.instance}.jsonl'
        self.max_bytes, self.backups = max_bytes, backups
        self.lock = threading.RLock()
        self.stream = None
        self.size = 0
        self.records = 0
        self.bytes_written = 0
        self.errors = 0
        self.defined = set()
        self.function_codes = {}
        self.event_codes = _codec.EVENT_CODES
        self.flushed_errors = set()
        self.encoder = json.JSONEncoder(ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        self.header = None
        self.pending = []
        self.last_flush = time.monotonic()

    def _open(self):
        fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self.stream = os.fdopen(fd, 'w', encoding='utf-8', buffering=262144)
        if self.header:
            self.stream.write(self.encoder.encode(dict(self.header, ts_ns=time.time_ns())) + '\n')

    def write(self, record, flush=False):
        # Only log-output errors are isolated; business exceptions are untouched.
        try:
            with self.lock:
                self.pending.append(record)
                self.records += 1
                due = time.monotonic() - self.last_flush >= 1
                if len(self.pending) < 128 and not flush and not due:
                    return
                self._drain()
                if flush or due:
                    self.stream.flush()
                    self.last_flush = time.monotonic()
        except (OSError, ValueError, TypeError):
            self.errors += 1
            self.pending = []
            if self.errors == 1:
                print('CFD diagnostic log output failed; business processing continues.', file=sys.stderr)

    def _drain(self):
        if not self.pending:
            return
        line = self.encoder.encode(_codec.encode(self.pending, self.function_codes, self.event_codes)) + '\n'
        self.pending = []
        if self.stream is None:
            self._open()
        if self.size >= self.max_bytes:
            self.stream.close()
            for n in range(self.backups - 1, 0, -1):
                old = Path(str(self.path) + f'.{n}')
                if old.exists():
                    old.replace(str(self.path) + f'.{n + 1}')
            self.path.replace(str(self.path) + '.1')
            self._open()
            self.size = 0
        self.stream.write(line)
        written = len(line.encode('utf-8'))
        self.size += written
        self.bytes_written += written

    def flush(self):
        with self.lock:
            try:
                self._drain()
                if self.stream is not None:
                    self.stream.flush()
                self.last_flush = time.monotonic()
            except (OSError, ValueError, TypeError):
                self.errors += 1
                self.pending = []

    def close(self):
        with self.lock:
            self.flush()
            if self.stream is not None:
                try:
                    self.stream.close()
                except OSError:
                    self.errors += 1
                self.stream = None


def configure(config=None, *, state_dir=None, component=None, active=None):
    """Startup/config-reload setting; no watcher and no business-state writes."""
    global enabled, _sink, _settings, _component, _logging_handler
    config = config if isinstance(config, dict) else {}
    options = config.get('diagnostic_logging') or {}
    if not isinstance(options, dict):
        options = {}
    setting = options.get('enabled', False) if active is None else active
    if active is None and 'CFD_BOT_DIAGNOSTICS' in os.environ:
        setting = os.environ['CFD_BOT_DIAGNOSTICS'].lower() not in ('0', 'off', 'false')
    directory = options.get('directory')
    if not isinstance(directory, (str, Path)) or not directory:
        directory = os.environ.get('CFD_BOT_DIAGNOSTICS_DIR')
    state_base = state_dir or config.get('state_dir') or 'state'
    if not isinstance(state_base, (str, Path)):
        state_base = 'state'
    directory = directory or str(Path(state_base) / 'diagnostics')
    if not Path(directory).is_absolute() and config.get('_path'):
        directory = str(Path(config['_path']).parent / directory)
    component = component or os.environ.get('CFD_BOT_DIAGNOSTICS_COMPONENT', _component) or 'python'
    component = re.sub(r'[^A-Za-z0-9_.-]', '_', str(component))
    maximum = options.get('max_bytes', 20 * 1024 * 1024)
    backups = options.get('backups', 3)
    # Bad logging options do not become new business-configuration failures.
    maximum = maximum if type(maximum) is int and maximum > 0 else 20 * 1024 * 1024
    backups = backups if type(backups) is int and backups > 0 else 3
    signature = (bool(setting), str(directory), component, maximum, backups, os.getpid())
    telegram = config.get('telegram')
    token_env = telegram.get('token_env', 'TELEGRAM_BOT_TOKEN') if isinstance(telegram, dict) else 'TELEGRAM_BOT_TOKEN'
    token_env = token_env if isinstance(token_env, str) else 'TELEGRAM_BOT_TOKEN'
    remember_secret(os.environ.get(token_env))
    if signature == _settings and (not setting or _sink is not None):
        return
    close()
    _settings, _component = signature, component
    enabled = bool(setting)
    if not enabled:
        return
    try:
        _sink = _Sink(directory, maximum, backups, component)
    except OSError:
        enabled = False
        print('CFD diagnostic log directory unavailable.', file=sys.stderr)
        return
    warnings.showwarning = _showwarning
    threading.excepthook = _thread_exception
    sys.excepthook = _uncaught_exception
    if _logging_handler is None:
        _logging_handler = _LogHandler()
        logging.getLogger().addHandler(_logging_handler)
    manifest = Path(__file__).with_name('diagnostic_map.json')
    try:
        manifest_data = manifest.read_bytes()
        manifest_document = json.loads(manifest_data)
        map_entries = manifest_document.get('entries', [])
    except (OSError, ValueError):
        manifest_data, map_entries, manifest_document = b'{}', [], {}
    digest = hashlib.sha256(manifest_data).hexdigest()
    _step_numbers.clear()
    _step_numbers.update({entry['step']: index for index, entry in enumerate(map_entries) if 'step' in entry})
    _sequence_functions.clear()
    _sequence_functions.update(node['function'] for flow in manifest_document.get('flows', [])
                               for node in flow['nodes'] if 'function' in node)
    _sink.header = {'event': 'log.file', 'instance': _sink.instance, 'pid': os.getpid(),
                    'schema_version': 2, 'source_map_sha256': digest, 'component': _component,
                    'event_codes': _codec.EVENTS}
    functions = sorted({entry['function'] for entry in map_entries if 'function' in entry})
    try:
        codebook_bytes = Path(__file__).with_name('diagnostic_codes.json').read_bytes()
        codebook = json.loads(codebook_bytes)
        functions = codebook['function_codes']
        _sink.header['event_codes'] = codebook['event_codes']
        codebook_hash = hashlib.sha256(codebook_bytes).hexdigest()
        _sink.header['codebook_sha256'] = codebook_hash
        codebook_path = _sink.directory / ('codes-' + codebook_hash + '.json')
        if not codebook_path.exists():
            fd = os.open(str(codebook_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as output: output.write(codebook_bytes)
    except (OSError, ValueError, KeyError):
        pass
    _sink.function_codes = {name: index for index, name in enumerate(functions)}
    _sink.event_codes = {name: int(number) for number, name in _sink.header['event_codes'].items()}
    _sink.header['function_codes'] = functions
    try:
        archive = _sink.directory / ('sources-' + digest + '.json')
        fd = os.open(str(archive), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(manifest_data)
    except FileExistsError:
        pass
    except OSError:
        print('CFD diagnostic source map could not be archived.', file=sys.stderr)
    _write('service.start', {'schema_version': 2, 'python': sys.version.split()[0],
                            'source_map_sha256': digest, 'clock': 'Unix UTC nanoseconds',
                            'max_bytes': maximum, 'backups': backups,
                            'parent_call_id': os.environ.get('CFD_BOT_PARENT_CALL_ID'),
                            'inherited_trace_id': os.environ.get('CFD_BOT_TRACE_ID')}, flush=True)


def _write(event, fields, flush=False):
    if not enabled or _sink is None:
        return
    frame = _current.get()
    if flush and fields.get('exception'):
        error = fields['exception'][0].get('error_id')
        if error and error in _sink.flushed_errors:
            flush = False
        elif error:
            if len(_sink.flushed_errors) >= 1024: _sink.flushed_errors.clear()
            _sink.flushed_errors.add(error)
    record = (event, time.time_ns(), next(_counter), threading.get_ident(),
              frame['id'] if frame else None, frame['parent'] if frame else None,
              frame['trace'] if frame else None, _actor.get(), fields)
    _sink.write(record, flush=flush)


def trace_id():
    current = _current.get()
    return current['trace'] if current else ''


class _LogHandler(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)

    def emit(self, record):
        if enabled:
            _write('python.logging', {'level': record.levelname, 'logger': record.name,
                                      'message': redact(record.getMessage())[:2048],
                                      'exception': exception_info(record.exc_info[1]) if record.exc_info else None},
                   flush=True)


def event(name, /, **fields):
    if enabled:
        _write(name, {k: summary(v, k) for k, v in fields.items()})


def step(name, /, **fields):
    if enabled:
        sequence, timestamp = next(_counter), time.time_ns()
        values = {k: call_value(v, k, False) for k, v in fields.items()}
        if name.endswith(':except'):
            values['exception'] = exception_info(sys.exc_info()[1])
        current = _current.get()
        if current is not None and 'exception' not in values:
            # Lossless batching, including order/time of each branch. No sampling.
            data = [_step_numbers.get(name, name), sequence, timestamp]
            if values:
                data.append(values)
            current['steps'].append(data)
            if len(current['steps']) >= 64:
                _write('sequence.steps', {'steps': current['steps']})
                current['steps'] = []
        else:
            _write('sequence.step', dict(values, sequence_step_id=name, seq=sequence, ts_ns=timestamp),
                   flush='exception' in values)


def _showwarning(message, category, filename, lineno, file=None, line=None):
    if enabled:
        _write('python.warning', {'category': category.__name__, 'message': redact(message),
                                  'file': redact(filename), 'line': lineno}, flush=True)
    return _warning_handler(message, category, filename, lineno, file, line)


def _thread_exception(args):
    if enabled:
        _write('thread.exception', {'exception': exception_info(args.exc_value),
                                    'thread_name': args.thread.name if args.thread else None}, flush=True)
    return _thread_exception_handler(args)


def _uncaught_exception(kind, exc, tb):
    if enabled:
        _write('python.uncaught', {'exception': exception_info(exc)}, flush=True)
    return _exception_handler(kind, exc, tb)


def component(name):
    global _component
    _component = name


def configure_path(path, component_name):
    """Logging-only startup read for entry points which do not load bot config."""
    path = Path(path)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data['_path'] = str(path.resolve())
    configure(data, component=component_name)


def _bootstrap(argv):
    path = 'bot.json'
    arguments = argv if isinstance(argv, (list, tuple)) else sys.argv[1:]
    for index, argument in enumerate(arguments):
        if isinstance(argument, str) and argument.startswith('--config='):
            path = argument.split('=', 1)[1]
        elif argument == '--config' and index + 1 < len(arguments):
            path = arguments[index + 1]
    # This is logging initialization only; argparse/load_bot still own all errors.
    try:
        configure_path(path, 'cli')
    except (OSError, ValueError, TypeError, AttributeError):
        configure(active=False)


def inherit_context(function):
    if not enabled:
        return function
    context = contextvars.copy_context()
    @functools.wraps(function)
    def run(*args, **kwargs):
        return context.run(function, *args, **kwargs)
    return run


def flush():
    if _sink is not None:
        _sink.flush()


def close():
    global enabled, _sink
    if _sink is not None:
        _write('log.closed', {'reason': 'reconfigure_or_exit'}, flush=True)
        _sink.close()
    _sink = None
    enabled = False


def settings():
    return {'enabled': enabled, 'directory': str(_sink.directory) if _sink else None,
            'instance': _sink.instance if _sink else None}


def child_environment(env=None):
    """Use on existing spawn calls only. Do not mutate the process environment."""
    result = dict(os.environ if env is None else env)
    result['CFD_BOT_DIAGNOSTICS'] = '1' if enabled else '0'
    if _sink is not None:
        result['CFD_BOT_DIAGNOSTICS_DIR'] = str(_sink.directory)
    current = _current.get()
    if current:
        result['CFD_BOT_TRACE_ID'] = current['trace']
        result['CFD_BOT_PARENT_CALL_ID'] = current['id']
    return result


def run_process(*args, **kwargs):
    kwargs['env'] = child_environment(kwargs.get('env'))
    if not enabled:
        return subprocess.run(*args, **kwargs)
    started = time.perf_counter_ns()
    command = args[0] if args else kwargs.get('args')
    event('subprocess.request', command=command, timeout=kwargs.get('timeout'),
          root=kwargs.get('cwd'))
    try:
        result = subprocess.run(*args, **kwargs)
    except BaseException as exc:
        _write('subprocess.error', {'exception': exception_info(exc),
                                    'duration_ns': time.perf_counter_ns()-started}, flush=True)
        raise
    event('subprocess.response', returncode=result.returncode, duration_ns=time.perf_counter_ns()-started,
          stdout=result.stdout, stderr=result.stderr)
    if result.stderr:
        text = result.stderr.decode(errors='replace') if isinstance(result.stderr, bytes) else result.stderr
        _write('subprocess.stderr', {'excerpt': redact(text[-2048:]), 'truncated': len(text) > 2048})
    return result


def spawn_process(*args, **kwargs):
    kwargs['env'] = child_environment(kwargs.get('env'))
    if not enabled:
        return subprocess.Popen(*args, **kwargs)
    command = args[0] if args else kwargs.get('args')
    event('subprocess.spawn', command=command, root=kwargs.get('cwd'))
    try:
        child = subprocess.Popen(*args, **kwargs)
    except BaseException as exc:
        _write('subprocess.error', {'exception': exception_info(exc)}, flush=True)
        raise
    event('subprocess.started', pid=child.pid, command=command)
    return child


def signal_process(pid, signum):
    if enabled:
        event('process.signal.request', pid=pid, signal=getattr(signum, 'name', str(signum)))
        flush()
    return os.killpg(pid, signum)


class _Call:
    def __init__(self, function, definition, args, kwargs):
        parent = _current.get()
        self.id = f'{os.getpid()}:{next(_counter)}'
        self.parent = parent['id'] if parent else os.environ.get('CFD_BOT_PARENT_CALL_ID')
        self.trace = parent['trace'] if parent else os.environ.get('CFD_BOT_TRACE_ID', f'{_sink.instance if _sink else ""}:{self.id}')
        self.frame = {'id': self.id, 'parent': self.parent, 'trace': self.trace, 'steps': [],
                      'errors': parent['errors'] if parent else {}}
        self.definition = definition
        self.detailed = definition['function'].replace('.<locals>.', '.') in _sequence_functions
        self.start = time.perf_counter_ns()
        self.token = None
        self.actor_token = None
        self.args, self.kwargs = args, kwargs
        self.values = None
        if definition['function'] in ('bot.Bot.handle', 'ticket_chat.TicketChat.handle',
                                       'web.Handler.do_GET', 'web.Handler.do_POST'):
            names = definition['parameters']
            self.values = {names[i] if i < len(names) else f'arg{i}': v for i, v in enumerate(args)}
            self.values.update(kwargs)
        values = self.values
        caller = sys._getframe(2)
        self.caller = _caller_definition(caller.f_globals.get('__name__'), caller.f_code, caller.f_lineno)
        self.request_root = (definition['function'] in ('bot.Bot.handle', 'bot.deliver',
                             'monitor.Monitor.run_once', 'web.Handler.do_GET', 'web.Handler.do_POST')
                             or str(caller.f_globals.get('__name__')).startswith('tkinter'))
        self.parent_trace = parent['trace'] if parent else None
        if self.request_root:
            self.trace = f'{_sink.instance if _sink else ""}:{self.id}'
            self.frame['trace'] = self.trace
            self.frame['errors'] = {}
        # Actor attribution is derived from existing request data, never auth logic.
        if definition['function'] in ('bot.Bot.handle', 'ticket_chat.TicketChat.handle'):
            update = values.get('update', {})
            message = update.get('callback_query') or update.get('message') or {}
            who = message.get('from', {}).get('id')
            if who is not None:
                self.actor_token = _actor.set({'type': 'user', 'platform': 'telegram', 'id': actor_id(who)})
        elif definition['function'] in ('web.Handler.do_GET', 'web.Handler.do_POST'):
            self.actor_token = _actor.set({'type': 'user', 'platform': 'web'})
        elif definition['function'].startswith('gui.') and (_actor.get() or {}).get('platform') != 'gui':
            self.actor_token = _actor.set({'type': 'user', 'platform': 'gui'})
        elif parent is None and _actor.get() is None:
            self.actor_token = _actor.set({'type': 'process', 'platform': _component})

    def enter(self):
        self.token = _current.set(self.frame)

    def leave(self):
        _current.reset(self.token)

    def begin(self):
        key = self.definition['function']
        if _sink is not None and key not in _sink.defined:
            _sink.defined.add(key)
            _write('function.definition', {k:v for k,v in self.definition.items() if not k.startswith('_')})
        args = self.args[self.definition['_skip']:]
        names = self.definition['_input_names']
        if not self.kwargs and len(args) <= len(names) and all(type(v) in (str, type(None)) for v in args):
            inputs = _argument_summary(names[:len(args)], args, _secret_epoch)
        else:
            raw = {names[i] if i < len(names) else f'arg{i+self.definition["_skip"]}': v for i, v in enumerate(args)}
            raw.update({k: v for k, v in self.kwargs.items() if k not in ('self', 'cls')})
            inputs = {k: call_value(v, k, self.detailed) for k, v in raw.items()}
        _write('function.call', {'function': key,
                                 'caller': self.caller,
                                 'parent_trace_id': self.parent_trace if self.request_root else None,
                                 'input': inputs})
        if key in ('bot.Bot.handle', 'ticket_chat.TicketChat.handle'):
            update = self.values.get('update', {})
            callback = update.get('callback_query') or {}
            message = update.get('message') or {}
            text = message.get('text') or ''
            event('ui.telegram.received' if key == 'bot.Bot.handle' else 'ui.telegram.routed', update_id=update.get('update_id'),
                  action=callback.get('data') or (text.split(None, 1)[0] if text.startswith('/') else 'text.input'),
                  callback_id=callback.get('id'), text_length=len(text))
        elif key in ('web.Handler.do_GET', 'web.Handler.do_POST'):
            handler = self.values['self']
            event('http.request', method=handler.command, path=handler.path.split('?', 1)[0])

    def finish(self, result=None, exc=None, cancelled=False):
        data = {'function': self.definition['function'],
                'duration_ns': time.perf_counter_ns() - self.start}
        if self.frame['steps']:
            data['steps'] = self.frame['steps']
        if exc is not None:
            data['exception'] = exception_info(exc)
        else:
            data['result'] = call_value(result, 'result', self.detailed)
        _write('function.raise' if exc is not None else 'function.cancelled' if cancelled else 'function.return',
               data, flush=exc is not None)
        if self.actor_token is not None:
            _actor.reset(self.actor_token)
        if self.parent is None or self.request_root:
            flush()
            # Exception objects retain traceback frames; never retain them past a request.
            self.frame['errors'].clear()


def trace(function):
    """Record real calls and preserve return values and exception identities."""
    unwrapped = inspect.unwrap(function)
    code = getattr(unwrapped, '__code__', None)
    name = (function.__module__.removeprefix('cfd_bot.') + '.' + function.__qualname__).replace('.<locals>.', '.')
    definition = {'function': name, 'file': Path(code.co_filename).name if code else '',
                  'line': code.co_firstlineno if code else 0,
                  'parameters': list(inspect.signature(function).parameters)}
    definition['_skip'] = int(bool(definition['parameters']) and definition['parameters'][0] in ('self', 'cls'))
    definition['_input_names'] = tuple(definition['parameters'][definition['_skip']:])
    _definitions[name] = definition

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        if name == 'cli.main' and _settings is None:
            _bootstrap(args[0] if args else kwargs.get('argv'))
        if not enabled:
            return function(*args, **kwargs)
        call = _Call(function, definition, args, kwargs)
        call.enter()
        call.begin()
        try:
            result = function(*args, **kwargs)
        except BaseException as exc:
            call.finish(exc=exc)
            raise
        else:
            call.finish(result)
            return result
        finally:
            call.leave()

    @functools.wraps(function)
    def generated(*args, **kwargs):
        if not enabled:
            return (yield from function(*args, **kwargs))
        call = _Call(function, definition, args, kwargs)
        generator = function(*args, **kwargs)
        send, failure, first = None, None, True
        while True:
            call.enter()
            try:
                if first:
                    call.begin()
                    first = False
                value = generator.throw(*failure) if failure else generator.send(send)
            except StopIteration as stop:
                call.finish(stop.value)
                return stop.value
            except BaseException as exc:
                call.finish(exc=exc)
                raise
            finally:
                call.leave()
            try:
                send = yield value
                failure = None
            except GeneratorExit:
                call.enter()
                try:
                    generator.close()
                    call.finish(cancelled=True)
                finally:
                    call.leave()
                raise
            except BaseException:
                failure = sys.exc_info()

    result = generated if inspect.isgeneratorfunction(function) else wrapped
    for attribute in ('cache_clear', 'cache_info', 'cache_parameters'):
        if hasattr(function, attribute):
            setattr(result, attribute, getattr(function, attribute))
    return result


def callback(function, name):
    function.__qualname__ = name
    return trace(function)


class _Connection(sqlite3.Connection):
    """Observe the existing SQLite calls; never add SQL or change transactions."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._diagnostic_id = f'db-{os.getpid()}-{next(_counter)}'
        self._diagnostic_acquired = None

    def _execute(self, method, sql, parameters=()):
        if not enabled:
            return method(sql, parameters) if method.__name__ != 'executescript' else method(sql)
        started = time.perf_counter_ns()
        changes = self.total_changes
        operation = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ''
        data = {'connection_id': self._diagnostic_id, 'operation': operation,
                'query_id': hashlib.sha256(sql.encode()).hexdigest()[:16]}
        _write('db.statement.start', data)
        try:
            result = method(sql, parameters) if method.__name__ != 'executescript' else method(sql)
        except BaseException as exc:
            _write('db.statement.error', dict(data, duration_ns=time.perf_counter_ns()-started,
                                               exception=exception_info(exc)), flush=True)
            raise
        if self.in_transaction and self._diagnostic_acquired is None:
            self._diagnostic_acquired = time.perf_counter_ns()
            _write('db.transaction.acquired', dict(data, wait_ns=self._diagnostic_acquired-started))
        _write('db.statement.end', dict(data, duration_ns=time.perf_counter_ns()-started,
                                         rowcount=result.rowcount, changed_rows=self.total_changes-changes))
        return result

    def execute(self, sql, parameters=()):
        return self._execute(super().execute, sql, parameters)

    def executemany(self, sql, parameters):
        return self._execute(super().executemany, sql, parameters)

    def executescript(self, sql):
        return self._execute(super().executescript, sql)

    def __exit__(self, kind, value, tb):
        started = time.perf_counter_ns() if enabled else 0
        acquired = self._diagnostic_acquired
        was_transaction = self.in_transaction if enabled else False
        try:
            result = super().__exit__(kind, value, tb)
        except BaseException as exc:
            if enabled:
                _write('db.transaction.error', {'connection_id': self._diagnostic_id,
                                                'exception': exception_info(exc)}, flush=True)
            raise
        else:
            if enabled:
                _write(('db.transaction.rollback' if kind else 'db.transaction.commit') if was_transaction else 'db.context.exit',
                       {'connection_id': self._diagnostic_id,
                        'duration_ns': time.perf_counter_ns()-started,
                        'held_ns': time.perf_counter_ns()-acquired if acquired else None})
            return result
        finally:
            self._diagnostic_acquired = None


def connect_sqlite(*args, **kwargs):
    if enabled:
        kwargs['factory'] = _Connection
    return sqlite3.connect(*args, **kwargs)


atexit.register(close)


def read_records(path):
    """Losslessly expand record batches, including files produced before batching."""
    header = {}
    with Path(path).open() as stream:
        for line in stream:
            record = json.loads(line)
            if record.get('event') in ('log.file', 'browser.export', 'shell.file'):
                header = record
            if record.get('event') == 'browser.batch.v2':
                for row in record['records']:
                    code, delta, seq, call, parent = row[:5]
                    item = dict(event=header['event_codes'][str(code)], ts_ms=header['utc_origin_ms']+delta,
                                mono_ms=header['mono_origin_ms']+delta, session=header['session'],
                                seq=seq, call_id=call, parent_call_id=parent)
                    if code == 1:
                        item.update(function=header['function_codes'][row[5]], input=record['values'][row[6]], interaction_id=row[7])
                    elif code in (2, 3):
                        item.update(function=header['function_codes'][row[5]], duration_ms=row[6], result=record['values'][row[7]])
                    else: item['data'] = record['values'][row[5]]
                    yield item
                continue
            if record.get('event') == 'shell.record.v2':
                code, stamp, pid, parent, call, function, line, status, detail = record['v']
                yield dict(event=header['event_codes'].get(str(code),code), ts_seconds=stamp, pid=pid,
                           parent_call_id=parent,call_id=call,trace_id=header.get('trace_id'),
                           function=header['function_codes'][function] if isinstance(function,int) else function,
                           line=line,status=status,detail=detail)
                continue
            if record.get('event') == 'log.batch.v2':
                yield from _codec.decode(record, header)
                continue
            if record.get('event') != 'log.batch':
                yield record
                continue
            for event, stamp, sequence, context, call, parent, fields in record['records']:
                item = {key: header[key] for key in ('instance', 'pid', 'component') if key in header}
                item.update(record['contexts'][context])
                item.update(event=event, ts_ns=stamp, seq=sequence, call_id=call, parent_call_id=parent)
                item.update(fields)
                yield item


def dump_records():
    """Expand compact branch batches; this command never loads bot configuration."""
    import argparse
    parser = argparse.ArgumentParser(description='Expand diagnostic JSONL branch records for retrospective analysis')
    parser.add_argument('files', nargs='+', type=Path)
    parser.add_argument('--trace')
    parser.add_argument('--map', type=Path, default=Path(__file__).with_name('diagnostic_map.json'))
    args = parser.parse_args()
    raw = args.map.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    entries = json.loads(raw)['entries']
    for path in args.files:
        matching = True
        for record in read_records(path):
            if record.get('event') == 'log.file':
                matching = record.get('source_map_sha256') == digest
            if args.trace and record.get('trace_id') != args.trace:
                continue
            steps = record.pop('steps', [])
            for packed in steps:
                index, sequence, stamp = packed[:3]
                name = entries[index]['step'] if isinstance(index, int) and matching else index
                fields = packed[3] if len(packed) > 3 else {}
                output = {k: record[k] for k in ('instance', 'pid', 'thread', 'call_id', 'parent_call_id', 'trace_id', 'initiator') if k in record}
                output.update(fields, event='sequence.step', sequence_step_id=name, seq=sequence, ts_ns=stamp)
                print(json.dumps(output, ensure_ascii=False))
            print(json.dumps(record, ensure_ascii=False))


if __name__ == '__main__':
    dump_records()
