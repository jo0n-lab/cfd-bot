import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

from cfd_bot.artifacts import MAX_DOCUMENT, freeze_exports, residual_files
from cfd_bot.bot import Bot, case_id
from cfd_bot.config import ConfigError
from cfd_bot.jobs import worker
from cfd_bot.report import render_run
from tests.test_core import Environment, FakeAPI


class ResidualTests(Environment):
    def setUp(self):
        super().setUp()
        self.images = self.case_root / 'plots'
        self.images.mkdir()
        self.case_data['residual_pattern'] = 'plots/residual*.png'
        self.write_case()

    def test_request_sends_latest_case_png_without_reading_solver_log(self):
        old = self.images / 'residual-old.png'
        new = self.images / 'residual-new.png'
        old.write_bytes(b'old case image')
        new.write_bytes(b'new case image')
        os.utime(old, (1, 1))
        api = FakeAPI()
        bot = Bot(self.config, self.store, api)
        with patch('cfd_bot.logs.recent_case_log', side_effect=AssertionError('must not read log')), \
                patch.object(bot, 'latest_run', side_effect=AssertionError('must not need telemetry')):
            bot.dispatch(20, 'residual:' + case_id(self.case), 'request')
        self.assertEqual(len(api.files), 1)
        self.assertEqual(api.files[0][1]['path'], str(new))
        self.assertEqual(api.files[0][1]['kind'], 'photo')
        self.assertEqual(new.read_bytes(), b'new case image')
        self.assertFalse((self.store.root / 'requests').exists())

    def test_missing_png_does_not_fall_back_to_log_graph_or_csv(self):
        (self.case_root / 'log.solver').write_text(
            'Time = 5\nSolving for U, Initial residual = 0.1, Final residual = 0.01, No Iterations 1\nEnd\n')
        api = FakeAPI()
        Bot(self.config, self.store, api).dispatch(20, 'residual:' + case_id(self.case), 'missing')
        self.assertEqual(api.files, [])
        self.assertIn('Residual PNG가 없습니다: plots/residual*.png', api.messages[-1][1])
        self.assertEqual(list(self.images.iterdir()), [])

    def test_blank_path_disables_button_and_handles_old_callback(self):
        self.case_data['residual_pattern'] = ''
        self.write_case()
        api = FakeAPI()
        bot = Bot(self.config, self.store, api)
        bot.case_menu(20, case_id(self.case))
        buttons = api.messages[-1][2]['inline_keyboard']
        self.assertFalse(any(b['callback_data'].startswith('residual:') for row in buttons for b in row))
        bot.dispatch(20, 'residual:' + case_id(self.case), 'old-button')
        self.assertIn('경로를 지정', api.messages[-1][1])
        self.assertEqual(api.files, [])

    def test_completion_skips_old_png_but_explicit_request_can_retrieve_it(self):
        path = self.images / 'residual.png'
        path.write_bytes(b'previous run')
        os.utime(path, (1, 1))
        files, _ = freeze_exports(self.case, self.root / 'frozen', time.time() - 10, 'succeeded')
        self.assertEqual(files, [])
        self.assertEqual(residual_files(self.case)[0]['path'], str(path))

    def test_worker_freezes_case_owned_png_at_completion(self):
        self.case_data['command'] = [sys.executable, '-c',
            "from pathlib import Path; Path('plots/residual.png').write_bytes(b'case result'); print('Time = 10\\nEnd')"]
        self.write_case()
        job = self.claim()
        self.assertEqual(worker(self.store.root, job['id']), 0)
        payload = self.store.get('event:' + job['id'])
        self.assertEqual(len(payload['files']), 1)
        self.assertEqual(Path(payload['files'][0]['path']).read_bytes(), b'case result')

    def test_escaping_paths_and_non_png_patterns_are_rejected(self):
        for pattern in ('../residual.png', '/tmp/residual.png', 'residual.csv', ['residual.png']):
            with self.subTest(pattern=pattern):
                self.case_data['residual_pattern'] = pattern
                with self.assertRaises(ConfigError):
                    self.write_case()

    def test_symlink_escape_and_oversized_png_are_not_sent(self):
        outside = self.root / 'outside.png'
        outside.write_bytes(b'private')
        link = self.images / 'residual-leak.png'
        link.symlink_to(outside)
        with self.assertRaises(ConfigError):
            residual_files(self.case)
        link.unlink()
        image = self.images / 'residual-large.png'
        with image.open('wb') as output:
            output.truncate(MAX_DOCUMENT + 1)
        with self.assertRaisesRegex(ValueError, '49 MiB'):
            residual_files(self.case)
        files, notes = freeze_exports(self.case, self.root / 'frozen', 0, 'failed')
        self.assertEqual(files, [])
        self.assertIn('49 MiB', notes[0])

    def test_report_uses_image_path_instead_of_log_residual_values(self):
        run = dict(id='run', case=self.case, status='running', started=time.time(),
                   telemetry={'residuals': {'U': {'initial': 0.1, 'final': 0.01, 'iterations': 1}}})
        text = render_run(run)
        self.assertIn('Residual PNG: plots/residual*.png', text)
        self.assertNotIn('U:', text)
