"""Loopback web adapter. The existing bot remains the only queue controller."""
from . import diagnostics as _diagnostics
import gzip
import os
import hashlib
import hmac
import subprocess
import json
import logging
from logging.handlers import RotatingFileHandler
import mimetypes
import re
import secrets
import signal
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, quote

from .artifacts import MAX_DOCUMENT, export_files, residual_files
from .bot import Bot, case_id
from .config import glob_patterns, inside, load_case
from .control import control_times
from .editor import TicketService, case_browser_start, lines, validate_export
from .logs import estimate, recent_case_log
from .patterns import PatternLibrary
from .queue_control import cancel_queued_jobs, interrupt_running_job, interrupt_running_jobs
from .run_views import job_view, running_macro_views, tracking_registry
from .storage import ACTIVE, TERMINAL, Store
from .ticket_run import TicketRunner
from .tickets import discover_cases, has_postprocessing, sync_ticket_states

LOG = logging.getLogger(__name__)
STATIC = Path(__file__).with_name('web_static')
MAX_BODY = 2 * 1024 * 1024


class WebApp:
    @_diagnostics.trace
    def __init__(self, config, store=None):
        self.config = config
        self.store = store or Store(config['state_dir'])
        self.service = TicketService(Path(config['_path']).parent / 'tickets')
        self.runner = TicketRunner(self.service, config, self.store)
        self.bot = Bot(config, self.store, None)
        self.library = PatternLibrary(self.service.folder.parent / 'ticket-patterns.json')
        self.token = secrets.token_urlsafe(32)
        _diagnostics.remember_secret(self.token)
        self._read_lock = threading.RLock()
        self._fallback_snapshot = {}
        self._catalog_generation = None
        self._catalog_entries = {}
        self._case_generation = None
        self._log_cursors = {}


    @_diagnostics.trace
    def fresh(self):
        snap = self.runner._snapshot()
        sync_ticket_states(self.config, self.store, snap)
        return snap

    @_diagnostics.trace
    def display_snapshot(self):
        """Read-only display path. Admission and /stat still perform fresh scans."""
        from . import ticket_run
        with self._read_lock:
            saved = self.store.get('snapshot', {})
            snap = max((saved, self._fallback_snapshot), key=lambda s: s.get('at', 0))
            max_age = max(15, 3 * self.config.get('poll_seconds', 5))
            if 0 <= time.time() - snap.get('at', 0) <= max_age:
                return snap, None
            try:
                snap = dict(ticket_run.snapshot(self.config['ofps_command']), at=time.time())
                self._fallback_snapshot = snap
                return snap, None
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                return snap, str(exc)

    @_diagnostics.trace
    def catalog(self):
        """Reuse TicketIndex; runtime-only queue changes do not change metadata version."""
        from .catalog import folder_index
        with self._read_lock:
            index = folder_index(self.service.folder)
            with index.mutex:
                generation = (id(index), index.generation)
                if generation == self._catalog_generation:
                    return self._catalog
                tickets = index.tickets()
                entries, rows = {}, []
                for case in tickets:
                    path = case['_config']
                    prior = self._catalog_entries.get(path)
                    stamp = index.stamps.get(path)
                    if prior and prior[0] == stamp:
                        row = prior[1]
                    else:
                        name = Path(path).name
                        row = dict(filename=name, name=case['name'], case_dir=case['_root'],
                                   task_type=case['task_type'], role=case['role'],
                                   count=len(case.get('cases', [])),
                                   revision=self.service.revision(name, data=index.raw[path]))
                    entries[path] = (stamp, row)
                    rows.append(row)
                rows.extend(dict(filename=Path(path).name, name=Path(path).name,
                                 state='invalid', enabled=False, run_enabled=False,
                                 queue_enabled=False, error=error)
                            for path, error in index.errors.items())
            rows.sort(key=lambda r: r['filename'])
            version = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()[:24]
            self._catalog = dict(tickets=tickets, rows=rows, version=version)
            self._catalog_entries, self._catalog_generation = entries, generation
            return self._catalog

    @_diagnostics.trace
    def ticket_rows(self):
        catalog = self.catalog()
        states = self.runner.states(catalog['tickets'])
        return [dict(row, **states.get(row['filename'], {})) for row in catalog['rows']]

    @_diagnostics.trace
    def read_log(self, case):
        # Parser already handles truncation, inode replacement and stage switches.
        key = (case['_root'], json.dumps(case.get('watcher', {}), sort_keys=True))
        with self._read_lock:
            state, path = recent_case_log(case, self._log_cursors.get(key))
            self._log_cursors[key] = state
            if len(self._log_cursors) > 64:
                self._log_cursors.pop(next(iter(self._log_cursors)))
            return state, path

    @_diagnostics.trace
    def overview(self, query=None):
        query = query or {}
        view = query.get('view', 'all')
        catalog = self.catalog()
        counts = dict(total=len(catalog['rows']),
                      macro=sum(c.get('task_type') == 'macro' for c in catalog['rows']))
        result = dict(ticket_counts=counts, catalog_version=catalog['version'])
        snap, error = self.display_snapshot()
        result.update(at=snap.get('at'), error=error,
                      paused=self.store.get('queue_paused', False),
                      scheduler_enabled=self.config['scheduler']['enabled'],
                      monitor_error=self.store.get('monitor_error'))
        if view in ('all', 'tickets'):
            states = self.runner.states(catalog['tickets'], observed=snap,
                                        capacity_for=None if view == 'all' else {query.get('selected')})
            states.update({r['filename']: {k: v for k, v in r.items() if k in
                           ('state', 'enabled', 'run_enabled', 'queue_enabled', 'error')}
                           for r in catalog['rows'] if r.get('state') == 'invalid'})
            result['ticket_states'] = states
            if view == 'all' or query.get('catalog') != catalog['version']:
                result['tickets'] = [dict(row, **states.get(row['filename'], {}))
                                     for row in catalog['rows']]
        if view == 'data':
            rows = self.case_rows()
            result['catalog_version'] = self._case_version
            if query.get('catalog') != self._case_version:
                result['cases'] = rows
            return result
        cases = list(self.cases().values())
        registry = tracking_registry(cases)
        jobs = self.store.jobs(ACTIVE)
        result['queue'] = [job_view(j, registry) for j in jobs]
        if view == 'tickets':
            return result
        history = self.store.jobs(TERMINAL, newest=True, limit=100 if view in ('all', 'queue') else 6)
        result['history'] = [job_view(j, registry) for j in history]
        if view == 'queue':
            return result
        live = []
        for run in self.bot.active_runs(snap, cases):
            case = run['case']
            item = job_view(run, registry)
            item.update(case_id=case_id(case), registered=run['registered'], owner=run['owner'])
            if run['registered']:
                telemetry, _ = self.read_log(case)
                prediction = estimate(case, telemetry, time.time() - run['started'])
                if prediction.get('basis') == 'unknown':
                    prediction = estimate(case, telemetry, time.time() - run['started'],
                                          self.store.runtime_history(case, run.get('actual_cores')))
                item.update(time=telemetry.get('time'), estimate=prediction)
            live.append(item)
        macros = [c for c in catalog['tickets'] if c['task_type'] == 'macro'
                  and c.get('queue', {}).get('state') == 'running']
        known = {j['id'] for j in jobs}
        jobs += self.store.jobs(ids=[r['job_id'] for m in macros for r in m['cases']
                                     if r.get('job_id') and r['job_id'] not in known])
        result.update(live=live, live_macros=running_macro_views(
            macros, cases, jobs, self.store, log_reader=self.read_log))
        return result

    @_diagnostics.trace
    def cases(self):
        from .catalog import ticket_index
        with self._read_lock:
            index = ticket_index(self.config)
            with index.mutex:
                generation = (id(index), index.generation)
                if generation != self._case_generation:
                    self._cases = {case_id(c): c for c in index.cases()}
                    self._case_rows = [dict(id=cid, name=c['name'], case_dir=c['_root'],
                        ticket=Path(c['_config']).name if Path(c['_config']).parent == self.service.folder else None,
                        residual_pattern=c.get('residual_pattern'), exports=c['exports'])
                        for cid, c in self._cases.items()]
                    self._case_version = hashlib.sha256(json.dumps(self._case_rows, sort_keys=True).encode()).hexdigest()[:24]
                    self._case_generation = generation
            return self._cases

    @_diagnostics.trace
    def case(self, cid):
        case = self.cases().get(cid)
        if case is None:
            if _diagnostics.detailed: _diagnostics.step('web.WebApp.case:L108:then')
            raise ValueError('케이스가 없거나 등록이 변경되었습니다. 목록을 새로고침하세요.')
        return case

    @_diagnostics.trace
    def case_rows(self):
        self.cases()
        return self._case_rows

    @_diagnostics.trace
    def detail(self, cid):
        case = self.case(cid)
        telemetry, path = self.read_log(case)
        run = self.bot.latest_run(case)
        started = (run or {}).get('started') or time.time()
        elapsed = max(0, ((run or {}).get('finished') or time.time()) - started)
        return dict(name=case['name'], case_dir=case['_root'], control=control_times(case),
                    telemetry={k: telemetry.get(k) for k in ('time', 'execution_time', 'clock_time',
                               'errors', 'missing', 'residuals')}, log=str(path),
                    estimate=estimate(case, telemetry, elapsed,
                                      (run or {}).get('runtime_history', [])),
                    latest=job_view(run, tracking_registry(self.cases().values())) if run else None,
                    history=self.store.runtime_history(case)[:10])

    @_diagnostics.trace
    def artifact_paths(self, cid, source, *, case=None):
        case = self.case(cid) if case is None else case
        if source == 'residual':
            if _diagnostics.detailed: _diagnostics.step('web.WebApp.artifact_paths:L134:then')
            return case, [Path(i['path']) for i in residual_files(case)]
        export = next((e for e in case['exports'] if 'export:' + e['name'] == source), None)
        if export is None:
            if _diagnostics.detailed: _diagnostics.step('web.WebApp.artifact_paths:L137:then')
            raise ValueError('등록되지 않은 요청 데이터입니다.')
        return case, export_files(case, export)

    @_diagnostics.trace
    def artifacts(self, cid):
        case = self.case(cid)
        groups = []
        definitions = ([dict(name='residual', source='residual', pattern=case['residual_pattern'])]
                       if case.get('residual_pattern') else []) + [dict(e, source='export:' + e['name'])
                                                                 for e in case['exports']]
        for definition in definitions:
            if _diagnostics.detailed: _diagnostics.step('web.WebApp.artifacts:L147:loop', definition=definition)
            group = dict(name=definition['name'], pattern=definition['pattern'], files=[])
            try:
                _, paths = self.artifact_paths(cid, definition['source'], case=case)
                for path in paths:
                    if _diagnostics.detailed: _diagnostics.step('web.WebApp.artifacts:L151:loop', path=path)
                    relative = str(path.relative_to(case['_root']))
                    stat = path.stat()
                    query = urlencode(dict(case=cid, source=definition['source'], file=relative))
                    group['files'].append(dict(name=path.name, path=relative, size=stat.st_size,
                                               modified=stat.st_mtime, url='/api/file?' + query,
                                               preview=path.suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp'),
                                               too_large=stat.st_size > MAX_DOCUMENT))
            except (OSError, ValueError) as exc:
                if _diagnostics.enabled: _diagnostics.step('web.WebApp.artifacts:L159:except')
                group['error'] = str(exc)
            groups.append(group)
        return groups

    @_diagnostics.trace
    def file(self, query):
        case, allowed = self.artifact_paths(query['case'], query['source'])
        path = inside(case['_root'], query['file'])
        if path not in allowed:
            if _diagnostics.detailed: _diagnostics.step('web.WebApp.file:L167:then')
            raise ValueError('현재 요청 데이터에 포함되지 않은 파일입니다.')
        # Open first, then check the opened inode, so a replaced symlink cannot
        # expose a different file between validation and streaming (Linux host).
        source = path.open('rb')
        try:
            descriptor = Path('/proc/self/fd') / str(source.fileno())
            if descriptor.exists() and descriptor.resolve() != path:
                if _diagnostics.detailed: _diagnostics.step('web.WebApp.file:L174:then')
                raise ValueError('조회 중 파일 경로가 변경되었습니다. 다시 조회하세요.')
            import os
            size = os.fstat(source.fileno()).st_size
            if size > MAX_DOCUMENT:
                if _diagnostics.detailed: _diagnostics.step('web.WebApp.file:L178:then')
                raise ValueError('요청 데이터 파일은 49 MiB 이하만 다운로드할 수 있습니다.')
            return source, path.name, size
        except Exception:
            if _diagnostics.enabled: _diagnostics.step('web.WebApp.file:L181:except')
            source.close()
            raise

    @_diagnostics.trace
    def browse(self, query):
        # This is a local filesystem picker, not a general file-content server.
        folder = case_browser_start(query.get('path', ''), self.service.folder)
        root = query.get('root', '').strip()
        if root:
            if _diagnostics.detailed: _diagnostics.step('web.WebApp.browse:L189:then')
            base = (self.service.folder / Path(root).expanduser()).resolve()
            try:
                folder.relative_to(base)
            except ValueError:
                if _diagnostics.enabled: _diagnostics.step('web.WebApp.browse:L193:except')
                folder = base
        else:
            if _diagnostics.detailed: _diagnostics.step('web.WebApp.browse:L189:else')
            base = None
        entries = []
        for child in sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if _diagnostics.detailed: _diagnostics.step('web.WebApp.browse:L198:loop', child=child)
            if child.name.startswith('.') or child.is_symlink():
                if _diagnostics.detailed: _diagnostics.step('web.WebApp.browse:L199:then')
                continue
            directory = child.is_dir()
            if not directory and query.get('kind') == 'directory':
                if _diagnostics.detailed: _diagnostics.step('web.WebApp.browse:L202:then')
                continue
            entries.append(dict(name=child.name, path=str(child), directory=directory))
            if len(entries) == 1000:
                if _diagnostics.detailed: _diagnostics.step('web.WebApp.browse:L205:then')
                break
        return dict(path=str(folder), root=str(base) if base else '',
                    parent=str(folder.parent) if folder != base else None,
                    entries=entries, truncated=len(entries) == 1000)

    @_diagnostics.trace
    def get(self, path, query):
        if path == '/api/health':
            if _diagnostics.detailed: _diagnostics.step('web.WebApp.get:L212:then')
            return dict(app='cfd-control-room', version=1, hostname=socket.gethostname())
        if path == '/api/bootstrap':
            if _diagnostics.detailed: _diagnostics.step('web.WebApp.get:L214:then')
            return dict(csrf=self.token, diagnostic_logging=_diagnostics.enabled, diagnostic_level=_diagnostics.level, default_directory=str(case_browser_start('', self.service.folder)),
                        patterns=self.library.load())
        if path == '/api/overview':
            if _diagnostics.detailed: _diagnostics.step('web.WebApp.get:L217:then')
            return self.overview(query)
        if path == '/api/ticket':
            if _diagnostics.detailed: _diagnostics.step('web.WebApp.get:L219:then')
            draft = self.service.open(query['name'])
            # Opening an editor is read-only. Use the monitor/overview snapshot so
            # selecting a ticket never waits for a full process-tree scan. The run
            # request still performs its own fresh scan before changing queue state.
            draft['run_state'] = self.runner.state(query['name'])
            draft['postprocessed'] = [row['case_dir'] for row in draft['values']['cases']
                                      if has_postprocessing(row['case_dir'])]
            return draft
        if path == '/api/cases':
            if _diagnostics.detailed: _diagnostics.step('web.WebApp.get:L228:then')
            return self.case_rows()
        if path == '/api/detail':
            if _diagnostics.detailed: _diagnostics.step('web.WebApp.get:L230:then')
            return self.detail(query['case'])
        if path == '/api/artifacts':
            if _diagnostics.detailed: _diagnostics.step('web.WebApp.get:L232:then')
            return self.artifacts(query['case'])
        if path == '/api/browse':
            if _diagnostics.detailed: _diagnostics.step('web.WebApp.get:L234:then')
            return self.browse(query)
        if path == '/api/control':
            if _diagnostics.detailed: _diagnostics.step('web.WebApp.get:L236:then')
            root = (self.service.folder / Path(query['path']).expanduser()).resolve()
            return control_times({'_root': str(root)})
        raise LookupError('없는 API입니다.')

    @staticmethod
    @_diagnostics.trace
    def revision(data):
        if not isinstance(data.get('revision'), str) or not data['revision']:
            if _diagnostics.detailed: _diagnostics.step('web.WebApp.revision:L243:then')
            raise ValueError('편집 기준 버전이 없습니다. 티켓을 다시 여세요.')
        return data['revision']

    @_diagnostics.trace
    def post(self, path, data):
        if path == '/api/new':
            if _diagnostics.detailed: _diagnostics.step('web.WebApp.post:L248:then')
            return self.service.new(data.get('kind', 'single'))
        if path == '/api/duplicate':
            if _diagnostics.detailed: _diagnostics.step('web.WebApp.post:L250:then')
            return self.service.duplicate(data['name'])
        if path in ('/api/validate', '/api/save'):
            if _diagnostics.detailed: _diagnostics.step('web.WebApp.post:L252:then')
            values = data['values']
            if not isinstance(values, dict):
                if _diagnostics.detailed: _diagnostics.step('web.WebApp.post:L254:then')
                raise ValueError('입력 폼이 올바르지 않습니다.')
            current = data.get('current')
            if current:
                if _diagnostics.detailed: _diagnostics.step('web.WebApp.post:L257:then')
                self.revision(data)
            if path.endswith('validate'):
                if _diagnostics.detailed: _diagnostics.step('web.WebApp.post:L259:then')
                self.service.validate(values, data.get('filename', ''), current)
                return dict(valid=True)
            # Refresh running flags before applying the shared edit guards.
            self.fresh()
            name, _ = self.service.save(values, data.get('filename', ''), current,
                                        expected_revision=data.get('revision'), request_id=data.get('request_id'))
            return self.service.open(name)
        if path == '/api/exports/validate':
            if _diagnostics.detailed: _diagnostics.step('web.WebApp.post:L267:then')
            root = (self.service.folder / Path(data['case_dir']).expanduser()).resolve()
            if 'items' in data:
                items = data['items']
                if not isinstance(items, list):
                    raise ValueError('요청 데이터 목록이 필요합니다.')
                return [validate_export(item, items, root, index) for index, item in enumerate(items)]
            return validate_export(data['item'], data.get('others', []), root, data.get('index'))
        if path == '/api/delete/preview':
            if _diagnostics.detailed: _diagnostics.step('web.WebApp.post:L270:then')
            return self.service.deletion_preview(data['names'])
        if path == '/api/delete':
            if _diagnostics.detailed: _diagnostics.step('web.WebApp.post:L273:then')
            if not data.get('revisions'):
                if _diagnostics.detailed: _diagnostics.step('web.WebApp.post:L274:then')
                raise ValueError('삭제 대상을 먼저 확인하세요.')
            return self.service.delete_many(data['names'], data['revisions'])
        if path == '/api/run':
            if _diagnostics.detailed: _diagnostics.step('web.WebApp.post:L278:then')
            return self.runner.request(data['name'], expected_revision=self.revision(data),
                                       request_id=data.get('request_id'), mode=data.get('mode', 'run'),
                                       lane=data.get('lane', 1))
        if path == '/api/discover':
            if _diagnostics.detailed: _diagnostics.step('web.WebApp.post:L282:then')
            snap = self.fresh()
            end = data.get('end_time')
            include = data.get('include_patterns', [])
            exclude = data.get('exclude_patterns', [])
            include = lines(include) if isinstance(include, str) else include
            exclude = lines(exclude) if isinstance(exclude, str) else exclude
            glob_patterns(include, 'discovery.include_patterns')
            glob_patterns(exclude, 'discovery.exclude_patterns')
            rows, skipped = discover_cases(data['case_dir'], snap['cases'],
                                           float(end) if end else None, include, exclude)
            return dict(cases=rows, skipped=skipped,
                        postprocessed=[r['case_dir'] for r in rows if has_postprocessing(r['case_dir'])])
        if path == '/api/patterns':
            if _diagnostics.detailed: _diagnostics.step('web.WebApp.post:L295:then')
            self.library.save(data['name'], data['rules'])
            return self.library.load()
        if path == '/api/queue':
            if _diagnostics.detailed: _diagnostics.step('web.WebApp.post:L298:then')
            action = data['action']
            if action in ('pause', 'resume'):
                if _diagnostics.detailed: _diagnostics.step('web.WebApp.post:L300:then')
                self.store.put('queue_paused', action == 'pause')
                return dict(ok=True)
            if action in ('cancel', 'cancel_many'):
                if _diagnostics.detailed: _diagnostics.step('web.WebApp.post:L303:then')
                ids = [data['id']] if action == 'cancel' else data.get('ids', [])
                result = cancel_queued_jobs(self.store, ids, ui=self.bot.ui)
                if action == 'cancel' and not result['cancelled']:
                    if _diagnostics.detailed: _diagnostics.step('web.WebApp.post:L306:then')
                    raise ValueError('대기 중인 작업만 취소할 수 있습니다. 상태를 새로고침하세요.')
                return result
            if action == 'interrupt_many':
                if _diagnostics.detailed: _diagnostics.step('web.WebApp.post:M375:then')
                return interrupt_running_jobs(self.store, data.get('ids', []), ui=self.bot.ui)
            if action == 'interrupt':
                if _diagnostics.detailed: _diagnostics.step('web.WebApp.post:M377:then')
                job = interrupt_running_job(self.store, data.get('id'), ui=self.bot.ui)
                if job is None:
                    if _diagnostics.detailed: _diagnostics.step('web.WebApp.post:M379:then')
                    raise ValueError('이미 종료되었거나 중단할 수 없는 작업입니다. 상태를 새로고침하세요.')
                return job_view(job, tracking_registry(list(self.cases().values())))
            raise ValueError('알 수 없는 큐 동작입니다.')
        raise LookupError('없는 API입니다.')


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    @_diagnostics.trace
    def __init__(self, app, port=8766):
        self.app = app
        super().__init__(('127.0.0.1', port), Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = 'CFDWeb/1'

    @_diagnostics.trace
    def setup(self):
        super().setup()
        self.connection.settimeout(60)

    @_diagnostics.trace
    def log_message(self, fmt, *args):
        # Never log request bodies, CSRF tokens, cookies or artifact query paths.
        LOG.info('web %s %s status=%s', self.command, urlsplit(self.path).path,
                 args[1] if len(args) > 1 else '-')

    @_diagnostics.trace
    def trusted(self):
        host = self.headers.get('Host', '')
        # SSH -L may expose a different browser-side port. Validate the local
        # hostname, not the backend port, while keeping strict Origin matching.
        local = re.fullmatch(r'(?:localhost|127\.0\.0\.1|\[::1\])(?::([0-9]{1,5}))?', host)
        if not local or (local[1] and not 1 <= int(local[1]) <= 65535):
            if _diagnostics.detailed: _diagnostics.step('web.Handler.trusted:L339:then')
            return False
        origin = self.headers.get('Origin')
        return ((not origin or origin == 'http://' + host)
                and self.headers.get('Sec-Fetch-Site') not in ('cross-site', 'same-site'))

    @_diagnostics.trace
    def send_headers(self, status, content_type, size, extra=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(size))
        self.send_header('Cache-Control', (extra or {}).get('Cache-Control', 'no-store'))
        self.send_header('X-CFD-Diagnostics', '1' if _diagnostics.enabled else '0')
        self.send_header('X-CFD-Diagnostics-Level', _diagnostics.level)
        if _diagnostics.enabled:
            self.send_header('X-CFD-Trace', _diagnostics.trace_id())
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "img-src 'self' data:; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'")
        for key, value in (extra or {}).items():
            if _diagnostics.detailed: _diagnostics.step('web.Handler.send_headers:L355:loop', key=key, value=value)
            if key != 'Cache-Control':
                self.send_header(key, value)
        self.end_headers()

    @_diagnostics.trace
    def respond(self, data, status=200):
        payload = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
        extra = {'Vary': 'Accept-Encoding'}
        if len(payload) > 1024 and 'gzip' in self.headers.get('Accept-Encoding', ''):
            payload = gzip.compress(payload, compresslevel=1)
            extra['Content-Encoding'] = 'gzip'
        self.send_headers(status, 'application/json; charset=utf-8', len(payload), extra)
        self.wfile.write(payload)

    @_diagnostics.trace
    def handle_request(self, mutation=False):
        try:
            if not self.trusted():
                if _diagnostics.detailed: _diagnostics.step('web.Handler.handle_request:L366:then')
                return self.respond(dict(error='localhost의 같은 출처에서만 사용할 수 있습니다.'), 403)
            url = urlsplit(self.path)
            query = {key: values[-1] for key, values in parse_qs(url.query).items()}
            app = self.server.app
            if mutation:
                if _diagnostics.detailed: _diagnostics.step('web.Handler.handle_request:L371:then')
                if (self.headers.get('Content-Type', '').split(';')[0] != 'application/json'
                        or not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), app.token)):
                    if _diagnostics.detailed: _diagnostics.step('web.Handler.handle_request:L372:then')
                    return self.respond(dict(error='인증 토큰이 변경되었습니다. 페이지를 새로고침하세요.'), 403)
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_BODY:
                    if _diagnostics.detailed: _diagnostics.step('web.Handler.handle_request:L376:then')
                    return self.respond(dict(error='요청 크기는 2 MiB 이하이어야 합니다.'), 413)
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    if _diagnostics.detailed: _diagnostics.step('web.Handler.handle_request:L379:then')
                    raise ValueError('JSON 객체가 필요합니다.')
                return self.respond(app.post(url.path, data))
            if url.path == '/api/file':
                if _diagnostics.detailed: _diagnostics.step('web.Handler.handle_request:L382:then')
                source, name, size = app.file(query)
                with source:
                    mime = mimetypes.guess_type(name)[0]
                    preview = mime in ('image/png', 'image/jpeg', 'image/webp') and query.get('download') != '1'
                    disposition = 'inline' if preview else 'attachment'
                    stat = os.fstat(source.fileno())
                    etag = '"%x-%x-%x-%x"' % (stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, size)
                    extra = {'Content-Disposition': disposition + "; filename*=UTF-8''" + quote(name)}
                    if preview:
                        extra.update({'ETag': etag, 'Cache-Control': 'private, no-cache'})
                        if self.headers.get('If-None-Match') == etag:
                            self.send_headers(304, mime, 0, extra)
                            return
                    self.send_headers(200, mime if preview else 'application/octet-stream', size,
                                 extra)
                    remaining = size
                    while remaining:
                        if _diagnostics.detailed: _diagnostics.step('web.Handler.handle_request:L391:loop')
                        block = source.read(min(65536, remaining))
                        if not block:
                            if _diagnostics.detailed: _diagnostics.step('web.Handler.handle_request:L393:then')
                            break
                        self.wfile.write(block)
                        remaining -= len(block)
                return
            if url.path.startswith('/api/'):
                if _diagnostics.detailed: _diagnostics.step('web.Handler.handle_request:L398:then')
                return self.respond(app.get(url.path, query))
            assets = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css',
                      '/diagnostics.js': 'diagnostics.js', '/diagnostic_codes.js': 'diagnostic_codes.js'}
            for platform in ('Windows', 'macOS'):
                if _diagnostics.detailed: _diagnostics.step('web.Handler.handle_request:L401:loop', platform=platform)
                name = f'downloads/CFD-Control-Room-{platform}.zip'
                assets['/' + name] = name
            asset = assets.get(url.path)
            if asset is None:
                if _diagnostics.detailed: _diagnostics.step('web.Handler.handle_request:L405:then')
                raise LookupError('없는 페이지입니다.')
            payload = (STATIC / asset).read_bytes()
            etag = '"' + hashlib.sha256(payload).hexdigest() + '"'
            if self.headers.get('If-None-Match') == etag:
                self.send_headers(304, mimetypes.guess_type(asset)[0] or 'text/plain', 0,
                                  {'ETag': etag, 'Cache-Control': 'private, no-cache'})
                return
            mime = mimetypes.guess_type(asset)[0] or 'text/plain'
            extra = {'Content-Disposition': 'attachment; filename="' + Path(asset).name + '"'} if asset.endswith('.zip') else {}
            extra.update({'ETag': etag, 'Cache-Control': 'private, no-cache'})
            self.send_headers(200, mime if asset.endswith('.zip') else mime + '; charset=utf-8', len(payload), extra)
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            if _diagnostics.enabled: _diagnostics.step('web.Handler.handle_request:L412:except')
            pass
        except (KeyError, TypeError, AttributeError) as exc:
            if _diagnostics.enabled: _diagnostics.step('web.Handler.handle_request:L414:except')
            LOG.warning('Invalid web input: %s', type(exc).__name__)
            self.respond(dict(error='필수 입력이 없거나 입력 형식이 올바르지 않습니다.'), 400)
        except LookupError as exc:
            if _diagnostics.enabled: _diagnostics.step('web.Handler.handle_request:L417:except')
            self.respond(dict(error=str(exc)), 404)
        except (ValueError, OSError) as exc:
            if _diagnostics.enabled: _diagnostics.step('web.Handler.handle_request:L419:except')
            self.respond(dict(error=str(exc)), 400)
        except Exception:
            if _diagnostics.enabled: _diagnostics.step('web.Handler.handle_request:L421:except')
            LOG.exception('Web request failed')
            self.respond(dict(error='요청 처리 중 오류가 발생했습니다. 웹 서비스 로그를 확인하세요.'), 500)

    @_diagnostics.trace
    def do_GET(self):
        self.handle_request()

    @_diagnostics.trace
    def do_POST(self):
        self.handle_request(mutation=True)


@_diagnostics.trace
def serve(config, store=None, port=8766):
    server = WebServer(WebApp(config, store), port)
    handler = RotatingFileHandler(server.app.store.root / 'web.log', maxBytes=5 * 1024 * 1024,
                                 backupCount=3, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    LOG.addHandler(handler)
    for sig in (signal.SIGINT, signal.SIGTERM):
        if _diagnostics.detailed: _diagnostics.step('web.serve:L438:loop', sig=sig)
        signal.signal(sig, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    LOG.info('CFD web: http://localhost:%s', server.server_port)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        LOG.removeHandler(handler)
        handler.close()


@_diagnostics.trace
def main(argv=None):
    import sys
    from .cli import main as cli_main
    return cli_main(['web', *(sys.argv[1:] if argv is None else argv)])
