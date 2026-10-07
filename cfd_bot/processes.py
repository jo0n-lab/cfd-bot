"""Reuse the existing ofps snapshot and CPU admission check without changing it."""
from . import diagnostics as _diagnostics
import os
import re
import subprocess
import threading
from pathlib import Path

from .ui import load_ui


SNAPSHOT_LOCK = threading.Lock()
MANAGED_SCAN_ENV = 'CFD_BOT_OFPS_MANAGED'


@_diagnostics.trace
def managed_scan_environment():
    """Tell the integrated ofps that its caller owns state reconciliation."""
    env = os.environ.copy()
    env[MANAGED_SCAN_ENV] = '1'
    return _diagnostics.child_environment(env)


@_diagnostics.trace
def identity(pid):
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
        parts = stat[stat.rfind(')') + 2:].split()
        if parts[0] == 'Z':
            if _diagnostics.detailed: _diagnostics.step('processes.identity:L26:then')
            return None
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return f'{boot}:{pid}:{parts[19]}'
    except (OSError, IndexError):
        if _diagnostics.enabled: _diagnostics.step('processes.identity:L30:except')
        return None


@_diagnostics.trace
def _proc(pid):
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
        fields = stat[stat.rfind(')') + 2:].split()
        return dict(ppid=int(fields[1]), session=int(fields[3]))
    except (OSError, ValueError, IndexError):
        if _diagnostics.enabled: _diagnostics.step('processes._proc:L39:except')
        return None


@_diagnostics.trace
def _comm(pid):
    try:
        return Path(f'/proc/{pid}/comm').read_text().strip()
    except OSError:
        if _diagnostics.enabled: _diagnostics.step('processes._comm:L46:except')
        return ''


@_diagnostics.trace
def _environment(pid):
    try:
        values = Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
        return {item.split(b'=', 1)[0].decode(errors='replace'):
                item.split(b'=', 1)[1].decode(errors='replace')
                for item in values if b'=' in item}
    except OSError:
        if _diagnostics.enabled: _diagnostics.step('processes._environment:L56:except')
        return {}


@_diagnostics.trace
def _tty(pid):
    for fd in (0, 1, 2):
        if _diagnostics.detailed: _diagnostics.step('processes._tty:L61:loop', fd=fd)
        try:
            value = os.readlink(f'/proc/{pid}/fd/{fd}')
        except OSError:
            if _diagnostics.enabled: _diagnostics.step('processes._tty:L64:except')
            continue
        if value.startswith('/dev/') and value not in ('/dev/null', '/dev/zero'):
            if _diagnostics.detailed: _diagnostics.step('processes._tty:L66:then')
            return value.removeprefix('/dev/')
    return None


@_diagnostics.trace
def owner_label(pid):
    """Describe the terminal, SSH session, or background owner of a run."""
    ui = load_ui()
    origin = int(pid)
    origin_tty = _tty(origin)
    remote = None
    sshd_pid = None
    manager = None
    current = origin
    seen = set()
    for _ in range(64):
        if _diagnostics.detailed: _diagnostics.step('processes.owner_label:L81:loop', _=_)
        if current <= 1 or current in seen:
            if _diagnostics.detailed: _diagnostics.step('processes.owner_label:L82:then')
            break
        seen.add(current)
        env = _environment(current)
        if not remote and env.get('SSH_CONNECTION'):
            if _diagnostics.detailed: _diagnostics.step('processes.owner_label:L86:then')
            remote = env['SSH_CONNECTION'].split()[0]
        name = _comm(current)
        if name == 'sshd' or name.startswith('sshd:'):
            if _diagnostics.detailed: _diagnostics.step('processes.owner_label:L89:then')
            sshd_pid = current
        if name in ('tmux', 'screen') and manager is None:
            if _diagnostics.detailed: _diagnostics.step('processes.owner_label:L91:then')
            manager = ui.text('scenarios.runtime.owner.manager', name=name, pid=current)
        if name in ('systemd', 'systemd-run') and manager is None:
            if _diagnostics.detailed: _diagnostics.step('processes.owner_label:L93:then')
            manager = ui.text('scenarios.runtime.owner.systemd')
        info = _proc(current)
        if not info:
            if _diagnostics.detailed: _diagnostics.step('processes.owner_label:L96:then')
            break
        current = info['ppid']
    if remote or sshd_pid:
        if _diagnostics.detailed: _diagnostics.step('processes.owner_label:L99:then')
        parts = [ui.text('scenarios.runtime.owner.ssh', remote=f' {remote}' if remote else '')]
        parts.append(origin_tty or ui.text('scenarios.runtime.owner.background'))
        if sshd_pid:
            if _diagnostics.detailed: _diagnostics.step('processes.owner_label:L102:then')
            parts.append(ui.text('scenarios.runtime.owner.sshd', pid=sshd_pid))
        return ' · '.join(parts)
    if manager:
        if _diagnostics.detailed: _diagnostics.step('processes.owner_label:L105:then')
        return manager + (f' · {origin_tty}' if origin_tty else '')
    if origin_tty:
        if _diagnostics.detailed: _diagnostics.step('processes.owner_label:L107:then')
        return ui.text('scenarios.runtime.owner.terminal', tty=origin_tty)
    return ui.text('scenarios.runtime.owner.background_pid', pid=origin)


