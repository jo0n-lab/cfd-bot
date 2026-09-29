import json
import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cfd_bot.artifacts import export_files, freeze_exports
from cfd_bot.bot import Bot, case_id, deliver
from cfd_bot.config import ConfigError, load_bot, load_case, tickets_for
from cfd_bot.jobs import Scheduler, terminal_event, worker
from cfd_bot.logs import (estimate, finish_log, read_log, recent_case_log,
                          recent_log, select_case_log, start_cursor)
from cfd_bot.monitor import Monitor, observation
from cfd_bot.outcomes import decide
from cfd_bot.processes import DaemonLock, identity, parse_snapshot
from cfd_bot.queue_control import cancel_queued_jobs
from cfd_bot.report import render_run
from cfd_bot.storage import Store
from cfd_bot.telegram import Telegram, TelegramError, chunks
from cfd_bot.texts import load_text


class Environment(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.case_root = self.root / 'case'
        self.case_root.mkdir()
        (self.case_root / 'system').mkdir()
        self.control = self.case_root / 'system/controlDict'
        self.control.write_text('startTime 0; stopAt endTime; endTime 10;\n')
        self.cpu = str(min(os.sched_getaffinity(0)))
        self.case_path = self.case_root / 'ofps.json'
        self.case_data = dict(version=1, name='test-case', cores=1, cpu_set=self.cpu,
                              log='log.solver', command=[sys.executable, '-c', "print('Time = 10\\nEnd')"],
                              simulation=dict(start=0, end=10))
        self.write_case()
        self.bot_path = self.root / 'bot.json'
        self.bot_path.write_text(json.dumps(dict(version=1, state_dir='state', cases=[str(self.case_path)],
                                                 telegram=dict(allowed_user_ids=[10], chat_ids=[20]),
                                                 scheduler=dict(enabled=True))))
        self.config = load_bot(self.bot_path)
        self.store = Store(self.config['state_dir'])

    def write_case(self):
        self.case_path.write_text(json.dumps(self.case_data))
        self.case = load_case(self.case_path)

    def claim(self):
        job = self.store.enqueue(self.case)
        return self.store.update_job(job['id'], status='starting', claimed=time.time())


class ConfigTests(Environment):
    def test_ticket_case_dir_is_independent_from_ticket_location(self):
        tickets = self.root / 'tickets'
        tickets.mkdir()
        ticket = tickets / 'case-a.json'
        data = dict(self.case_data, case_dir=str(self.case_root))
        ticket.write_text(json.dumps(data))
        loaded = load_case(ticket)
        self.assertEqual(loaded['_root'], str(self.case_root))
        self.assertEqual(loaded['_config'], str(ticket))

    def test_paths_and_config_validation(self):
        self.case_data['exports'] = [dict(name='secret', pattern='../token')]
        with self.assertRaises(ConfigError):
            self.write_case()
        self.case_data.pop('exports')
        self.case_data['cores'] = 3
        with self.assertRaises(ConfigError):
            self.write_case()
        self.case_data['cores'] = 1
        self.case_data['typo'] = True
        with self.assertRaises(ConfigError):
            self.write_case()

    def test_export_rejects_symlink_escape(self):
        secret = self.root / 'secret'
        secret.write_text('do not transmit')
        (self.case_root / 'leak.csv').symlink_to(secret)
        export = dict(pattern='*.csv', max_files=1)
        with self.assertRaises(ConfigError):
            export_files(self.case, export)

    def test_exports_newest_and_freshness(self):
        a, b = self.case_root / 'a.csv', self.case_root / 'b.csv'
        a.write_text('old')
        b.write_text('new')
        os.utime(a, (1, 1))
        e = dict(pattern='*.csv', max_files=1)
        self.assertEqual(export_files(self.case, e), [b])
        self.assertEqual(export_files(self.case, e, time.time() + 10), [])

    def test_watcher_regex_group_is_validated(self):
        self.case_data['watcher'] = dict(
            log='log.solver',
            progress=dict(pattern=r'^ITER (\d+)$', group='missing', start=0, end=10))
        with self.assertRaises(ConfigError):
            self.write_case()

    def test_watcher_logs_are_validated_and_mutually_exclusive(self):
        self.case_data['watcher'] = dict(logs=[])
        with self.assertRaises(ConfigError):
            self.write_case()
        self.case_data['watcher'] = dict(log='log.a', logs=['log.b'])
        with self.assertRaises(ConfigError):
            self.write_case()

    def test_export_events_are_case_configured(self):
        result = self.case_root / 'result.csv'
        result.write_text('ok')
        self.case_data['exports'] = [dict(name='result', pattern='result.csv',
                                          on=['succeeded'])]
        self.write_case()
        files, _ = freeze_exports(self.case, self.root / 'frozen-failure', 0,
                                  'failed')
        self.assertEqual(files, [])
        files, _ = freeze_exports(self.case, self.root / 'frozen-success', 0,
                                  'succeeded')
        self.assertEqual(len(files), 1)

    def test_ticket_progress_end_does_not_override_control_dict(self):
        self.case_data['watcher'] = dict(
            log='log.solver',
            progress=dict(pattern=r'^Time = (?P<value>\d+)$', group='value',
                          start=0, end=100),
            success=dict(patterns=[r'^End$'], match='all',
                         require_progress_end=False, required_files=[]))
        self.write_case()
        status, _ = decide(self.case, {
            'time': 40, 'success_matches': {r'^End$': 'End'}, 'errors': []
        }, time.time(), external=True)
        self.assertEqual(status, 'succeeded')

    def test_control_dict_end_time_is_the_success_boundary(self):
        status, reason = decide(self.case, {
            'time': 10, 'success_matches': {}, 'errors': []
        }, time.time(), external=True)
        self.assertEqual(status, 'succeeded')
        self.assertIn('endTime 10', reason)

        status, reason = decide(self.case, {
            'time': 9, 'success_matches': {r'^End$': 'End'}, 'errors': []
        }, time.time(), external=True)
        self.assertEqual(status, 'failed')
        self.assertIn('조기 종료', reason)

    def test_non_end_time_stop_mode_cannot_be_normal_completion(self):
        self.control.write_text('startTime 0; stopAt writeNow; endTime 10;\n')
        status, reason = decide(self.case, {'time': 10, 'errors': []},
                                time.time())
        self.assertEqual(status, 'failed')
        self.assertIn('stopAt', reason)


class LogTests(Environment):
    def test_newest_multi_stage_log_is_selected_and_switch_resets_cursor(self):
        first = self.case_root / 'log.first'
        second = self.case_root / 'log.second'
        first.write_text('Time = 3\n')
        os.utime(first, (1, 1))
        second.write_text('Time = 7\n')
        self.case_data.pop('log')
        self.case_data.pop('simulation')
        self.case_data['watcher'] = dict(
            logs=['log.first', 'log.second'],
            progress=dict(pattern=r'^Time = (?P<value>\d+)$', group='value',
                          start=0, end=10))
        self.write_case()
        self.assertEqual(select_case_log(self.case), second)
        state, path = recent_case_log(self.case, final=True)
        self.assertEqual((state['time'], path), (7, second))
        first.write_text('Time = 9\n')
        future = time.time() + 2
        os.utime(first, (future, future))
        state, path = recent_case_log(self.case, state, final=True)
        self.assertEqual((state['time'], path), (9, first))
        self.assertEqual(state['log_path'], str(first))

    def test_case_watcher_controls_progress_success_and_failure_patterns(self):
        watcher = dict(
            progress=dict(pattern=r'^ITER (?P<n>\d+)$', group='n', start=0,
                          end=7),
            success=dict(patterns=[r'^PIPELINE COMPLETE$'], match='all',
                         required_files=[]),
            failure=dict(patterns=[r'^CASE ABORTED:'],
                         include_openfoam_defaults=False, updated_files=[]))
        p = self.case_root / 'custom.log'
        p.write_text('ITER 7\nPIPELINE COMPLETE\n')
        state = finish_log(p, watcher=watcher)
        self.assertEqual(state['time'], 7)
        self.assertTrue(state['ended'])
        self.assertEqual(state['errors'], [])
        p.write_text('ITER 3\nCASE ABORTED: custom reason\n')
        state = finish_log(p, watcher=watcher)
        self.assertIn('CASE ABORTED', state['errors'][-1])

    def test_openfoam_dev_time_units_and_sigfpe_banner(self):
        p = self.case_root / 'log.solver'
        p.write_text('sigFpe : Enabling floating point exception trapping (FOAM_SIGFPE).\n'
                     '             Time = 1.199904e-06s\nEnd\n')
        t = finish_log(p)
        self.assertEqual(t['time'], 1.199904e-6)
        self.assertEqual(t['errors'], [])
        self.assertTrue(t['ended'])

    def test_recent_large_log_reaches_current_time_immediately(self):
        p = self.case_root / 'log.solver'
        p.write_text('Time = 1\n' + ('padding\n' * 40000) + 'Time = 9\nEnd\n')
        t = recent_log(p, final=True, window=1024)
        self.assertEqual(t['first_time'], 1)
        self.assertEqual(t['time'], 9)
        self.assertTrue(t['ended'])
        self.assertGreater(t['skipped_bytes'], 0)

    def test_incremental_partial_and_multiregion(self):
        p = self.case_root / 'log.solver'
        p.write_text('Time = 0.5\nfluid smoothSolver:  Solving for Ux, Initial residual = 1e-2, Final residual = 2e-5, No Iterations 3\nTime = ')
        state = read_log(p)
        self.assertEqual(state['time'], .5)
        self.assertEqual(state['residuals']['fluid/Ux']['final'], 2e-5)
        with p.open('a') as f:
            f.write('1\nExecutionTime = 3 s  ClockTime = 5 s\nEnd')
        state = finish_log(p, state)
        self.assertEqual(state['time'], 1)
        self.assertTrue(state['ended'])
        self.assertEqual(len(state['history']), 1)

    def test_old_end_ignored_and_log_rewrite_detected(self):
        p = self.case_root / 'log.solver'
        p.write_text('Time = 9\nEnd\n')
        cursor = start_cursor(p)
        state = finish_log(p, cursor)
        self.assertFalse(state['ended'])
        p.write_text('Time = 1\nFOAM FATAL ERROR: broken\n')
        state = finish_log(p, state)
        self.assertFalse(state['ended'])
        self.assertTrue(state['errors'])
        self.assertEqual(state['time'], 1)

    def test_rotation_and_bounded_reads(self):
        p = self.case_root / 'log.solver'
        p.write_text('Time = 1\nEnd\n')
        state = read_log(p, limit=5)
        self.assertGreater(state['backlog'], 0)
        state = finish_log(p, state)
        p.rename(self.case_root / 'log.old')
        p.write_text('Time = 2\n')
        state = finish_log(p, state)
        self.assertEqual(state['time'], 2)
        self.assertFalse(state['ended'])

    def test_eta_without_configured_duration_or_control_target_uses_recent_successes(self):
        self.control.unlink()
        telemetry = dict(time=5, first_time=1, clock=40)
        self.assertIsNone(estimate(self.case, telemetry, 30)['remaining_seconds'])
        history = [{'seconds': 100}, {'seconds': 120}, {'seconds': 900}]
        eta = estimate(self.case, telemetry, 50, history)
        self.assertEqual(eta['remaining_seconds'], 70)
        self.assertEqual(eta['basis'], 'recent_successes')
        self.assertIsNone(eta['progress'])
        self.assertIsNone(estimate(self.case, {}, 121, history)['remaining_seconds'])


class StoreTests(Environment):
    def test_enqueue_is_atomic_and_idempotent(self):
        j = self.store.enqueue(self.case, request_key='update1')
        self.assertEqual(self.store.enqueue(self.case, request_key='update1')['id'], j['id'])
        with self.assertRaises(ValueError):
            self.store.enqueue(self.case)
        self.store.update_job(j['id'], status='succeeded')
        self.assertEqual(self.store.enqueue(self.case, request_key='update1')['id'], j['id'])
        self.assertNotEqual(self.store.enqueue(self.case, request_key='update2')['id'], j['id'])

    def test_compare_and_swap_prevents_cancel_after_claim(self):
        j = self.claim()
        self.assertIsNone(self.store.update_job(j['id'], expected=('queued',), status='cancelled'))
        self.assertEqual(self.store.job(j['id'])['status'], 'starting')

    def test_bulk_cancel_updates_all_queued_jobs_and_reports_stale_selection(self):
        first = self.store.enqueue(self.case)
        self.store.update_job(first['id'], status='succeeded')
        second = self.store.enqueue(self.case, request_key='second')
        stale = self.store.update_job(second['id'], status='starting')
        self.store.update_job(stale['id'], status='cancelled')
        third = self.store.enqueue(self.case, request_key='third')

        result = cancel_queued_jobs(
            self.store, [first['id'], third['id'], third['id'], stale['id'], 'missing-job'])

        self.assertEqual(result['cancelled'], [third['id']])
        self.assertEqual(result['unavailable'], [first['id'], stale['id'], 'missing-job'])
        self.assertEqual(self.store.job(third['id'])['status'], 'cancelled')
        with self.assertRaisesRegex(ValueError, '하나 이상 선택'):
            cancel_queued_jobs(self.store, [])

    def test_outbox_per_recipient_dedup_and_retry(self):
        self.store.event('done', [20, 30], dict(kind='text', text='done'))
        self.store.event('done', [20, 30], dict(kind='text', text='done'))
        pending = self.store.pending()
        self.assertEqual(len(pending), 2)
        self.store.delivered(pending[0]['id'])
        self.store.retry(pending[1]['id'])
        self.assertEqual(self.store.pending(), [])
        restored = Store(self.config['state_dir'])
        with restored.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM outbox WHERE sent=1').fetchone()[0], 1)

    def test_single_daemon_lock(self):
        with DaemonLock(self.store.root / 'daemon.lock'):
            with self.assertRaises(RuntimeError):
                with DaemonLock(self.store.root / 'daemon.lock'):
                    pass


class WorkerTests(Environment):
    def test_worker_uses_custom_watcher_from_case_json(self):
        self.case_data.pop('simulation')
        self.case_data['watcher'] = dict(
            log='custom.log',
            progress=dict(pattern=r'^ITER (?P<n>\d+)$', group='n', start=0,
                          end=42),
            success=dict(patterns=[r'^PIPELINE COMPLETE$'], match='all',
                         required_files=[]),
            failure=dict(patterns=[r'^CASE ABORTED:'],
                         include_openfoam_defaults=False, updated_files=[]))
        self.case_data['command'] = [sys.executable, '-c',
                                     "print('ITER 42\\nPIPELINE COMPLETE')"]
        self.control.write_text('startTime 0; stopAt endTime; endTime 42;\n')
        self.write_case()
        j = self.claim()
        self.assertEqual(worker(self.store.root, j['id']), 0)
        self.assertEqual(self.store.job(j['id'])['status'], 'succeeded')

    def test_worker_uses_custom_failure_pattern_from_case_json(self):
        self.case_data['watcher'] = dict(
            log='log.solver',
            success=dict(patterns=[r'^DONE$'], match='all', required_files=[]),
            failure=dict(patterns=[r'^CASE ABORTED:'],
                         include_openfoam_defaults=False, updated_files=[]))
        self.case_data['command'] = [sys.executable, '-c',
                                     "print('CASE ABORTED: chemistry monitor')"]
        self.write_case()
        j = self.claim()
        worker(self.store.root, j['id'])
        result = self.store.job(j['id'])
        self.assertEqual(result['status'], 'failed')
        self.assertIn('CASE ABORTED', result['reason'])

    def test_completion_freezes_generated_exports(self):
        self.case_data['postprocess'] = [dict(command=[sys.executable, '-c', "from pathlib import Path; Path('summary.csv').write_text('value,1')"])]
        self.case_data['exports'] = [dict(name='result', pattern='summary.csv')]
        self.write_case()
        j = self.claim()
        worker(self.store.root, j['id'])
        event = self.store.get('event:' + j['id'])
        self.assertEqual(len(event['files']), 1)
        (self.case_root / 'summary.csv').write_text('value,2')
        self.assertEqual(Path(event['files'][0]['path']).read_text(), 'value,1')

    def test_success_captures_exit_code_even_before_monitor_poll(self):
        j = self.claim()
        self.assertEqual(worker(self.store.root, j['id']), 0)
        result = self.store.job(j['id'])
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['returncode'], 0)
        self.assertTrue(result['telemetry']['ended'])

    def test_immediate_failure_with_old_success_log(self):
        (self.case_root / 'log.solver').write_text('Time = 10\nEnd\n')
        self.case_data['command'] = [sys.executable, '-c', "print('FOAM FATAL ERROR: boom'); raise SystemExit(7)"]
        self.write_case()
        j = self.claim()
        self.assertEqual(worker(self.store.root, j['id']), 1)
        result = self.store.job(j['id'])
        self.assertEqual(result['returncode'], 7)
        self.assertEqual(result['status'], 'failed')

    def test_zero_exit_without_new_end_is_failure(self):
        (self.case_root / 'log.solver').write_text('End\n')
        self.case_data['command'] = [sys.executable, '-c', "print('nothing')"]
        self.write_case()
        j = self.claim()
        worker(self.store.root, j['id'])
        self.assertEqual(self.store.job(j['id'])['status'], 'failed')

    def test_command_that_writes_its_own_log(self):
        self.case_data['command'] = [sys.executable, '-c', "from pathlib import Path; Path('log.solver').write_text('Time = 10\\nEnd\\n')"]
        self.write_case()
        j = self.claim()
        worker(self.store.root, j['id'])
        self.assertEqual(self.store.job(j['id'])['status'], 'succeeded')

    def test_postprocess_failure_preserves_solver_success(self):
        self.case_data['postprocess'] = [dict(command=[sys.executable, '-c', 'raise SystemExit(9)'])]
        self.write_case()
        j = self.claim()
        worker(self.store.root, j['id'])
        result = self.store.job(j['id'])
        self.assertEqual(result['status'], 'succeeded')
        self.assertTrue(result['postprocess_errors'])

    def test_fatal_with_zero_exit_is_failure(self):
        self.case_data['command'] = [sys.executable, '-c', "print('FOAM FATAL ERROR\\nEnd')"]
        self.write_case()
        j = self.claim()
        worker(self.store.root, j['id'])
        self.assertEqual(self.store.job(j['id'])['status'], 'failed')


