"""Derived ticket catalog; JSON remains authoritative, never a second database.

Linux folder events avoid walking all tickets on a warm lookup. Other systems
and recursive globs use metadata comparison, loading only changed documents.
File locks precede the catalog mutex, including callers already editing tickets.
"""
from . import diagnostics as _diagnostics
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
    @_diagnostics.trace
    def __init__(self, folders):
        self.fd = -1
        self.paths = {}
        self.stamps = {}
        self.folders = set(folders)
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            self.fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
            if self.fd < 0:
                if _diagnostics.detailed: _diagnostics.step('catalog.FolderWatch.__init__:L29:then')
                raise OSError(ctypes.get_errno(), 'inotify_init1')
            for folder in folders:
                # MODIFY/CLOSE_WRITE/ATTRIB/CREATE/DELETE/MOVE, including self.
                if _diagnostics.detailed: _diagnostics.step('catalog.FolderWatch.__init__:L31:loop', folder=folder)
                wd = libc.inotify_add_watch(self.fd, os.fsencode(folder), 0x00000fce)
                if wd < 0:
                    if _diagnostics.detailed: _diagnostics.step('catalog.FolderWatch.__init__:L34:then')
                    raise OSError(ctypes.get_errno(), 'inotify_add_watch')
                self.paths[wd] = folder
                stat = os.stat(folder)
                self.stamps[folder] = (stat.st_dev, stat.st_ino)
        except (AttributeError, OSError):
            if _diagnostics.enabled: _diagnostics.step('catalog.FolderWatch.__init__:L39:except')
            self.close()

    @_diagnostics.trace
    def close(self):
        if self.fd >= 0:
            if _diagnostics.detailed: _diagnostics.step('catalog.FolderWatch.close:L43:then')
            os.close(self.fd)
            self.fd = -1

    @_diagnostics.trace
    def __del__(self):
        self.close()

    @_diagnostics.trace
    def drain(self):
        changed = set()
        if self.fd < 0:
            if _diagnostics.detailed: _diagnostics.step('catalog.FolderWatch.drain:L52:then')
            return changed, True
        try:
            for folder, stamp in self.stamps.items():
                if _diagnostics.detailed: _diagnostics.step('catalog.FolderWatch.drain:L55:loop', folder=folder, stamp=stamp)
                stat = os.stat(folder)
                if (stat.st_dev, stat.st_ino) != stamp:
                    if _diagnostics.detailed: _diagnostics.step('catalog.FolderWatch.drain:L57:then')
                    return changed, True
            while True:
                if _diagnostics.detailed: _diagnostics.step('catalog.FolderWatch.drain:L59:loop')
                try:
                    data = os.read(self.fd, 65536)
                except BlockingIOError:
                    if _diagnostics.enabled: _diagnostics.step('catalog.FolderWatch.drain:L62:except')
                    return changed, False
                offset = 0
                while offset < len(data):
                    if _diagnostics.detailed: _diagnostics.step('catalog.FolderWatch.drain:L65:loop')
                    wd, mask, _, size = struct.unpack_from('iIII', data, offset)
                    name = os.fsdecode(data[offset + 16:offset + 16 + size].split(b'\0', 1)[0])
                    offset += 16 + size
                    if mask & 0x0000ec00:  # overflow, ignored, unmount, delete/move self
                        if _diagnostics.detailed: _diagnostics.step('catalog.FolderWatch.drain:L69:then')
                        return changed, True
                    if wd in self.paths and name:
                        if _diagnostics.detailed: _diagnostics.step('catalog.FolderWatch.drain:L71:then')
                        changed.add(str(Path(self.paths[wd]) / name))
        except OSError:
            if _diagnostics.enabled: _diagnostics.step('catalog.FolderWatch.drain:L73:except')
            return changed, True


@_diagnostics.trace
def active_cases(tickets, ui_dir=None, *, check_duplicates=True):
    """One normalized membership set per parent, never child × parent rows."""
    macros = {c['_config']: c for c in tickets if c['task_type'] == 'macro'}
    members = {path: {(str((Path(path).parent / row.get('ticket', '')).resolve()), row['case_dir'])
                      for row in macro['cases']} for path, macro in macros.items()}
    result = []
    for case in tickets:
        if _diagnostics.detailed: _diagnostics.step('catalog.active_cases:L83:loop', case=case)
        if ui_dir:
            if _diagnostics.detailed: _diagnostics.step('catalog.active_cases:L84:then')
            case['_ui_dir'] = ui_dir
        if case['task_type'] == 'macro':
            if _diagnostics.detailed: _diagnostics.step('catalog.active_cases:L86:then')
            continue
        if case['role'] == 'child':
            if _diagnostics.detailed: _diagnostics.step('catalog.active_cases:L88:then')
            parent = str((Path(case['_config']).parent / case['macro_ticket']).resolve())
            if (case['_config'], case['_root']) not in members.get(parent, ()):
                if _diagnostics.detailed: _diagnostics.step('catalog.active_cases:L90:then')
                continue
        result.append(case)
    roots = [case['_root'] for case in result]
    if check_duplicates and len(roots) != len(set(roots)):
        if _diagnostics.detailed: _diagnostics.step('catalog.active_cases:L94:then')
        raise ConfigError(_message('duplicate_case'))
    return result


