import json
import os
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cfd_bot.config import ConfigError, cpu_set
from cfd_bot.cpu_allocation import occupied_cpus, select_cpus, topology
from cfd_bot.execution import apply_execution_settings, execution_case
from cfd_bot.jobs import Scheduler
from cfd_bot.processes import check_cpus, snapshot
from tests.test_core import Environment


class AllocationTests(unittest.TestCase):
    def setUp(self):
        self.layout = {i: (i // 4, i // 4, i % 4) for i in range(8)}

    def test_selects_other_socket_and_supports_fragmented_free_cores(self):
        self.assertEqual(select_cpus(3, self.layout, range(8), {0, 1})['cpu_set'], '4-6')
        allocation = select_cpus(3, self.layout, range(8), {1, 4, 5})
        self.assertEqual(allocation, dict(cpu_set='0,2-3', sockets=[0], numa_nodes=[0]))

    def test_busy_and_reserved_cpus_are_both_excluded(self):
        observed = {'external': {'processes': [{'cpu_list': '0-1'}, {'cpu_list': '4'}]}}
        active = [{'status': 'postprocessing', 'case': {'cpu_set': '2,5'}}]
        busy = occupied_cpus(observed, active)
        self.assertEqual(busy, {0, 1, 2, 4, 5})
        self.assertEqual(select_cpus(2, self.layout, range(8), busy)['cpu_set'], '6-7')
        with self.assertRaisesRegex(ValueError, 'CPU 범위'):
            occupied_cpus({'unknown': {'processes': [{'cpu_list': ''}]}}, [])

    def test_monitor_cpu_is_reserved_for_later_jobs(self):
        active = [{'case': {'cpu_set': '0-2', 'monitor_cpu': '4'}}]
        self.assertEqual(occupied_cpus({}, active), {0, 1, 2, 4})
        self.assertEqual(select_cpus(2, self.layout, range(8),
                                     occupied_cpus({}, active))['cpu_set'], '5-6')

    def test_spans_sockets_without_reducing_count_or_using_busy_cores(self):
        allocation = select_cpus(5, self.layout, range(8), set())
        self.assertEqual(allocation, dict(cpu_set='0-2,4-5', sockets=[0, 1], numa_nodes=[0, 1]))
        allocation = select_cpus(3, self.layout, range(8), {0, 1, 4, 5})
        self.assertEqual(allocation['cpu_set'], '2-3,6')
        with self.assertRaisesRegex(ValueError, '최대는 8코어'):
            select_cpus(9, self.layout, range(8), set())
        with self.assertRaisesRegex(ValueError, '자동 CPU 배정 대기'):
            select_cpus(5, self.layout, range(8), {0, 1, 4, 5})

    def test_48_and_52_cores_use_both_sockets_and_exclude_reserved_cores(self):
        layout = {i: (i // 26, i // 26, i % 26) for i in range(52)}
        self.assertEqual(select_cpus(48, layout, range(52), set())['cpu_set'], '0-23,26-49')
        self.assertEqual(select_cpus(52, layout, range(52), set())['cpu_set'], '0-51')
        self.assertEqual(select_cpus(48, layout, range(52), {0, 1, 26, 27})['cpu_set'], '2-25,28-51')
        with self.assertRaisesRegex(ValueError, '자동 CPU 배정 대기'):
            select_cpus(52, layout, range(52), {0})

    def test_multi_socket_smt_siblings_of_busy_cores_stay_excluded(self):
        layout = {i: (i // 4, i // 4, i % 2) for i in range(8)}
        allocation = select_cpus(3, layout, {0, 1, 4, 5, 6, 7}, {2})
        self.assertEqual(allocation['cpu_set'], '1,4-5')
        self.assertEqual(len({(layout[c][0], layout[c][2]) for c in cpu_set(allocation['cpu_set'])}), 3)

    def test_affinity_and_numa_limits_are_respected(self):
        self.assertEqual(select_cpus(2, self.layout, {2, 3, 4}, set())['cpu_set'], '2-3')
        self.assertEqual(select_cpus(3, self.layout, {2, 3, 4}, set())['cpu_set'], '2-4')
        with self.assertRaisesRegex(ValueError, '최대는 3코어'):
            select_cpus(4, self.layout, {2, 3, 4}, set())
        layout = {i: (0, i // 2, i) for i in range(4)}
        self.assertEqual(select_cpus(3, layout, range(4), set()),
                         dict(cpu_set='0-2', sockets=[0], numa_nodes=[0, 1]))

    def test_smt_siblings_are_neither_double_counted_nor_shared(self):
        layout = {0: (0, 0, 0), 1: (0, 0, 1), 2: (0, 0, 0), 3: (0, 0, 1)}
        self.assertEqual(select_cpus(2, layout, range(4), set())['cpu_set'], '0-1')
        self.assertEqual(select_cpus(1, layout, {0, 1}, {2})['cpu_set'], '1')
        with self.assertRaisesRegex(ValueError, '최대는 2코어'):
            select_cpus(3, layout, range(4), set())

    def test_topology_reads_online_cpus_and_socket_node_core_ids(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'online').write_text('0,2-3\n')
            for cpu, socket, node, core in [(0, 0, 0, 0), (2, 1, 2, 0), (3, 1, 3, 1)]:
                folder = root / f'cpu{cpu}'
                (folder / 'topology').mkdir(parents=True)
                (folder / 'topology/physical_package_id').write_text(str(socket))
                (folder / 'topology/core_id').write_text(str(core))
                (folder / f'node{node}').mkdir()
            self.assertEqual(topology(root), {0: (0, 0, 0), 2: (1, 2, 0), 3: (1, 3, 1)})

    def test_case_source_preserves_np_with_trailing_export_during_auto_allocation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            allrun = root / 'Allrun'
            before = '#!/bin/sh\nNP=4; export NP\nCPU_SET=0-3; export CPU_SET\n'
            allrun.write_text(before)
            case = dict(_root=str(root), resource_source='case', cpu_policy='auto',
                        cores=1, command=['./Allrun'])

            resolved = execution_case(case)

            self.assertEqual(resolved['cores'], 4)
            resolved['cpu_set'] = '4-7'
            apply_execution_settings(resolved, root / 'job')
            self.assertEqual(allrun.read_text(),
                             '#!/bin/sh\nNP=4; export NP\nCPU_SET=4-7; export CPU_SET\n')
            backup = root / 'job/case-settings-before/Allrun'
            self.assertEqual(backup.read_text(), before)
            self.assertEqual((root / '.process-core').read_text(),
                             'NP=4\nCPU_SET="4-7"\n\nexport NP CPU_SET\n')

    def test_missing_process_core_is_created_for_manual_case(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'Allrun').write_text('#!/bin/sh\nNP=2\nCPU_SET=6-7\n')
            case = execution_case(dict(_root=str(root), resource_source='case',
                                       cpu_policy='manual', cores=1,
                                       command=['./Allrun']))

            apply_execution_settings(case, root / 'job')

            self.assertEqual((root / '.process-core').read_text(),
                             'NP=2\nCPU_SET="6-7"\n\nexport NP CPU_SET\n')
            self.assertFalse((root / 'job/case-settings-before/.process-core').exists())

    def test_process_core_wins_over_legacy_settings_and_tracks_auto_allocation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config').mkdir()
            (root / 'Allrun').write_text('#!/bin/sh\nNP=99\nCPU_SET=0-98\n')
            (root / 'config/solverRun').write_text('NP=8\nCPU_SET=0-7\n')
            original = 'NP=4\nCPU_SET="0-3"\n\nexport NP CPU_SET\n'
            (root / '.process-core').write_text(original)
            case = execution_case(dict(_root=str(root), resource_source='case',
                                       cpu_policy='auto', cores=1,
                                       command=['./Allrun']))
            self.assertEqual(case['cores'], 4)

            case['cpu_set'] = '4-7'
            apply_execution_settings(case, root / 'job')

            self.assertEqual((root / '.process-core').read_text(),
                             'NP=4\nCPU_SET="4-7"\n\nexport NP CPU_SET\n')
            self.assertEqual((root / 'job/case-settings-before/.process-core').read_text(),
                             original)
            self.assertIn('NP=8', (root / 'config/solverRun').read_text())

    def test_process_core_symlink_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'outside'
            target.write_text('NP=2\nCPU_SET=0-1\n')
            (root / '.process-core').symlink_to(target)
            (root / 'Allrun').write_text('#!/bin/sh\n')
            with self.assertRaisesRegex(ValueError, '일반 파일'):
                execution_case(dict(_root=str(root), resource_source='case',
                                    cpu_policy='auto', cores=2,
                                    command=['./Allrun']))

    def test_case_np_parser_does_not_execute_shell_expressions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / 'unsafe'
            (root / 'Allrun').write_text(f'NP="$(touch {marker})"; export NP\n')
            resolved = execution_case(dict(_root=str(root), resource_source='case',
                                           cpu_policy='auto', cores=2,
                                           command=['./Allrun']))
            self.assertEqual(resolved['cores'], 2)
            self.assertFalse(marker.exists())


class AutomaticSchedulerTests(Environment):
    def setUp(self):
        super().setUp()
        self.case_data.update(cpu_policy='auto', resource_source='macro')
        self.case_data.pop('cpu_set')
        self.write_case()
        self.layout = {i: (i // 4, i // 4, i % 4) for i in range(8)}
        for name, value in [('cfd_bot.cpu_allocation.topology', self.layout),
                            ('cfd_bot.cpu_allocation.os.sched_getaffinity', set(range(8)))]:
            context = patch(name, return_value=value)
            context.start()
            self.addCleanup(context.stop)

    def test_auto_schema_allows_cross_socket_including_legacy_auto_tickets(self):
        self.assertNotIn('cpu_set', self.case)
        for flag in (True, False):
            self.case_data['allow_cross_socket'] = flag
            self.write_case()
            self.assertTrue(self.case['allow_cross_socket'])
            self.assertTrue(execution_case(self.case)['allow_cross_socket'])
        self.case_data.update(allow_cross_socket=False, cpu_policy='unknown')
        with self.assertRaisesRegex(ConfigError, 'cpu_policy'):
            self.write_case()

    def test_case_source_uses_semicolon_export_np_for_allocation(self):
        self.case_data['resource_source'] = 'case'
        self.write_case()
        (self.case_root / 'Allrun').write_text('#!/bin/sh\nNP=4; export NP\n')
        job = self.store.enqueue(self.case)
        with patch('cfd_bot.jobs.openfoam_environment', return_value=os.environ.copy()), \
                patch('cfd_bot.jobs.snapshot', return_value={'cases': {}}), \
                patch('cfd_bot.jobs.check_cpus', return_value=(True, 'SAFE')) as check, \
                patch('cfd_bot.jobs.subprocess.Popen') as launch:
            Scheduler(self.config, self.store).tick({})
        admitted = self.store.job(job['id'])['case']
        self.assertEqual(admitted['cores'], 4)
        self.assertEqual(admitted['cpu_set'], '0-3')
        self.assertEqual(check.call_count, 1)
        launch.assert_called_once()

    def test_fresh_scan_after_environment_setup_picks_other_socket(self):
        self.case_data['cores'] = 3
        self.write_case()
        job = self.store.enqueue(self.case)
        order = []
        def environment(*args):
            order.append('environment')
            return os.environ.copy()
        def scan(*args):
            order.append('snapshot')
            return {'cases': {'external': {'processes': [{'cpu_list': '0-2'}]}}}
        def check(command, case):
            order.append('check')
            self.assertEqual(case['cpu_set'], '4-6')
            self.assertTrue(case['allow_cross_socket'])
            return True, 'SAFE'
        with patch('cfd_bot.jobs.openfoam_environment', side_effect=environment), \
                patch('cfd_bot.jobs.snapshot', side_effect=scan), \
                patch('cfd_bot.jobs.check_cpus', side_effect=check), \
                patch('cfd_bot.jobs.subprocess.Popen') as launch:
            Scheduler(self.config, self.store).tick({})
        launch.assert_called_once()
        self.assertEqual(order, ['environment', 'snapshot', 'check'])
        self.assertEqual(self.store.job(job['id'])['case']['cpu_set'], '4-6')
        self.assertNotIn('cpu_set', json.loads(self.case_path.read_text()))

    def test_auto_monitor_reserves_one_extra_cpu_without_changing_solver_cores(self):
        self.case_data.update(cores=3, monitoring={
            'allocate_cpu': True, 'command': ['./Allmonitor']})
        self.write_case()
        job = self.store.enqueue(self.case)
        with patch('cfd_bot.jobs.openfoam_environment', return_value=os.environ.copy()), \
                patch('cfd_bot.jobs.snapshot', return_value={'cases': {}}), \
                patch('cfd_bot.jobs.check_cpus', return_value=(True, 'SAFE')) as check, \
                patch('cfd_bot.jobs.subprocess.Popen') as launch:
            Scheduler(self.config, self.store).tick({})
        admitted = self.store.job(job['id'])['case']
        self.assertEqual(len(cpu_set(admitted['cpu_set'])), 3)
        self.assertEqual(len(cpu_set(admitted['monitor_cpu'])), 1)
        self.assertFalse(cpu_set(admitted['cpu_set']) & cpu_set(admitted['monitor_cpu']))
        self.assertEqual(admitted['cores'], 3)
        self.assertEqual(check.call_count, 2)
        launch.assert_called_once()

    def test_reserves_cpu_sets_for_two_workers_started_in_the_same_tick(self):
        self.config['scheduler']['max_parallel'] = 2
        self.case_data['cores'] = 3
        self.write_case()
        self.store.enqueue(self.case, queue_lane=1)
        other = deepcopy(self.case)
        other.pop('_config')
        other['_root'] = str(self.root / 'another')
        self.store.enqueue(other, queue_lane=2)
        with patch('cfd_bot.jobs.snapshot', return_value={'cases': {}}), \
                patch('cfd_bot.jobs.check_cpus', return_value=(True, 'SAFE')), \
                patch('cfd_bot.jobs.subprocess.Popen') as launch:
            Scheduler(self.config, self.store).tick({})
        self.assertEqual(launch.call_count, 2)
        jobs = self.store.jobs(('starting',))
        self.assertEqual([j['case']['cpu_set'] for j in jobs], ['0-2', '4-6'])

    def test_cross_socket_jobs_reserve_every_core_before_next_admission(self):
        self.config['scheduler']['max_parallel'] = 3
        self.case_data['cores'] = 5
        self.write_case()
        self.store.enqueue(self.case, queue_lane=1)
        for lane, (name, count) in enumerate([('second', 3), ('third', 1)], start=2):
            other = deepcopy(self.case)
            other.pop('_config')
            other.update(_root=str(self.root / name), cores=count)
            self.store.enqueue(other, queue_lane=lane)
        with patch('cfd_bot.jobs.snapshot', return_value={'cases': {}}), \
                patch('cfd_bot.jobs.check_cpus', return_value=(True, 'SAFE')), \
                patch('cfd_bot.jobs.subprocess.Popen') as launch:
            Scheduler(self.config, self.store).tick({})
        self.assertEqual(launch.call_count, 2)
        first, second = self.store.jobs(('starting',))
        self.assertEqual(first['case']['cpu_set'], '0-2,4-5')
        self.assertEqual(second['case']['cpu_set'], '3,6-7')
        self.assertFalse(cpu_set(first['case']['cpu_set']) & cpu_set(second['case']['cpu_set']))
        self.assertIn('자동 CPU 배정 대기', self.store.jobs(('queued',))[0]['reason'])

    def test_ofps_cross_socket_flag_does_not_bypass_overlap_failure(self):
        with patch('cfd_bot.processes.subprocess.run') as run:
            run.return_value.returncode = 4
            run.return_value.stdout = 'BLOCKED: CPU overlap'
            run.return_value.stderr = ''
            safe, report = check_cpus(['ofps'], dict(cpu_set='0-7', allow_cross_socket=True))
        self.assertFalse(safe)
        self.assertIn('CPU overlap', report)
        self.assertEqual(run.call_args.args[0], ['ofps', '--check', '0-7', '--allow-cross-socket'])
        self.assertEqual(run.call_args.kwargs['env']['CFD_BOT_OFPS_MANAGED'], '1')

    def test_application_snapshot_disables_embedded_ofps_state_sync(self):
        with patch('cfd_bot.processes.subprocess.run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = 'No active OpenFOAM or Basilisk calculations found.\n'
            run.return_value.stderr = ''
            result = snapshot(['ofps'])
        self.assertEqual(result['cases'], {})
        self.assertEqual(run.call_args.kwargs['env']['CFD_BOT_OFPS_MANAGED'], '1')

    def test_allocation_and_final_check_failures_keep_job_queued(self):
        job = self.store.enqueue(self.case)
        for live, check in [({'external': {'processes': [{'cpu_list': '0-7'}]}}, (True, 'SAFE')),
                            ({}, (False, 'occupied since scan')),
                            ({self.case['_root']: {}}, (True, 'SAFE'))]:
            with self.subTest(live=live, check=check), \
                    patch('cfd_bot.jobs.snapshot', return_value={'cases': live}), \
                    patch('cfd_bot.jobs.check_cpus', return_value=check), \
                    patch('cfd_bot.jobs.subprocess.Popen') as launch:
                Scheduler(self.config, self.store).tick({})
            launch.assert_not_called()
            waiting = self.store.job(job['id'])
            self.assertEqual(waiting['status'], 'queued')
            self.assertTrue(waiting['reason'])
        with patch('cfd_bot.jobs.snapshot', side_effect=RuntimeError('scan unavailable')), \
                patch('cfd_bot.jobs.subprocess.Popen') as launch:
            Scheduler(self.config, self.store).tick({})
        launch.assert_not_called()
        self.assertIn('scan unavailable', self.store.job(job['id'])['reason'])
        with patch('cfd_bot.jobs.snapshot', return_value={'cases': {}}), \
                patch('cfd_bot.jobs.check_cpus', return_value=(True, 'SAFE')), \
                patch('cfd_bot.jobs.subprocess.Popen') as launch:
            Scheduler(self.config, self.store).tick({})
        launch.assert_called_once()
        self.assertEqual(self.store.job(job['id'])['status'], 'starting')

    def test_stale_cpu_ids_are_discarded_and_standalone_np_comes_from_case(self):
        (self.case_root / 'config').mkdir()
        (self.case_root / 'config/testRun').write_text('NP=3\nCPU_SET=0-7\n')
        case = dict(self.case, resource_source='case', cpu_set='0-7')
        resolved = execution_case(case)
        self.assertEqual(resolved['cores'], 3)
        self.assertNotIn('cpu_set', resolved)


class AutomaticWorkerTests(Environment):
    def test_worker_updates_case_assignments_to_the_chosen_allocation(self):
        (self.case_root / 'config').mkdir()
        settings = self.case_root / 'config/testRun'
        before = 'NP=48\nCPU_SET=0-47\n'
        settings.write_text(before)
        allrun = self.case_root / 'Allrun'
        allrun.write_text('#!/bin/sh\nset -eu\n. ./config/testRun\n'
                          f'test "$NP" = 1\ntest "$CPU_SET" = {self.cpu}\n'
                          'printf "Time = 10\\nEnd\\n"\n')
        allrun.chmod(0o755)
        self.case_data.update(cpu_policy='auto', resource_source='macro', command=['./Allrun'])
        self.case_data.pop('cpu_set')
        self.write_case()
        job = self.store.enqueue(self.case)
        with patch('cfd_bot.jobs.snapshot', return_value={'cases': {}}), \
                patch('cfd_bot.jobs.check_cpus', return_value=(True, 'SAFE')), \
                patch('cfd_bot.jobs.allocate_cpus', return_value={'cpu_set': self.cpu}):
            scheduler = Scheduler(self.config, self.store)
            scheduler.tick({})
        self.assertEqual(scheduler.children[0].wait(timeout=10), 0)
        finished = self.store.job(job['id'])
        self.assertEqual(finished['status'], 'succeeded', finished.get('reason'))
        self.assertEqual(finished['case']['cpu_set'], self.cpu)
        backup = self.store.root / 'jobs' / job['id'] / 'case-settings-before/config/testRun'
        self.assertEqual(backup.read_text(), before)
        self.assertEqual(settings.read_text(), f'NP=1\nCPU_SET={self.cpu}\n')