class SchedulerTests(Environment):
    def test_cpu_reservation_blocks_second_case_before_solver_is_visible(self):
        first = self.claim()
        self.store.update_job(first['id'], status='running', worker_pid=os.getpid(), worker_identity=identity(os.getpid()))
        second_root = self.root / 'second'
        second_root.mkdir()
        second_config = second_root / 'ofps.json'
        second_config.write_text(json.dumps(self.case_data))
        second = self.store.enqueue(load_case(second_config))
        self.config['scheduler']['max_parallel'] = 2
        with patch('cfd_bot.jobs.check_cpus') as check:
            Scheduler(self.config, self.store).tick({})
        check.assert_not_called()
        self.assertEqual(self.store.job(second['id'])['status'], 'queued')
        self.assertIn('CPU', self.store.job(second['id'])['reason'])

    def test_unavailable_cpu_is_blocked_even_if_taskset_would_accept_subset(self):
        unavailable = max(os.sched_getaffinity(0)) + 1
        self.case_data['cpu_set'] += ',' + str(unavailable)
        self.write_case()
        j = self.store.enqueue(self.case)
        with patch('cfd_bot.jobs.check_cpus') as check:
            Scheduler(self.config, self.store).tick({})
        check.assert_not_called()
        self.assertIn('사용할 수 없는 CPU', self.store.job(j['id'])['reason'])

    def test_fifo_blocked_by_cpu_check(self):
        j = self.store.enqueue(self.case)
        with patch('cfd_bot.jobs.check_cpus', return_value=(False, 'BLOCKED: overlap')):
            Scheduler(self.config, self.store).tick({})
        self.assertEqual(self.store.job(j['id'])['status'], 'queued')
        self.assertIn('BLOCKED', self.store.job(j['id'])['reason'])

    def test_real_detached_worker_and_notification_recovery(self):
        job = self.store.enqueue(self.case)
        scheduler = Scheduler(self.config, self.store)
        with patch('cfd_bot.jobs.check_cpus', return_value=(True, 'SAFE')):
            scheduler.tick({})
        for child in scheduler.children:
            self.assertEqual(child.wait(timeout=10), 0)
        self.assertEqual(self.store.job(job['id'])['status'], 'succeeded')
        restarted = Scheduler(self.config, Store(self.config['state_dir']))
        restarted.tick({})
        restarted.tick({})
        self.assertEqual(len(self.store.pending()), 2)
        self.assertEqual({item['event_key'] for item in self.store.pending()},
                         {job['id'] + ':start', job['id'] + ':terminal'})

    def test_worker_pid_reuse_and_boot_id_do_not_mask_loss(self):
        j = self.claim()
        self.store.update_job(j['id'], status='running', worker_pid=os.getpid(), worker_identity='old-boot:wrong-start')
        Scheduler(self.config, self.store).recover()
        self.assertEqual(self.store.job(j['id'])['status'], 'failed')

    def test_lost_worker_but_live_solver_keeps_reservation(self):
        j = self.claim()
        self.store.update_job(j['id'], status='running', solver_pid=os.getpid(), solver_identity=identity(os.getpid()))
        Scheduler(self.config, self.store).recover()
        self.assertEqual(self.store.job(j['id'])['status'], 'running')
        self.assertEqual(len(self.store.pending()), 1)


