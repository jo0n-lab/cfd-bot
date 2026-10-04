from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from unittest.mock import patch

from cfd_bot.config import cases_for, load_case, load_bot, read_json, tickets_for
from cfd_bot.control import control_times
from cfd_bot.execution import execution_case
from cfd_bot.editor import TicketService
from cfd_bot.ticket_run import TicketRunner
from cfd_bot.jobs import Scheduler, worker
from cfd_bot.outcomes import decide
from cfd_bot.storage import Store
from cfd_bot.tickets import (accept_submissions, checkpoint_time, discover_cases,
                             postprocess_time, publish_macro, sync_ticket_states, ticket_name)
from tests.test_core import Environment


class QueueTicketTests(Environment):
    def setUp(self):
        super().setUp()
        self.tickets = self.root / 'tickets'
        self.tickets.mkdir()
        self.parent = self.root / 'batch'
        self.parent.mkdir()
        self.config['cases'] = []
        self.config['case_globs'] = [str(self.tickets / '*.json')]

    def case_dir(self, name, *, checkpoint=None, post=None):
        root = self.parent / name
        root.mkdir(parents=True)
        (root / 'system').mkdir()
        (root / 'system/controlDict').write_text('startTime 0; stopAt endTime; endTime 10;\n')
        (root / 'Allrun').write_text('#!/bin/sh\nprintf "Time = 10\\nEnd\\n"\n')
        (root / 'Allrun').chmod(0o755)
        (root / 'config').mkdir()
        (root / 'config/testRun').write_text(f'NP=1\nCPU_SET="{self.cpu}"\n')
        if checkpoint is not None:
            (root / str(checkpoint)).mkdir()
        if post is not None:
            folder = root / 'postProcessing/sample/0'
            folder.mkdir(parents=True)
            (folder / 'metrics.csv').write_text(f'time,value\n{post},1\n')
        return root

    def macro(self, roots):
        data = dict(version=1, name='Batch', case_dir=str(self.parent), task_type='macro',
                    role='alone', watcher={'logs': ['log.solver']},
                    residual_pattern='plots/residual*.png',
                    exports=[dict(name='metrics', pattern='metrics.csv', on=[])],
                    cases=[dict(case_dir=str(root), state='waiting') for root in roots])
        path = self.tickets / 'macro-batch.json'
        return path, publish_macro(path, data)

    def test_discovery_includes_completed_cases_and_filters_templates_and_running(self):
        pending = self.case_dir('a-new')
        interrupted = self.case_dir('b-incomplete', checkpoint=5, post=7)
        done = self.case_dir('c-done', checkpoint=10, post=10)
        active = self.case_dir('d-active')
        self.case_dir('e-template')
        self.case_dir('f-template/nested')
        rows, skipped = discover_cases(self.parent, {str(active): {}})
        self.assertEqual([row['case_dir'] for row in rows], [str(pending), str(interrupted), str(done)])
        self.assertTrue(all(row['state'] == 'waiting' for row in rows))
        self.assertIn('미실행', rows[0]['reason'])
        self.assertIn('미완료', rows[1]['reason'])
        self.assertIn('종료값 도달', rows[2]['reason'])
        self.assertIn((str(active), '계산중'), skipped)
        rows, _ = discover_cases(self.parent, {}, end_time=20)
        self.assertIn(str(done), [r['case_dir'] for r in rows])

    def test_discovery_only_checks_direct_child_directories(self):
        direct = self.case_dir('direct')
        nested = self.case_dir('group/nested')
        self.case_dir('.hidden')
        self.case_dir('ignored-template')
        (self.parent / 'Allrun').write_text('#!/bin/sh\n')
        (self.parent / 'linked').symlink_to(nested, target_is_directory=True)

        rows, skipped = discover_cases(self.parent, {})

        self.assertEqual([row['case_dir'] for row in rows], [str(direct)])
        self.assertEqual(skipped, [])

    def test_postprocessing_cases_without_end_time_are_selectable_and_publishable(self):
        unknown = self.case_dir('a-unknown', post=10)
        (unknown / 'system/controlDict').unlink()
        empty = self.case_dir('b-empty')
        (empty / 'postProcessing').mkdir()
        rows, skipped = discover_cases(self.parent, {})
        self.assertEqual([row['case_dir'] for row in rows], [str(unknown), str(empty)])
        self.assertEqual(skipped, [])
        self.assertIn('endTime 확인 불가', rows[0]['reason'])
        self.assertTrue(all('postProcessing 있음' in row['reason'] for row in rows))
        service = TicketService(self.tickets)
        draft = service.new('macro')
        draft['values'].update(case_dir=str(self.parent), macro_cores='1', macro_cpu_set=self.cpu, cases=rows)
        filename, document = service.save(draft['values'], 'macro-history.json')
        self.assertEqual([r['case_dir'] for r in document['cases']], [str(unknown), str(empty)])
        self.assertTrue(all(r['state'] == 'waiting' for r in document['cases']))
        self.assertEqual(len(cases_for(self.config)), 2)

    def test_checkpoint_requires_progress_on_every_processor_and_reads_postprocessing_samples(self):
        root = self.case_dir('distributed', post=10)
        for rank, step in ((0, 10), (1, 5)):
            (root / f'processor{rank}' / str(step)).mkdir(parents=True)
        self.assertEqual(checkpoint_time(root), 5)
        self.assertEqual(postprocess_time(root), 10)
        rows, _ = discover_cases(self.parent, {})
        self.assertEqual(len(rows), 1)

    def test_publish_copies_common_settings_and_relative_parent_links(self):
        roots = [self.case_dir('a'), self.case_dir('b')]
        path, macro = self.macro(roots)
        children = cases_for(self.config)
        self.assertEqual(len(children), 2)
        self.assertTrue(macro['queue']['submit'])
        self.assertEqual([r['case_dir'] for r in macro['cases']], list(map(str, roots)))
        for child in children:
            self.assertTrue(Path(child['_config']).name.startswith('child-'))
            self.assertEqual(child['macro_ticket'], path.name)
            self.assertEqual(child['role'], 'child')
            self.assertEqual(child['residual_pattern'], 'plots/residual*.png')
            self.assertEqual(child['exports'][0]['pattern'], 'metrics.csv')
            self.assertEqual(child['queue']['state'], 'waiting')
        self.assertEqual(ticket_name('previous.json', 'alone'), 'alone-previous.json')

    def test_submission_is_ordered_idempotent_and_survives_restart(self):
        path, macro = self.macro([self.case_dir('b'), self.case_dir('a')])
        accept_submissions(self.config, self.store)
        jobs = self.store.jobs()
        self.assertEqual([j['case_root'] for j in jobs], [r['case_dir'] for r in macro['cases']])
        self.assertFalse(read_json(path)['queue']['submit'])
        # Simulate a crash after the SQLite commit but before flag acknowledgement.
        data = read_json(path)
        data['queue']['submit'] = True
        path.write_text(json.dumps(data))
        accept_submissions(self.config, Store(self.store.root))
        self.assertEqual([j['id'] for j in self.store.jobs()], [j['id'] for j in jobs])
        self.assertEqual(len(self.store.pending()), 1)

    def test_conflicting_member_rolls_back_entire_batch(self):
        path, _ = self.macro([self.case_dir('a'), self.case_dir('b')])
        children = cases_for(self.config)
        self.store.enqueue(children[1])
        accept_submissions(self.config, self.store)
        self.assertEqual(len(self.store.jobs()), 1)
        self.assertTrue(read_json(path)['queue']['submit'])
        self.assertIn('다른 큐', read_json(path)['queue']['reason'])

    def test_json_states_follow_observation_and_terminal_result(self):
        path, macro = self.macro([self.case_dir('a')])
        accept_submissions(self.config, self.store)
        job = self.store.jobs()[0]
        child_path = self.tickets / macro['cases'][0]['ticket']
        snapshot = {'cases': {job['case_root']: {}}}
        sync_ticket_states(self.config, self.store, snapshot)
        self.assertEqual(read_json(path)['cases'][0]['state'], 'running')
        self.assertEqual(read_json(child_path)['queue']['state'], 'running')
        # ofps disappearance alone cannot declare success while the worker is alive.
        self.store.update_job(job['id'], status='running')
        sync_ticket_states(self.config, self.store, {'cases': {}})
        self.assertEqual(read_json(child_path)['queue']['state'], 'running')
        self.store.update_job(job['id'], status='failed', finished=time.time(), reason='early exit')
        sync_ticket_states(self.config, self.store, {'cases': {}})
        self.assertEqual(read_json(path)['queue']['state'], 'finished')
        self.assertEqual(read_json(path)['cases'][0]['result'], 'failed')
        self.assertEqual(read_json(child_path)['queue']['state'], 'finished')

    def test_existing_macro_save_does_not_resubmit_and_cannot_silently_reorder(self):
        path, macro = self.macro([self.case_dir('a'), self.case_dir('b')])
        accept_submissions(self.config, self.store)
        data = read_json(path)
        data['residual_pattern'] = 'other/residual*.png'
        updated = publish_macro(path, data, previous=path)
        self.assertFalse(updated['queue']['submit'])
        self.assertEqual(updated['queue']['request_id'], macro['queue']['request_id'])
        data['cases'].reverse()
        with self.assertRaisesRegex(ValueError, '순서'):
            publish_macro(path, data, previous=path)

    def finished_macro(self):
        path, macro = self.macro([self.case_dir('a'), self.case_dir('b')])
        accept_submissions(self.config, self.store)
        for job in self.store.jobs():
            self.store.update_job(job['id'], status='succeeded', finished=time.time())
        sync_ticket_states(self.config, self.store, {'cases': {}})
        return path

    def test_finished_macro_can_change_members_and_order_before_explicit_run(self):
        path = self.finished_macro()
        before = read_json(path)
        jobs = self.store.jobs()
        root = self.case_dir('c')
        standalone = self.tickets / 'alone-c.json'
        standalone.write_text(json.dumps(dict(version=1, case_dir=str(root), name='C')))
        new = self.case_dir('d')
        data = deepcopy(before)
        data['cases'] = [before['cases'][1], dict(case_dir=str(root), state='waiting'),
                         dict(case_dir=str(new), state='waiting')]
        changed = publish_macro(path, data, previous=path)
        self.assertFalse(changed['queue']['submit'])
        self.assertNotIn('request_id', changed['queue'])
        self.assertTrue(all(row['ticket'].startswith('child-') for row in changed['cases']))
        self.assertFalse(standalone.exists())
        cases = cases_for(self.config)
        detached = next(c for c in cases if c['_root'] == before['cases'][0]['case_dir'])
        self.assertEqual(detached['role'], 'alone')
        self.assertNotIn('macro_ticket', detached)
        self.assertTrue(Path(detached['_config']).name.startswith('alone-'))
        self.assertTrue(Path(detached['_root']).is_dir())
        # A revised but unsubmitted batch can be edited again, including a new case.
        changed['cases'].reverse()
        changed = publish_macro(path, changed, previous=path)
        accept_submissions(self.config, self.store)
        self.assertEqual(self.store.jobs(), jobs)
        runner = TicketRunner(TicketService(self.tickets), self.config, self.store)
        with patch('cfd_bot.ticket_run.snapshot', return_value={'cases': {}}):
            runner.request(path.name)
        accept_submissions(self.config, self.store)
        queued = self.store.jobs(('queued',))
        self.assertEqual([j['case_root'] for j in queued], [r['case_dir'] for r in changed['cases']])
        self.assertTrue(all(j['batch'] != before['queue']['request_id'] for j in queued))

    def test_reconfiguration_checks_child_queue_even_when_macro_state_is_stale(self):
        path = self.finished_macro()
        data = read_json(path)
        child = self.tickets / data['cases'][0]['ticket']
        changed_child = read_json(child)
        changed_child['queue'].update(state='waiting', job_id='queued-elsewhere')
        child.write_text(json.dumps(changed_child))
        before = {p: p.read_bytes() for p in self.tickets.glob('*.json')}
        data['cases'].reverse()
        with self.assertRaisesRegex(ValueError, '대기·실행 중인 케이스'):
            publish_macro(path, data, previous=path)
        self.assertEqual({p: p.read_bytes() for p in self.tickets.glob('*.json')}, before)

    def test_reconfiguration_rolls_back_adoption_and_detachment_on_write_failure(self):
        path = self.finished_macro()
        data = read_json(path)
        data['cases'] = [data['cases'][1], dict(case_dir=str(self.case_dir('c')), state='waiting')]
        before = {p: p.read_bytes() for p in self.tickets.glob('*.json')}
        from cfd_bot.tickets import atomic_json
        def fail_commit(target, document):
            if target == path:
                raise OSError('commit failed')
            atomic_json(target, document)
        with patch('cfd_bot.tickets.atomic_json', side_effect=fail_commit):
            with self.assertRaisesRegex(OSError, 'commit failed'):
                publish_macro(path, data, previous=path)
        self.assertEqual({p: p.read_bytes() for p in self.tickets.glob('*.json')}, before)

    def running_macro(self):
        path, data = self.macro([self.case_dir('a'), self.case_dir('b')])
        data.update(resource_source='macro', cores=1, cpu_set=self.cpu, command=['./Allrun'])
        publish_macro(path, data, previous=path)
        accept_submissions(self.config, self.store)
        job = self.store.jobs()[0]
        self.store.update_job(job['id'], status='running', started=time.time())
        sync_ticket_states(self.config, self.store, {'cases': {job['case_root']: {}}})
        return path

    def test_running_macro_can_edit_add_and_remove_request_data_without_requeueing(self):
        path = self.running_macro()
        service = TicketService(self.tickets)
        # An individually renamed child must stay at the path referenced by its parent.
        first = read_json(path)['cases'][0]['ticket']
        draft = service.open(first)
        service.save(draft['values'], 'child-renamed.json', first)
        before = {p: read_json(p) for p in self.tickets.glob('*.json')}
        jobs = self.store.jobs()
        exports = [dict(name='metrics', pattern='validation/live/steady_coupled_naoh.png',
                        kind='photo', max_files=1),
                   dict(name='data', pattern='postProcessing/*.csv', kind='document', max_files=2)]
        for items in (exports, exports[1:], []):
            with self.subTest(exports=items):
                draft = service.open(path.name)
                draft['values']['exports'] = deepcopy(items)
                service.save(draft['values'], path.name, path.name, expected_revision=draft['revision'])
                macro = read_json(path)
                self.assertEqual(macro['queue'], before[path]['queue'])
                self.assertEqual(macro['cases'], before[path]['cases'])
                for row in macro['cases']:
                    child_path = self.tickets / row['ticket']
                    child = read_json(child_path)
                    self.assertEqual(child['exports'], macro['exports'])
                    self.assertEqual([e['pattern'] for e in child['exports']], [e['pattern'] for e in items])
                    self.assertEqual(child['queue'], before[child_path]['queue'])
                    for key in ('case_dir', 'command', 'cores', 'cpu_set', 'resource_source', 'macro_ticket'):
                        self.assertEqual(child[key], before[child_path][key])
                accept_submissions(self.config, self.store)
                self.assertEqual(self.store.jobs(), jobs)
                self.assertEqual(set(self.tickets.glob('*.json')), set(before))

    def test_running_macro_execution_edits_are_rejected_before_any_file_changes(self):
        path = self.running_macro()
        before = {p: p.read_bytes() for p in self.tickets.glob('*.json')}
        changes = dict(command=['./Allrun', '--changed'], cores=2,
                       cpu_set=str(int(self.cpu) + 1), resource_source='case',
                       allow_cross_socket=True, preprocess=[{'command': ['true']}],
                       postprocess=[{'command': ['true']}],
                       monitoring={'allocate_cpu': True, 'command': ['./Allmonitor']})
        for key, value in changes.items():
            with self.subTest(field=key):
                data = read_json(path)
                data[key] = value
                data['exports'][0]['pattern'] = 'edited.csv'
                with self.assertRaisesRegex(ValueError, '계산 중에는'):
                    publish_macro(path, data, previous=path)
                self.assertEqual({p: p.read_bytes() for p in self.tickets.glob('*.json')}, before)

    def test_running_alone_cannot_be_adopted_by_new_macro(self):
        root = self.case_dir('active')
        original = self.tickets / 'alone-active.json'
        original.write_text(json.dumps(dict(version=1, case_dir=str(root), queue={'state': 'running'})))
        before = original.read_bytes()
        with self.assertRaisesRegex(ValueError, '새 매크로에 편입'):
            self.macro([root])
        self.assertEqual(original.read_bytes(), before)
        self.assertEqual(list(self.tickets.glob('*.json')), [original])

    def test_running_macro_save_restores_all_files_on_io_failure(self):
        path = self.running_macro()
        before = {p: p.read_bytes() for p in self.tickets.glob('*.json')}
        data = read_json(path)
        data['exports'][0]['pattern'] = 'edited.csv'
        from cfd_bot.tickets import atomic_json
        def fail_second(target, document):
            if target.name == data['cases'][1]['ticket']:
                raise OSError('disk error')
            atomic_json(target, document)
        with patch('cfd_bot.tickets.atomic_json', side_effect=fail_second):
            with self.assertRaisesRegex(OSError, 'disk error'):
                publish_macro(path, data, previous=path)
        self.assertEqual({p: p.read_bytes() for p in self.tickets.glob('*.json')}, before)

    def test_publish_failure_rolls_back_all_files(self):
        first, second = self.case_dir('a'), self.case_dir('b')
        import cfd_bot.tickets as tickets
        original = tickets.atomic_json
        count = 0
        def fail(path, data):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('simulated disk error')
            return original(path, data)
        with patch('cfd_bot.tickets.atomic_json', side_effect=fail):
            with self.assertRaises(OSError):
                self.macro([first, second])
        self.assertEqual(list(self.tickets.glob('*.json')), [])

    def test_execution_resolves_actual_case_settings_and_foreground(self):
        root = self.case_dir('a')
        path, _ = self.macro([root])
        case = cases_for(self.config)[0]
        (root / 'Allrun').write_text('#!/bin/sh\n# --foreground\n')
        execution = execution_case(case)
        self.assertEqual(execution['cpu_set'], self.cpu)
        self.assertEqual(execution['cores'], 1)
        self.assertEqual(execution['command'], ['./Allrun', '--foreground'])
        (root / 'config/testRun').write_text('NP=1\nCPU_SET="$(touch unsafe)"\n')
        with self.assertRaises(ValueError):
            execution_case(case)
        self.assertFalse((root / 'unsafe').exists())

    def test_macro_runs_one_child_at_a_time_even_with_parallel_capacity(self):
        self.macro([self.case_dir('a'), self.case_dir('b')])
        accept_submissions(self.config, self.store)
        self.config['scheduler']['max_parallel'] = 2
        scheduler = Scheduler(self.config, self.store)
        with patch('cfd_bot.jobs.check_cpus', return_value=(True, 'SAFE')):
            scheduler.tick({})
        self.assertEqual(len(scheduler.children), 1)
        for process in scheduler.children:
            self.assertEqual(process.wait(timeout=10), 0)
        jobs = self.store.jobs()
        self.assertEqual([j['status'] for j in jobs], ['succeeded', 'queued'])
        with patch('cfd_bot.jobs.check_cpus', return_value=(True, 'SAFE')):
            scheduler.tick({})
        for process in scheduler.children:
            self.assertEqual(process.wait(timeout=10), 0)
        self.assertEqual([j['status'] for j in self.store.jobs()], ['succeeded', 'succeeded'])

    def test_explicit_end_value_controls_verdict_and_does_not_edit_control_dict(self):
        before = self.control.read_bytes()
        case = dict(self.case, end_time=5)
        self.assertEqual(control_times(case)['end'], 5)
        self.assertEqual(decide(case, {'time': 5}, time.time())[0], 'succeeded')
        self.assertEqual(self.control.read_bytes(), before)

    def test_macro_resources_override_case_defaults_and_are_applied_with_backups(self):
        root = self.case_dir('common')
        settings = root / 'config/testRun'
        settings.write_text('NP=99\nCPU_SET="0-98"\n# retained\n')
        original = settings.read_bytes()
        path, data = self.macro([root])
        data.update(resource_source='macro', cores=1, cpu_set=self.cpu,
                    command=['./Allrun'])
        publish_macro(path, data, previous=path)
        case = cases_for(self.config)[0]
        self.assertEqual(case['cpu_set'], self.cpu)
        execution = execution_case(case)
        self.assertEqual(execution['cores'], 1)
        job = self.store.enqueue(execution)
        self.store.update_job(job['id'], status='starting', claimed=time.time())
        self.assertEqual(worker(self.store.root, job['id']), 0)
        self.assertIn(f'CPU_SET={self.cpu}', settings.read_text())
        backup = self.store.root / 'jobs' / job['id'] / 'case-settings-before/config/testRun'
        self.assertEqual(backup.read_bytes(), original)
        self.assertIn('# retained', settings.read_text())

    def test_single_ticket_manual_resources_are_applied_by_worker(self):
        root = self.case_dir('standalone')
        settings = root / 'config/testRun'
        settings.write_text('NP=99\nCPU_SET=0-98\n')
        path = self.tickets / 'alone-case.json'
        path.write_text(json.dumps(dict(version=1, case_dir=str(root), resource_source='ticket',
                                        cores=1, cpu_set=self.cpu, command=['./Allrun'])))
        execution = execution_case(load_case(path))
        self.assertEqual(execution['cores'], 1)
        self.assertEqual(execution['cpu_set'], self.cpu)
        job = self.store.enqueue(execution)
        self.store.update_job(job['id'], status='starting', claimed=time.time())
        self.assertEqual(worker(self.store.root, job['id']), 0)
        self.assertEqual(settings.read_text(), f'NP=1\nCPU_SET={self.cpu}\n')

    def test_new_submission_is_not_reported_as_an_old_finished_run(self):
        root = self.case_dir('again')
        path, data = self.macro([root])
        accept_submissions(self.config, self.store)
        job = self.store.jobs()[0]
        self.store.update_job(job['id'], status='succeeded', finished=time.time())
        child = self.tickets / data['cases'][0]['ticket']
        case = read_json(child)
        case['queue'].update(submit=True, request_id='new-request', state='waiting')
        child.write_text(json.dumps(case))
        sync_ticket_states(self.config, self.store, {'cases': {}})
        self.assertEqual(read_json(child)['queue']['state'], 'waiting')

    def test_queued_job_takes_priority_over_a_newer_finished_external_run(self):
        path, macro = self.macro([self.case_dir('queued')])
        accept_submissions(self.config, self.store)
        job = self.store.jobs()[0]
        self.store.put('observed:' + job['case_root'], dict(
            id='external', case=job['case'], case_root=job['case_root'],
            status='failed', created=job['created'] + 1, finished=time.time(), reason='stopped'))
        sync_ticket_states(self.config, self.store, {'cases': {}})
        self.assertEqual(read_json(path)['cases'][0]['state'], 'waiting')

    def test_integrated_ofps_updates_json_without_changing_cpu_check_result(self):
        path, macro = self.macro([self.case_dir('observed')])
        config = self.root / 'queue-bot.json'
        config.write_text(json.dumps(dict(version=1, state_dir=str(self.store.root),
                                         case_globs=[str(self.tickets / '*.json')])))
        env = dict(os.environ, CFD_BOT_CONFIG=str(config))
        wrapper = Path(__file__).resolve().parents[1] / 'bin/ofps'
        case_root = macro['cases'][0]['case_dir']
        solver = subprocess.Popen(
            ['taskset', '-c', self.cpu, 'bash', '-c',
             'cd "$1" && exec -a simpleFoam sleep 30', 'ofps-test', case_root],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                cmdline = Path(f'/proc/{solver.pid}/cmdline')
                cwd = Path(f'/proc/{solver.pid}/cwd')
                if (cmdline.exists() and cmdline.read_bytes().split(b'\0', 1)[0] == b'simpleFoam'
                        and cwd.resolve() == Path(case_root)):
                    break
                time.sleep(0.01)
            result = subprocess.run([str(wrapper), '--check', self.cpu], env=env,
                                    capture_output=True, text=True, timeout=10)
        finally:
            solver.terminate()
            solver.wait(timeout=5)
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn('BLOCKED: overlaps PID', result.stderr)
        self.assertEqual(read_json(path)['cases'][0]['state'], 'running')

    def test_integrated_ofps_ignores_openfoam_name_outside_a_case(self):
        wrapper = Path(__file__).resolve().parents[1] / 'bin/ofps'
        folder = self.root / 'not-an-openfoam-case'
        folder.mkdir()
        solver = subprocess.Popen(
            ['taskset', '-c', self.cpu, 'bash', '-c',
             'cd "$1" && exec -a foamRun sleep 30', 'ofps-false-positive', str(folder)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                cmdline = Path(f'/proc/{solver.pid}/cmdline')
                cwd = Path(f'/proc/{solver.pid}/cwd')
                if (cmdline.exists() and cmdline.read_bytes().split(b'\0', 1)[0] == b'foamRun'
                        and cwd.resolve() == folder):
                    break
                time.sleep(0.01)
            result = subprocess.run([str(wrapper)],
                                    env=dict(os.environ, CFD_BOT_OFPS_MANAGED='1'),
                                    capture_output=True, text=True, timeout=10)
        finally:
            solver.terminate()
            solver.wait(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(str(folder), result.stdout)
        self.assertNotIn('[controlDict not found]', result.stdout)

    def test_ofps_symlink_directory_is_not_an_implicit_basilisk_root(self):
        wrapper = Path(__file__).resolve().parents[1] / 'bin/ofps'
        local_bin = self.root / 'local-bin'
        local_bin.mkdir()
        linked_wrapper = local_bin / 'ofps'
        linked_wrapper.symlink_to(wrapper)
        ordinary = local_bin / 'ordinary-cli'
        shutil.copy('/bin/sleep', ordinary)
        solver = subprocess.Popen([str(ordinary), '30'])
        try:
            for _ in range(100):
                executable = Path(f'/proc/{solver.pid}/exe')
                if executable.exists() and executable.resolve() == ordinary:
                    break
                time.sleep(0.01)
            result = subprocess.run([str(linked_wrapper)],
                                    env=dict(os.environ, CFD_BOT_OFPS_MANAGED='1'),
                                    capture_output=True, text=True, timeout=10)
        finally:
            solver.terminate()
            solver.wait(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(str(local_bin), result.stdout)

    def test_ofps_detects_basilisk_case_with_explicit_root_and_sources(self):
        wrapper = Path(__file__).resolve().parents[1] / 'bin/ofps'
        root = self.root / 'basilisk'
        case = root / 'wave'
        case.mkdir(parents=True)
        (case / 'Makefile').write_text('all:\n\t@true\n')
        (case / 'wave.c').write_text('int main(void) { return 0; }\n')
        solver_path = case / 'wave'
        shutil.copy('/bin/sleep', solver_path)
        solver = subprocess.Popen([str(solver_path), '30'])
        try:
            for _ in range(100):
                executable = Path(f'/proc/{solver.pid}/exe')
                if executable.exists() and executable.resolve() == solver_path:
                    break
                time.sleep(0.01)
            result = subprocess.run([str(wrapper)], env=dict(
                os.environ, CFD_BOT_OFPS_MANAGED='1', OFPS_BASILISK_ROOT=str(root)),
                capture_output=True, text=True, timeout=10)
        finally:
            solver.terminate()
            solver.wait(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('ENGINE: Basilisk', result.stdout)
        self.assertIn('CASE: ' + str(case), result.stdout)

    def test_integrated_ofps_has_no_legacy_scanner_dependency(self):
        wrapper = Path(__file__).resolve().parents[1] / 'bin/ofps'
        source = wrapper.read_text()
        self.assertTrue(os.access(wrapper, os.X_OK))
        self.assertTrue(source.startswith('#!/usr/bin/env bash\n'))
        self.assertNotIn('OFPS_LEGACY', source)
        self.assertNotIn('ofps-legacy', source)
        result = subprocess.run([str(wrapper), '--help'], capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--check CPU_SET', result.stdout)
        self.assertIn('--status', result.stdout)

    def test_managed_ofps_scan_skips_embedded_ticket_sync(self):
        wrapper = Path(__file__).resolve().parents[1] / 'bin/ofps'
        marker = self.root / 'python-called'
        fake_python = self.root / 'fake-python'
        fake_python.write_text('#!/bin/sh\nprintf called > "$CFD_BOT_TEST_MARKER"\n')
        fake_python.chmod(0o700)
        env = dict(os.environ, CFD_BOT_OFPS_MANAGED='1', CFD_BOT_PYTHON=str(fake_python),
                   CFD_BOT_TEST_MARKER=str(marker))
        result = subprocess.run([str(wrapper)], env=env, capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())

    def test_ticket_state_sync_loads_catalog_once(self):
        self.macro([self.case_dir('single-load')])
        with patch('cfd_bot.config.tickets_for', wraps=tickets_for) as catalog:
            sync_ticket_states(self.config, self.store, {'cases': {}})
        self.assertEqual(catalog.call_count, 1)

    def test_queued_ticket_rename_keeps_the_same_job(self):
        root = self.case_dir('renamed')
        original = self.tickets / 'old.json'
        original.write_text(json.dumps(dict(version=1, case_dir=str(root))))
        job = self.store.enqueue(load_case(original))
        renamed = original.with_name('alone-renamed.json')
        original.rename(renamed)
        scheduler = Scheduler(self.config, self.store)
        with patch('cfd_bot.jobs.check_cpus', return_value=(True, 'SAFE')):
            scheduler.tick({})
        for process in scheduler.children:
            self.assertEqual(process.wait(timeout=10), 0)
        saved = self.store.job(job['id'])
        self.assertEqual(saved['status'], 'succeeded')
        self.assertEqual(saved['case']['_config'], str(renamed))
