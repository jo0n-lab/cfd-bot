from unittest.mock import patch

from cfd_bot.config import read_json
from cfd_bot.editor import TicketService
from cfd_bot.ticket_run import TicketRunner
from cfd_bot.tickets import atomic_json, accept_submissions, publish_macro
from tests.test_core import Environment


class TicketRunTests(Environment):
    def setUp(self):
        super().setUp()
        self.service = TicketService(self.root / 'tickets')
        self.config.update(cases=[], case_globs=[str(self.service.folder / '*.json')])
        self.runner = TicketRunner(self.service, self.config, self.store)
        self.name = 'alone-example.json'
        atomic_json(self.service.path(self.name), dict(self.case_data, case_dir=str(self.case_root)))
        scanner = patch('cfd_bot.ticket_run.snapshot', return_value={'cases': {}})
        self.scan = scanner.start()
        self.addCleanup(scanner.stop)

    def macro(self):
        second = self.root / 'second'
        second.mkdir()
        macro = dict(self.case_data, case_dir=str(self.root), task_type='macro', role='alone', resource_source='macro',
                     cases=[dict(case_dir=str(root), state='waiting') for root in (self.case_root, second)])
        name = 'macro-batch.json'
        data = publish_macro(self.service.path(name), macro)
        data['queue'].update(state='finished', submit=False)
        for row in data['cases']:
            row.update(state='finished', result='failed', job_id='old')
            child = read_json(self.service.path(row['ticket']))
            child['queue'].update(state='finished', result='failed', job_id='old')
            atomic_json(self.service.path(row['ticket']), child)
        atomic_json(self.service.path(name), data)
        return name, data

    def test_single_executes_once_and_duplicate_click_preserves_queue(self):
        revision = self.service.revision(self.name)
        self.assertTrue(self.runner.state(self.name)['enabled'])
        first = self.runner.request(self.name, expected_revision=revision, request_id='click')
        self.assertFalse(first['already_queued'])
        accept_submissions(self.config, self.store)
        second = self.runner.request(self.name, request_id='different-click')
        self.assertTrue(second['already_queued'])
        self.assertEqual(len(self.store.jobs()), 1)
        self.assertEqual(self.runner.state(self.name)['state'], 'queued')

    def test_live_external_or_managed_case_cannot_be_enqueued(self):
        before = self.service.path(self.name).read_bytes()
        self.scan.return_value = {'cases': {str(self.case_root): {}}}
        with self.assertRaisesRegex(ValueError, '계산 중'):
            self.runner.request(self.name)
        self.assertFalse(self.runner.state(self.name)['enabled'])
        self.scan.return_value = {'cases': {}}
        self.store.put('snapshot', {'cases': {}})
        job = self.store.enqueue(self.case)
        self.store.update_job(job['id'], status='starting')
        with self.assertRaisesRegex(ValueError, '계산 중'):
            self.runner.request(self.name)
        self.assertEqual(before, self.service.path(self.name).read_bytes())

    def test_macro_disabled_when_any_child_runs_then_can_be_reexecuted_in_order(self):
        name, data = self.macro()
        self.scan.return_value = {'cases': {data['cases'][1]['case_dir']: {}}}
        self.assertFalse(self.runner.state(name, fresh=True)['enabled'])
        with self.assertRaisesRegex(ValueError, '계산 중'):
            self.runner.request(name)
        self.scan.return_value = {'cases': {}}
        self.assertTrue(self.runner.state(name, fresh=True)['enabled'])
        result = self.runner.request(name)
        current = read_json(self.service.path(name))
        self.assertNotEqual(result['request_id'], data['queue']['request_id'])
        self.assertTrue(current['queue']['submit'])
        self.assertEqual([row['state'] for row in current['cases']], ['waiting', 'waiting'])
        self.assertTrue(all('result' not in row and 'job_id' not in row for row in current['cases']))
        accept_submissions(self.config, self.store)
        self.assertEqual([job['case_root'] for job in self.store.jobs()], [row['case_dir'] for row in data['cases']])
        self.assertTrue(self.runner.request(name)['already_queued'])

    def test_changed_configuration_and_failed_scan_cannot_start_a_ticket(self):
        old = self.service.revision(self.name)
        document = read_json(self.service.path(self.name))
        document['name'] = 'changed'
        atomic_json(self.service.path(self.name), document)
        with self.assertRaisesRegex(ValueError, '다른 편집기'):
            self.runner.request(self.name, expected_revision=old)
        self.scan.side_effect = RuntimeError('scanner unavailable')
        with self.assertRaisesRegex(ValueError, '실행 상태 확인 실패'):
            self.runner.request(self.name)
        self.assertFalse(read_json(self.service.path(self.name)).get('queue', {}).get('submit', False))
