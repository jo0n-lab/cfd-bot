import json
import os
import time
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from cfd_bot.config import ConfigError, cases_for, cpu_set, load_case
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

    def test_immediate_macro_exposes_only_its_current_batch_head(self):
        jobs = [
            {'id': 'm1', 'created': 1, 'priority': 'run', 'batch': 'macro'},
            {'id': 'm2', 'created': 2, 'priority': 'run', 'batch': 'macro'},
            {'id': 'm3', 'created': 3, 'priority': 'run', 'batch': 'macro'},
            {'id': 'single', 'created': 4, 'priority': 'run'},
        ]

        self.assertEqual([job['id'] for job in scheduling_candidates(jobs)],
                         ['m1', 'single'])

    def test_active_macro_blocks_its_next_child_before_dynamic_claim_planning(self):
        queued = [
            {'id': 'm2', 'created': 2, 'priority': 'run', 'batch': 'macro'},
            {'id': 'm3', 'created': 3, 'priority': 'run', 'batch': 'macro'},
            {'id': 'other', 'created': 4, 'priority': 'run'},
        ]
        active = [{'id': 'm1', 'priority': 'run', 'batch': 'macro'}]

        self.assertEqual([job['id'] for job in scheduling_candidates(queued, active)],
                         ['other'])

    def test_active_named_queue_blocks_its_next_fifo_head(self):
        queued = [
            {'id': 'q1-next', 'created': 1, 'priority': 'queue', 'queue_id': 'q1'},
            {'id': 'q2-head', 'created': 2, 'priority': 'queue', 'queue_id': 'q2'},
        ]
        active = [{'id': 'q1-live', 'priority': 'queue', 'queue_id': 'q1'}]

        self.assertEqual([job['id'] for job in scheduling_candidates(queued, active)],
                         ['q2-head'])

    def test_dynamic_plan_uses_unreserved_cpus_then_small_quotas_first(self):
        profiles = {'macro1': set(range(16)), 'macro2': set(range(16, 21))}
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
                'execution_queue': {'id': 'shared'},
                'dynamic_cores': True, 'watcher': {'logs': ['log.solver']},
                'cases': rows,
            }

            macro = publish_macro(tickets / 'macro-batch.json', data, submit=False)
            children = [load_case(tickets / row['ticket']) for row in macro['cases']]

            self.assertEqual([child['cores'] for child in children], [3, 40])
            self.assertTrue(all(child['dynamic_cores'] for child in children))
            self.assertTrue(all(child['execution_queue']['id'] == 'shared' for child in children))

    def test_ticket_service_derives_quota_from_cores_without_queue_cpu_input(self):
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
                         macro_cores='2', queue_id='queue-a')
            service.save(first, 'a.json')
            second = service.new()['values']
            second.update(case_dir=str(cases / 'b'), name='b', execution_source='ticket',
                          macro_cores='5', queue_id='queue-b')
            service.save(second, 'b.json')
            self.assertEqual(load_case(tickets / 'alone-a.json')['execution_queue'],
                             {'id': 'queue-a'})
            self.assertEqual(load_case(tickets / 'alone-b.json')['execution_queue'],
                             {'id': 'queue-b'})

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

    def case_for(self, name, cores, queue_id, cpu_range=None, dynamic=False):
        root = self.root / name
        root.mkdir(exist_ok=True)
        (root / 'Allrun').write_text('#!/bin/sh\n')
        profile = {'id': queue_id}
        if cpu_range:
            profile['cpu_set'] = cpu_range
        return dict(deepcopy(self.case), _root=str(root), _config='', name=name,
                    cores=cores, cpu_policy='auto', resource_source='macro',
                    execution_queue=profile, dynamic_cores=dynamic)

    def test_fixed_queue_profile_is_assigned_from_head_core_count(self):
        queued = self.store.enqueue(self.case_for('fixed-auto', 3, 'fixed'),
                                    queue_id='fixed')
        with patch('cfd_bot.jobs.topology', self.layout, create=True), \
                patch('cfd_bot.cpu_allocation.topology', return_value=self.layout), \
                patch('cfd_bot.cpu_allocation.os.sched_getaffinity', return_value=set(range(8))), \
                patch('cfd_bot.jobs.terminal_event'), \
                patch('cfd_bot.jobs.snapshot', return_value={'cases': {}}), \
                patch('cfd_bot.jobs.check_cpus', return_value=(True, 'SAFE')), \
                patch('cfd_bot.jobs.subprocess.Popen'):
            Scheduler(self.config, self.store).tick({})

        started = self.store.job(queued['id'])
        self.assertEqual(started['status'], 'starting')
        self.assertEqual(len(cpu_set(started['queue_cpu_set'])), 3)
        self.assertEqual(started['case']['cpu_set'], started['queue_cpu_set'])

    def test_case_owned_np_is_the_fixed_queue_quota(self):
        root = self.root / 'case-owned'
        root.mkdir()
        (root / 'Allrun').write_text('#!/bin/sh\nNP=5\nCPU_SET=0-4\n')
        case = dict(deepcopy(self.case), _root=str(root), _config='', name='case-owned',
                    execution_queue={'id': 'case-owned'}, dynamic_cores=False)
        case.pop('resource_source', None)
        queued = self.store.enqueue(case, queue_id='case-owned')
        with patch('cfd_bot.cpu_allocation.topology', return_value=self.layout), \
                patch('cfd_bot.cpu_allocation.os.sched_getaffinity', return_value=set(range(8))), \
                patch('cfd_bot.jobs.terminal_event'), \
                patch('cfd_bot.jobs.snapshot', return_value={'cases': {}}), \
                patch('cfd_bot.jobs.check_cpus', return_value=(True, 'SAFE')), \
                patch('cfd_bot.jobs.subprocess.Popen'):
            Scheduler(self.config, self.store).tick({})

        started = self.store.job(queued['id'])
        self.assertEqual(started['status'], 'starting')
        self.assertEqual(len(cpu_set(started['queue_cpu_set'])), 5)
        self.assertEqual(started['case']['cores'], 5)

    def test_fixed_queues_get_non_overlapping_profiles_and_start_together(self):
        first = self.store.enqueue(self.case_for('parallel-a', 3, 'queue-a'),
                                   queue_id='queue-a')
        second = self.store.enqueue(self.case_for('parallel-b', 2, 'queue-b'),
                                    queue_id='queue-b')
        with patch('cfd_bot.cpu_allocation.topology', return_value=self.layout), \
                patch('cfd_bot.cpu_allocation.os.sched_getaffinity', return_value=set(range(8))), \
                patch('cfd_bot.jobs.terminal_event'), \
                patch('cfd_bot.jobs.snapshot', return_value={'cases': {}}), \
                patch('cfd_bot.jobs.check_cpus', return_value=(True, 'SAFE')), \
                patch('cfd_bot.jobs.subprocess.Popen'):
            Scheduler(self.config, self.store).tick({})

        a, b = self.store.job(first['id']), self.store.job(second['id'])
        self.assertEqual((a['status'], b['status']), ('starting', 'starting'))
        self.assertEqual(len(cpu_set(a['queue_cpu_set'])), 3)
        self.assertEqual(len(cpu_set(b['queue_cpu_set'])), 2)
        self.assertFalse(cpu_set(a['queue_cpu_set']) & cpu_set(b['queue_cpu_set']))

    def test_next_head_derives_its_own_smaller_quota_after_active_finishes(self):
        first = self.store.enqueue(self.case_for('resize-first', 4, 'resize'),
                                   queue_id='resize')
        second = self.store.enqueue(self.case_for('resize-second', 2, 'resize'),
                                    queue_id='resize')
        patches = (
            patch('cfd_bot.cpu_allocation.topology', return_value=self.layout),
            patch('cfd_bot.cpu_allocation.os.sched_getaffinity', return_value=set(range(8))),
            patch('cfd_bot.jobs.terminal_event'),
            patch('cfd_bot.jobs.snapshot', return_value={'cases': {}}),
            patch('cfd_bot.jobs.check_cpus', return_value=(True, 'SAFE')),
            patch('cfd_bot.jobs.subprocess.Popen'),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            scheduler = Scheduler(self.config, self.store)
            scheduler.tick({})
            self.assertEqual(self.store.job(first['id'])['status'], 'starting')
            self.assertEqual(self.store.job(second['id'])['status'], 'queued')
            self.assertNotIn('queue_cpu_set', self.store.job(second['id']))
            self.store.update_job(first['id'], status='succeeded', finished=time.time())
            scheduler.tick({})

        resized = self.store.job(second['id'])
        self.assertEqual(resized['status'], 'starting')
        self.assertEqual(len(cpu_set(resized['queue_cpu_set'])), 2)

    def test_dynamic_job_drains_donors_then_yields_one_turn(self):
        donor_big = self.store.enqueue(self.case_for('big-active', 3, 'big', '0-2'),
                                       queue_id='big', queue_cpu_set='0-2')
        donor_small = self.store.enqueue(self.case_for('small-active', 2, 'small', '3-4'),
                                         queue_id='small', queue_cpu_set='3-4')
        for job, cpus in ((donor_big, '0-2'), (donor_small, '3-4')):
            case = deepcopy(job['case'])
            case['cpu_set'] = cpus
            self.store.update_job(job['id'], status='starting', claimed=time.time(), case=case)
        dynamic = self.store.enqueue(self.case_for('dynamic-1', 6, 'dynamic', dynamic=True),
                                     queue_id='dynamic', dynamic_cores=True)

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
            next_dynamic = self.store.enqueue(self.case_for('dynamic-2', 6, 'dynamic', dynamic=True),
                                              queue_id='dynamic', dynamic_cores=True)
            scheduler.tick({})

        self.assertEqual(self.store.job(next_big['id'])['status'], 'starting')
        self.assertEqual(self.store.job(next_small['id'])['status'], 'starting')
        self.assertEqual(self.store.job(next_dynamic['id'])['status'], 'queued')
        self.assertEqual(job_queue_id(self.store.job(next_dynamic['id'])), 'dynamic')

    def test_immediate_dynamic_job_uses_the_same_donor_drain_policy(self):
        donor = self.store.enqueue(self.case_for('run-donor', 3, 'fixed', '0-2'),
                                   queue_id='fixed', queue_cpu_set='0-2')
        donor_case = deepcopy(donor['case'])
        donor_case['cpu_set'] = '0-2'
        self.store.update_job(donor['id'], status='starting', claimed=time.time(), case=donor_case)
        dynamic = self.store.enqueue(
            self.case_for('run-dynamic', 7, 'dynamic-run', dynamic=True),
            priority='run', queue_id='dynamic-run', dynamic_cores=True)

        with patch('cfd_bot.jobs.topology', self.layout, create=True), \
                patch('cfd_bot.cpu_allocation.topology', return_value=self.layout), \
                patch('cfd_bot.cpu_allocation.os.sched_getaffinity', return_value=set(range(8))), \
                patch('cfd_bot.jobs.terminal_event'), \
                patch('cfd_bot.jobs.snapshot', return_value={'cases': {}}), \
                patch('cfd_bot.jobs.check_cpus', return_value=(True, 'SAFE')), \
                patch('cfd_bot.jobs.subprocess.Popen'):
            scheduler = Scheduler(self.config, self.store)
            scheduler.tick({})
            self.assertEqual(self.store.job(dynamic['id'])['status'], 'queued')
            self.assertEqual(self.store.get('queue_drain_claim')['donors'], ['fixed'])

            self.store.update_job(donor['id'], status='succeeded', finished=time.time())
            scheduler.tick({})

        started = self.store.job(dynamic['id'])
        self.assertEqual(started['status'], 'starting')
        self.assertEqual(started['borrowed_queues'], ['fixed'])


class DynamicGuidanceTests(Environment):
    def setUp(self):
        super().setUp()
        self.config['scheduler']['cpu_capacity'] = 8
        self.runner = TicketRunner(TicketService(self.root / 'tickets'), self.config, self.store)

    def resource(self, cores, dynamic):
        return dict(deepcopy(self.case), cores=cores, cpu_policy='auto',
                    resource_source='macro', dynamic_cores=dynamic,
                    execution_queue={'id': 'shared'})

    def test_fixed_queue_quota_is_derived_from_case_cores(self):
        status = self.runner._capacity([self.resource(2, False)], {}, [])
        self.assertTrue(status['queue_possible'])
        self.assertEqual(status['queue_quota'], 2)

    def test_dynamic_request_above_managed_capacity_is_disabled(self):
        status = self.runner._capacity([self.resource(9, True)], {}, [])
        self.assertFalse(status['queue_possible'])
        self.assertIn('동적 매크로로도 실행할 수 없', status['availability_message'])

    def capacity(self, required, can_run, *, free=11):
        return dict(capacity=51, used_cores=51 - free, free_cores=free,
                    required_cores=required, can_run=can_run,
                    capacity_reason='' if can_run else 'insufficient')

    def test_dynamic_macro_admits_the_first_child_instead_of_the_largest_child(self):
        members = [self.resource(cores, True) for cores in (4, 4, 24)]
        statuses = [self.capacity(4, True), self.capacity(4, True),
                    self.capacity(24, False)]

        with patch('cfd_bot.ticket_run.capacity_status', side_effect=statuses):
            status = self.runner._capacity(members, {}, [])

        self.assertTrue(status['can_run'])
        self.assertTrue(status['queue_possible'])
        self.assertEqual(status['required_cores'], 4)
        self.assertEqual(status['queue_required'], 4)
        self.assertEqual(status['queue_max_required'], 24)
        self.assertIsNone(status['queue_quota'])
        self.assertIn('첫 하위 케이스를 지금 시작', status['availability_message'])
        self.assertIn('최대 24코어', status['availability_message'])

    def test_dynamic_macro_queues_when_only_the_first_child_cannot_start(self):
        members = [self.resource(cores, True) for cores in (12, 4, 24)]
        statuses = [self.capacity(12, False), self.capacity(4, True),
                    self.capacity(24, False)]

        with patch('cfd_bot.ticket_run.capacity_status', side_effect=statuses):
            status = self.runner._capacity(members, {}, [])

        self.assertFalse(status['can_run'])
        self.assertTrue(status['queue_possible'])
        self.assertEqual(status['queue_required'], 12)
        self.assertIn('대기열에 등록', status['availability_message'])

    def test_dynamic_macro_rejects_a_later_child_above_total_capacity(self):
        members = [self.resource(cores, True) for cores in (4, 52)]
        statuses = [self.capacity(4, True), self.capacity(52, False)]

        with patch('cfd_bot.ticket_run.capacity_status', side_effect=statuses):
            status = self.runner._capacity(members, {}, [])

        self.assertFalse(status['can_run'])
        self.assertFalse(status['queue_possible'])
        self.assertEqual(status['queue_max_required'], 52)
        self.assertIn('전체 관리 한도 51코어보다 큽니다', status['availability_message'])

    def test_legacy_lane_choice_is_not_overridden_by_old_ticket_state(self):
        case = dict(deepcopy(self.case), queue={'lane': 1, 'state': 'finished'})
        job = self.store.enqueue(case, queue_lane=3)
        self.assertEqual(job_queue_id(job), 'legacy-3')
