import json
import os
import time
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from cfd_bot.config import ConfigError, cases_for, load_case
from cfd_bot.editor import TicketService
from cfd_bot.jobs import Scheduler, scheduling_candidates
from cfd_bot.queueing import borrowing_plan, job_queue_id, registered_profiles
from cfd_bot.ticket_run import TicketRunner
from cfd_bot.tickets import atomic_json, publish_macro
from tests.test_core import Environment


class QueuePlanningTests(TestCase):
    def test_unbounded_named_queues_keep_one_fifo_head_each(self):
        jobs = [
            {'id': 'a1', 'created': 1, 'priority': 'queue', 'queue_id': 'macro-a'},
            {'id': 'a2', 'created': 2, 'priority': 'queue', 'queue_id': 'macro-a'},
            {'id': 'b1', 'created': 3, 'priority': 'queue', 'queue_id': 'macro-b'},
            {'id': 'z1', 'created': 4, 'priority': 'queue', 'queue_id': 'queue-99'},
        ]
        self.assertEqual([job['id'] for job in scheduling_candidates(jobs)],
                         ['a1', 'b1', 'z1'])

    def test_dynamic_plan_uses_unreserved_cpus_then_small_quotas_first(self):
        profiles = {'macro1': set(range(16)), 'macro2': set(range(16, 21)),
                    'shared': set(range(21, 29))}
        job = {'queue_id': 'shared', 'dynamic_cores': True, 'case': {'cores': 40}}

        plan = borrowing_plan(job, profiles, set(range(51)))

        self.assertTrue(plan['oversized'])
        self.assertEqual(plan['donors'], ['macro2', 'macro1'])
        self.assertEqual(len(plan['allowed']), 51)

        job['case']['cores'] = 34
        plan = borrowing_plan(job, profiles, set(range(51)))
        self.assertEqual(plan['donors'], ['macro2'])
        self.assertNotIn('macro1', plan['donors'])

    def test_fixed_queue_cannot_borrow(self):
        profiles = {'fixed': {0, 1}, 'other': {2, 3}}
        job = {'queue_id': 'fixed', 'dynamic_cores': False, 'case': {'cores': 3}}
        self.assertIsNone(borrowing_plan(job, profiles, set(range(6))))

    def test_queue_quota_must_stay_inside_managed_pool(self):
        profiles = {'fixed': {0, 8}}
        job = {'queue_id': 'fixed', 'dynamic_cores': False, 'case': {'cores': 1}}
        self.assertIsNone(borrowing_plan(job, profiles, set(range(8))))

    def test_empty_registered_queue_stays_reserved(self):
        tickets = [{'execution_queue': {'id': 'macro1', 'cpu_set': '0-3'}},
                   {'execution_queue': {'id': 'shared', 'cpu_set': '4-5'}}]
        self.assertEqual(registered_profiles([], tickets),
                         {'macro1': {0, 1, 2, 3}, 'shared': {4, 5}})


