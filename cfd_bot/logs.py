"""Incremental OpenFOAM log parsing, including prefixed multi-region output."""
from . import diagnostics as _diagnostics
import math
import re
from statistics import median
from pathlib import Path

from .control import control_times

RATE_POINTS = 21  # At most the latest 20 measured progress intervals.
LIVE_BACKLOG_TOLERANCE = 64 * 1024
NUM = r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?'
TIME = re.compile(r'^\s*(?:\[\d+\]\s*)?Time\s*=\s*(' + NUM + r')\s*s?\s*$')
RESIDUAL = re.compile(r'Solving for ([\w.]+),\s*Initial residual = (' + NUM +
                      r'),\s*Final residual = (' + NUM + r'),\s*No Iterations (\d+)')
CLOCK = re.compile(r'ExecutionTime\s*=\s*(' + NUM + r')\s*s\s+ClockTime\s*=\s*(' + NUM + r')')
FATAL = re.compile(r'FOAM FATAL|MPI_ABORT|MPI_Abort|Segmentation fault|Floating point exception|'
                   r'\bSIG(?:SEGV|FPE|ABRT)\b|std::bad_alloc|Killed|Out of memory|'
                   r'^\s*(?:ERROR|FAILED):', re.I)


@_diagnostics.trace
def fresh_state():
    return dict(offset=0, partial='', residuals={}, history=[], errors=[],
                rate_samples=[], success_matches={}, ended=False)


@_diagnostics.trace
def start_cursor(path):
    """Record existing bytes so a previous run's End cannot complete a new run."""
    state = fresh_state()
    try:
        with Path(path).open('rb') as f:
            stat = Path(path).stat()
            state.update(inode=[stat.st_dev, stat.st_ino], offset=stat.st_size)
            f.seek(max(0, stat.st_size - 128))
            state['checkpoint'] = f.read(128).hex()
    except FileNotFoundError:
        if _diagnostics.enabled: _diagnostics.step('logs.start_cursor:L35:except')
        pass
    return state


