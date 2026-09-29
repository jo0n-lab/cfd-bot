"""Reuse the existing ofps snapshot and CPU admission check without changing it."""
import os
import re
import subprocess
import threading
from pathlib import Path

from .ui import load_ui


SNAPSHOT_LOCK = threading.Lock()
MANAGED_SCAN_ENV = 'CFD_BOT_OFPS_MANAGED'


def managed_scan_environment():
    """Tell the integrated ofps that its caller owns state reconciliation."""
    env = os.environ.copy()
    env[MANAGED_SCAN_ENV] = '1'
    return env


def identity(pid):
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
        parts = stat[stat.rfind(')') + 2:].split()
        if parts[0] == 'Z':
            return None
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return f'{boot}:{pid}:{parts[19]}'
    except (OSError, IndexError):
        return None


def _proc(pid):
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
        fields = stat[stat.rfind(')') + 2:].split()
        return dict(ppid=int(fields[1]), session=int(fields[3]))
    except (OSError, ValueError, IndexError):
        return None


def _comm(pid):
    try:
        return Path(f'/proc/{pid}/comm').read_text().strip()
    except OSError:
        return ''


def _environment(pid):
    try:
        values = Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
        return {item.split(b'=', 1)[0].decode(errors='replace'):
                item.split(b'=', 1)[1].decode(errors='replace')
                for item in values if b'=' in item}
    except OSError:
        return {}


def _tty(pid):
    for fd in (0, 1, 2):
        try:
            value = os.readlink(f'/proc/{pid}/fd/{fd}')
        except OSError:
            continue
        if value.startswith('/dev/') and value not in ('/dev/null', '/dev/zero'):
            return value.removeprefix('/dev/')
    return None


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
        if current <= 1 or current in seen:
            break
        seen.add(current)
        env = _environment(current)
        if not remote and env.get('SSH_CONNECTION'):
            remote = env['SSH_CONNECTION'].split()[0]
        name = _comm(current)
        if name == 'sshd' or name.startswith('sshd:'):
            sshd_pid = current
        if name in ('tmux', 'screen') and manager is None:
            manager = ui.text('scenarios.runtime.owner.manager', name=name, pid=current)
        if name in ('systemd', 'systemd-run') and manager is None:
            manager = ui.text('scenarios.runtime.owner.systemd')
        info = _proc(current)
        if not info:
            break
        current = info['ppid']
    if remote or sshd_pid:
        parts = [ui.text('scenarios.runtime.owner.ssh', remote=f' {remote}' if remote else '')]
        parts.append(origin_tty or ui.text('scenarios.runtime.owner.background'))
        if sshd_pid:
            parts.append(ui.text('scenarios.runtime.owner.sshd', pid=sshd_pid))
        return ' · '.join(parts)
    if manager:
        return manager + (f' · {origin_tty}' if origin_tty else '')
    if origin_tty:
        return ui.text('scenarios.runtime.owner.terminal', tty=origin_tty)
    return ui.text('scenarios.runtime.owner.background_pid', pid=origin)


def cpu_layout(record):
    ui = load_ui()
    cpus = set()
    for process in record.get('processes', []):
        for part in str(process.get('cpu_list', '')).split(','):
            if not part:
                continue
            ends = part.split('-', 1)
            try:
                lo, hi = int(ends[0]), int(ends[-1])
            except ValueError:
                continue
            cpus.update(range(lo, hi + 1))
    if not cpus:
        return 0, ui.text('strings.common.unspecified')
    ordered = sorted(cpus)
    ranges = []
    start = previous = ordered[0]
    for value in ordered[1:] + [None]:
        if value is not None and value == previous + 1:
            previous = value
            continue
        ranges.append(str(start) if start == previous else f'{start}-{previous}')
        if value is not None:
            start = previous = value
    return len(cpus), ','.join(ranges)


def parse_snapshot(raw):
    cases = {}
    current = None
    engine = None
    for line in raw.splitlines():
        if line.startswith('ENGINE: '):
            engine = line[8:]
        elif line.startswith('CASE: '):
            root = line[6:]
            if root.endswith(' [controlDict not found]'):
                root = root[:-len(' [controlDict not found]')]
            root = str(Path(root).expanduser().resolve())
            current = cases.setdefault(root, {'root': root, 'engines': [], 'processes': [], 'supervisors': []})
            if engine not in current['engines']:
                current['engines'].append(engine)
        elif line.startswith('SUPERVISOR: ') and current is not None:
            match = re.search(r'PID (\d+), PPID (\S+), PROCESS (\S+), ELAPSED (\S+)', line)
            if match:
                pid = int(match[1])
                current['supervisors'].append(dict(pid=pid, ppid=match[2], name=match[3],
                                                   elapsed=match[4], identity=identity(pid),
                                                   owner=owner_label(pid)))
        elif current is not None:
            fields = line.split()
            if len(fields) == 10 and fields[0].isdigit():
                current['processes'].append(dict(zip(
                    ('pid', 'ppid', 'name', 'mode', 'threads', 'cpu_count', 'cpu_list', 'sockets', 'numa', 'elapsed'), fields)))
                current['processes'][-1]['identity'] = identity(int(fields[0]))
    for record in cases.values():
        members = record['supervisors'] + record['processes']
        record['owner'] = next((p.get('owner') for p in record['supervisors']
                                if p.get('owner')), None)
        if record['owner'] is None and members:
            record['owner'] = owner_label(int(members[0]['pid']))
        record['actual_cores'], record['actual_cpu_list'] = cpu_layout(record)
    return cases


def snapshot(command):
    # The daemon monitor and a Telegram /stat request can arrive together.
    # Run only one ofps scan at a time so its process walk/output cannot race.
    with SNAPSHOT_LOCK:
        result = subprocess.run(command, capture_output=True, text=True, timeout=45,
                                env=managed_scan_environment())
    if result.returncode:
        raise RuntimeError(f'ofps exited {result.returncode}: {result.stderr[-1000:]}')
    if not result.stdout.strip():
        raise RuntimeError('ofps returned an empty snapshot')
    if not ('ENGINE: ' in result.stdout or 'No active ' in result.stdout):
        raise RuntimeError('unrecognized ofps output; check ofps_command')
    cases = parse_snapshot(result.stdout)
    if 'CASE: ' in result.stdout and not cases:
        raise RuntimeError('ofps reported an active CASE but the snapshot parser returned none')
    return {'raw': result.stdout, 'cases': cases}


def check_cpus(command, case):
    args = command + ['--check', case['cpu_set']]
    if case.get('allow_cross_socket'):
        args.append('--allow-cross-socket')
    result = subprocess.run(args, capture_output=True, text=True, timeout=45,
                            env=managed_scan_environment())
    return result.returncode == 0, (result.stdout + result.stderr)[-3000:]


class DaemonLock:
    def __init__(self, path):
        self.path = path
        self.file = None

    def __enter__(self):
        import fcntl
        self.file = open(self.path, 'a+')
        try:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise RuntimeError('another daemon is already using this state directory') from exc
        return self

    def __exit__(self, *args):
        self.file.close()
