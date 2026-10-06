"""Derived ticket catalog; JSON remains authoritative, never a second database.

Linux folder events avoid walking all tickets on a warm lookup. Other systems
and recursive globs use metadata comparison, loading only changed documents.
File locks precede the catalog mutex, including callers already editing tickets.
"""
from contextlib import ExitStack
from copy import deepcopy
import ctypes
from functools import lru_cache
import glob
import os
from pathlib import Path
import struct
import threading

from .config import ConfigError, _message, load_case, read_json


class FolderWatch:
    def __init__(self, folders):
        self.fd = -1
        self.paths = {}
        self.stamps = {}
        self.folders = set(folders)
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            self.fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
            if self.fd < 0:
                raise OSError(ctypes.get_errno(), 'inotify_init1')
            for folder in folders:
                # MODIFY/CLOSE_WRITE/ATTRIB/CREATE/DELETE/MOVE, including self.
                wd = libc.inotify_add_watch(self.fd, os.fsencode(folder), 0x00000fce)
                if wd < 0:
                    raise OSError(ctypes.get_errno(), 'inotify_add_watch')
                self.paths[wd] = folder
                stat = os.stat(folder)
                self.stamps[folder] = (stat.st_dev, stat.st_ino)
        except (AttributeError, OSError):
            self.close()

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __del__(self):
        self.close()

    def drain(self):
        changed = set()
        if self.fd < 0:
            return changed, True
        try:
            for folder, stamp in self.stamps.items():
                stat = os.stat(folder)
                if (stat.st_dev, stat.st_ino) != stamp:
                    return changed, True
            while True:
                try:
                    data = os.read(self.fd, 65536)
                except BlockingIOError:
                    return changed, False
                offset = 0
                while offset < len(data):
                    wd, mask, _, size = struct.unpack_from('iIII', data, offset)
                    name = os.fsdecode(data[offset + 16:offset + 16 + size].split(b'\0', 1)[0])
                    offset += 16 + size
                    if mask & 0x0000ec00:  # overflow, ignored, unmount, delete/move self
                        return changed, True
                    if wd in self.paths and name:
                        changed.add(str(Path(self.paths[wd]) / name))
        except OSError:
            return changed, True


def active_cases(tickets, ui_dir=None, *, check_duplicates=True):
    """One normalized membership set per parent, never child × parent rows."""
    macros = {c['_config']: c for c in tickets if c['task_type'] == 'macro'}
    members = {path: {(str((Path(path).parent / row.get('ticket', '')).resolve()), row['case_dir'])
                      for row in macro['cases']} for path, macro in macros.items()}
    result = []
    for case in tickets:
        if ui_dir:
            case['_ui_dir'] = ui_dir
        if case['task_type'] == 'macro':
            continue
        if case['role'] == 'child':
            parent = str((Path(case['_config']).parent / case['macro_ticket']).resolve())
            if (case['_config'], case['_root']) not in members.get(parent, ()):
                continue
        result.append(case)
    roots = [case['_root'] for case in result]
    if check_duplicates and len(roots) != len(set(roots)):
        raise ConfigError(_message('duplicate_case'))
    return result