class TicketIndex:
    @_diagnostics.trace
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

    @_diagnostics.trace
    def _discover(self):
        paths = set(self.paths)
        for pattern in self.patterns:
            if _diagnostics.detailed: _diagnostics.step('catalog.TicketIndex._discover:L118:loop', pattern=pattern)
            paths.update(glob.glob(pattern, recursive=True))
        return paths

    @_diagnostics.trace
    def _matches(self, path):
        from fnmatch import fnmatchcase
        return path in self.paths or any(
            str(Path(path).parent) == str(Path(pattern).parent)
            and fnmatchcase(Path(path).name, Path(pattern).name) for pattern in self.patterns)

    @staticmethod
    @_diagnostics.trace
    def _stamp(path):
        s = os.stat(path, follow_symlinks=False)
        return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)

    @_diagnostics.trace
    def refresh(self, force=False):
        from .tickets import ticket_lock
        dynamic = any(glob.has_magic(str(Path(p).parent)) for p in self.patterns)
        folders = {str(Path(p).parent) for p in (*self.paths, *self.patterns)
                   if not glob.has_magic(str(Path(p).parent))}
        discovered = self._discover() if dynamic else None
        if discovered is not None:
            if _diagnostics.detailed: _diagnostics.step('catalog.TicketIndex.refresh:L139:then')
            folders.update(str(Path(p).parent) for p in discovered)
        with ExitStack() as locks:
            for folder in sorted(folders):
                if _diagnostics.detailed: _diagnostics.step('catalog.TicketIndex.refresh:L142:loop', folder=folder)
                locks.enter_context(ticket_lock(folder))
            with self.mutex:
                try:
                    full = force or self.invalid
                    if self.watch is None or self.watch.folders != folders:
                        if _diagnostics.detailed: _diagnostics.step('catalog.TicketIndex.refresh:L147:then')
                        if self.watch:
                            if _diagnostics.detailed: _diagnostics.step('catalog.TicketIndex.refresh:L148:then')
                            self.watch.close()
                        self.watch = FolderWatch(sorted(folders))
                        full = True
                    changed, lost = self.watch.drain()
                    if lost and self.watch.fd >= 0:
                        if _diagnostics.detailed: _diagnostics.step('catalog.TicketIndex.refresh:L153:then')
                        self.watch.close()
                        self.watch = FolderWatch(sorted(folders))
                        full = True
                    fallback = dynamic or self.watch.fd < 0
                    stamps = self.stamps.copy()
                    if full or fallback:
                        if _diagnostics.detailed: _diagnostics.step('catalog.TicketIndex.refresh:L159:then')
                        paths = self._discover()
                        stamps = {path: self._stamp(path) for path in paths}
                        changed = (paths | set(self.documents)) if full else {
                            p for p in paths | set(self.documents) if stamps.get(p) != self.stamps.get(p)}
                    else:
                        if _diagnostics.detailed: _diagnostics.step('catalog.TicketIndex.refresh:L159:else')
                        changed = {p for p in changed if self._matches(p)}
                        paths = (set(self.documents) | changed)
                        for p in changed:
                            if _diagnostics.detailed: _diagnostics.step('catalog.TicketIndex.refresh:L167:loop', p=p)
                            if os.path.lexists(p):
                                if _diagnostics.detailed: _diagnostics.step('catalog.TicketIndex.refresh:L168:then')
                                stamps[p] = self._stamp(p)
                            else:
                                if _diagnostics.detailed: _diagnostics.step('catalog.TicketIndex.refresh:L168:else')
                                paths.discard(p)
                                stamps.pop(p, None)
                    if not changed and not full:
                        if _diagnostics.detailed: _diagnostics.step('catalog.TicketIndex.refresh:L173:then')
                        return self
                    documents = self.documents.copy()
                    raw = self.raw.copy()
                    errors = self.errors.copy()
                    for path in sorted(changed):
                        if _diagnostics.detailed: _diagnostics.step('catalog.TicketIndex.refresh:L178:loop', path=path)
                        errors.pop(path, None)
                        if path not in paths:
                            if _diagnostics.detailed: _diagnostics.step('catalog.TicketIndex.refresh:L180:then')
                            documents.pop(path, None)
                            raw.pop(path, None)
                            continue
                        try:
                            if Path(path).is_symlink():
                                if _diagnostics.detailed: _diagnostics.step('catalog.TicketIndex.refresh:L185:then')
                                raise ConfigError(_message('path_escape', path=path))
                            case = load_case(path)
                            raw[path] = read_json(path)
                            if self.ui_dir:
                                if _diagnostics.detailed: _diagnostics.step('catalog.TicketIndex.refresh:L189:then')
                                case['_ui_dir'] = self.ui_dir
                            documents[path] = case
                        except (OSError, ValueError) as exc:
                            if _diagnostics.enabled: _diagnostics.step('catalog.TicketIndex.refresh:L192:except')
                            if self.strict:
                                if _diagnostics.detailed: _diagnostics.step('catalog.TicketIndex.refresh:L193:then')
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
                        if _diagnostics.detailed: _diagnostics.step('catalog.TicketIndex.refresh:L205:loop', path=path)
                        before, after = self.documents.get(path, {}), documents.get(path, {})
                        if before.get('task_type') == 'macro' or after.get('task_type') == 'macro':
                            if _diagnostics.detailed: _diagnostics.step('catalog.TicketIndex.refresh:L207:then')
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
                        if _diagnostics.detailed: _diagnostics.step('catalog.TicketIndex.refresh:L218:loop', macro=macro)
                        for row in macro['cases']:
                            if _diagnostics.detailed: _diagnostics.step('catalog.TicketIndex.refresh:L219:loop', row=row)
                            self.parents.setdefault(row['case_dir'], set()).add(macro['_root'])
                    self.invalid = False
                    return self
                except BaseException:
                    # Drained events cannot be replayed after a partial/invalid edit.
                    if _diagnostics.enabled: _diagnostics.step('catalog.TicketIndex.refresh:L223:except')
                    self.invalid = True
                    raise

    @_diagnostics.trace
    def tickets(self, *, submit_only=False):
        with self.mutex:
            return deepcopy([c for _, c in sorted(self.documents.items())
                             if not submit_only or c.get('queue', {}).get('submit')])

    @_diagnostics.trace
    def queue_profiles(self):
        """Return only queue profile fields without copying large macro rows."""
        with self.mutex:
            return [dict(execution_queue=deepcopy(case['execution_queue']))
                    for case in self.documents.values() if case.get('execution_queue')]

    @_diagnostics.trace
    def cases(self, roots=None):
        with self.mutex:
            return deepcopy(list(self.roots.values()) if roots is None else
                            [self.roots[r] for r in roots if r in self.roots])

    @_diagnostics.trace
    def document(self, path, *, raw=False):
        with self.mutex:
            if str(path) in self.errors:
                if _diagnostics.detailed: _diagnostics.step('catalog.TicketIndex.document:L246:then')
                raise ValueError(self.errors[str(path)])
            value = (self.raw if raw else self.documents).get(str(path))
            if value is None:
                if _diagnostics.detailed: _diagnostics.step('catalog.TicketIndex.document:L249:then')
                raise FileNotFoundError(str(path))
            return deepcopy(value)

    @_diagnostics.trace
    def lookup(self, root):
        with self.mutex:
            return deepcopy(self.roots.get(root))

    @_diagnostics.trace
    def related_macros(self, roots):
        with self.mutex:
            selected = set(roots) & self.macros.keys()
            for root in roots:
                if _diagnostics.detailed: _diagnostics.step('catalog.TicketIndex.related_macros:L260:loop', root=root)
                selected.update(self.parents.get(root, ()))
            return deepcopy([self.macros[r] for r in sorted(selected)])

    @_diagnostics.trace
    def changes(self):
        with self.mutex:
            return self.dirty.copy()

    @_diagnostics.trace
    def acknowledge(self, changes):
        with self.mutex:
            for root, version in changes.items():
                if _diagnostics.detailed: _diagnostics.step('catalog.TicketIndex.acknowledge:L270:loop', root=root, version=version)
                if self.dirty.get(root) == version:
                    if _diagnostics.detailed: _diagnostics.step('catalog.TicketIndex.acknowledge:L271:then')
                    self.dirty.pop(root)


@_diagnostics.trace
@lru_cache(maxsize=16)
def _index(paths, patterns, ui_dir, strict=True):
    return TicketIndex(paths, patterns, ui_dir, strict)


@_diagnostics.trace
def ticket_index(bot, *, force=False):
    return _index(tuple(bot.get('cases', ())), tuple(bot.get('case_globs', ())),
                  bot.get('_ui_dir')).refresh(force=force)


@_diagnostics.trace
def folder_index(folder):
    return _index((), (str(Path(folder) / '*.json'),), None, False).refresh()
