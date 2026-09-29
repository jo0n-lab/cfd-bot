"""Incremental OpenFOAM log parsing, including prefixed multi-region output."""
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


def fresh_state():
    return dict(offset=0, partial='', residuals={}, history=[], errors=[],
                rate_samples=[], success_matches={}, ended=False)


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
        pass
    return state


def feed(state, line, watcher=None):
    if not line.strip():
        return
    progress = watcher.get('progress', {}) if watcher else {}
    if progress.get('pattern'):
        m = re.search(progress['pattern'], line)
        value_text = m.group(progress.get('group', 'value')) if m else None
    else:
        m = TIME.search(line)
        value_text = m[1] if m else None
    if m:
        value = float(value_text)
        if math.isfinite(value):
            if state.get('ended'):
                state['rate_samples'] = []
                # A later Time record starts another solver stage.  Do not let
                # the previous stage's End match immediately re-finish every
                # line in the new stage and clear its rate samples forever.
                state['success_matches'] = {}
            # A restarted solver in the same log begins a new segment.
            if 'time' in state and value < state['time']:
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
        values = [float(m[2]), float(m[3])]
        if all(math.isfinite(v) for v in values):
            prefix = line[:m.start()].strip().split()
            region = prefix[0] if len(prefix) > 1 and not prefix[0].endswith(':') else ''
            field = f'{region}/{m[1]}' if region else m[1]
            state['residuals'][field] = dict(initial=values[0], final=values[1], iterations=int(m[4]))
            state['history'].append([state.get('time'), field, values[0], values[1]])
            state['history'] = state['history'][-2000:]
    m = CLOCK.search(line)
    if m:
        cpu, wall = float(m[1]), float(m[2])
        if math.isfinite(cpu) and math.isfinite(wall):
            samples = list(state.get('rate_samples', []))
            if wall < state.get('clock', wall):
                samples = []
            current = state.get('time')
            if current is not None:
                if samples and current < samples[-1][0]:
                    samples = []
                if samples and current == samples[-1][0]:
                    samples[-1] = [current, wall]
                elif not samples or wall > samples[-1][1]:
                    samples.append([current, wall])
            state['rate_samples'] = samples[-RATE_POINTS:]
            state.update(execution=cpu, clock=wall)
    success = watcher.get('success', {}) if watcher else {}
    success_patterns = success.get('patterns', [])
    if success_patterns:
        for pattern in success_patterns:
            if re.search(pattern, line):
                state['success_matches'][pattern] = line[-500:]
        matched = sum(pattern in state['success_matches'] for pattern in success_patterns)
        state['ended'] = (matched == len(success_patterns) if success.get('match', 'all') == 'all'
                          else matched > 0)
    elif not watcher and re.fullmatch(r'\s*(?:\[\d+\]\s*)?End\s*', line):
        state['ended'] = True
    failure = watcher.get('failure', {}) if watcher else {}
    custom_failure = any(re.search(pattern, line)
                         for pattern in failure.get('patterns', []))
    default_failure = failure.get('include_openfoam_defaults', True) and FATAL.search(line)
    if ((default_failure and 'floating point exception trapping' not in line.lower())
            or custom_failure):
        state['errors'] = (state['errors'] + [line[-500:]])[-12:]
        state['ended'] = False
    state['tail'] = (state.get('tail', []) + [line[-500:]])[-15:]


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
                f.seek(max(0, state['offset'] - 128))
                reset = f.read(min(128, state['offset'])).hex() != state['checkpoint']
            if reset:
                state = fresh_state()
            state['inode'] = inode
            f.seek(state['offset'])
            data = f.read(limit)
            state['offset'] = f.tell()
            state['backlog'] = max(0, stat.st_size - state['offset'])
            f.seek(max(0, state['offset'] - 128))
            state['checkpoint'] = f.read(min(128, state['offset'])).hex()
    except FileNotFoundError:
        state['missing'] = True
        return state
    state.pop('missing', None)
    lines = (state.get('partial', '') + data.decode('utf-8', errors='replace')).split('\n')
    state['partial'] = lines.pop()
    for line in lines:
        feed(state, line, watcher)
    if final and not state['backlog'] and state['partial']:
        feed(state, state.pop('partial'), watcher)
        state['partial'] = ''
    return state