class QueueTicketSchemaTests(TestCase):
    def test_dynamic_macro_publishes_per_child_cores_and_queue_profile(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tickets = root / 'tickets'
            tickets.mkdir()
            batch = root / 'batch'
            batch.mkdir()
            rows = []
            for index, cores in enumerate((3, 40), 1):
                case = batch / f'case{index}'
                case.mkdir()
                (case / 'Allrun').write_text('#!/bin/sh\n')
                rows.append({'case_dir': str(case), 'cores': cores, 'state': 'waiting'})
            data = {
                'version': 1, 'task_type': 'macro', 'role': 'alone',
                'case_dir': str(batch), 'resource_source': 'macro', 'cores': 3,
                'cpu_policy': 'auto', 'command': ['./Allrun'],
                'execution_queue': {'id': 'shared', 'cpu_set': '0-7'},
                'dynamic_cores': True, 'watcher': {'logs': ['log.solver']},
                'cases': rows,
            }

            macro = publish_macro(tickets / 'macro-batch.json', data, submit=False)
            children = [load_case(tickets / row['ticket']) for row in macro['cases']]

            self.assertEqual([child['cores'] for child in children], [3, 40])
            self.assertTrue(all(child['dynamic_cores'] for child in children))
            self.assertTrue(all(child['execution_queue']['id'] == 'shared' for child in children))

    def test_ticket_service_rejects_overlapping_distinct_queue_profiles(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tickets = root / 'tickets'
            cases = root / 'cases'
            tickets.mkdir()
            cases.mkdir()
            service = TicketService(tickets)
            for name in ('a', 'b'):
                case = cases / name
                case.mkdir()
                (case / 'Allrun').write_text('#!/bin/sh\n')
            first = service.new()['values']
            first.update(case_dir=str(cases / 'a'), name='a', execution_source='ticket',
                         macro_cores='2', queue_id='queue-a', queue_cpu_set='0-3')
            service.save(first, 'a.json')
            second = service.new()['values']
            second.update(case_dir=str(cases / 'b'), name='b', execution_source='ticket',
                          macro_cores='2', queue_id='queue-b', queue_cpu_set='3-5')
            with self.assertRaisesRegex(ValueError, '겹칩니다'):
                service.validate(second, 'b.json')

    def test_config_check_rejects_hand_edited_overlapping_profiles(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tickets = root / 'tickets'
            tickets.mkdir()
            cpu = min(os.sched_getaffinity(0))
            config = {'case_globs': [str(tickets / '*.json')], 'cases': [], '_ui_dir': None,
                      'scheduler': {'cpu_capacity': len(os.sched_getaffinity(0))}}
            for name, queue_id in (('a', 'queue-a'), ('b', 'queue-b')):
                case = root / name
                case.mkdir()
                atomic_json(tickets / f'{name}.json', {
                    'version': 1, 'case_dir': str(case), 'cores': 1,
                    'cpu_policy': 'auto', 'command': ['./Allrun'],
                    'execution_queue': {'id': queue_id, 'cpu_set': str(cpu)},
                })
            with self.assertRaisesRegex(ConfigError, '겹칩니다'):
                cases_for(config, force=True)


class DynamicFairnessTests(Environment):
    def setUp(self):
        super().setUp()
        self.config['scheduler'].update(max_parallel=8, cpu_capacity=8)
        self.layout = {i: (0, 0, i) for i in range(8)}

    def case_for(self, name, cores, queue_id, cpu_range, dynamic=False):
        root = self.root / name
        root.mkdir(exist_ok=True)
        (root / 'Allrun').write_text('#!/bin/sh\n')
        return dict(deepcopy(self.case), _root=str(root), _config='', name=name,
                    cores=cores, cpu_policy='auto', resource_source='macro',
                    execution_queue={'id': queue_id, 'cpu_set': cpu_range},
                    dynamic_cores=dynamic)

    def test_dynamic_job_drains_donors_then_yields_one_turn(self):
        donor_big = self.store.enqueue(self.case_for('big-active', 3, 'big', '0-2'),
                                       queue_id='big', queue_cpu_set='0-2')
        donor_small = self.store.enqueue(self.case_for('small-active', 2, 'small', '3-4'),
                                         queue_id='small', queue_cpu_set='3-4')
        for job, cpus in ((donor_big, '0-2'), (donor_small, '3-4')):
            case = deepcopy(job['case'])
            case['cpu_set'] = cpus
            self.store.update_job(job['id'], status='starting', claimed=time.time(), case=case)
        dynamic = self.store.enqueue(self.case_for('dynamic-1', 6, 'dynamic', '5', True),
                                     queue_id='dynamic', queue_cpu_set='5', dynamic_cores=True)

        with patch('cfd_bot.jobs.topology', self.layout, create=True), \
                patch('cfd_bot.cpu_allocation.topology', return_value=self.layout), \
                patch('cfd_bot.cpu_allocation.os.sched_getaffinity', return_value=set(range(8))), \
                patch('cfd_bot.jobs.terminal_event'), \
                patch('cfd_bot.jobs.snapshot', return_value={'cases': {}}), \
                patch('cfd_bot.jobs.check_cpus', return_value=(True, 'SAFE')), \
                patch('cfd_bot.jobs.subprocess.Popen') as launch:
            scheduler = Scheduler(self.config, self.store)
            scheduler.tick({})
            self.assertEqual(self.store.job(dynamic['id'])['status'], 'queued')
            self.assertEqual(self.store.get('queue_drain_claim')['donors'], ['small', 'big'])
            launch.assert_not_called()

            for job in (donor_big, donor_small):
                self.store.update_job(job['id'], status='succeeded', finished=time.time())
            scheduler.tick({})
            first = self.store.job(dynamic['id'])
            self.assertEqual(first['status'], 'starting')
            self.assertEqual(first['borrowed_queues'], ['small', 'big'])

            self.store.update_job(dynamic['id'], status='succeeded', finished=time.time())
            next_big = self.store.enqueue(self.case_for('big-next', 3, 'big', '0-2'),
                                          queue_id='big', queue_cpu_set='0-2')
            next_small = self.store.enqueue(self.case_for('small-next', 2, 'small', '3-4'),
                                            queue_id='small', queue_cpu_set='3-4')
            next_dynamic = self.store.enqueue(self.case_for('dynamic-2', 6, 'dynamic', '5', True),
                                              queue_id='dynamic', queue_cpu_set='5', dynamic_cores=True)
            scheduler.tick({})

        self.assertEqual(self.store.job(next_big['id'])['status'], 'starting')
        self.assertEqual(self.store.job(next_small['id'])['status'], 'starting')
        self.assertEqual(self.store.job(next_dynamic['id'])['status'], 'queued')
        self.assertEqual(job_queue_id(self.store.job(next_dynamic['id'])), 'dynamic')


class DynamicGuidanceTests(Environment):
    def setUp(self):
        super().setUp()
        self.config['scheduler']['cpu_capacity'] = 8
        self.runner = TicketRunner(TicketService(self.root / 'tickets'), self.config, self.store)

    def resource(self, cores, dynamic):
        return dict(deepcopy(self.case), cores=cores, cpu_policy='auto',
                    resource_source='macro', dynamic_cores=dynamic,
                    execution_queue={'id': 'shared', 'cpu_set': self.cpu})

    def test_fixed_oversized_queue_is_disabled_with_dynamic_guidance(self):
        status = self.runner._capacity([self.resource(2, False)], {}, [])
        self.assertFalse(status['queue_possible'])
        self.assertIn('동적 코어 옵션', status['availability_message'])

    def test_dynamic_request_above_managed_capacity_is_disabled(self):
        status = self.runner._capacity([self.resource(9, True)], {}, [])
        self.assertFalse(status['queue_possible'])
        self.assertIn('동적 매크로로도 실행할 수 없', status['availability_message'])

    def test_queue_outside_managed_pool_is_disabled(self):
        case = self.resource(1, False)
        case['execution_queue']['cpu_set'] = str(max(os.sched_getaffinity(0)) + 100)
        status = self.runner._capacity([case], {}, [])
        self.assertFalse(status['queue_possible'])
        self.assertIn('관리 CPU 범위 밖', status['availability_message'])

    def test_legacy_lane_choice_is_not_overridden_by_old_ticket_state(self):
        case = dict(deepcopy(self.case), queue={'lane': 1, 'state': 'finished'})
        job = self.store.enqueue(case, queue_lane=3)
        self.assertEqual(job_queue_id(job), 'legacy-3')
