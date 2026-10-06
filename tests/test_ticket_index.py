"""Isolated catalog and lifecycle regression tests; no solver or Telegram I/O."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import time
from unittest.mock import patch

from cfd_bot.bot import Bot
from cfd_bot.catalog import folder_index, ticket_index
from cfd_bot.config import ConfigError, cases_for, load_case, read_json
from cfd_bot.editor import TicketService
from cfd_bot.monitor import Monitor
from cfd_bot.storage import Store
from cfd_bot.ticket_run import TicketRunner
from cfd_bot.tickets import atomic_json, publish_macro, sync_ticket_states, ticket_lock
from tests.test_core import Environment, FakeAPI


class TicketIndexTests(Environment):
    def setUp(self):
        super().setUp()
        self.folder = self.root / 'tickets'
        self.folder.mkdir()
        self.config.update(cases=[], case_globs=[str(self.folder / '*.json')])
        self.config['scheduler']['enabled'] = False

    def ticket(self, name, root=None):
        root = root or self.root / name
        (root / 'system').mkdir(parents=True, exist_ok=True)
        (root / 'system/controlDict').write_text('stopAt endTime; endTime 10;\n')
        path = self.folder / (name + '.json')
        atomic_json(path, dict(version=1, case_dir=str(root), command=['fixture-never-execute'], cores=1, cpu_set=self.cpu))
        return path

    def test_warm_catalog_and_one_external_change(self):
        a, b = self.ticket('a'), self.ticket('b')
        with patch('cfd_bot.catalog.load_case', wraps=load_case) as read:
            first = cases_for(self.config)
            self.assertEqual(read.call_count, 2)
            read.reset_mock()
            self.assertEqual(cases_for(self.config), first)
            self.assertEqual(read.call_count, 0)
            data = read_json(a)
            data['name'] = 'changed externally'
            a.write_text(json.dumps(data))
            result = cases_for(self.config)
            self.assertEqual(read.call_count, 1)
            self.assertEqual(result[0]['name'], 'changed externally')
            self.assertEqual(result[1]['_config'], str(b))

    def test_returned_documents_cannot_mutate_index(self):
        self.ticket('a')
        index = ticket_index(self.config)
        copy = index.cases()[0]
        copy['watcher']['logs'].append('tampered')
        self.assertNotIn('tampered', index.lookup(copy['_root'])['watcher']['logs'])

    def test_rename_delete_and_same_basename_in_different_parents(self):
        a = self.ticket('a', self.root / 'first' / 'same')
        b = self.ticket('b', self.root / 'second' / 'same')
        self.assertEqual(len(cases_for(self.config)), 2)
        renamed = a.with_name('renamed.json')
        a.rename(renamed)
        b.unlink()
        self.assertEqual([c['_config'] for c in cases_for(self.config)], [str(renamed)])

    def test_invalid_edit_fails_closed_and_repair_recovers(self):
        a = self.ticket('a')
        cases_for(self.config)
        before = a.read_bytes()
        a.write_text('{broken')
        with self.assertRaises(ConfigError):
            cases_for(self.config)
        # Repair UI still exposes an invalid file and permits deletion preview.
        self.assertIn(str(a), folder_index(self.folder).errors)
        a.write_bytes(before)
        self.assertEqual(len(cases_for(self.config)), 1)

    def test_overflow_and_explicit_check_revalidate_all(self):
        self.ticket('a')
        self.ticket('b')
        index = ticket_index(self.config)
        if index.watch.fd < 0:
            self.skipTest('inotify unavailable')
        with patch.object(index.watch, 'drain', return_value=(set(), True)), \
                patch('cfd_bot.catalog.load_case', wraps=load_case) as read:
            index.refresh()
            self.assertEqual(read.call_count, 2)
        with patch('cfd_bot.catalog.load_case', wraps=load_case) as read:
            cases_for(self.config, force=True)
            self.assertEqual(read.call_count, 2)

    def test_metadata_fallback_loads_only_changed_json(self):
        a = self.ticket('a')
        self.ticket('b')
        index = ticket_index(self.config)
        index.watch.close()
        with patch('cfd_bot.catalog.load_case', wraps=load_case) as read:
            index.refresh()
            self.assertEqual(read.call_count, 0)
            atomic_json(a, dict(read_json(a), name='fallback'))
            self.assertEqual(index.refresh().lookup(str(self.root / 'a'))['name'], 'fallback')
            self.assertEqual(read.call_count, 1)

    def test_ticket_symlink_and_duplicate_root_are_rejected(self):
        a = self.ticket('a')
        cases_for(self.config)
        link = self.folder / 'alias.json'
        link.symlink_to(a)
        with self.assertRaises(ConfigError):
            cases_for(self.config)

        link.unlink()
        atomic_json(link, read_json(a))
        with self.assertRaises(ConfigError):
            cases_for(self.config)

    def test_replaced_folder_invalidates_watch(self):
        self.ticket('a')
        cases_for(self.config)
        self.folder.rename(self.folder.with_name('old-tickets'))
        self.folder.mkdir()
        b = self.ticket('b')
        self.assertEqual([c['_config'] for c in cases_for(self.config)], [str(b)])

    def test_invalid_macro_does_not_hide_other_button_states(self):
        a = self.ticket('a')
        macro = self.folder / 'macro.json'
        atomic_json(macro, dict(version=1, task_type='macro', case_dir=str(self.root),
                               cases=[dict(ticket='missing.json', case_dir=str(self.root / 'missing'), state='waiting')]))
        service = TicketService(self.folder)
        result = TicketRunner(service, self.config, self.store).states(folder_index(self.folder).tickets())
        self.assertEqual(result[a.name]['state'], 'idle')
        self.assertEqual(result[macro.name]['state'], 'invalid')
        self.assertFalse(result[macro.name]['enabled'])

    def test_membership_resolution_is_linear(self):
        parent = str(self.folder / 'macro.json')
        count = 80
        rows = [dict(ticket=f'child-{i}.json', case_dir=f'/fixture/{i}') for i in range(count)]
        tickets = [dict(_config=parent, task_type='macro', cases=rows)]
        tickets.extend(dict(_config=str(self.folder / r['ticket']), _root=r['case_dir'],
                            task_type='single', role='child', macro_ticket='macro.json') for r in rows)
        original = Path.resolve
        calls = []
        def resolve(path, *args, **kwargs):
            calls.append(path)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'resolve', resolve):
            self.assertEqual(len(cases_for(self.config, tickets)), count)
        self.assertLessEqual(len(calls), count * 3)

    def test_editor_lock_and_catalog_reads_do_not_deadlock(self):
        path = self.ticket('a')
        service = TicketService(self.folder)
        def write():
            for i in range(10):
                with ticket_lock(self.folder):
                    folder_index(self.folder)
                    atomic_json(path, dict(read_json(path), name=f'a-{i}'))
        def read():
            for _ in range(10):
                self.assertEqual(len(folder_index(self.folder).tickets()), 1)
                service.revision(path.name)
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(write), pool.submit(read)
            a.result(timeout=10)
            b.result(timeout=10)

    def test_stat_scans_every_request_but_reuses_validated_catalog(self):
        self.ticket('a')
        bot = Bot(self.config, self.store, FakeAPI())
        with patch('cfd_bot.bot.process_snapshot', return_value={'cases': {}, 'raw': 'fixture'}) as scan, \
                patch('cfd_bot.catalog.load_case', wraps=load_case) as read:
            bot.fresh_runs()
            bot.fresh_runs()
        self.assertEqual(scan.call_count, 2)
        self.assertEqual(read.call_count, 1)
        self.assertIsNone(self.store.get('snapshot'))

    def test_journal_survives_reopen_and_old_worker_sql(self):
        case = load_case(self.ticket('a'))
        job = self.store.enqueue(case)
        sync_ticket_states(self.config, self.store, {'cases': {}})
        self.assertEqual(self.store.ticket_changes(), {})
        # An already-running worker may still execute the old UPDATE statement.
        job.update(status='postprocessing')
        with self.store.connect() as db:
            db.execute('UPDATE jobs SET status=?, body=? WHERE id=?',
                       (job['status'], json.dumps(job), job['id']))
        reopened = Store(self.store.root)
        self.assertIn(case['_root'], reopened.ticket_changes())
        sync_ticket_states(self.config, reopened, {'cases': {}})
        self.assertEqual(read_json(case['_config'])['queue']['state'], 'running')
        self.store.update_job(job['id'], status='succeeded')
        sync_ticket_states(self.config, reopened, {'cases': {}})
        self.assertEqual(read_json(case['_config'])['queue']['result'], 'succeeded')

    def test_journal_ack_does_not_remove_newer_change(self):
        job = self.store.enqueue(load_case(self.ticket('a')))
        old = self.store.ticket_changes()
        self.store.update_job(job['id'], status='starting')
        self.store.acknowledge_ticket_changes(old)
        self.assertIn(job['case_root'], self.store.ticket_changes())

    def test_cancel_is_published_without_process_detection(self):
        case = load_case(self.ticket('a'))
        job = self.store.enqueue(case)
        sync_ticket_states(self.config, self.store, {'cases': {}})
        self.store.cancel_queued([job['id']])
        sync_ticket_states(self.config, self.store, {'cases': {}})
        queue = read_json(case['_config'])['queue']
        self.assertEqual((queue['state'], queue['result'], queue['job_id']),
                         ('finished', 'cancelled', job['id']))

    def test_idle_sync_touches_no_case_after_initial_reconciliation(self):
        self.ticket('a')
        self.ticket('b')
        for _ in range(3):
            sync_ticket_states(self.config, self.store, {'cases': {}})
        with patch('cfd_bot.tickets.read_json', wraps=read_json) as read, \
                patch('cfd_bot.catalog.load_case', wraps=load_case) as validate:
            sync_ticket_states(self.config, self.store, {'cases': {}})
        self.assertEqual(read.call_count, 0)
        self.assertEqual(validate.call_count, 0)

    def test_completed_child_and_macro_are_retried_after_partial_write(self):
        parent = self.root / 'batch'
        parent.mkdir()
        root = parent / 'child'
        root.mkdir()
        (root / 'Allrun').write_text('# fixture: never executed\n')
        path = self.folder / 'macro.json'
        publish_macro(path, dict(version=1, case_dir=str(parent), task_type='macro',
                                command=['fixture-never-execute'], cores=1, cpu_set=self.cpu,
                                cases=[dict(case_dir=str(root), state='waiting')]))
        case = cases_for(self.config)[0]
        job = self.store.enqueue_batch([case], read_json(path)['queue']['request_id'])[0]
        sync_ticket_states(self.config, self.store, {'cases': {}})
        self.store.update_job(job['id'], status='succeeded')
        original = atomic_json
        def fail_parent(target, data):
            if target == path:
                raise OSError('simulated failure after child commit')
            original(target, data)
        with patch('cfd_bot.tickets.atomic_json', side_effect=fail_parent):
            with self.assertRaises(OSError):
                sync_ticket_states(self.config, self.store, {'cases': {}})
        self.assertIn(case['_root'], self.store.ticket_changes())
        sync_ticket_states(self.config, Store(self.store.root), {'cases': {}})
        result = read_json(path)
        self.assertEqual(result['cases'][0]['state'], 'finished')
        self.assertEqual(result['cases'][0]['result'], 'succeeded')
        self.assertEqual(result['queue']['state'], 'finished')

    def record(self, token):
        return dict(processes=[dict(pid=os.getpid(), identity=token, mode='MPI', cpu_list=self.cpu)],
                    supervisors=[])

    def test_disappeared_run_is_finished_after_monitor_restart_without_scanning_idle_cases(self):
        path = self.ticket('a')
        self.ticket('idle')
        case = load_case(path)
        root = case['_root']
        (Path(root) / 'log.solver').write_text('Time = 10\nEnd\n')
        Monitor(self.config, self.store).observe(case, self.record('boot:1:10'))
        monitor = Monitor(self.config, Store(self.store.root))
        with patch('cfd_bot.monitor.snapshot', return_value={'cases': {}, 'raw': 'empty'}), \
                patch.object(monitor, 'observe', wraps=monitor.observe) as observe:
            monitor.tick()
            monitor.tick()
            monitor.tick()
        self.assertEqual(observe.call_count, 2)
        self.assertEqual(self.store.get('observed:' + root)['status'], 'succeeded')
        self.assertEqual(self.store.tracked_observations(), {})
        self.assertEqual(read_json(path)['queue']['result'], 'succeeded')

    def test_same_path_restart_uses_new_identity_and_keeps_terminal_retry(self):
        case = load_case(self.ticket('a'))
        monitor = Monitor(self.config, self.store)
        monitor.observe(case, self.record('boot:1:10'))
        before = self.store.get('observed:' + case['_root'])
        with patch('cfd_bot.monitor.terminal_event', side_effect=OSError('outbox unavailable')):
            with self.assertRaises(OSError):
                monitor.observe(case, self.record('boot:1:20'))
        monitor.observe(case, self.record('boot:1:20'))
        after = self.store.get('observed:' + case['_root'])
        self.assertNotEqual(before['id'], after['id'])
        self.assertEqual(after['status'], 'running')
        self.assertEqual(len(self.store.pending()), 3)  # two starts + old interrupted
        monitor.observe(case, self.record('boot:1:20'))
        self.assertEqual(len(self.store.pending()), 3)

    def test_solver_phase_change_under_same_supervisor_is_one_execution(self):
        case = load_case(self.ticket('a'))
        monitor = Monitor(self.config, self.store)
        a, b = self.record('solver:1'), self.record('solver:2')
        for record in (a, b):
            record['supervisors'] = [dict(pid=os.getpid(), identity='supervisor:1')]
        monitor.observe(case, a)
        old = self.store.get('observed:' + case['_root'])['id']
        monitor.observe(case, b)
        self.assertEqual(self.store.get('observed:' + case['_root'])['id'], old)
        self.assertEqual(len(self.store.pending()), 1)
