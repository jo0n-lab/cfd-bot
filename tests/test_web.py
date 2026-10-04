"""Real HTTP adapter tests with isolated tickets; never launch a solver."""
import base64
import http.client
import json
import threading
import time
from unittest.mock import patch
from urllib.parse import urlencode

from cfd_bot.bot import case_id
from cfd_bot.config import load_case, read_json
from cfd_bot.tickets import accept_submissions, atomic_json
from cfd_bot.web import WebApp, WebServer
from tests.test_core import Environment


class WebTests(Environment):
    def setUp(self):
        super().setUp()
        self.config.update(cases=[], case_globs=[str(self.root / 'tickets/*.json')])
        self.app = WebApp(self.config, self.store)
        self.name = 'alone-test.json'
        atomic_json(self.app.service.path(self.name), dict(self.case_data, case_dir=str(self.case_root)))
        scanner = patch('cfd_bot.ticket_run.snapshot', return_value={'cases': {}, 'raw': 'No active jobs'})
        self.scan = scanner.start()
        self.addCleanup(scanner.stop)
        self.server = WebServer(self.app, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path, data=None, headers=None, raw=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        merged = {'Content-Type': 'application/json', 'X-CSRF-Token': self.app.token}
        merged.update(headers or {})
        connection.request('POST' if data is not None or raw is not None else 'GET', path,
                           raw if raw is not None else json.dumps(data) if data is not None else None, merged)
        response = connection.getresponse()
        body, status, response_headers = response.read(), response.status, dict(response.getheaders())
        connection.close()
        if response_headers.get('Content-Type', '').startswith('application/json'):
            body = json.loads(body)
        return status, body, response_headers

    def get(self, path):
        status, body, _ = self.request(path)
        self.assertEqual(status, 200, body)
        return body

    def post(self, path, data):
        status, body, _ = self.request(path, data)
        self.assertEqual(status, 200, body)
        return body

    def draft(self):
        return self.get('/api/ticket?' + urlencode({'name': self.name}))

    def test_bootstrap_static_assets_and_localhost_security(self):
        self.assertEqual(self.get('/api/health')['app'], 'cfd-control-room')
        self.assertEqual(self.get('/api/bootstrap')['csrf'], self.app.token)
        for path in ('/', '/app.js', '/style.css'):
            status, body, headers = self.request(path)
            self.assertEqual(status, 200)
            self.assertTrue(body)
            self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        for platform in ('Windows', 'macOS'):
            status, body, headers = self.request(f'/downloads/CFD-Control-Room-{platform}.zip')
            self.assertEqual(status, 200)
            self.assertTrue(body.startswith(b'PK'))
            self.assertIn('attachment', headers['Content-Disposition'])
        self.assertEqual(self.request('/api/bootstrap', headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.request('/api/bootstrap', headers={'Origin': 'http://evil.example'})[0], 403)
        self.assertEqual(self.request('/api/bootstrap', headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        for host in ('localhost:18765', '127.0.0.1:28765', '[::1]:38765'):
            self.assertEqual(self.request('/api/bootstrap', headers={
                'Host': host, 'Origin': 'http://' + host, 'Sec-Fetch-Site': 'same-origin'})[0], 200)
        for host in ('localhost.evil.example:8765', 'localhost:65536', 'localhost:0',
                     'user@localhost:8765', 'localhost:8765/evil', '100.100.1.1:8765'):
            self.assertEqual(self.request('/api/bootstrap', headers={'Host': host})[0], 403)
        self.assertEqual(self.request('/api/queue', {'action': 'pause'}, {
            'Host': 'localhost:18765', 'Origin': 'http://localhost:9999'})[0], 403)
        self.assertEqual(self.request('/api/queue', {'action': 'pause'}, {'X-CSRF-Token': ''})[0], 403)
        self.assertEqual(self.request('/api/queue', {'action': 'pause'}, {'Content-Type': 'text/plain'})[0], 403)
        self.assertFalse(self.store.get('queue_paused', False))
        self.assertEqual(self.request('/api/new', raw='[]')[0], 400)
        self.assertEqual(self.request('/api/new', raw='{')[0], 400)
        self.assertEqual(self.request('/api/new', raw='{}', headers={'Content-Length': '9999999'})[0], 413)
        self.assertEqual(self.request('/../bot.json')[0], 404)
        self.assertEqual(self.request('/bot.json')[0], 404)

    def test_ticket_edit_revision_and_duplicate_bulk_delete(self):
        draft = self.draft()
        stale = dict(draft)
        draft['values']['name'] = 'edited on web'
        self.post('/api/validate', draft)
        saved = self.post('/api/save', draft)
        self.assertEqual(self.app.service.open(self.name)['values']['name'], 'edited on web')
        self.assertNotEqual(saved['revision'], stale['revision'])
        self.assertEqual(self.request('/api/save', stale)[0], 400)
        copy = self.post('/api/duplicate', {'name': self.name})
        second = self.root / 'second'
        second.mkdir()
        copy['values']['case_dir'] = str(second)
        saved_copy = self.post('/api/save', copy)
        plan = self.post('/api/delete/preview', {'names': [self.name, saved_copy['current']]})
        changed = self.app.service.open(saved_copy['current'])
        changed['values']['name'] = 'a concurrent GUI edit'
        self.post('/api/save', changed)
        self.assertEqual(self.request('/api/delete', plan)[0], 400)
        self.assertEqual(len(self.app.service.listing()), 2)
        plan = self.post('/api/delete/preview', {'names': [self.name, saved_copy['current']]})
        self.assertEqual(len(self.post('/api/delete', plan)), 2)
        self.assertEqual(self.app.service.listing(), [])
        self.assertTrue(second.exists())

    def test_ticket_selection_uses_cached_state_without_ofps_scan(self):
        self.store.put('snapshot', {'cases': {}, 'raw': 'No active jobs', 'at': time.time()})
        self.scan.reset_mock()
        draft = self.draft()
        self.assertTrue(draft['run_state']['enabled'])
        self.scan.assert_not_called()

    def test_external_running_disables_run_but_allows_request_data_edits(self):
        observed = {'cases': {str(self.case_root): {
            'processes': [], 'actual_cores': 1, 'actual_cpu_list': self.cpu}},
                    'raw': 'active', 'at': time.time()}
        self.store.put('snapshot', observed)
        self.scan.return_value = observed
        draft = self.draft()
        self.assertFalse(draft['run_state']['enabled'])
        self.assertEqual(self.request('/api/run', {'name': self.name, 'revision': draft['revision']})[0], 400)
        draft['values']['exports'] = [dict(name='results', pattern='validation/*.png',
                                          kind='photo', max_files=2)]
        saved = self.post('/api/save', draft)
        self.assertEqual(saved['values']['exports'][0]['name'], 'results')
        saved['values']['preprocess'] = './different-clean'
        self.assertEqual(self.request('/api/save', saved)[0], 400)
        self.assertFalse(self.store.jobs())

    def test_single_execution_settings_roundtrip_and_shared_queue(self):
        from cfd_bot.execution import execution_case
        (self.case_root / 'Allrun').write_text('NP=99\nCPU_SET=0-98\n')
        draft = self.draft()
        draft['values'].update(execution_source='ticket', macro_cores='4', macro_command='./Allrun',
                               macro_cpu_policy='auto', monitoring_cpu=True,
                               monitoring_command='./Allmonitor --interval 2')
        self.post('/api/validate', draft)
        saved = self.post('/api/save', draft)
        self.assertEqual(saved['values']['execution_source'], 'ticket')
        self.assertEqual(saved['values']['macro_cores'], '4')
        self.assertTrue(saved['values']['monitoring_cpu'])
        self.assertEqual(saved['values']['monitoring_command'], './Allmonitor --interval 2')
        self.assertEqual(execution_case(load_case(self.app.service.path(self.name)))['cores'], 4)
        self.post('/api/run', dict(name=self.name, revision=saved['revision'], request_id='single-web-run'))
        accept_submissions(self.config, self.store)
        job = self.store.jobs()[0]
        self.assertEqual(job['case']['resource_source'], 'ticket')
        self.assertEqual(job['case']['cores'], 4)
        self.assertEqual(job['case']['command'], ['./Allrun'])
        self.assertEqual(job['case']['monitoring'], {
            'allocate_cpu': True, 'command': ['./Allmonitor', '--interval', '2']})
        self.assertNotIn('cpu_set', job['case'])

    def test_queue_uses_shared_submission_and_cas_cancel(self):
        draft = self.draft()
        body = dict(name=self.name, revision=draft['revision'], request_id='web-click')
        self.assertFalse(self.post('/api/run', body)['already_queued'])
        self.assertTrue(self.post('/api/run', body)['already_queued'])
        self.assertEqual(self.store.jobs(), [])  # web never runs its own Scheduler
        accept_submissions(self.config, self.store)
        jobs = self.store.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['status'], 'queued')
        self.post('/api/queue', dict(action='pause'))
        self.assertTrue(self.store.get('queue_paused'))
        self.post('/api/queue', dict(action='resume'))
        self.assertFalse(self.store.get('queue_paused'))
        self.store.update_job(jobs[0]['id'], status='running')
        self.assertEqual(self.request('/api/queue', dict(action='cancel', id=jobs[0]['id']))[0], 400)
        self.store.update_job(jobs[0]['id'], status='queued')
        self.post('/api/queue', dict(action='cancel', id=jobs[0]['id']))
        self.assertEqual(self.store.job(jobs[0]['id'])['status'], 'cancelled')

    def test_queue_bulk_cancel_returns_cancelled_and_stale_selections(self):
        jobs = []
        for index in range(3):
            root = self.root / f'web-queue-{index}'
            root.mkdir()
            jobs.append(self.store.enqueue(dict(self.case, _root=str(root), name=f'queue {index}')))
        self.store.update_job(jobs[-1]['id'], status='starting')

        result = self.post('/api/queue', dict(
            action='cancel_many', ids=[jobs[0]['id'], jobs[1]['id'], jobs[-1]['id'], 'missing']))

        self.assertEqual(result['cancelled'], [jobs[0]['id'], jobs[1]['id']])
        self.assertEqual(result['unavailable'], [jobs[-1]['id'], 'missing'])
        self.assertEqual(self.store.job(jobs[0]['id'])['status'], 'cancelled')
        self.assertEqual(self.store.job(jobs[-1]['id'])['status'], 'starting')
        self.assertEqual(self.request('/api/queue', dict(action='cancel_many', ids=[]))[0], 400)
        self.assertEqual(self.request('/api/queue', dict(action='cancel_many', ids='not-a-list'))[0], 400)

    def test_overview_tracks_only_jobs_with_a_current_ticket_json(self):
        tracked_case = load_case(self.app.service.path(self.name))
        tracked_history = self.store.enqueue(tracked_case)
        self.store.update_job(tracked_history['id'], status='succeeded', started=time.time() - 10,
                              finished=time.time())
        untracked_root = self.root / 'ticket-was-deleted'
        untracked_root.mkdir()
        untracked_case = dict(tracked_case, name='ticket was deleted', _root=str(untracked_root))
        untracked_history = self.store.enqueue(untracked_case)
        self.store.update_job(untracked_history['id'], status='failed', started=time.time() - 5,
                              finished=time.time())
        running = self.store.enqueue(tracked_case)
        self.store.update_job(running['id'], status='running', started=time.time() - 2)

        overview = self.get('/api/overview')
        history = {item['id']: item for item in overview['history']}
        active = {item['id']: item for item in overview['queue']}

        self.assertTrue(history[tracked_history['id']]['trackable'])
        self.assertEqual(history[tracked_history['id']]['case_id'], case_id(tracked_case))
        self.assertFalse(history[untracked_history['id']]['trackable'])
        self.assertIsNone(history[untracked_history['id']]['case_id'])
        self.assertTrue(active[running['id']]['trackable'])

    def test_macro_discovery_publish_children_and_shared_settings(self):
        for name in ('one', 'two', 'skip-template', 'running', 'group/deep'):
            root = self.root / 'batch' / name
            root.mkdir(parents=True)
            (root / 'Allrun').write_text('#!/bin/sh\nexit 0\n')
            (root / 'system').mkdir()
            (root / 'system/controlDict').write_text('endTime 10;')
        (self.root / 'batch/two/postProcessing').mkdir()
        self.scan.return_value = {'cases': {str(self.root / 'batch/running'): {}}}
        found = self.post('/api/discover', dict(case_dir=str(self.root / 'batch')))
        self.assertEqual([r['case_dir'].split('/')[-1] for r in found['cases']], ['one', 'two'])
        self.assertEqual(found['postprocessed'], [str(self.root / 'batch/two')])
        self.assertEqual(len(found['skipped']), 1)
        draft = self.post('/api/new', {'kind': 'macro'})
        draft['values'].update(case_dir=str(self.root / 'batch'), macro_cores='2',
                               name='batch', cases=found['cases'])
        draft.update(request_id='macro-save')
        saved = self.post('/api/save', draft)
        self.assertEqual(saved['filename'], 'macro-batch.json')
        self.assertEqual(len(saved['values']['cases']), 2)
        macro = read_json(self.app.service.path(saved['filename']))
        self.assertTrue(macro['queue']['submit'])
        for row in macro['cases']:
            child = load_case(self.app.service.path(row['ticket']))
            self.assertEqual(child['role'], 'child')
            self.assertEqual(child['cores'], 2)
            self.assertEqual(child['cpu_policy'], 'auto')
            self.assertTrue(child['allow_cross_socket'])
        # Same request is safe to retry; no child duplicates or running solver.
        self.post('/api/save', draft)
        self.assertEqual(len(self.app.service.listing()), 4)
        self.assertEqual(self.store.jobs(), [])

    def test_artifacts_allow_only_declared_files_and_reject_symlink_escape(self):
        png = self.case_root / 'residual.png'
        png.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='))
        (self.case_root / 'data.csv').write_text('time,value\n1,2\n')
        draft = self.draft()
        draft['values'].update(residual_pattern='residual.png',
                               exports=[dict(name='data', pattern='*.csv', kind='document', max_files=1)])
        self.post('/api/save', draft)
        cid = case_id(load_case(self.app.service.path(self.name)))
        groups = self.get('/api/artifacts?case=' + cid)
        url = groups[0]['files'][0]['url']
        status, body, headers = self.request(url)
        self.assertEqual(status, 200)
        self.assertEqual(body, png.read_bytes())
        self.assertTrue(headers['Content-Type'].startswith('image/png'))
        _, _, headers = self.request(url + '&download=1')
        self.assertIn('attachment', headers['Content-Disposition'])
        for relative in ('system/controlDict', '../bot.json', '/etc/passwd'):
            query = urlencode(dict(case=cid, source='residual', file=relative))
            self.assertEqual(self.request('/api/file?' + query)[0], 400)
        (self.case_root / 'data.csv').unlink()
        (self.case_root / 'data.csv').symlink_to(self.bot_path)
        query = urlencode(dict(case=cid, source='export:data', file='data.csv'))
        self.assertEqual(self.request('/api/file?' + query)[0], 400)

    def test_status_fresh_snapshot_includes_unregistered_and_reports_outage(self):
        root = str(self.root / 'external')
        self.scan.return_value = {'cases': {root: {'processes': [], 'actual_cores': 4,
                                                  'actual_cpu_list': '0-3', 'owner': 'user'}}}
        result = self.get('/api/overview')
        self.assertEqual(result['live'][0]['case_dir'], root)
        self.assertFalse(result['live'][0]['registered'])
        self.assertIsNone(result['error'])
        self.get('/api/overview')
        self.assertEqual(self.scan.call_count, 2)
        self.scan.side_effect = RuntimeError('scanner unavailable')
        result = self.get('/api/overview')
        self.assertIn('scanner unavailable', result['error'])
        self.assertEqual(len(result['live']), 1)  # explicitly stale, never silently empty

    def test_browse_control_patterns_and_paths(self):
        result = self.get('/api/browse?' + urlencode(dict(path=str(self.case_root), root=str(self.case_root))))
        self.assertIsNone(result['parent'])
        self.assertTrue(any(row['name'] == 'system' for row in result['entries']))
        constrained = self.get('/api/browse?' + urlencode(dict(path='/etc', root=str(self.case_root))))
        self.assertEqual(constrained['path'], str(self.case_root))
        control = self.get('/api/control?' + urlencode(dict(path=str(self.case_root))))
        self.assertEqual(control['end'], 10)
        result = self.post('/api/patterns', dict(name='custom', rules=dict(
            failure_patterns=['ERROR'], openfoam_defaults=True)))
        self.assertEqual(result['custom']['failure_patterns'], ['ERROR'])
        self.assertEqual(self.app.library.load(), result)
        self.assertEqual(self.request('/api/ticket?name=../bot.json')[0], 400)
        export = dict(name='test', pattern='../secrets', kind='document', max_files=1)
        self.assertEqual(self.request('/api/exports/validate', dict(case_dir=str(self.case_root), item=export))[0], 400)