class MonitorTests(Environment):
    def record(self):
        return dict(processes=[dict(pid=os.getpid(), cpu_list=self.cpu, mode='MPI')], supervisors=[])

    def test_wrapper_only_snapshot_keeps_last_solver_allocation(self):
        previous = dict(owner='SSH host · pts/1', actual_cores=24,
                        actual_cpu_list='0-23')
        record = dict(processes=[], supervisors=[dict(pid=os.getpid())],
                      owner='SSH host · pts/1', actual_cores=0,
                      actual_cpu_list='미지정')
        result = observation(record, previous)
        self.assertEqual(result['actual_cores'], 24)
        self.assertEqual(result['actual_cpu_list'], '0-23')

    def test_unregistered_ofps_case_always_gets_start_and_terminal_notifications(self):
        root = self.root / 'unregistered'
        (root / 'system').mkdir(parents=True)
        (root / 'system/controlDict').write_text(
            'startTime 0; stopAt endTime; endTime 10;\n')
        (root / 'log.solver').write_text('Time = 10\nEnd\n')
        record = self.record()
        monitor = Monitor(self.config, self.store)
        scans = [
            {'raw': 'ENGINE: OpenFOAM', 'cases': {str(root): record}},
            {'raw': 'No active', 'cases': {}},
            {'raw': 'No active', 'cases': {}},
        ]
        with patch('cfd_bot.monitor.snapshot', side_effect=scans) as scan:
            monitor.tick()
            self.assertEqual(self.store.get('auto_observed_roots'), [str(root)])
            monitor.tick()
            monitor.tick()
        self.assertEqual(scan.call_count, 3)
        scan.assert_called_with(self.config['ofps_command'])
        run = self.store.get('observed:' + str(root))
        self.assertEqual(run['status'], 'succeeded')
        self.assertTrue(run['case']['auto_detected'])
        self.assertEqual(self.store.get('auto_observed_roots'), [])
        self.assertEqual(len(self.store.pending()), 2)

    def test_unregistered_ofps_case_uses_default_fatal_error_detection(self):
        root = self.root / 'unregistered-failed'
        (root / 'system').mkdir(parents=True)
        (root / 'system/controlDict').write_text(
            'startTime 0; stopAt endTime; endTime 10;\n')
        (root / 'log.run').write_text(
            'Time = 4\nFOAM FATAL ERROR: external solver crashed\n')
        monitor = Monitor(self.config, self.store)
        scans = [
            {'raw': 'ENGINE: OpenFOAM', 'cases': {str(root): self.record()}},
            {'raw': 'No active', 'cases': {}},
            {'raw': 'No active', 'cases': {}},
        ]
        with patch('cfd_bot.monitor.snapshot', side_effect=scans):
            monitor.tick()
            monitor.tick()
            monitor.tick()
        run = self.store.get('observed:' + str(root))
        self.assertEqual(run['status'], 'failed')
        self.assertIn('FOAM FATAL ERROR', run['reason'])
        self.assertEqual(len(self.store.pending()), 2)

    def test_external_completion_and_notification_dedup(self):
        monitor = Monitor(self.config, self.store)
        (self.case_root / 'log.solver').write_text('Time = 10\nEnd\n')
        monitor.observe(self.case, self.record())
        monitor.observe(self.case, None)
        self.assertEqual(self.store.get('observed:' + self.case['_root'])['status'], 'running')
        monitor.observe(self.case, None)
        monitor.observe(self.case, None)
        self.assertEqual(self.store.get('observed:' + self.case['_root'])['status'], 'succeeded')
        self.assertEqual(len(self.store.pending()), 2)

    def test_intermediate_openfoam_end_is_not_pipeline_success(self):
        monitor = Monitor(self.config, self.store)
        (self.case_root / 'log.solver').write_text('Time = 3\nEnd\n')
        monitor.observe(self.case, self.record())
        monitor.observe(self.case, None)
        monitor.observe(self.case, None)
        result = self.store.get('observed:' + self.case['_root'])
        self.assertEqual(result['status'], 'failed')
        self.assertIn('endTime 10', result['reason'])

    def test_case_files_participate_in_external_verdict(self):
        self.case_data['watcher'] = dict(
            log='log.solver',
            progress=dict(pattern=r'^Time = (?P<value>\d+)$', group='value',
                          start=0, end=10),
            success=dict(patterns=[r'^End$'], match='all',
                         required_files=['.pipeline.complete']),
            failure=dict(patterns=[], include_openfoam_defaults=True,
                         updated_files=['FAILED_CASES.md']))
        self.write_case()
        monitor = Monitor(self.config, self.store)
        (self.case_root / 'log.solver').write_text('Time = 10\nEnd\n')
        monitor.observe(self.case, self.record())
        (self.case_root / 'FAILED_CASES.md').write_text('final monitoring failed\n')
        monitor.observe(self.case, None)
        monitor.observe(self.case, None)
        result = self.store.get('observed:' + self.case['_root'])
        self.assertEqual(result['status'], 'failed')
        self.assertIn('FAILED_CASES.md', result['reason'])

    def test_case_can_suppress_started_notification(self):
        self.case_data['notifications'] = dict(events=['failed'])
        self.write_case()
        Monitor(self.config, self.store).observe(self.case, self.record())
        self.assertEqual(self.store.pending(), [])

    def test_stale_end_does_not_complete_external_run(self):
        logfile = self.case_root / 'log.solver'
        logfile.write_text('End\n')
        os.utime(logfile, (1, 1))
        monitor = Monitor(self.config, self.store)
        monitor.observe(self.case, self.record())
        monitor.observe(self.case, None)
        monitor.observe(self.case, None)
        self.assertEqual(self.store.get('observed:' + self.case['_root'])['status'], 'failed')

    def test_scan_failure_does_not_mark_running_jobs_dead(self):
        monitor = Monitor(self.config, self.store)
        monitor.observe(self.case, self.record())
        with patch('cfd_bot.monitor.snapshot', side_effect=RuntimeError('scan timeout')):
            self.assertFalse(monitor.run_once())
            self.assertFalse(monitor.run_once())
        self.assertEqual(self.store.get('observed:' + self.case['_root'])['missing'], 0)
        self.assertEqual(len(self.store.pending()), 2)  # one start and one outage

    def test_legacy_snapshot_parser(self):
        raw = 'ENGINE: OpenFOAM\nCASE: /tmp/a case\nSUPERVISOR: PID 900001, PPID 1, PROCESS Allrun, ELAPSED 00:12\n  900002 900001 foamRun MPI 1 4 4-7 0 0 00:10\n'
        parsed = parse_snapshot(raw)
        self.assertEqual(parsed['/tmp/a case']['processes'][0]['cpu_list'], '4-7')
        self.assertEqual(parsed['/tmp/a case']['supervisors'][0]['name'], 'Allrun')