@_diagnostics.trace
def feed(state, line, watcher=None):
    if not line.strip():
        if _diagnostics.detailed: _diagnostics.step('logs.feed:L41:then')
        return
    progress = watcher.get('progress', {}) if watcher else {}
    if progress.get('pattern'):
        if _diagnostics.detailed: _diagnostics.step('logs.feed:L44:then')
        m = re.search(progress['pattern'], line)
        value_text = m.group(progress.get('group', 'value')) if m else None
    else:
        if _diagnostics.detailed: _diagnostics.step('logs.feed:L44:else')
        m = TIME.search(line)
        value_text = m[1] if m else None
    if m:
        if _diagnostics.detailed: _diagnostics.step('logs.feed:L50:then')
        value = float(value_text)
        if math.isfinite(value):
            if _diagnostics.detailed: _diagnostics.step('logs.feed:L52:then')
            if state.get('ended'):
                if _diagnostics.detailed: _diagnostics.step('logs.feed:L53:then')
                state['rate_samples'] = []
                # A later Time record starts another solver stage.  Do not let
                # the previous stage's End match immediately re-finish every
                # line in the new stage and clear its rate samples forever.
                state['success_matches'] = {}
            # A restarted solver in the same log begins a new segment.
            if 'time' in state and value < state['time']:
                if _diagnostics.detailed: _diagnostics.step('logs.feed:L60:then')
                state.pop('first_time', None)
                state['history'] = []
                state['residuals'] = {}
                state['errors'] = []
                state['success_matches'] = {}
                state['rate_samples'] = []
                state.pop('clock', None)
            state['time'] = value
            state.setdefault('first_time', value)
            state['ended'] = False
    m = RESIDUAL.search(line)
    if m:
        if _diagnostics.detailed: _diagnostics.step('logs.feed:L72:then')
        values = [float(m[2]), float(m[3])]
        if all(math.isfinite(v) for v in values):
            if _diagnostics.detailed: _diagnostics.step('logs.feed:L74:then')
            prefix = line[:m.start()].strip().split()
            region = prefix[0] if len(prefix) > 1 and not prefix[0].endswith(':') else ''
            field = f'{region}/{m[1]}' if region else m[1]
            state['residuals'][field] = dict(initial=values[0], final=values[1], iterations=int(m[4]))
            state['history'].append([state.get('time'), field, values[0], values[1]])
            state['history'] = state['history'][-2000:]
    m = CLOCK.search(line)
    if m:
        if _diagnostics.detailed: _diagnostics.step('logs.feed:L82:then')
        cpu, wall = float(m[1]), float(m[2])
        if math.isfinite(cpu) and math.isfinite(wall):
            if _diagnostics.detailed: _diagnostics.step('logs.feed:L84:then')
            samples = list(state.get('rate_samples', []))
            if wall < state.get('clock', wall):
                if _diagnostics.detailed: _diagnostics.step('logs.feed:L86:then')
                samples = []
            current = state.get('time')
            if current is not None:
                if _diagnostics.detailed: _diagnostics.step('logs.feed:L89:then')
                if samples and current < samples[-1][0]:
                    if _diagnostics.detailed: _diagnostics.step('logs.feed:L90:then')
                    samples = []
                if samples and current == samples[-1][0]:
                    if _diagnostics.detailed: _diagnostics.step('logs.feed:L92:then')
                    samples[-1] = [current, wall]
                elif not samples or wall > samples[-1][1]:
                    if _diagnostics.detailed: _diagnostics.step('logs.feed:L94:then')
                    samples.append([current, wall])
            state['rate_samples'] = samples[-RATE_POINTS:]
            state.update(execution=cpu, clock=wall)
    success = watcher.get('success', {}) if watcher else {}
    success_patterns = success.get('patterns', [])
    if success_patterns:
        if _diagnostics.detailed: _diagnostics.step('logs.feed:L100:then')
        for pattern in success_patterns:
            if _diagnostics.detailed: _diagnostics.step('logs.feed:L101:loop', pattern=pattern)
            if re.search(pattern, line):
                if _diagnostics.detailed: _diagnostics.step('logs.feed:L102:then')
                state['success_matches'][pattern] = line[-500:]
        matched = sum(pattern in state['success_matches'] for pattern in success_patterns)
        state['ended'] = (matched == len(success_patterns) if success.get('match', 'all') == 'all'
                          else matched > 0)
    elif not watcher and re.fullmatch(r'\s*(?:\[\d+\]\s*)?End\s*', line):
        if _diagnostics.detailed: _diagnostics.step('logs.feed:L107:then')
        state['ended'] = True
    failure = watcher.get('failure', {}) if watcher else {}
    custom_failure = any(re.search(pattern, line)
                         for pattern in failure.get('patterns', []))
    default_failure = failure.get('include_openfoam_defaults', True) and FATAL.search(line)
    if ((default_failure and 'floating point exception trapping' not in line.lower())
            or custom_failure):
        if _diagnostics.detailed: _diagnostics.step('logs.feed:L113:then', error=line[-500:])
        if _diagnostics.enabled:
            _diagnostics.event('solver.output.error', error=line[-500:], state=state)
        state['errors'] = (state['errors'] + [line[-500:]])[-12:]
        state['ended'] = False
    state['tail'] = (state.get('tail', []) + [line[-500:]])[-15:]