class TicketIndex:
    def __init__(self, paths, patterns, ui_dir=None, strict=True):
        self.paths, self.patterns, self.ui_dir = paths, patterns, ui_dir
        self.strict = strict
        self.errors = {}
        self.mutex = threading.RLock()
        self.watch = None
        self.documents = {}
        self.raw = {}
        self.roots = {}
        self.parents = {}
        self.macros = {}
        self.stamps = {}
        self.dirty = {}
        self.generation = 0
        self.invalid = True

    def _discover(self):
        paths = set(self.paths)
        for pattern in self.patterns:
            paths.update(glob.glob(pattern, recursive=True))
        return paths

    def _matches(self, path):
        from fnmatch import fnmatchcase
        return path in self.paths or any(
            str(Path(path).parent) == str(Path(pattern).parent)
            and fnmatchcase(Path(path).name, Path(pattern).name) for pattern in self.patterns)

    @staticmethod
    def _stamp(path):
        s = os.stat(path, follow_symlinks=False)
        return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)

    def refresh(self, force=False):
        from .tickets import ticket_lock
        dynamic = any(glob.has_magic(str(Path(p).parent)) for p in self.patterns)
        folders = {str(Path(p).parent) for p in (*self.paths, *self.patterns)
                   if not glob.has_magic(str(Path(p).parent))}
        discovered = self._discover() if dynamic else None
        if discovered is not None:
            folders.update(str(Path(p).parent) for p in discovered)
        with ExitStack() as locks:
            for folder in sorted(folders):
                locks.enter_context(ticket_lock(folder))
            with self.mutex:
                try:
                    full = force or self.invalid
                    if self.watch is None or self.watch.folders != folders:
                        if self.watch:
                            self.watch.close()
                        self.watch = FolderWatch(sorted(folders))
                        full = True
                    changed, lost = self.watch.drain()
                    if lost and self.watch.fd >= 0:
                        self.watch.close()
                        self.watch = FolderWatch(sorted(folders))
                        full = True
                    fallback = dynamic or self.watch.fd < 0
                    stamps = self.stamps.copy()
                    if full or fallback:
                        paths = self._discover()
                        stamps = {path: self._stamp(path) for path in paths}
                        changed = (paths | set(self.documents)) if full else {
                            p for p in paths | set(self.documents) if stamps.get(p) != self.stamps.get(p)}
                    else:
                        changed = {p for p in changed if self._matches(p)}
                        paths = (set(self.documents) | changed)
                        for p in changed:
                            if os.path.lexists(p):
                                stamps[p] = self._stamp(p)
                            else:
                                paths.discard(p)
                                stamps.pop(p, None)
                    if not changed and not full:
                        return self
                    documents = self.documents.copy()
                    raw = self.raw.copy()
                    errors = self.errors.copy()
                    for path in sorted(changed):
                        errors.pop(path, None)
                        if path not in paths:
                            documents.pop(path, None)
                            raw.pop(path, None)
                            continue
                        try:
                            if Path(path).is_symlink():
                                raise ConfigError(_message('path_escape', path=path))
                            case = load_case(path)
                            raw[path] = read_json(path)
                            if self.ui_dir:
                                case['_ui_dir'] = self.ui_dir
                            documents[path] = case
                        except (OSError, ValueError) as exc:
                            if self.strict:
                                raise
                            errors[path] = str(exc)
                            documents.pop(path, None)
                            raw.pop(path, None)
                    ordered = [documents[p] for p in sorted(documents)]
                    roots = {c['_root']: c for c in active_cases(ordered, self.ui_dir, check_duplicates=self.strict)}
                    self.generation += 1
                    affected = {c['_root'] for p in changed for c in
                                (self.documents.get(p), documents.get(p)) if c and c['task_type'] != 'macro'}
                    affected.update(r for r in self.roots.keys() | roots.keys()
                                    if self.roots.get(r) != roots.get(r))
                    for path in changed:
                        before, after = self.documents.get(path, {}), documents.get(path, {})
                        if before.get('task_type') == 'macro' or after.get('task_type') == 'macro':
                            affected.update(c['_root'] for c in (before, after) if c)
                            old_rows = {r['case_dir']: r for r in before.get('cases', [])}
                            new_rows = {r['case_dir']: r for r in after.get('cases', [])}
                            affected.update(r for r in old_rows.keys() | new_rows.keys()
                                            if old_rows.get(r) != new_rows.get(r))
                    self.dirty.update((root, self.generation) for root in affected)
                    self.documents, self.roots, self.stamps = documents, roots, stamps
                    self.raw, self.errors = raw, errors
                    self.macros = {c['_root']: c for c in ordered if c['task_type'] == 'macro'}
                    self.parents = {}
                    for macro in self.macros.values():
                        for row in macro['cases']:
                            self.parents.setdefault(row['case_dir'], set()).add(macro['_root'])
                    self.invalid = False
                    return self
                except BaseException:
                    # Drained events cannot be replayed after a partial/invalid edit.
                    self.invalid = True
                    raise

    def tickets(self, *, submit_only=False):
        with self.mutex:
            return deepcopy([c for _, c in sorted(self.documents.items())
                             if not submit_only or c.get('queue', {}).get('submit')])

    def queue_profiles(self):
        """Return only queue profile fields without copying large macro rows."""
        with self.mutex:
            return [dict(execution_queue=deepcopy(case['execution_queue']))
                    for case in self.documents.values() if case.get('execution_queue')]

    def cases(self, roots=None):
        with self.mutex:
            return deepcopy(list(self.roots.values()) if roots is None else
                            [self.roots[r] for r in roots if r in self.roots])

    def document(self, path, *, raw=False):
        with self.mutex:
            if str(path) in self.errors:
                raise ValueError(self.errors[str(path)])
            value = (self.raw if raw else self.documents).get(str(path))
            if value is None:
                raise FileNotFoundError(str(path))
            return deepcopy(value)

    def lookup(self, root):
        with self.mutex:
            return deepcopy(self.roots.get(root))

    def related_macros(self, roots):
        with self.mutex:
            selected = set(roots) & self.macros.keys()
            for root in roots:
                selected.update(self.parents.get(root, ()))
            return deepcopy([self.macros[r] for r in sorted(selected)])

    def changes(self):
        with self.mutex:
            return self.dirty.copy()

    def acknowledge(self, changes):
        with self.mutex:
            for root, version in changes.items():
                if self.dirty.get(root) == version:
                    self.dirty.pop(root)


@lru_cache(maxsize=16)
def _index(paths, patterns, ui_dir, strict=True):
    return TicketIndex(paths, patterns, ui_dir, strict)


def ticket_index(bot, *, force=False):
    return _index(tuple(bot.get('cases', ())), tuple(bot.get('case_globs', ())),
                  bot.get('_ui_dir')).refresh(force=force)


def folder_index(folder):
    return _index((), (str(Path(folder) / '*.json'),), None, False).refresh()
