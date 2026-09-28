import json
import os
import sys
import time
from pathlib import Path

from cfd_bot.jobs import Scheduler, worker
from cfd_bot.processes import identity
from tests.test_core import Environment


class ScriptExecutionTests(Environment):
    def test_wrapper_failure_records_underlying_error_alongside_exit_code(self):
        error = 'ERROR: refusing to replace 0/ fields after calculation: processors24/100'
        phase = 'FAILED: phase=rpm_scaled_initial_flow exit=1'
        self.case_data['command'] = [sys.executable, '-c',
                                    f'print({error!r}); print({phase!r}); raise SystemExit(1)']
        self.write_case()
        job = self.claim()
        self.assertEqual(worker(self.store.root, job['id']), 1)
        result = self.store.job(job['id'])
        self.assertEqual(result['returncode'], 1)
        self.assertIn(error, result['reason'])
        self.assertIn(phase, result['reason'])
        event = self.store.get('event:' + job['id'])
        self.assertEqual(event['run']['reason'], result['reason'])

    def script(self, name, body):
        path = self.case_root / name
        path.write_text(f'#!{sys.executable}\n' + body)
        path.chmod(0o755)
        return path

    def test_pre_solver_post_order_environment_cpu_and_frozen_output(self):
        self.script('Allclean',
                    'import json, os, sys\nfrom pathlib import Path\n'
                    'Path("order").write_text("pre\\n")\n'
                    'Path("log.solver").unlink()\n'
                    'Path("pre-env.json").write_text(json.dumps(dict(\n'
                    '    cwd=os.getcwd(), np=os.environ["NP"], cpus=sorted(os.sched_getaffinity(0)),\n'
                    '    root=os.environ["CFD_BOT_CASE_DIR"], job=os.environ["CFD_BOT_JOB_ID"], args=sys.argv[1:])))\n'
                    'print("clean complete")\n')
        self.script('Allpost',
                    'import os\nfrom pathlib import Path\n'
                    'assert Path("order").read_text() == "pre\\nsolver\\n"\n'
                    'assert os.environ["NP"] == "1"\n'
                    f'assert sorted(os.sched_getaffinity(0)) == [{self.cpu}]\n'
                    'with Path("order").open("a") as f: f.write("post\\n")\n'
                    'Path("summary.csv").write_text("value,1")\n'
                    'print("post complete")\n')
        self.case_data.update(
            preprocess=[dict(command=['Allclean', 'argument with spaces'])],
            postprocess=[dict(command=['Allpost'])],
            command=[sys.executable, '-c',
                     'from pathlib import Path; assert Path("order").read_text() == "pre\\n"; '
                     'Path("order").write_text("pre\\nsolver\\n"); '
                     'Path("log.solver").write_text("Time = 10\\nEnd\\n")'],
            exports=[dict(name='result', pattern='summary.csv')])
        (self.case_root / 'log.solver').write_text('FOAM FATAL ERROR\nTime = 3\n')
        self.write_case()
        job = self.claim()
        self.assertEqual(worker(self.store.root, job['id']), 0)
        self.assertEqual((self.case_root / 'order').read_text(), 'pre\nsolver\npost\n')
        env = json.loads((self.case_root / 'pre-env.json').read_text())
        self.assertEqual(env, dict(cwd=str(self.case_root), root=str(self.case_root), np='1',
                                  cpus=[int(self.cpu)], job=job['id'], args=['argument with spaces']))
        result = self.store.job(job['id'])
        self.assertEqual(result['status'], 'succeeded')
        self.assertIsNone(result['hook_identity'])
        self.assertEqual(result['postprocess_errors'], [])
        folder = self.store.root / 'jobs' / job['id']
        self.assertIn('clean complete', (folder / 'preprocess.log').read_text())
        self.assertIn('post complete', (folder / 'postprocess.log').read_text())
        files = self.store.get('event:' + job['id'])['files']
        self.assertEqual(Path(files[0]['path']).read_text(), 'value,1')

    def test_failed_missing_and_timed_out_preprocessor_never_launch_solver(self):
        self.case_data['command'] = [sys.executable, '-c',
                                    'from pathlib import Path; Path("solver-started").touch(); print("Time = 10\\nEnd")']
        self.script('Fail', 'print("pre failed")\nraise SystemExit(7)\n')
        self.script('Slow', 'import time\ntime.sleep(60)\n')
        for command, reason in [('./Fail', '전처리 종료 코드 7'),
                                ('./Missing', '전처리 종료 코드'),
                                ('./Slow', '전처리 시간 제한 초과')]:
            with self.subTest(command=command):
                self.case_data['preprocess'] = [dict(command=[command], timeout_seconds=1)]
                self.write_case()
                job = self.claim()
                self.assertEqual(worker(self.store.root, job['id']), 1)
                result = self.store.job(job['id'])
                self.assertEqual(result['status'], 'failed')
                self.assertIn(reason, result['reason'])
                self.assertIsNone(result['hook_identity'])
                self.assertNotIn('solver_pid', result)
                self.assertFalse((self.case_root / 'solver-started').exists())

    def test_failed_solver_does_not_execute_postprocessor(self):
        self.script('Allpost', 'from pathlib import Path\nPath("post-started").touch()\n')
        self.case_data.update(command=[sys.executable, '-c', 'raise SystemExit(7)'],
                              postprocess=[dict(command=['./Allpost'])])
        self.write_case()
        job = self.claim()
        self.assertEqual(worker(self.store.root, job['id']), 1)
        self.assertFalse((self.case_root / 'post-started').exists())

    def test_live_hook_retains_cpu_reservation_and_lost_preprocessor_cannot_reuse_old_success(self):
        job = self.claim()
        self.store.update_job(job['id'], status='running', phase='preprocess', started=time.time() - 1,
                              hook_pid=os.getpid(), hook_identity=identity(os.getpid()))
        scheduler = Scheduler(self.config, self.store)
        scheduler.recover()
        self.assertEqual(self.store.job(job['id'])['status'], 'running')
        self.assertIn('CPU 예약 유지', self.store.job(job['id'])['reason'])
        (self.case_root / 'log.solver').write_text('Time = 10\nEnd\n')
        self.store.update_job(job['id'], hook_identity='old-process')
        scheduler.recover()
        self.assertEqual(self.store.job(job['id'])['status'], 'failed')
        self.assertIn('전처리', self.store.job(job['id'])['reason'])

    def test_preprocess_json_validation(self):
        for value in ('./Allclean', [{'command': []}], [{'command': ['./Allclean'], 'timeout_seconds': 0}]):
            with self.subTest(value=value):
                self.case_data['preprocess'] = value
                with self.assertRaises(ValueError):
                    self.write_case()
