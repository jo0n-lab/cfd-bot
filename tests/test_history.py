from copy import deepcopy
import time
from unittest.mock import patch

from cfd_bot.artifacts import freeze_exports
from cfd_bot.bot import Bot, case_id
from cfd_bot.gui import form_document, form_values
from cfd_bot.jobs import terminal_event
from cfd_bot.logs import estimate
from cfd_bot.monitor import Monitor
from cfd_bot.processes import parse_snapshot
from cfd_bot.report import compact_unobserved, render_run
from cfd_bot.storage import Store
from tests.test_core import Environment, FakeAPI


class HistoryTests(Environment):
    def run_record(self, jid, seconds=100, *, status='succeeded', cores=4):
        now = time.time()
        return dict(id=jid, case=self.case, case_root=self.case['_root'], status=status,
                    started=now - seconds, created=now - seconds, finished=now,
                    telemetry={}, actual_cores=cores, actual_cpu_list='0-3')

    def test_success_history_survives_restart_and_deduplicates(self):
        run = self.run_record('one')
        self.store.remember_run(run)
        self.store.remember_run(run)
        self.store.remember_run(self.run_record('failed', status='failed'))
        self.store.remember_run(self.run_record('interrupted', status='interrupted'))
        history = Store(self.store.root).runtime_history(self.case, 4)
        self.assertEqual(len(history), 1)
        self.assertAlmostEqual(history[0]['seconds'], 100)
        self.assertEqual(estimate(self.case, {}, 25, history)['remaining_seconds'], 75)

    def test_history_is_case_pipeline_and_allocation_specific(self):
        self.store.remember_run(self.run_record('one'))
        other = deepcopy(self.case)
        other['_root'] = str(self.root / 'other')
        self.assertEqual(self.store.runtime_history(other), [])
        other = deepcopy(self.case)
        other['watcher']['logs'] = ['log.anotherSolver']
        self.assertEqual(self.store.runtime_history(other), [])
        self.assertEqual(self.store.runtime_history(self.case, 24), [])

    def test_recent_history_is_bounded_and_ignores_postprocess_time(self):
        for index in range(25):
            run = self.run_record(str(index), 100)
            run['solver_finished'] = run['finished'] - 20
            self.store.remember_run(run)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM run_history').fetchone()[0], 20)
        history = self.store.runtime_history(self.case)
        self.assertEqual(len(history), 5)
        self.assertEqual(history[0]['seconds'], 80)

    def test_disabled_notifications_do_not_disable_learning(self):
        run = self.run_record('silent')
        run['case'] = deepcopy(self.case)
        run['case']['notifications']['events'] = []
        terminal_event(self.store, run, [20])
        self.assertEqual(len(self.store.runtime_history(self.case)), 1)
        self.assertEqual(self.store.pending(), [])

    def test_previous_external_run_is_saved_before_next_run_replaces_it(self):
        previous = self.run_record('previous')
        self.store.put('observed:' + self.case['_root'], previous)
        record = dict(supervisors=[], processes=[], actual_cores=4, actual_cpu_list='0-3')
        Monitor(self.config, self.store).observe(self.case, record)
        current = self.store.get('observed:' + self.case['_root'])
        self.assertEqual(current['status'], 'running')
        self.assertEqual(current['case_root'], self.case['_root'])
        self.assertEqual(self.store.runtime_history(self.case, 4)[0]['id'], 'previous')
        api = FakeAPI()
        bot = Bot(self.config, self.store, api)
        bot.dispatch(20, 'detail:' + case_id(self.case), 'detail')
        self.assertIn('최근 정상 종료 1회 기준', api.messages[-1][1])
        self.store.put('snapshot', dict(at=time.time(), cases={self.case['_root']: dict(
            owner='test', actual_cores=4, actual_cpu_list='0-3',
            supervisors=[dict(elapsed='00:10')], processes=[])}))
        self.assertIn('4/0~3', bot.status())

    def test_old_saved_successes_bootstrap_history(self):
        job = self.store.enqueue(self.case)
        self.store.update_job(job['id'], status='succeeded', started=10, finished=110)
        self.assertEqual(self.store.runtime_history(self.case)[0]['seconds'], 100)

    def test_report_never_uses_ticket_core_count_as_an_observation(self):
        run = self.run_record('unobserved', status='running')
        run.pop('actual_cores')
        run.pop('actual_cpu_list')
        self.assertIn('코어: 관측 대기/관측 대기', render_run(run))
        self.assertIn('코어·CPU 관측 대기', compact_unobserved(self.case))

    def test_ofps_case_path_is_resolved_before_matching(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.case_root, target_is_directory=True)
        raw = ('ENGINE: OpenFOAM\nCASE: ' + str(alias) + '\n'
               '999999 1 foamRun MPI 1 4 8-11 0 0 00:10\n')
        with patch('cfd_bot.processes.owner_label', return_value='test'), patch('cfd_bot.processes.identity'):
            records = parse_snapshot(raw)
        self.assertEqual(records[self.case['_root']]['actual_cores'], 4)
        self.assertEqual(records[self.case['_root']]['actual_cpu_list'], '8-11')


class RequestDataTests(Environment):
    def test_data_is_sent_on_request_but_not_attached_at_completion(self):
        self.case_data.update(case_dir=str(self.case_root), exports=[
            dict(name='metrics', pattern='metrics.csv', kind='document', max_files=1)])
        self.case_data = form_document(form_values(self.case_data, self.case_root))
        self.write_case()
        result = self.case_root / 'metrics.csv'
        result.write_text('time,value\n1,2\n')
        files, _ = freeze_exports(self.case, self.root / 'frozen', 0, 'succeeded')
        self.assertEqual(files, [])
        api = FakeAPI()
        bot = Bot(self.config, self.store, api)
        bot.dispatch(20, 'case:' + case_id(self.case), 'menu')
        buttons = api.messages[-1][2]['inline_keyboard']
        self.assertTrue(any(button['text'] == 'metrics' for row in buttons for button in row))
        self.assertEqual(api.files, [])
        bot.dispatch(20, 'export:' + case_id(self.case) + ':metrics', 'request')
        self.assertEqual(api.files[0][1]['path'], str(result))