class FakeAPI:
    def __init__(self):
        self.messages, self.calls, self.files, self.deleted = [], [], [], []
        self.next_message = 100

    def send(self, chat, text, keyboard=None):
        self.messages.append((chat, text, keyboard))
        self.next_message += 1
        return [{'message_id': self.next_message}]

    def call(self, method, payload):
        self.calls.append((method, payload))

    def file(self, chat, item):
        self.files.append((chat, item))
        self.next_message += 1
        return {'message_id': self.next_message}

    def delete_messages(self, chat, message_ids):
        self.deleted.append((chat, list(message_ids)))
        return list(message_ids)


class BotTests(Environment):
    def test_fresh_stat_reads_ticket_catalog_once_without_state_write(self):
        snap = dict(raw='No active OpenFOAM calculations found.\n', cases={})
        bot = Bot(self.config, self.store, FakeAPI())
        with patch('cfd_bot.bot.process_snapshot', return_value=snap), \
                patch('cfd_bot.bot.tickets_for', wraps=tickets_for) as catalog:
            current, runs, error = bot.fresh_runs()
        self.assertEqual(catalog.call_count, 1)
        self.assertEqual(runs, [])
        self.assertIsNone(error)
        self.assertIn('at', current)
        self.assertIsNone(self.store.get('snapshot'))

    def test_callback_dispatch_does_not_wait_for_acknowledgement(self):
        class BlockingAckAPI(FakeAPI):
            def __init__(self):
                super().__init__()
                self.ack_started = threading.Event()
                self.release_ack = threading.Event()

            def call(self, method, payload):
                super().call(method, payload)
                if method == 'answerCallbackQuery':
                    self.ack_started.set()
                    self.release_ack.wait(2)

        api = BlockingAckAPI()
        bot = Bot(self.config, self.store, api)
        update = dict(update_id=2, callback_query=dict(
            id='slow-ack', data='home', message=dict(chat=dict(id=20)),
            **{'from': dict(id=10)}))
        bot.handle(update)
        self.assertTrue(api.ack_started.wait(1))
        self.assertTrue(api.messages)
        self.assertFalse(api.release_ack.is_set())
        api.release_ack.set()

    def test_stat_is_compact_and_has_only_case_selection_button(self):
        snap = dict(raw='ENGINE: noisy raw output', cases={self.case['_root']: dict(
            owner='SSH 10.0.0.2 · pts/3', actual_cores=4,
            actual_cpu_list='0-3', supervisors=[dict(elapsed='01:30')],
            processes=[])})
        api = FakeAPI()
        with patch('cfd_bot.bot.process_snapshot', return_value=snap):
            Bot(self.config, self.store, api).handle(
                dict(update_id=3, message=dict(message_id=30, text='/stat', chat=dict(id=20),
                     **{'from': dict(id=10)})))
        text, markup = api.messages[-1][1:]
        self.assertNotIn('ENGINE:', text)
        self.assertIn('test-case | SSH 10.0.0.2 · pts/3 | 4/0~3 |', text)
        self.assertEqual(len(markup['inline_keyboard']), 1)
        self.assertTrue(markup['inline_keyboard'][0][0]['callback_data'].startswith('detail:'))

    def test_stat_has_no_plural_or_status_alias(self):
        api = FakeAPI()
        bot = Bot(self.config, self.store, api)
        with patch('cfd_bot.bot.process_snapshot') as scan:
            for update_id, command in enumerate(('/stats', '/status'), 4):
                bot.handle(dict(update_id=update_id, message=dict(
                    message_id=update_id + 27, text=command, chat=dict(id=20),
                    **{'from': dict(id=10)})))
        scan.assert_not_called()
        self.assertEqual([item['command'] for item in bot.ui.value('menus.home.commands')].count('stat'), 1)
        self.assertNotIn('stats', [item['command'] for item in bot.ui.value('menus.home.commands')])
        self.assertNotIn('status', [item['command'] for item in bot.ui.value('menus.home.commands')])

    def test_stat_excludes_terminal_case(self):
        run = dict(id='done1', case=self.case, case_root=self.case['_root'],
                   created=time.time(), started=time.time() - 90,
                   finished=time.time(), status='succeeded', telemetry={},
                   external=True, owner='background', actual_cores=1,
                   actual_cpu_list=self.cpu)
        self.store.put('observed:' + self.case['_root'], run)
        bot = Bot(self.config, self.store, FakeAPI())
        self.assertEqual(bot.status().splitlines()[-1], '실행 중인 계산이 없습니다.')
        self.assertIsNone(bot.status_keyboard())

    def test_stat_includes_unregistered_ofps_case_without_button(self):
        root = str(self.root / 'unregistered-case')
        self.store.put('snapshot', dict(at=time.time(), cases={root: dict(
            owner='SSH 10.0.0.8 · 백그라운드', actual_cores=24,
            actual_cpu_list='0-23', supervisors=[dict(elapsed='01:30')],
            processes=[])}))
        bot = Bot(self.config, self.store, FakeAPI())
        self.assertIn('unregistered-case · 미등록 | SSH 10.0.0.8 · 백그라운드 | 24/0~23 |',
                      bot.status())
        self.assertIsNone(bot.status_keyboard())

    def test_clean_deletes_all_tracked_conversation_messages(self):
        api = FakeAPI()
        bot = Bot(self.config, self.store, api)
        bot.handle(dict(update_id=1, message=dict(message_id=11, text='/start', chat=dict(id=20),
                        **{'from': dict(id=10)})))
        bot.handle(dict(update_id=2, message=dict(message_id=12, text='/clean', chat=dict(id=20),
                        **{'from': dict(id=10)})))
        self.assertEqual(set(api.deleted[-1][1]), {11, 12, 101})
        self.assertEqual(self.store.chat_messages(20), [])

    def test_clean_batches_only_messages_within_telegram_delete_window(self):
        api = FakeAPI()
        bot = Bot(self.config, self.store, api)
        self.store.remember_message(20, 5, time.time() - 49 * 3600)
        self.store.remember_message(20, 6, time.time() - 60)
        bot.handle(dict(update_id=2, message=dict(
            message_id=7, date=int(time.time()), text='/clean', chat=dict(id=20),
            **{'from': dict(id=10)})))
        self.assertEqual(api.deleted[-1], (20, [6, 7]))
        self.assertEqual(self.store.chat_messages(20), [])

    def test_global_text_template_controls_detail_and_completion(self):
        path = self.root / 'text.json'
        path.write_text(json.dumps(dict(version=1, detail='DETAIL {case_name} {owner}',
                                        completion='DONE {status} {elapsed}')))
        run = dict(id='x', case=self.case, started=time.time() - 1, finished=time.time(),
                   status='succeeded', telemetry={}, owner='background')
        templates = load_text(path)
        self.assertEqual(render_run(run, templates, 'detail'), 'DETAIL test-case background')
        self.assertTrue(render_run(run, templates, 'completion').startswith('DONE 정상 종료'))
        path.write_text(json.dumps(dict(version=1, detail='{unknown}', completion='ok')))
        with self.assertRaises(ValueError):
            load_text(path)

    def test_failed_terminal_summary_delivered_without_network(self):
        self.case_data['command'] = [sys.executable, '-c', "print('FOAM FATAL ERROR: test'); raise SystemExit(2)"]
        self.write_case()
        j = self.claim()
        worker(self.store.root, j['id'])
        Scheduler(self.config, self.store).tick({})
        api = FakeAPI()
        deliver(self.store, api)
        self.assertEqual(len(api.messages), 1)
        self.assertIn('실패', api.messages[0][1])
        self.assertIn('종료 코드: 2', api.messages[0][1])
        self.assertEqual(self.store.pending(), [])

    def test_completion_sends_one_residual_attachment(self):
        self.case_data['residual_pattern'] = 'residual*.png'
        self.write_case()
        for name in ('residual-old.png', 'residual-new.png'):
            (self.case_root / name).write_bytes(name.encode())
        os.utime(self.case_root / 'residual-old.png', (1, 1))
        run = dict(id='residual-only', case=self.case, case_root=self.case['_root'],
                   created=time.time(), started=time.time() - 1,
                   finished=time.time(), status='succeeded', telemetry={})
        terminal_event(self.store, run, [20])
        # A subsequent run must not alter the queued completion image.
        (self.case_root / 'residual-new.png').write_bytes(b'next run')
        api = FakeAPI()
        deliver(self.store, api)
        self.assertEqual(len(api.files), 1)
        self.assertEqual(api.files[0][1]['kind'], 'photo')
        self.assertEqual(Path(api.files[0][1]['path']).read_bytes(), b'residual-new.png')

    def test_authorization_requires_both_user_and_chat(self):
        api = FakeAPI()
        bot = Bot(self.config, self.store, api)
        for user, chat in [(999, 20), (10, 999)]:
            bot.handle(dict(update_id=1, message=dict(text='/stat', chat=dict(id=chat), **{'from': dict(id=user)})))
        self.assertFalse(api.messages)
        bot.handle(dict(update_id=2, message=dict(text='/start', chat=dict(id=20), **{'from': dict(id=10)})))
        self.assertTrue(api.messages)

    def test_queue_callback_replay_does_not_enqueue_again(self):
        api = FakeAPI()
        bot = Bot(self.config, self.store, api)
        update = dict(update_id=42, callback_query=dict(id='q', data='enqueue:' + case_id(self.case),
                      message=dict(chat=dict(id=20)), **{'from': dict(id=10)}))
        with patch('cfd_bot.ticket_run.snapshot', return_value={'cases': {}}):
            bot.handle(update)
            j = self.store.jobs()[0]
            self.store.update_job(j['id'], status='succeeded')
            bot.handle(update)
        self.assertEqual(len(self.store.jobs()), 1)

    def test_queue_multi_select_all_clear_and_cancel_selected(self):
        api = FakeAPI()
        bot = Bot(self.config, self.store, api)
        jobs = []
        for index in range(3):
            root = self.root / f'queued-{index}'
            root.mkdir()
            case = dict(self.case, _root=str(root), name=f'queued {index}')
            jobs.append(self.store.enqueue(case))

        bot.dispatch(20, 'qselect', 'queue-select')
        bot.dispatch(20, 'qall', 'queue-all')
        self.assertEqual(self.store.get(bot.queue_selection_key(20)), [job['id'] for job in jobs])
        bot.dispatch(20, 'qnone', 'queue-none')
        self.assertEqual(self.store.get(bot.queue_selection_key(20)), [])
        bot.dispatch(20, 'qall', 'queue-all-again')
        bot.dispatch(20, 'qcancel', 'queue-review')
        self.store.update_job(jobs[-1]['id'], status='starting')
        bot.dispatch(20, 'qcancelyes', 'queue-confirm')

        self.assertEqual([self.store.job(job['id'])['status'] for job in jobs],
                         ['cancelled', 'cancelled', 'starting'])
        self.assertIn('선택 취소 완료: 2개 · 이미 시작/변경 1개', api.messages[-1][1])

    def test_queue_result_buttons_exist_only_for_current_ticket_cases(self):
        api = FakeAPI()
        bot = Bot(self.config, self.store, api)
        tracked = self.store.enqueue(self.case)
        self.store.update_job(tracked['id'], status='running', started=time.time())
        other_root = self.root / 'untracked'
        other_root.mkdir()
        untracked = self.store.enqueue(dict(self.case, _root=str(other_root), name='untracked'))
        self.store.update_job(untracked['id'], status='running', started=time.time())

        bot.dispatch(20, 'queue', 'queue-running')
        buttons = [item for row in api.messages[-1][2]['inline_keyboard'] for item in row]
        result_buttons = [item for item in buttons if item['text'].startswith('결과 요청 데이터')]
        self.assertEqual([item['callback_data'] for item in result_buttons],
                         ['case:' + case_id(self.case)])

        self.store.update_job(tracked['id'], status='succeeded', finished=time.time())
        bot.dispatch(20, 'queue', 'queue-history')
        buttons = [item for row in api.messages[-1][2]['inline_keyboard'] for item in row]
        recent = [item for item in buttons if item['text'].startswith('최근 결과')]
        self.assertEqual([item['callback_data'] for item in recent],
                         ['case:' + case_id(self.case)])

    def test_case_menu_and_old_enqueue_button_cannot_run_an_active_case(self):
        api = FakeAPI()
        bot = Bot(self.config, self.store, api)
        self.store.put('snapshot', {'cases': {self.case['_root']: {}}})
        bot.case_menu(20, case_id(self.case))
        buttons = [button for row in api.messages[-1][2]['inline_keyboard'] for button in row]
        self.assertTrue(any('실행 불가' in button['text'] for button in buttons))
        self.assertFalse(any(button['callback_data'].startswith('prepare:') for button in buttons))
        with patch('cfd_bot.ticket_run.snapshot', return_value={'cases': {self.case['_root']: {}}}):
            bot.handle(dict(update_id=55, callback_query=dict(id='run', data='enqueue:' + case_id(self.case),
                            message={'chat': {'id': 20}}, **{'from': {'id': 10}})))
        self.assertEqual(self.store.jobs(), [])
        self.assertIn('계산 중', api.messages[-1][1])

    def test_delivery_retries_only_failed_recipient(self):
        api = FakeAPI()
        self.store.event('x', [20, 30], dict(kind='text', text='hello'))
        original = api.send
        def fail_second(chat, text, keyboard=None):
            if chat == 30:
                raise TelegramError('rate limit', 1)
            original(chat, text, keyboard)
        api.send = fail_second
        deliver(self.store, api)
        api.send = original
        with self.store.connect() as db:
            db.execute('UPDATE outbox SET next_attempt=0')
        deliver(self.store, api)
        self.assertEqual([m[0] for m in api.messages], [20, 30])

    def test_text_chunks_respect_utf16_budget(self):
        text = '한글🙂' * 2000
        result = list(chunks(text))
        self.assertEqual(''.join(result), text)
        self.assertTrue(all(len(s.encode('utf-16-le')) // 2 <= 3500 for s in result))


class TransportTests(Environment):
    def test_delete_messages_uses_one_bulk_call_for_up_to_100_ids(self):
        api = Telegram('test-token')
        with patch.object(api, 'call', return_value=True) as call:
            deleted = api.delete_messages(20, [9, 3, 7, 3])
        self.assertEqual(deleted, [3, 7, 9])
        call.assert_called_once_with(
            'deleteMessages', {'chat_id': 20, 'message_ids': [3, 7, 9]})

    def test_bad_id_is_isolated_without_delete_message_api_fallback(self):
        api = Telegram('test-token')

        def delete(_method, payload):
            if 2 in payload['message_ids']:
                raise TelegramError('message cannot be deleted', code=400)
            return True

        with patch.object(api, 'call', side_effect=delete) as call:
            deleted = api.delete_messages(20, [1, 2, 3, 4])
        self.assertEqual(deleted, [1, 3, 4])
        self.assertTrue(all(item.args[0] == 'deleteMessages'
                            for item in call.call_args_list))

    def test_photo_rejected_by_telegram_falls_back_to_document(self):
        p = self.case_root / 'contour.png'
        p.write_bytes(b'placeholder')
        api = Telegram('test-token')
        with patch.object(api, 'call', side_effect=[TelegramError('invalid dimensions', code=400), {'message_id': 1}]) as call:
            api.file(20, dict(path=str(p), kind='photo'))
        self.assertEqual([c.args[0] for c in call.call_args_list], ['sendPhoto', 'sendDocument'])

    def test_multipart_upload_has_file_and_chat_fields(self):
        p = self.case_root / 'result.csv'
        p.write_text('a,b\n1,2\n')
        response = io.BytesIO(b'{"ok":true,"result":{"message_id":7}}')
        with patch('urllib.request.urlopen', return_value=response) as request:
            result = Telegram('test-token').file(20, dict(path=str(p), kind='document'))
        self.assertEqual(result['message_id'], 7)
        body = request.call_args.args[0].data
        self.assertIn(b'name="chat_id"\r\n\r\n20', body)
        self.assertIn(b'name="document"; filename="result.csv"', body)
        self.assertIn(b'a,b\n1,2\n', body)

    def test_transport_error_does_not_leak_token(self):
        with patch('urllib.request.urlopen', side_effect=OSError('url contains secret-token')):
            with self.assertRaises(TelegramError) as raised:
                Telegram('secret-token').call('getMe', {})
        self.assertNotIn('secret-token', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
