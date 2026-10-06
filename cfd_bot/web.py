"""Loopback web adapter. The existing bot remains the only queue controller."""
import hmac
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
from .queue_control import cancel_queued_jobs
from .run_views import job_view, running_macro_views, tracking_registry
from .storage import Store
from .ticket_run import TicketRunner
from .tickets import discover_cases, has_postprocessing, sync_ticket_states

LOG = logging.getLogger(__name__)
STATIC = Path(__file__).with_name('web_static')
MAX_BODY = 2 * 1024 * 1024


class WebApp:
    def __init__(self, config, store=None):
        self.config = config
        self.store = store or Store(config['state_dir'])
        self.service = TicketService(Path(config['_path']).parent / 'tickets')
        self.runner = TicketRunner(self.service, config, self.store)
        self.bot = Bot(config, self.store, None)
        self.library = PatternLibrary(self.service.folder.parent / 'ticket-patterns.json')
        self.token = secrets.token_urlsafe(32)

    def fresh(self):
        snap = self.runner._snapshot()
        sync_ticket_states(self.config, self.store, snap)
        return snap

    def ticket_rows(self):
        from .catalog import folder_index
        index = folder_index(self.service.folder)
        tickets = index.tickets()
        states = self.runner.states(tickets)
        rows = []
        for case in tickets:
            name = Path(case['_config']).name
            rows.append(dict(filename=name, name=case['name'], case_dir=case['_root'],
                             task_type=case['task_type'], role=case['role'],
                             count=len(case.get('cases', [])), queue=case.get('queue', {}),
                             revision=self.service.revision(name, data=index.document(self.service.path(name), raw=True)), **states[name]))
        rows.extend(dict(filename=Path(path).name, name=Path(path).name, state='invalid',
                         enabled=False, run_enabled=False, queue_enabled=False, error=error)
                    for path, error in index.errors.copy().items())
        return sorted(rows, key=lambda row: row['filename'])

    def overview(self):
        error = None
        try:
            snap = self.fresh()
        except (ValueError, OSError) as exc:
            snap, error = self.store.get('snapshot', {}), str(exc)
        cases = list(self.bot.cases().values())
        registry = tracking_registry(cases)
        live = []
        for run in self.bot.active_runs(snap, cases):
            case = run['case']
            item = job_view(run, registry)
            item.update(case_id=case_id(case), registered=run['registered'], owner=run['owner'])
            if run['registered']:
                telemetry, _ = recent_case_log(case)
                item['time'] = telemetry.get('time')
                item['estimate'] = estimate(case, telemetry, time.time() - run['started'],
                                            self.store.runtime_history(case, run.get('actual_cores')))
            live.append(item)
        jobs = self.store.jobs()
        tickets = self.ticket_rows()
        macros = [ticket for ticket in tickets if ticket.get('task_type') == 'macro']
        macro_documents = []
        for row in macros:
            try:
                from .catalog import folder_index
                macro_documents.append(folder_index(self.service.folder).document(self.service.path(row['filename'])))
            except (OSError, ValueError):
                continue
        return dict(at=snap.get('at'), error=error, live=live, tickets=tickets,
                    live_macros=running_macro_views(macro_documents, cases, jobs, self.store),
                    queue=[job_view(j, registry) for j in jobs if j['status'] in
                           ('queued', 'starting', 'running', 'postprocessing')],
                    history=[job_view(j, registry) for j in reversed(jobs) if j['status'] not in
                             ('queued', 'starting', 'running', 'postprocessing')][:100],
                    paused=self.store.get('queue_paused', False),
                    scheduler_enabled=self.config['scheduler']['enabled'],
                    monitor_error=self.store.get('monitor_error'))

    def case(self, cid):
        case = self.bot.cases().get(cid)
        if case is None:
            raise ValueError('케이스가 없거나 등록이 변경되었습니다. 목록을 새로고침하세요.')
        return case

    def case_rows(self):
        return [dict(id=cid, name=c['name'], case_dir=c['_root'],
                     ticket=Path(c['_config']).name if Path(c['_config']).parent == self.service.folder else None,
                     residual_pattern=c.get('residual_pattern'), exports=c['exports'])
                for cid, c in self.bot.cases().items()]

    def detail(self, cid):
        case = self.case(cid)
        telemetry, path = recent_case_log(case)
        run = self.bot.latest_run(case)
        started = (run or {}).get('started') or time.time()
        elapsed = max(0, ((run or {}).get('finished') or time.time()) - started)
        return dict(name=case['name'], case_dir=case['_root'], control=control_times(case),
                    telemetry={k: telemetry.get(k) for k in ('time', 'execution_time', 'clock_time',
                               'errors', 'missing', 'residuals')}, log=str(path),
                    estimate=estimate(case, telemetry, elapsed,
                                      self.store.runtime_history(case, (run or {}).get('actual_cores'))),
                    latest=job_view(run, tracking_registry(self.bot.cases().values())) if run else None,
                    history=self.store.runtime_history(case)[:10])

    def artifact_paths(self, cid, source):
        case = self.case(cid)
        if source == 'residual':
            return case, [Path(i['path']) for i in residual_files(case)]
        export = next((e for e in case['exports'] if 'export:' + e['name'] == source), None)
        if export is None:
            raise ValueError('등록되지 않은 요청 데이터입니다.')
        return case, export_files(case, export)

    def artifacts(self, cid):
        case = self.case(cid)
        groups = []
        definitions = ([dict(name='residual', source='residual', pattern=case['residual_pattern'])]
                       if case.get('residual_pattern') else []) + [dict(e, source='export:' + e['name'])
                                                                 for e in case['exports']]
        for definition in definitions:
            group = dict(name=definition['name'], pattern=definition['pattern'], files=[])
            try:
                _, paths = self.artifact_paths(cid, definition['source'])
                for path in paths:
                    relative = str(path.relative_to(case['_root']))
                    stat = path.stat()
                    query = urlencode(dict(case=cid, source=definition['source'], file=relative))
                    group['files'].append(dict(name=path.name, path=relative, size=stat.st_size,
                                               modified=stat.st_mtime, url='/api/file?' + query,
                                               preview=path.suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp'),
                                               too_large=stat.st_size > MAX_DOCUMENT))
            except (OSError, ValueError) as exc:
                group['error'] = str(exc)
            groups.append(group)
        return groups

    def file(self, query):
        case, allowed = self.artifact_paths(query['case'], query['source'])
        path = inside(case['_root'], query['file'])
        if path not in allowed:
            raise ValueError('현재 요청 데이터에 포함되지 않은 파일입니다.')
        # Open first, then check the opened inode, so a replaced symlink cannot
        # expose a different file between validation and streaming (Linux host).
        source = path.open('rb')
        try:
            descriptor = Path('/proc/self/fd') / str(source.fileno())
            if descriptor.exists() and descriptor.resolve() != path:
                raise ValueError('조회 중 파일 경로가 변경되었습니다. 다시 조회하세요.')
            import os
            size = os.fstat(source.fileno()).st_size
            if size > MAX_DOCUMENT:
                raise ValueError('요청 데이터 파일은 49 MiB 이하만 다운로드할 수 있습니다.')
            return source, path.name, size
        except Exception:
            source.close()
            raise

    def browse(self, query):
        # This is a local filesystem picker, not a general file-content server.
        folder = case_browser_start(query.get('path', ''), self.service.folder)
        root = query.get('root', '').strip()
        if root:
            base = (self.service.folder / Path(root).expanduser()).resolve()
            try:
                folder.relative_to(base)
            except ValueError:
                folder = base
        else:
            base = None
        entries = []
        for child in sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if child.name.startswith('.') or child.is_symlink():
                continue
            directory = child.is_dir()
            if not directory and query.get('kind') == 'directory':
                continue
            entries.append(dict(name=child.name, path=str(child), directory=directory))
            if len(entries) == 1000:
                break
        return dict(path=str(folder), root=str(base) if base else '',
                    parent=str(folder.parent) if folder != base else None,
                    entries=entries, truncated=len(entries) == 1000)

    def get(self, path, query):
        if path == '/api/health':
            return dict(app='cfd-control-room', version=1, hostname=socket.gethostname())
        if path == '/api/bootstrap':
            return dict(csrf=self.token, default_directory=str(case_browser_start('', self.service.folder)),
                        patterns=self.library.load())
        if path == '/api/overview':
            return self.overview()
        if path == '/api/ticket':
            draft = self.service.open(query['name'])
            # Opening an editor is read-only. Use the monitor/overview snapshot so
            # selecting a ticket never waits for a full process-tree scan. The run
            # request still performs its own fresh scan before changing queue state.
            draft['run_state'] = self.runner.state(query['name'])
            draft['postprocessed'] = [row['case_dir'] for row in draft['values']['cases']
                                      if has_postprocessing(row['case_dir'])]
            return draft
        if path == '/api/cases':
            return self.case_rows()
        if path == '/api/detail':
            return self.detail(query['case'])
        if path == '/api/artifacts':
            return self.artifacts(query['case'])
        if path == '/api/browse':
            return self.browse(query)
        if path == '/api/control':
            root = (self.service.folder / Path(query['path']).expanduser()).resolve()
            return control_times({'_root': str(root)})
        raise LookupError('없는 API입니다.')

    @staticmethod
    def revision(data):
        if not isinstance(data.get('revision'), str) or not data['revision']:
            raise ValueError('편집 기준 버전이 없습니다. 티켓을 다시 여세요.')
        return data['revision']

    def post(self, path, data):
        if path == '/api/new':
            return self.service.new(data.get('kind', 'single'))
        if path == '/api/duplicate':
            return self.service.duplicate(data['name'])
        if path in ('/api/validate', '/api/save'):
            values = data['values']
            if not isinstance(values, dict):
                raise ValueError('입력 폼이 올바르지 않습니다.')
            current = data.get('current')
            if current:
                self.revision(data)
            if path.endswith('validate'):
                self.service.validate(values, data.get('filename', ''), current)
                return dict(valid=True)
            # Refresh running flags before applying the shared edit guards.
            self.fresh()
            name, _ = self.service.save(values, data.get('filename', ''), current,
                                        expected_revision=data.get('revision'), request_id=data.get('request_id'))
            return self.service.open(name)
        if path == '/api/exports/validate':
            root = (self.service.folder / Path(data['case_dir']).expanduser()).resolve()
            return validate_export(data['item'], data.get('others', []), root, data.get('index'))
        if path == '/api/delete/preview':
            self.fresh()
            return self.service.deletion_preview(data['names'])
        if path == '/api/delete':
            if not data.get('revisions'):
                raise ValueError('삭제 대상을 먼저 확인하세요.')
            self.fresh()
            return self.service.delete_many(data['names'], data['revisions'])
        if path == '/api/run':
            return self.runner.request(data['name'], expected_revision=self.revision(data),
                                       request_id=data.get('request_id'), mode=data.get('mode', 'run'),
                                       lane=data.get('lane', 1))
        if path == '/api/discover':
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
            self.library.save(data['name'], data['rules'])
            return self.library.load()
        if path == '/api/queue':
            action = data['action']
            if action in ('pause', 'resume'):
                self.store.put('queue_paused', action == 'pause')
                return dict(ok=True)
            if action in ('cancel', 'cancel_many'):
                ids = [data['id']] if action == 'cancel' else data.get('ids', [])
                result = cancel_queued_jobs(self.store, ids, ui=self.bot.ui)
                if action == 'cancel' and not result['cancelled']:
                    raise ValueError('대기 중인 작업만 취소할 수 있습니다. 상태를 새로고침하세요.')
                return result
            raise ValueError('알 수 없는 큐 동작입니다.')
        raise LookupError('없는 API입니다.')


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, app, port=8766):
        self.app = app
        super().__init__(('127.0.0.1', port), Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = 'CFDWeb/1'

    def setup(self):
        super().setup()
        self.connection.settimeout(60)

    def log_message(self, fmt, *args):
        # Never log request bodies, CSRF tokens, cookies or artifact query paths.
        LOG.info('web %s %s status=%s', self.command, urlsplit(self.path).path,
                 args[1] if len(args) > 1 else '-')

    def trusted(self):
        host = self.headers.get('Host', '')
        # SSH -L may expose a different browser-side port. Validate the local
        # hostname, not the backend port, while keeping strict Origin matching.
        local = re.fullmatch(r'(?:localhost|127\.0\.0\.1|\[::1\])(?::([0-9]{1,5}))?', host)
        if not local or (local[1] and not 1 <= int(local[1]) <= 65535):
            return False
        origin = self.headers.get('Origin')
        return ((not origin or origin == 'http://' + host)
                and self.headers.get('Sec-Fetch-Site') not in ('cross-site', 'same-site'))

    def send_headers(self, status, content_type, size, extra=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(size))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "img-src 'self' data:; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def respond(self, data, status=200):
        payload = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_headers(status, 'application/json; charset=utf-8', len(payload))
        self.wfile.write(payload)

    def handle_request(self, mutation=False):
        try:
            if not self.trusted():
                return self.respond(dict(error='localhost의 같은 출처에서만 사용할 수 있습니다.'), 403)
            url = urlsplit(self.path)
            query = {key: values[-1] for key, values in parse_qs(url.query).items()}
            app = self.server.app
            if mutation:
                if (self.headers.get('Content-Type', '').split(';')[0] != 'application/json'
                        or not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), app.token)):
                    return self.respond(dict(error='인증 토큰이 변경되었습니다. 페이지를 새로고침하세요.'), 403)
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_BODY:
                    return self.respond(dict(error='요청 크기는 2 MiB 이하이어야 합니다.'), 413)
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError('JSON 객체가 필요합니다.')
                return self.respond(app.post(url.path, data))
            if url.path == '/api/file':
                source, name, size = app.file(query)
                with source:
                    mime = mimetypes.guess_type(name)[0]
                    preview = mime in ('image/png', 'image/jpeg', 'image/webp') and query.get('download') != '1'
                    disposition = 'inline' if preview else 'attachment'
                    self.send_headers(200, mime if preview else 'application/octet-stream', size,
                                 {'Content-Disposition': disposition + "; filename*=UTF-8''" + quote(name)})
                    remaining = size
                    while remaining:
                        block = source.read(min(65536, remaining))
                        if not block:
                            break
                        self.wfile.write(block)
                        remaining -= len(block)
                return
            if url.path.startswith('/api/'):
                return self.respond(app.get(url.path, query))
            assets = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}
            for platform in ('Windows', 'macOS'):
                name = f'downloads/CFD-Control-Room-{platform}.zip'
                assets['/' + name] = name
            asset = assets.get(url.path)
            if asset is None:
                raise LookupError('없는 페이지입니다.')
            payload = (STATIC / asset).read_bytes()
            mime = mimetypes.guess_type(asset)[0] or 'text/plain'
            extra = {'Content-Disposition': 'attachment; filename="' + Path(asset).name + '"'} if asset.endswith('.zip') else None
            self.send_headers(200, mime if extra else mime + '; charset=utf-8', len(payload), extra)
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (KeyError, TypeError, AttributeError) as exc:
            LOG.warning('Invalid web input: %s', type(exc).__name__)
            self.respond(dict(error='필수 입력이 없거나 입력 형식이 올바르지 않습니다.'), 400)
        except LookupError as exc:
            self.respond(dict(error=str(exc)), 404)
        except (ValueError, OSError) as exc:
            self.respond(dict(error=str(exc)), 400)
        except Exception:
            LOG.exception('Web request failed')
            self.respond(dict(error='요청 처리 중 오류가 발생했습니다. 웹 서비스 로그를 확인하세요.'), 500)

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request(mutation=True)


def serve(config, store=None, port=8766):
    server = WebServer(WebApp(config, store), port)
    handler = RotatingFileHandler(server.app.store.root / 'web.log', maxBytes=5 * 1024 * 1024,
                                 backupCount=3, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    LOG.addHandler(handler)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    LOG.info('CFD web: http://localhost:%s', server.server_port)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        LOG.removeHandler(handler)
        handler.close()


def main(argv=None):
    import sys
    from .cli import main as cli_main
    return cli_main(['web', *(sys.argv[1:] if argv is None else argv)])