def finish_log(path, state=None, watcher=None):
    state = read_log(path, state, final=True, watcher=watcher)
    while state.get('backlog', 0):
        state = read_log(path, state, final=True, watcher=watcher)
    return state


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
        return read_log(path, state, final=final, watcher=watcher)
    if not state:
        state = read_log(path, limit=min(window, 128 * 1024), watcher=watcher)
    if size - state['offset'] > window:
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


def case_logs(case):
    """Return the validated log candidates declared by a case watcher."""
    root = Path(case['_root'])
    names = case.get('watcher', {}).get('logs') or [case['log']]
    return [root / name for name in names]


def select_case_log(case, *, changed_from=None):
    """Select the newest existing stage log, optionally only if it changed.

    Multi-stage Allrun scripts often write one log per stage. The currently
    active stage is the candidate with the newest modification time.
    """
    candidates = []
    for path in case_logs(case):
        try:
            mtime = path.stat().st_mtime_ns
        except FileNotFoundError:
            continue
        if changed_from is not None and changed_from.get(str(path)) == mtime:
            continue
        candidates.append((mtime, str(path), path))
    return max(candidates)[2] if candidates else None


def recent_case_log(case, state=None, *, final=False):
    """Read the newest stage log and reset the cursor when stages change."""
    path = select_case_log(case) or case_logs(case)[0]
    if state and (state.get('log_path') != str(path) or 'rate_samples' not in state):
        state = None
    state = recent_log(path, state, final=final, watcher=case['watcher'])
    state['log_path'] = str(path)
    return state, path


def estimate(case, telemetry, elapsed, history=None):
    """Honor a configured duration; otherwise predict from recent log speed."""
    result = {'remaining_seconds': None, 'progress': None, 'basis': 'unknown'}
    expected = case.get('expected_seconds')
    if (isinstance(expected, (int, float)) and not isinstance(expected, bool)
            and math.isfinite(expected) and expected >= 1):
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
        telemetry = {}
    control = control_times(case)
    if control and control['stop_at'] != 'endTime':
        result['basis'] = 'stop_requested'
        return result
    if control:
        target = control['end']
        current = telemetry.get('time')
        result['target'] = target
        if case.get('end_time') is not None:
            result['target_source'] = 'ticket'
        if isinstance(current, (int, float)) and math.isfinite(current):
            origin = max(control['start'], telemetry.get('first_time', control['start']))
            if target > origin:
                result['progress'] = max(0, min(1, (current - origin) / (target - origin)))
            if current >= target:
                result.update(basis='target_reached', progress=1.0)
                return result
        samples = telemetry.get('rate_samples', [])[-RATE_POINTS:]
        if len(samples) >= 2:
            first, last = samples[0], samples[-1]
            delta_time = last[0] - first[0]
            delta_clock = last[1] - first[1]
            if (delta_time > 0 and delta_clock > 0 and target > last[0]
                    and math.isfinite(delta_time) and math.isfinite(delta_clock)):
                seconds_per_unit = delta_clock / delta_time
                remaining = (target - last[0]) * seconds_per_unit
                if math.isfinite(remaining):
                    result.update(remaining_seconds=remaining, basis='recent_log_rate',
                                  seconds_per_unit=seconds_per_unit, intervals=len(samples) - 1,
                                  measured_from=first[0], measured_to=last[0])
                    return result
    durations = [sample['seconds'] for sample in (history or [])[:5]
                 if isinstance(sample.get('seconds'), (int, float))
                 and math.isfinite(sample['seconds']) and sample['seconds'] > 0]
    if durations:
        expected = median(durations)
        remaining = expected - max(0, elapsed)
        result.update(remaining_seconds=remaining if remaining > 0 else None,
                      expected_seconds=expected, samples=len(durations),
                      basis='recent_successes' if remaining > 0 else 'history_overrun')
    return result