@_diagnostics.trace
def read_log(path, state=None, limit=2 * 1024 * 1024, final=False,
             watcher=None):
    state = dict(state) if state else fresh_state()
    path = Path(path)
    try:
        with path.open('rb') as f:
            stat = path.stat()
            inode = [stat.st_dev, stat.st_ino]
            reset = state.get('inode', inode) != inode or stat.st_size < state['offset']
            if not reset and state.get('checkpoint'):
                if _diagnostics.detailed: _diagnostics.step('logs.read_log:L129:then')
                f.seek(max(0, state['offset'] - 128))
                reset = f.read(min(128, state['offset'])).hex() != state['checkpoint']
            if reset:
                if _diagnostics.detailed: _diagnostics.step('logs.read_log:L132:then')
                state = fresh_state()
            state['inode'] = inode
            f.seek(state['offset'])
            data = f.read(limit)
            state['offset'] = f.tell()
            state['backlog'] = max(0, stat.st_size - state['offset'])
            f.seek(max(0, state['offset'] - 128))
            state['checkpoint'] = f.read(min(128, state['offset'])).hex()
    except FileNotFoundError:
        if _diagnostics.enabled: _diagnostics.step('logs.read_log:L141:except')
        state['missing'] = True
        return state
    state.pop('missing', None)
    lines = (state.get('partial', '') + data.decode('utf-8', errors='replace')).split('\n')
    state['partial'] = lines.pop()
    for line in lines:
        if _diagnostics.detailed: _diagnostics.step('logs.read_log:L147:loop', line=line)
        if _diagnostics.enabled and 'warning' in line.lower():
            _diagnostics.event('solver.output.warning', log_path=path, error=line[-500:])
        feed(state, line, watcher)
    if final and not state['backlog'] and state['partial']:
        if _diagnostics.detailed: _diagnostics.step('logs.read_log:L149:then')
        feed(state, state.pop('partial'), watcher)
        state['partial'] = ''
    return state


@_diagnostics.trace
def finish_log(path, state=None, watcher=None):
    state = read_log(path, state, final=True, watcher=watcher)
    while state.get('backlog', 0):
        if _diagnostics.detailed: _diagnostics.step('logs.finish_log:L157:loop')
        state = read_log(path, state, final=True, watcher=watcher)
    return state


@_diagnostics.trace
def recent_log(path, state=None, final=False, window=2 * 1024 * 1024,
               watcher=None):
    """Catch up external/previous logs from their tail without rereading hundreds of MB.

    Keep the opening simulation time for progress display; intermediate residual
    samples and errors in skipped bytes cannot be recovered by this fast path.
    Managed workers use read_log/finish_log and never skip bytes.
    """
    path = Path(path)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        if _diagnostics.enabled: _diagnostics.step('logs.recent_log:L173:except')
        return read_log(path, state, final=final, watcher=watcher)
    if not state:
        if _diagnostics.detailed: _diagnostics.step('logs.recent_log:L175:then')
        state = read_log(path, limit=min(window, 128 * 1024), watcher=watcher)
    if size - state['offset'] > window:
        if _diagnostics.detailed: _diagnostics.step('logs.recent_log:L177:then')
        state = dict(state)
        with path.open('rb') as f:
            f.seek(size - window)
            f.readline()  # start at the first complete line in the window
            offset = f.tell()
        state['skipped_bytes'] = state.get('skipped_bytes', 0) + offset - state['offset']
        state.update(offset=offset, partial='', checkpoint='', history=[], rate_samples=[],
                     residuals={}, errors=[], success_matches={}, ended=False)
        state.pop('time', None)
        state.pop('clock', None)
    return read_log(path, state, limit=window, final=final, watcher=watcher)


@_diagnostics.trace
def case_logs(case):
    """Return the validated log candidates declared by a case watcher."""
    root = Path(case['_root'])
    names = case.get('watcher', {}).get('logs') or [case['log']]
    return [root / name for name in names]


@_diagnostics.trace
def select_case_log(case, *, changed_from=None):
    """Select the newest existing stage log, optionally only if it changed.

    Multi-stage Allrun scripts often write one log per stage. The currently
    active stage is the candidate with the newest modification time.
    """
    candidates = []
    for path in case_logs(case):
        if _diagnostics.detailed: _diagnostics.step('logs.select_case_log:L205:loop', path=path)
        try:
            mtime = path.stat().st_mtime_ns
        except FileNotFoundError:
            if _diagnostics.enabled: _diagnostics.step('logs.select_case_log:L208:except')
            continue
        if changed_from is not None and changed_from.get(str(path)) == mtime:
            if _diagnostics.detailed: _diagnostics.step('logs.select_case_log:L210:then')
            continue
        candidates.append((mtime, str(path), path))
    return max(candidates)[2] if candidates else None


@_diagnostics.trace
def recent_case_log(case, state=None, *, final=False):
    """Read the newest stage log and reset the cursor when stages change."""
    path = select_case_log(case) or case_logs(case)[0]
    if state and (state.get('log_path') != str(path) or 'rate_samples' not in state):
        if _diagnostics.detailed: _diagnostics.step('logs.recent_case_log:L219:then')
        state = None
    state = recent_log(path, state, final=final, watcher=case['watcher'])
    state['log_path'] = str(path)
    return state, path


