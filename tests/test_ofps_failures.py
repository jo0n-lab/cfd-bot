"""Exercise the real ofps entrypoint without scanning or starting calculations."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cfd_bot import diagnostics
from cfd_bot.processes import check_cpus, snapshot


OFPS = Path(__file__).resolve().parents[1] / 'bin/ofps'


class OfpsFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ofps-failure-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fake_mktemp = self.root / 'mktemp'
        self.env = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ['PATH'],
                        CFD_BOT_OFPS_MANAGED='1', CFD_BOT_DIAGNOSTICS='0',
                        CFD_BOT_DIAGNOSTICS_LEVEL='basic',
                        CFD_BOT_DIAGNOSTICS_DIR=str(self.root / 'diagnostics'),
                        OFPS_TEST_SCRATCH=str(self.root / 'missing' / 'scratch'))
        self.env.pop('BASH_ENV', None)
        self.failing_mktemp()

    def failing_mktemp(self):
        self.fake_mktemp.write_text('#!/bin/sh\nprintf "mktemp: simulated No space left on device\\n" >&2\nexit 73\n')
        self.fake_mktemp.chmod(0o700)

    def run_ofps(self, *args):
        return subprocess.run([str(OFPS), *args], env=self.env, text=True,
                              capture_output=True, timeout=3)

    def test_mktemp_failure_exits_before_scan_in_every_mode_and_logging_level(self):
        for level in ('off', 'basic', 'detailed'):
            self.env.update(CFD_BOT_DIAGNOSTICS='0' if level == 'off' else '1',
                            CFD_BOT_DIAGNOSTICS_LEVEL='basic' if level == 'off' else level)
            for args in ((), ('--check', '0'), ('--check-cpu-set', '0'), ('--watch', '0.01')):
                with self.subTest(level=level, args=args):
                    result = self.run_ofps(*args)
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, '')
                    self.assertIn('No space left on device', result.stderr)
                    self.assertIn('scan aborted', result.stderr)
                    self.assertNotIn('/snapshot.txt', result.stderr)
                    self.assertNotIn('/records.tsv', result.stderr)
                    self.assertNotIn('Permission denied', result.stderr)
        records = [r for path in (self.root / 'diagnostics').glob('*.jsonl')
                   for r in diagnostics.read_records(path)]
        exits = [r for r in records if r['event'] == 'shell.exit']
        self.assertEqual(len(exits), 8)

    def test_snapshot_write_failure_never_reaches_cpu_check_or_next_watch_tick(self):
        self.fake_mktemp.write_text('#!/bin/sh\nprintf "%s\\n" "$OFPS_TEST_SCRATCH"\n')
        for args in ((), ('--check', '0'), ('--watch', '0.01')):
            with self.subTest(args=args):
                result = self.run_ofps(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('SAFE:', result.stdout)
                self.assertNotIn('CPU_SET check:', result.stdout)
                self.assertNotIn('ENGINE:', result.stdout)
                self.assertLessEqual(result.stdout.count('TCB process snapshot:'), 1)
                self.assertNotIn('cat:', result.stderr)

    def test_python_snapshot_and_admission_propagate_real_ofps_failure(self):
        with patch.dict(os.environ, self.env, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'ofps exited 1:.*No space left on device'):
                snapshot([str(OFPS)])
            safe, reason = check_cpus([str(OFPS)], {'cpu_set': '0'})
        self.assertFalse(safe)
        self.assertIn('scan aborted', reason)
        self.assertNotIn('SAFE:', reason)

    def test_help_does_not_require_a_temporary_directory(self):
        result = self.run_ofps('--help')
        self.assertEqual(result.returncode, 0)
        self.assertIn('Usage: ofps', result.stdout)
        self.assertEqual(result.stderr, '')
