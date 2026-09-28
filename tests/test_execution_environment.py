import os
import shlex
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from cfd_bot.config import load_bot, read_json
from cfd_bot.execution import openfoam_environment
from cfd_bot.jobs import Scheduler
from tests.test_core import Environment


class ExecutionEnvironmentTests(Environment):
    def setup_foam(self):
        folder = self.root / "foam setup ' with spaces"
        folder.mkdir()
        binary = folder / 'foamDictionary'
        binary.write_text('#!/bin/sh\nprintf "Time = 10\\nEnd\\n"\n')
        binary.chmod(0o755)
        for name in ('foamRun', 'decomposePar', 'wmake', 'mpicc'):
            tool = folder / name
            tool.write_bytes(binary.read_bytes())
            tool.chmod(0o755)
        mpi = folder / 'mpirun'
        mpi.write_text('#!/bin/sh\nset -eu\n'
                       'if [ "$1" = --version ]; then echo "mpirun (Open MPI) fixture"; exit 0; fi\n'
                       '[ "$1" = -np ] && shift 2\nexec "$@"\n')
        mpi.chmod(0o755)
        bashrc = folder / 'bashrc'
        bashrc.write_text('test -z "${BOT_TEST_SECRET+x}" || return 1\n'
                          'echo "startup chatter must not corrupt environment"\n'
                          f'export PATH={shlex.quote(str(folder))}:"$PATH"\n'
                          'export FOAM_MPI=openmpi-system\n'
                          'export FOAM_TEST_VALUE="space and\nnewline"\n')
        return bashrc

    def test_clean_service_environment_reaches_detached_worker_and_allrun(self):
        bashrc = self.setup_foam()
        allrun = self.case_root / 'Allrun'
        allrun.write_text('#!/bin/sh\nset -eu\n'
                          'test -z "${BOT_TEST_SECRET+x}"\n'
                          'test "$FOAM_TEST_VALUE" = "space and\nnewline"\n'
                          'mpirun -np 1 foamRun\n')
        allrun.chmod(0o755)
        self.case_data['command'] = ['./Allrun']
        self.write_case()
        job = self.store.enqueue(self.case)
        self.config['telegram']['token_env'] = 'BOT_TEST_SECRET'
        self.config['scheduler']['openfoam_bashrc'] = str(bashrc)
        scheduler = Scheduler(self.config, self.store)
        with patch.dict(os.environ, {'PATH': '/usr/bin:/bin', 'BOT_TEST_SECRET': 'do-not-inherit'}), \
                patch('cfd_bot.jobs.check_cpus', return_value=(True, 'SAFE')):
            scheduler.tick({})
        self.assertEqual(len(scheduler.children), 1)
        self.assertEqual(scheduler.children[0].wait(timeout=10), 0)
        finished = self.store.job(job['id'])
        self.assertEqual(finished['status'], 'succeeded')
        self.assertEqual(finished['openfoam_bashrc'], str(bashrc))
        self.assertNotIn('do-not-inherit', str(finished))

    def test_environment_failure_keeps_fifo_waiting_without_launch_or_case_changes(self):
        job = self.store.enqueue(self.case)
        self.config['scheduler']['openfoam_bashrc'] = str(self.root / 'missing-bashrc')
        scheduler = Scheduler(self.config, self.store)
        with patch('cfd_bot.jobs.check_cpus', return_value=(True, 'SAFE')), \
                patch('cfd_bot.jobs.subprocess.Popen') as launch:
            scheduler.tick({})
            scheduler.tick({})
        launch.assert_not_called()
        waiting = self.store.job(job['id'])
        self.assertEqual(waiting['status'], 'queued')
        self.assertIn('OpenFOAM 환경 파일이 없습니다', waiting['reason'])
        self.assertNotIn('started', waiting)
        self.assertEqual(len(self.store.pending()), 1)

    def test_source_failure_and_missing_utility_are_actionable(self):
        bashrc = self.root / 'broken-bashrc'
        bashrc.write_text('echo private-output >&2\nreturn 2\n')
        with self.assertRaisesRegex(ValueError, 'OpenFOAM 환경 초기화 실패') as raised:
            openfoam_environment(bashrc, {'PATH': '/usr/bin:/bin'})
        self.assertNotIn('private-output', str(raised.exception))
        bashrc.write_text('export PATH=/no/such/binaries\n')
        with self.assertRaisesRegex(ValueError, 'foamDictionary를 찾을 수 없습니다'):
            openfoam_environment(bashrc, {'PATH': '/usr/bin:/bin'})

    def test_bot_config_resolves_environment_file_and_rejects_empty_setting(self):
        data = read_json(self.bot_path)
        data['scheduler']['openfoam_bashrc'] = 'foam/etc/bashrc'
        self.bot_path.write_text(json.dumps(data))
        self.assertEqual(load_bot(self.bot_path)['scheduler']['openfoam_bashrc'],
                         str(self.root / 'foam/etc/bashrc'))
        data['scheduler']['openfoam_bashrc'] = ''
        self.bot_path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'openfoam_bashrc'):
            load_bot(self.bot_path)

    def test_execution_env_check_probes_utility_without_enqueuing(self):
        from cfd_bot.cli import main
        bashrc = self.setup_foam()
        data = read_json(self.bot_path)
        data['scheduler']['openfoam_bashrc'] = str(bashrc)
        self.bot_path.write_text(json.dumps(data))
        output = io.StringIO()
        with patch.dict(os.environ, {'PATH': '/usr/bin:/bin'}), redirect_stdout(output):
            self.assertEqual(main(['--config', str(self.bot_path), 'check', '--execution-env']), 0)
        self.assertIn('OpenFOAM 환경 정상', output.getvalue())
        self.assertEqual(self.store.jobs(), [])

    def test_missing_mpirun_blocks_before_any_worker_or_case_changes(self):
        bashrc = self.setup_foam()
        (bashrc.parent / 'mpirun').unlink()
        before = self.case_path.read_bytes()
        job = self.store.enqueue(self.case)
        self.config['scheduler']['openfoam_bashrc'] = str(bashrc)
        scheduler = Scheduler(self.config, self.store)
        with patch.dict(os.environ, {'PATH': '/usr/bin:/bin'}):
            scheduler.tick({})
        waiting = self.store.job(job['id'])
        self.assertEqual(waiting['status'], 'queued')
        self.assertIn('mpirun', waiting['reason'])
        self.assertEqual(scheduler.children, [])
        self.assertNotIn('started', waiting)
        self.assertEqual(self.case_path.read_bytes(), before)

    def test_openmpi_configuration_rejects_mpich_launcher(self):
        bashrc = self.setup_foam()
        (bashrc.parent / 'mpirun').write_text('#!/bin/sh\necho "HYDRA build details MPICH"\n')
        with self.assertRaisesRegex(ValueError, '다른 MPI'):
            openfoam_environment(bashrc, {'PATH': '/usr/bin:/bin'})

    def test_diagnostic_does_not_hide_missing_mpi_with_interactive_shell_paths(self):
        from cfd_bot.cli import main
        bashrc = self.setup_foam()
        ambient = self.root / 'shell-only-mpi'
        ambient.mkdir()
        (bashrc.parent / 'mpirun').replace(ambient / 'mpirun')
        data = read_json(self.bot_path)
        data['scheduler']['openfoam_bashrc'] = str(bashrc)
        self.bot_path.write_text(json.dumps(data))
        error = io.StringIO()
        with patch.dict(os.environ, {'PATH': str(ambient) + ':/usr/bin:/bin'}), redirect_stderr(error):
            self.assertEqual(main(['--config', str(self.bot_path), 'check', '--execution-env']), 2)
        self.assertIn('mpirun', error.getvalue())
        self.assertEqual(self.store.jobs(), [])

    def test_mpi_probe_runs_help_without_starting_a_case(self):
        from cfd_bot.cli import main
        bashrc = self.setup_foam()
        data = read_json(self.bot_path)
        data['scheduler']['openfoam_bashrc'] = str(bashrc)
        self.bot_path.write_text(json.dumps(data))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--config', str(self.bot_path), 'check', '--execution-env', '--mpi-probe']), 0)
        self.assertIn('MPI foamRun -help', output.getvalue())
        self.assertEqual(self.store.jobs(), [])
