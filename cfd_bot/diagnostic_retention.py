"""Expire diagnostic files outside request paths. No bot/DB/solver operations."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import resource
import stat
import tempfile
import time

RETENTION_SECONDS = 86400
SEGMENT_SECONDS = 300
_LOG = re.compile(r'[A-Za-z0-9_.-]+-(?P<pid>\d+)-[0-9a-f]+(?P<segment>-s[0-9a-f]+)?\.jsonl(?:\.[1-9]\d*)?\Z')
_HEADER = re.compile(rb'\s*\{\s*"event"\s*:\s*"(?:log.file|shell.file)"\s*[,}]')


def directory_from_config(path):
    """Read only the logging location; do not configure a logger or load cases."""
    path = Path(path).resolve()
    config = json.loads(path.read_text())
    options = config.get('diagnostic_logging') or {}
    directory = options.get('directory') or os.environ.get('CFD_BOT_DIAGNOSTICS_DIR')
    directory = Path(directory or str(Path(config.get('state_dir', 'state')) / 'diagnostics'))
    return directory if directory.is_absolute() else path.parent / directory


def _open_inodes():
    """Legacy writers have no time segments: retain files still open by this uid."""
    opened = set()
    unavailable = set()
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            for fd in (entry / 'fd').iterdir():
                try:
                    info = fd.stat()
                    if stat.S_ISREG(info.st_mode):
                        opened.add((info.st_dev, info.st_ino))
                except FileNotFoundError:
                    pass  # A process closed a descriptor during the scan.
        except FileNotFoundError:
            pass
        except PermissionError:
            # A protected unrelated desktop process must not defer every log.
            # Defer that writer and its ancestors (possibly inherited handles).
            pid = int(entry.name)
            while pid > 1 and pid not in unavailable:
                unavailable.add(pid)
                try:
                    fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
                    pid = int(fields[1])
                except (OSError, ValueError, IndexError):
                    break
    return opened, unavailable


def _io_counts():
    try:
        return {key: int(value) for key, value in
                (line.split(':', 1) for line in Path('/proc/self/io').read_text().splitlines())}
    except (OSError, ValueError):
        return {}


def collect(directory, *, apply=False, now=None):
    """Use last persisted write time. Never recurse or follow a file symlink."""
    started, cpu = time.monotonic(), time.process_time()
    usage, io = resource.getrusage(resource.RUSAGE_SELF), _io_counts()
    now = time.time() if now is None else now
    cutoff = now - RETENTION_SECONDS
    result = dict(event='diagnostic.retention', apply=apply, retention_seconds=RETENTION_SECONDS,
                  cutoff=cutoff, scanned_files=0, scanned_bytes=0, candidate_files=0,
                  candidate_bytes=0, deleted_files=0, deleted_bytes=0, deferred_open=0,
                  skipped_changed=0, ignored_files=0, errors=0, status='ok')
    directory = Path(directory)
    if not directory.exists():
        result['status'] = 'missing_directory'
    else:
        # One cleanup process per directory, including manual invocations.
        with _directory_lock(directory) as acquired:
            if not acquired:
                result['status'] = 'already_running'
            else:
                opened, unavailable = None, set()
                with os.scandir(directory) as entries:
                    for entry in entries:
                        match = _LOG.fullmatch(entry.name)
                        if not match or not entry.is_file(follow_symlinks=False):
                            result['ignored_files'] += 1
                            continue
                        try:
                            info = entry.stat(follow_symlinks=False)
                            result['scanned_files'] += 1
                            result['scanned_bytes'] += info.st_size
                            if info.st_mtime >= cutoff:
                                continue
                            if not match['segment']:
                                if opened is None:
                                    opened, unavailable = _open_inodes()
                                if int(match['pid']) in unavailable or (info.st_dev, info.st_ino) in opened:
                                    result['deferred_open'] += 1
                                    continue
                            fd = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                            with os.fdopen(fd, 'rb', buffering=0) as source:
                                current = os.fstat(source.fileno())
                                if (not stat.S_ISREG(current.st_mode) or
                                        (current.st_ino, current.st_dev, current.st_mtime_ns, current.st_size) !=
                                        (info.st_ino, info.st_dev, info.st_mtime_ns, info.st_size)):
                                    result['skipped_changed'] += 1
                                    continue
                                if not _HEADER.match(source.read(256)):
                                    result['ignored_files'] += 1
                                    continue
                                # Check the name still identifies the same unchanged file.
                                latest = os.stat(entry.path, follow_symlinks=False)
                                if (latest.st_ino, latest.st_dev, latest.st_mtime_ns, latest.st_size) != (
                                        info.st_ino, info.st_dev, info.st_mtime_ns, info.st_size):
                                    result['skipped_changed'] += 1
                                    continue
                                result['candidate_files'] += 1
                                result['candidate_bytes'] += info.st_size
                                if apply:
                                    os.unlink(entry.path)
                                    result['deleted_files'] += 1
                                    result['deleted_bytes'] += info.st_size
                        except FileNotFoundError:
                            result['skipped_changed'] += 1
                        except OSError:
                            result['errors'] += 1
    result['remaining_files'] = result['scanned_files'] - result['deleted_files']
    result['remaining_bytes'] = result['scanned_bytes'] - result['deleted_bytes']
    if result['errors']:
        result['status'] = 'partial_failure'
    result['wall_seconds'] = time.monotonic() - started
    result['cpu_seconds'] = time.process_time() - cpu
    after = resource.getrusage(resource.RUSAGE_SELF)
    result['block_inputs'] = after.ru_inblock - usage.ru_inblock
    result['block_outputs'] = after.ru_oublock - usage.ru_oublock
    result['io_delta'] = {k: v - io[k] for k, v in _io_counts().items() if k in io}
    result['finished_at'] = time.time()
    return result


class _directory_lock:
    def __init__(self, directory):
        self.path = directory / '.retention.lock'

    def __enter__(self):
        self.fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def __exit__(self, *args):
        os.close(self.fd)


def write_status(directory, result):
    """One bounded status file; readable without access to the system journal."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=directory, prefix='.retention-status-',
                                         encoding='utf-8', delete=False) as output:
            temporary = Path(output.name)
            json.dump(result, output, sort_keys=True)
            output.write('\n')
        temporary.replace(Path(directory) / 'retention-status.json')
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--config', type=Path)
    source.add_argument('--directory', type=Path)
    parser.add_argument('--apply', action='store_true', help='delete expired logs; default is dry-run')
    args = parser.parse_args(argv)
    try:
        directory = args.directory or directory_from_config(args.config)
        result = collect(directory, apply=args.apply)
        if args.apply and result['status'] in ('ok', 'partial_failure'):
            try:
                write_status(directory, result)
            except OSError:
                result['errors'] += 1
                result['status'] = 'status_write_failed'
    except (OSError, ValueError) as error:
        print(json.dumps({'event': 'diagnostic.retention', 'status': 'failed',
                          'error_type': type(error).__name__}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 1 if result['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