@_diagnostics.trace
def cpu_layout(record):
    ui = load_ui()
    cpus = set()
    for process in record.get('processes', []):
        if _diagnostics.detailed: _diagnostics.step('processes.cpu_layout:L115:loop', process=process)
        for part in str(process.get('cpu_list', '')).split(','):
            if _diagnostics.detailed: _diagnostics.step('processes.cpu_layout:L116:loop', part=part)
            if not part:
                if _diagnostics.detailed: _diagnostics.step('processes.cpu_layout:L117:then')
                continue
            ends = part.split('-', 1)
            try:
                lo, hi = int(ends[0]), int(ends[-1])
            except ValueError:
                if _diagnostics.enabled: _diagnostics.step('processes.cpu_layout:L122:except')
                continue
            cpus.update(range(lo, hi + 1))
    if not cpus:
        if _diagnostics.detailed: _diagnostics.step('processes.cpu_layout:L125:then')
        return 0, ui.text('strings.common.unspecified')
    ordered = sorted(cpus)
    ranges = []
    start = previous = ordered[0]
    for value in ordered[1:] + [None]:
        if _diagnostics.detailed: _diagnostics.step('processes.cpu_layout:L130:loop', value=value)
        if value is not None and value == previous + 1:
            if _diagnostics.detailed: _diagnostics.step('processes.cpu_layout:L131:then')
            previous = value
            continue
        ranges.append(str(start) if start == previous else f'{start}-{previous}')
        if value is not None:
            if _diagnostics.detailed: _diagnostics.step('processes.cpu_layout:L135:then')
            start = previous = value
    return len(cpus), ','.join(ranges)


