import os
import time

from cfd_bot.config import ConfigError
from cfd_bot.control import control_times
from cfd_bot.logs import estimate, finish_log, recent_case_log, recent_log
from cfd_bot.report import render_run
from tests.test_core import Environment


class RecentLogEtaTests(Environment):
    def setUp(self):
        super().setUp()
        (self.case_root / 'system').mkdir(exist_ok=True)
        self.control = self.case_root / 'system/controlDict'
        self.control.write_text('startTime 0; stopAt endTime; endTime 100;\n')
        self.log = self.case_root / 'log.solver'

    def telemetry(self, samples):
        self.log.write_text(''.join(f'Time = {step}\nExecutionTime = {clock} s  ClockTime = {clock} s\n'
                                    for step, clock in samples))
        return finish_log(self.log)

    def test_configured_duration_takes_priority_over_live_rate_and_history(self):
        self.case_data['expected_seconds'] = 600.5
        self.write_case()
        telemetry = self.telemetry([(10, 100), (20, 120), (30, 140)])
        result = estimate(self.case, telemetry, 30, [{'seconds': 9999}])
        self.assertEqual(result['basis'], 'configured_seconds')
        self.assertEqual(result['remaining_seconds'], 570.5)
        self.assertEqual(result['expected_seconds'], 600.5)
        self.assertEqual(estimate(self.case, {}, 0)['remaining_seconds'], 600.5)
        run = dict(id='test', case=self.case, status='running', started=100, finished=130,
                   telemetry=telemetry)
        self.assertIn('9분 30초 (expected_seconds 설정값 기준)', render_run(run))

    def test_blank_duration_uses_live_prediction(self):
        telemetry = self.telemetry([(10, 100), (20, 120), (30, 140)])
        for value in (None, '', '  \t '):
            with self.subTest(value=value):
                self.case_data['expected_seconds'] = value
                self.write_case()
                self.assertNotIn('expected_seconds', self.case)
                result = estimate(self.case, telemetry, 9999, [{'seconds': 20000}])
                self.assertEqual(result['basis'], 'recent_log_rate')
                self.assertEqual(result['remaining_seconds'], 140)
                # Older stored case snapshots may still contain the blank value.
                self.assertEqual(estimate(dict(self.case, expected_seconds=value), telemetry, 0)
                                 ['remaining_seconds'], 140)

    def test_invalid_nonempty_duration_is_not_silently_treated_as_automatic(self):
        for value in (0, -1, True, '600', 'invalid', [], float('nan'), float('inf')):
            with self.subTest(value=value):
                self.case_data['expected_seconds'] = value
                with self.assertRaisesRegex(ConfigError, 'expected_seconds'):
                    self.write_case()

    def test_configured_duration_overrun_does_not_switch_prediction_sources(self):
        self.case_data['expected_seconds'] = 100
        self.write_case()
        telemetry = self.telemetry([(10, 100), (20, 120), (30, 140)])
        for elapsed in (100, 200):
            with self.subTest(elapsed=elapsed):
                result = estimate(self.case, telemetry, elapsed, [{'seconds': 9999}])
                self.assertEqual(result['basis'], 'configured_overrun')
                self.assertIsNone(result['remaining_seconds'])
        run = dict(id='test', case=self.case, status='running', started=100, finished=300,
                   telemetry=telemetry)
        self.assertIn('expected_seconds 설정 시간을 초과함', render_run(run))

    def test_eta_uses_control_target_and_clock_deltas_instead_of_whole_run_average(self):
        telemetry = self.telemetry([(10, 100), (20, 120), (30, 140)])
        result = estimate(self.case, telemetry, 9999, [{'seconds': 20000}])
        self.assertEqual(result['basis'], 'recent_log_rate')
        self.assertEqual(result['target'], 100)
        self.assertEqual(result['seconds_per_unit'], 2)
        self.assertEqual(result['remaining_seconds'], 140)
        self.assertEqual(result['intervals'], 2)
        self.control.write_text('endTime 200;\n')
        self.assertEqual(estimate(self.case, telemetry, 0)['remaining_seconds'], 340)

    def test_only_recent_twenty_intervals_determine_rate(self):
        telemetry = self.telemetry([(step, step * 100 if step <= 35 else 3500 + (step - 35) * 2)
                                    for step in range(1, 61)])
        result = estimate(self.case, telemetry, 0)
        self.assertEqual(result['remaining_seconds'], 80)
        self.assertEqual(result['measured_from'], 40)
        self.assertEqual(result['measured_to'], 60)
        self.assertEqual(len(telemetry['rate_samples']), 21)

    def test_restart_and_clock_reset_do_not_mix_segments(self):
        for pairs, remaining, first in [
                ([(70, 700), (80, 720), (10, 0), (20, 20), (30, 40)], 140, 10),
                ([(10, 100), (20, 120), (30, 1), (40, 11)], 60, 30)]:
            with self.subTest(pairs=pairs):
                telemetry = self.telemetry(pairs)
                self.assertEqual(estimate(self.case, telemetry, 0)['remaining_seconds'], remaining)
                self.assertEqual(telemetry['rate_samples'][0][0], first)

    def test_new_stage_does_not_reapply_previous_end_match(self):
        self.log.write_text(
            'Time = 10\nExecutionTime = 2 s ClockTime = 2 s\nEnd\n'
            'Time = 20\nExecutionTime = 4 s ClockTime = 4 s\n'
            'Time = 30\nExecutionTime = 6 s ClockTime = 6 s\n')
        telemetry = finish_log(self.log, watcher=self.case['watcher'])
        self.assertFalse(telemetry['ended'])
        self.assertEqual(telemetry['rate_samples'], [[20, 4], [30, 6]])
        self.assertEqual(estimate(self.case, telemetry, 0)['remaining_seconds'], 14)

    def test_rounded_and_duplicate_clock_values_need_positive_elapsed_time(self):
        telemetry = self.telemetry([(1, 0), (2, 0), (3, 0)])
        self.assertIsNone(estimate(self.case, telemetry, 0)['remaining_seconds'])
        with self.log.open('a') as output:
            output.write('Time = 4\nExecutionTime = 1 s ClockTime = 1 s\n'
                         'ExecutionTime = 1 s ClockTime = 1 s\n')
        telemetry = finish_log(self.log, telemetry)
        self.assertEqual(len(telemetry['rate_samples']), 2)
        self.assertEqual(estimate(self.case, telemetry, 0)['remaining_seconds'], 32)

    def test_large_log_skip_does_not_pair_header_time_with_tail_clock(self):
        self.log.write_text('Time = 1\nExecutionTime = 0 s ClockTime = 0 s\n' + 'padding\n' * 1000 +
                            'ExecutionTime = 9999 s ClockTime = 9999 s\n'
                            'Time = 90\nExecutionTime = 10000 s ClockTime = 10000 s\n'
                            'Time = 91\nExecutionTime = 10002 s ClockTime = 10002 s\n')
        telemetry = recent_log(self.log, window=256)
        self.assertGreater(telemetry['skipped_bytes'], 0)
        self.assertEqual(telemetry['rate_samples'], [[90, 10000], [91, 10002]])
        self.assertEqual(estimate(self.case, telemetry, 0)['remaining_seconds'], 18)

    def test_log_rewrite_and_stage_switch_reset_samples(self):
        telemetry = self.telemetry([(80, 500), (90, 600)])
        self.log.write_text('Time = 10\nExecutionTime = 1 s ClockTime = 1 s\n')
        telemetry = finish_log(self.log, telemetry)
        self.assertEqual(telemetry['rate_samples'], [[10, 1]])
        self.case['watcher']['logs'] = ['log.solver', 'log.second']
        telemetry, _ = recent_case_log(self.case)
        second = self.case_root / 'log.second'
        second.write_text('Time = 50\nExecutionTime = 2 s ClockTime = 2 s\n')
        future = time.time() + 1
        os.utime(second, (future, future))
        telemetry, path = recent_case_log(self.case, telemetry)
        self.assertEqual(path, second)
        self.assertEqual(telemetry['rate_samples'], [[50, 2]])
        self.assertIsNone(estimate(self.case, telemetry, 0)['remaining_seconds'])

    def test_previous_parser_state_is_reloaded_to_acquire_recent_samples(self):
        self.telemetry([(10, 2), (20, 4)])
        old = finish_log(self.log)
        old['log_path'] = str(self.log)
        old.pop('rate_samples')
        telemetry, _ = recent_case_log(self.case, old)
        self.assertEqual(telemetry['rate_samples'], [[10, 2], [20, 4]])

    def test_fractional_simulation_time_uses_same_rate_formula(self):
        self.control.write_text('startTime 0; endTime 1e-3;\n')
        telemetry = self.telemetry([(0.0001, 10), (0.0002, 12), (0.0003, 14)])
        self.assertAlmostEqual(estimate(self.case, telemetry, 0)['remaining_seconds'], 14)

    def test_target_reached_or_stop_requested_does_not_fall_back_to_old_duration(self):
        telemetry = self.telemetry([(99, 10), (100, 12)])
        eta = estimate(self.case, telemetry, 0, [{'seconds': 9999}])
        self.assertIsNone(eta['remaining_seconds'])
        self.assertEqual(eta['basis'], 'target_reached')
        self.control.write_text('stopAt writeNow; endTime 200;\n')
        eta = estimate(self.case, telemetry, 0, [{'seconds': 9999}])
        self.assertEqual(eta['basis'], 'stop_requested')
        self.assertIsNone(eta['remaining_seconds'])

    def test_detail_shows_live_log_basis(self):
        run = dict(id='test', case=self.case, status='running', started=time.time() - 500,
                   telemetry=self.telemetry([(10, 100), (20, 120), (30, 140)]))
        text = render_run(run)
        self.assertIn('2분 20초 (최근 로그 2구간 · endTime 100 기준)', text)


    def test_control_reader_handles_comments_nested_limits_and_case_local_includes(self):
        (self.case_root / 'system/limits').write_text('finalIter 1e2; endTime $finalIter;\n')
        self.control.write_text('''
            FoamFile { version 2.0; object controlDict; }
            /* endTime 9000; */ startTime 5;
            #include "limits"
            #includeIfPresent "missing-optional"
            stopAt endTime; // endTime 4000;
            functions { sample { endTime 99999; } }
        ''')
        self.assertEqual(control_times(self.case), {'start': 5, 'end': 100, 'stop_at': 'endTime'})
        self.control.write_text('#include "limits"\nendTime 200;\n')
        self.assertEqual(control_times(self.case)['end'], 200)

    def test_control_reader_does_not_execute_expressions_or_follow_unsafe_includes(self):
        marker = self.case_root / 'must-not-be-created'
        for text in [f'endTime #calc "system(\\"touch {marker}\\")";',
                     'endTime nan;', 'startTime 100; endTime 10;',
                     '#include "controlDict"', '#include "../../outside"',
                     'endTime $missing;', 'a $b; b $a; endTime $a;']:
            with self.subTest(text=text):
                self.control.write_text(text)
                self.assertIsNone(control_times(self.case))
        self.assertFalse(marker.exists())

    def test_history_is_only_a_fallback_when_live_rate_cannot_be_computed(self):
        self.control.unlink()
        telemetry = self.telemetry([(10, 100), (20, 120)])
        self.assertIsNone(estimate(self.case, telemetry, 30)['remaining_seconds'])
        result = estimate(self.case, telemetry, 30, [{'seconds': 100}])
        self.assertEqual(result['basis'], 'recent_successes')
        self.assertEqual(result['remaining_seconds'], 70)

    def test_missing_or_not_yet_caught_up_log_does_not_supply_a_live_rate(self):
        telemetry = self.telemetry([(10, 2), (20, 4)])
        for status in ({'missing': True}, {'backlog': 100000}):
            with self.subTest(status=status):
                result = estimate(self.case, dict(telemetry, **status), 10)
                self.assertIsNone(result['remaining_seconds'])
                self.assertNotEqual(result['basis'], 'recent_log_rate')

    def test_small_concurrent_write_backlog_keeps_complete_live_samples(self):
        telemetry = self.telemetry([(10, 2), (20, 4), (30, 6)])
        result = estimate(self.case, dict(telemetry, backlog=315), 10)
        self.assertEqual(result['basis'], 'recent_log_rate')
        self.assertEqual(result['remaining_seconds'], 14)
        self.assertAlmostEqual(result['progress'], 20 / 90)