@_diagnostics.trace
def estimate(case, telemetry, elapsed, history=None):
    """Honor a configured duration; otherwise predict from recent log speed."""
    result = {'remaining_seconds': None, 'progress': None, 'basis': 'unknown'}
    expected = case.get('expected_seconds')
    if (isinstance(expected, (int, float)) and not isinstance(expected, bool)
            and math.isfinite(expected) and expected >= 1):
        if _diagnostics.detailed: _diagnostics.step('logs.estimate:L230:then')
        remaining = expected - max(0, elapsed)
        result.update(remaining_seconds=remaining if remaining > 0 else None,
                      expected_seconds=expected,
                      basis='configured_seconds' if remaining > 0 else 'configured_overrun')
        return result
    # A solver can append another partial record between stat() and read().  A
    # small tail does not invalidate the complete Time/ClockTime pairs already
    # parsed.  Keep rejecting a genuinely lagging cursor so old samples are not
    # presented as a current live rate.
    if (telemetry.get('missing')
            or telemetry.get('backlog', 0) > LIVE_BACKLOG_TOLERANCE):
        if _diagnostics.detailed: _diagnostics.step('logs.estimate:L241:then')
        telemetry = {}
    control = control_times(case)
    if control and control['stop_at'] != 'endTime':
        if _diagnostics.detailed: _diagnostics.step('logs.estimate:L245:then')
        result['basis'] = 'stop_requested'
        return result
    if control:
        if _diagnostics.detailed: _diagnostics.step('logs.estimate:L248:then')
        target = control['end']
        current = telemetry.get('time')
        result['target'] = target
        if case.get('end_time') is not None:
            if _diagnostics.detailed: _diagnostics.step('logs.estimate:L252:then')
            result['target_source'] = 'ticket'
        if isinstance(current, (int, float)) and math.isfinite(current):
            if _diagnostics.detailed: _diagnostics.step('logs.estimate:L254:then')
            origin = max(control['start'], telemetry.get('first_time', control['start']))
            if target > origin:
                if _diagnostics.detailed: _diagnostics.step('logs.estimate:L256:then')
                result['progress'] = max(0, min(1, (current - origin) / (target - origin)))
            if current >= target:
                if _diagnostics.detailed: _diagnostics.step('logs.estimate:L258:then')
                result.update(basis='target_reached', progress=1.0)
                return result
        samples = telemetry.get('rate_samples', [])[-RATE_POINTS:]
        if len(samples) >= 2:
            if _diagnostics.detailed: _diagnostics.step('logs.estimate:L262:then')
            first, last = samples[0], samples[-1]
            delta_time = last[0] - first[0]
            delta_clock = last[1] - first[1]
            if (delta_time > 0 and delta_clock > 0 and target > last[0]
                    and math.isfinite(delta_time) and math.isfinite(delta_clock)):
                if _diagnostics.detailed: _diagnostics.step('logs.estimate:L266:then')
                seconds_per_unit = delta_clock / delta_time
                remaining = (target - last[0]) * seconds_per_unit
                if math.isfinite(remaining):
                    if _diagnostics.detailed: _diagnostics.step('logs.estimate:L270:then')
                    result.update(remaining_seconds=remaining, basis='recent_log_rate',
                                  seconds_per_unit=seconds_per_unit, intervals=len(samples) - 1,
                                  measured_from=first[0], measured_to=last[0])
                    return result
    durations = [sample['seconds'] for sample in (history or [])[:5]
                 if isinstance(sample.get('seconds'), (int, float))
                 and math.isfinite(sample['seconds']) and sample['seconds'] > 0]
    if durations:
        if _diagnostics.detailed: _diagnostics.step('logs.estimate:L278:then')
        expected = median(durations)
        remaining = expected - max(0, elapsed)
        result.update(remaining_seconds=remaining if remaining > 0 else None,
                      expected_seconds=expected, samples=len(durations),
                      basis='recent_successes' if remaining > 0 else 'history_overrun')
    return result