@_diagnostics.trace
def parse_snapshot(raw):
    cases = {}
    current = None
    engine = None
    for line in raw.splitlines():
        if _diagnostics.detailed: _diagnostics.step('processes.parse_snapshot:L144:loop', line=line)
        if line.startswith('ENGINE: '):
            if _diagnostics.detailed: _diagnostics.step('processes.parse_snapshot:L145:then')
            engine = line[8:]
        elif line.startswith('CASE: '):
            if _diagnostics.detailed: _diagnostics.step('processes.parse_snapshot:L147:then')
            root = line[6:]
            if root.endswith((' [controlDict not found]', ' [Basilisk case not found]')):
                if _diagnostics.detailed: _diagnostics.step('processes.parse_snapshot:L149:then')
                current = None
                continue
            root = str(Path(root).expanduser().resolve())
            current = cases.setdefault(root, {'root': root, 'engines': [], 'processes': [], 'supervisors': []})
            if engine not in current['engines']:
                if _diagnostics.detailed: _diagnostics.step('processes.parse_snapshot:L154:then')
                current['engines'].append(engine)
        elif line.startswith('SUPERVISOR: ') and current is not None:
            if _diagnostics.detailed: _diagnostics.step('processes.parse_snapshot:L156:then')
            match = re.search(r'PID (\d+), PPID (\S+), PROCESS (\S+), ELAPSED (\S+)', line)
            if match:
                if _diagnostics.detailed: _diagnostics.step('processes.parse_snapshot:L158:then')
                pid = int(match[1])
                current['supervisors'].append(dict(pid=pid, ppid=match[2], name=match[3],
                                                   elapsed=match[4], identity=identity(pid),
                                                   owner=owner_label(pid)))
        elif current is not None:
            if _diagnostics.detailed: _diagnostics.step('processes.parse_snapshot:L163:then')
            fields = line.split()
            if len(fields) == 10 and fields[0].isdigit():
                if _diagnostics.detailed: _diagnostics.step('processes.parse_snapshot:L165:then')
                current['processes'].append(dict(zip(
                    ('pid', 'ppid', 'name', 'mode', 'threads', 'cpu_count', 'cpu_list', 'sockets', 'numa', 'elapsed'), fields)))
                current['processes'][-1]['identity'] = identity(int(fields[0]))
                if _diagnostics.enabled: _diagnostics.event('ofps.process.observed', case_root=current['root'], process=current['processes'][-1])
    for record in cases.values():
        if _diagnostics.detailed: _diagnostics.step('processes.parse_snapshot:L169:loop', record=record)
        members = record['supervisors'] + record['processes']
        record['owner'] = next((p.get('owner') for p in record['supervisors']
                                if p.get('owner')), None)
        if record['owner'] is None and members:
            if _diagnostics.detailed: _diagnostics.step('processes.parse_snapshot:L173:then')
            record['owner'] = owner_label(int(members[0]['pid']))
        record['actual_cores'], record['actual_cpu_list'] = cpu_layout(record)
    return cases


@_diagnostics.trace
def snapshot(command):
    # The daemon monitor and a Telegram /stat request can arrive together.
    # Run only one ofps scan at a time so its process walk/output cannot race.
    if _diagnostics.enabled: _diagnostics.event('ofps.lock.wait', name='SNAPSHOT_LOCK')
    with SNAPSHOT_LOCK:
        if _diagnostics.enabled: _diagnostics.event('ofps.lock.acquired', name='SNAPSHOT_LOCK')
        result = _diagnostics.run_process(command, capture_output=True, text=True, timeout=45,
                                env=managed_scan_environment())
    if _diagnostics.enabled: _diagnostics.event('ofps.lock.released', name='SNAPSHOT_LOCK')
    if result.returncode:
        if _diagnostics.detailed: _diagnostics.step('processes.snapshot:L185:then')
        raise RuntimeError(f'ofps exited {result.returncode}: {result.stderr[-1000:]}')
    if not result.stdout.strip():
        if _diagnostics.detailed: _diagnostics.step('processes.snapshot:L187:then')
        raise RuntimeError('ofps returned an empty snapshot')
    if not ('ENGINE: ' in result.stdout or 'No active ' in result.stdout):
        if _diagnostics.detailed: _diagnostics.step('processes.snapshot:L189:then')
        raise RuntimeError('unrecognized ofps output; check ofps_command')
    cases = parse_snapshot(result.stdout)
    if 'CASE: ' in result.stdout and not cases:
        if _diagnostics.detailed: _diagnostics.step('processes.snapshot:L192:then')
        raise RuntimeError('ofps reported an active CASE but the snapshot parser returned none')
    return {'raw': result.stdout, 'cases': cases}


@_diagnostics.trace
def check_cpus(command, case):
    args = command + ['--check', case['cpu_set']]
    if case.get('allow_cross_socket'):
        if _diagnostics.detailed: _diagnostics.step('processes.check_cpus:L199:then')
        args.append('--allow-cross-socket')
    result = _diagnostics.run_process(args, capture_output=True, text=True, timeout=45,
                            env=managed_scan_environment())
    return result.returncode == 0, (result.stdout + result.stderr)[-3000:]


class DaemonLock:
    @_diagnostics.trace
    def __init__(self, path):
        self.path = path
        self.file = None

    @_diagnostics.trace
    def __enter__(self):
        import fcntl
        self.file = open(self.path, 'a+')
        try:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if _diagnostics.enabled: _diagnostics.step('processes.DaemonLock.__enter__:L216:except')
            self.file.close()
            raise RuntimeError('another daemon is already using this state directory') from exc
        return self

    @_diagnostics.trace
    def __exit__(self, *args):
        self.file.close()
