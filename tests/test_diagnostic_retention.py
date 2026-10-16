import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from cfd_bot import diagnostics as d
from cfd_bot import diagnostic_retention as r


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = time.time()

    def log(self, name='ofps-999999-abc-s123.jsonl', age=90000, event='shell.file'):
        path = self.root / name
        path.write_text(json.dumps({'event': event, 'schema_version': 2}) + '\n')
        os.utime(path, (self.now - age, self.now - age))
        return path

    def test_boundary_dry_run_apply_and_recent_mixed_file(self):
        expired = self.log(age=86401)
        boundary = self.log('web-999999-def-s124.jsonl', age=86400)
        recent = self.log('serve-999999-def-s125.jsonl', age=10)
        recent.write_text(recent.read_text() + '{"old_record":true}\n{"recent_record":true}\n')
        backup = self.log('web-999999-aaa.jsonl.1', age=86401)
        with patch.object(r, '_open_inodes', return_value=(set(), set())):
            preview = r.collect(self.root, now=self.now)
            self.assertEqual(preview['candidate_files'], 2)
            self.assertEqual(preview['deleted_files'], 0)
            self.assertTrue(expired.exists())
            result = r.collect(self.root, apply=True, now=self.now)
        self.assertEqual(result['deleted_files'], 2)
        self.assertEqual(result['remaining_files'], 2)
        self.assertFalse(expired.exists())
        self.assertFalse(backup.exists())
        self.assertTrue(boundary.exists())
        self.assertTrue(recent.exists())

    def test_preserves_maps_other_files_symlinks_and_subdirectories(self):
        outside = self.root / 'solver.log'
        outside.write_text('solver evidence')
        link = self.root / 'ofps-999999-abc-s111.jsonl'
        link.symlink_to(outside)
        for name in ('codes-dead.json', 'sources-dead.json', 'tickets.json', 'state.sqlite'):
            (self.root / name).write_text('retain')
        fake = self.log('ofps-999999-abc-s222.jsonl', event='not.a.diagnostic')
        nested = self.root / 'investigation'
        nested.mkdir()
        (nested / 'ofps-999999-abc.jsonl').write_text('retained copy')
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = r.collect(self.root, apply=True, now=self.now)
        after = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*')
                 if p.is_file() and p.name != '.retention.lock'}
        self.assertEqual(before, after)
        self.assertEqual(result['deleted_files'], 0)
        self.assertTrue(fake.exists())

    def test_open_legacy_is_deferred_but_idle_segment_can_expire(self):
        legacy = self.log('serve-999999-abc.jsonl')
        segment = self.log()
        with legacy.open() as a, segment.open() as b:
            result = r.collect(self.root, apply=True, now=self.now)
            self.assertEqual(result['deferred_open'], 1)
            self.assertTrue(legacy.exists())
            self.assertFalse(segment.exists())
        result = r.collect(self.root, apply=True, now=self.now)
        self.assertEqual(result['deleted_files'], 1)

    def test_failed_process_inspection_defers_legacy(self):
        legacy = self.log('serve-999999-abc.jsonl')
        with patch.object(r, '_open_inodes', return_value=(set(), {999999})):
            result = r.collect(self.root, apply=True, now=self.now)
        self.assertEqual(result['deferred_open'], 1)
        self.assertTrue(legacy.exists())

    def test_unrelated_unreadable_process_does_not_block_cleanup(self):
        legacy = self.log('serve-999999-abc.jsonl')
        with patch.object(r, '_open_inodes', return_value=(set(), {111111})):
            result = r.collect(self.root, apply=True, now=self.now)
        self.assertEqual(result['deleted_files'], 1)
        self.assertFalse(legacy.exists())

    def test_concurrent_cleanup_skips_and_delete_failure_is_counted(self):
        expired = self.log()
        with r._directory_lock(self.root):
            result = r.collect(self.root, apply=True, now=self.now)
        self.assertEqual(result['status'], 'already_running')
        self.assertTrue(expired.exists())
        with patch.object(r.os, 'unlink', side_effect=PermissionError):
            result = r.collect(self.root, apply=True, now=self.now)
        self.assertEqual(result['errors'], 1)
        self.assertTrue(expired.exists())

    def test_replaced_file_is_not_deleted(self):
        expired = self.log()
        original_open = os.open
        def replace_before_open(path, *args, **kwargs):
            if str(path) == str(expired):
                expired.unlink()
                expired.write_text('new unrelated file')
            return original_open(path, *args, **kwargs)
        with patch.object(r.os, 'open', side_effect=replace_before_open):
            result = r.collect(self.root, apply=True, now=self.now)
        self.assertTrue(expired.exists())
        self.assertEqual(result['deleted_files'], 0)

    def test_config_resolution_does_not_start_logger_and_off_still_cleans(self):
        cfg = self.root / 'bot.json'
        cfg.write_text(json.dumps({'state_dir': 'state', 'diagnostic_logging': {'enabled': False}}))
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(r.directory_from_config(cfg), self.root / 'state/diagnostics')
            with patch.object(d, 'configure', side_effect=AssertionError('must not initialize bot')):
                result = subprocess.run(['python3', '-m', 'cfd_bot.diagnostic_retention', '--config', str(cfg)],
                                        capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)['status'], 'missing_directory')
        self.assertFalse((self.root / 'state').exists())

    def test_apply_publishes_one_private_status_file_and_dry_run_preserves_it(self):
        self.log()
        command = ['python3', '-m', 'cfd_bot.diagnostic_retention', '--directory', str(self.root)]
        result = subprocess.run(command + ['--apply'], capture_output=True, text=True, check=True)
        status = self.root / 'retention-status.json'
        self.assertEqual(json.loads(status.read_text()), json.loads(result.stdout))
        self.assertEqual(status.stat().st_mode & 0o777, 0o600)
        original = status.read_bytes()
        subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertEqual(status.read_bytes(), original)
        subprocess.run(command + ['--apply'], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(status.read_text())['deleted_files'], 0)
        self.assertEqual(list(self.root.glob('.retention-status-*')), [])


class SegmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(d.close)
        self.root = Path(self.temp.name)

    def test_python_rotation_preserves_trace_decode_and_restarts_after_expiry(self):
        d.configure({'diagnostic_logging': {'directory': str(self.root)}}, active=True)
        d.event('test.before', trace_id='trace-retained')
        d.flush()
        old = d._sink.path
        before = list(d.read_records(old))
        d._sink.segment_started -= 301
        d.event('test.after', trace_id='trace-retained')
        d.flush()
        new = d._sink.path
        self.assertNotEqual(old, new)
        self.assertEqual(list(d.read_records(old)), before)
        self.assertEqual(json.loads(old.read_text().splitlines()[0])['instance'],
                         json.loads(new.read_text().splitlines()[0])['instance'])
        self.assertTrue(any(x['event'] == 'test.after' for x in d.read_records(new)))
        # Idle segment can be removed while its descriptor remains open.
        os.utime(new, (time.time() - 90000,) * 2)
        d._sink.segment_started -= 90000
        result = r.collect(self.root, apply=True)
        self.assertEqual(result['deleted_files'], 1)
        d.event('test.resumed')
        d.flush()
        self.assertNotEqual(new, d._sink.path)
        self.assertTrue(any(x['event'] == 'test.resumed' for x in d.read_records(d._sink.path)))
        # Wall time advances during machine suspend even when monotonic does not.
        current = d._sink.path
        d._sink.segment_wall_started -= 301
        d.event('test.after_suspend')
        d.flush()
        self.assertNotEqual(current, d._sink.path)

    def test_shell_time_rotation_and_idle_expiry_preserve_exit_and_headers(self):
        script = Path(__file__).resolve().parents[1] / 'bin/ofps-diagnostics.sh'
        env = dict(os.environ, CFD_BOT_DIAGNOSTICS='1', CFD_BOT_DIAGNOSTICS_LEVEL='basic',
                   CFD_BOT_DIAGNOSTICS_DIR=str(self.root))
        command = '''
source "$1"
old=$_cfd_diag_file
_cfd_diag_segment_wall=$((EPOCHSECONDS-301))
_cfd_diag_write shell.call 0 before scan_once
[[ $old != "$_cfd_diag_file" ]] || exit 90
rm -- "$_cfd_diag_file"
_cfd_diag_segment_started=$((SECONDS-90000))
_cfd_diag_write shell.call 0 resumed scan_once
[[ -s $_cfd_diag_file ]] || exit 91
exit 7
'''
        result = subprocess.run(['bash', '-c', command, 'test', str(script)], env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 7, result.stderr)
        paths = list(self.root.glob('*.jsonl'))
        self.assertEqual(len(paths), 2)
        for path in paths:
            self.assertEqual(json.loads(path.read_text().splitlines()[0])['event'], 'shell.file')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        records = [x for path in paths for x in d.read_records(path)]
        self.assertTrue(any(x.get('detail') == 'resumed' for x in records), records)


if __name__ == '__main__':
    unittest.main()
