import tempfile
import time
import unittest
from pathlib import Path

from cfd_bot.run_views import job_view, running_macro_views, tracking_registry
from cfd_bot.storage import Store


class RunViewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / 'state')

    def case(self, name, expected=None):
        root = self.root / name
        (root / 'system').mkdir(parents=True)
        (root / 'system/controlDict').write_text(
            'startTime 0; stopAt endTime; endTime 100;\n', encoding='utf-8')
        ticket = self.root / f'child-{name}.json'
        ticket.write_text('{}', encoding='utf-8')
        case = dict(name=name, _root=str(root), _config=str(ticket), command=['/bin/true'],
                    watcher={'logs': ['log.solver']}, exports=[], residual_pattern='')
        if expected is not None:
            case['expected_seconds'] = expected
        return case

    def test_only_current_ticket_registry_makes_history_trackable(self):
        case = self.case('tracked')
        job = dict(id='history', case=case, case_root=case['_root'], status='succeeded',
                   created=1, started=2, finished=3)

        tracked = job_view(job, tracking_registry([case]))
        self.assertTrue(tracked['trackable'])
        self.assertTrue(tracked['case_id'])
        self.assertEqual(tracked['ticket'], 'child-tracked.json')

        Path(case['_config']).unlink()
        missing = job_view(job, tracking_registry([case]))
        self.assertFalse(missing['trackable'])
        self.assertIsNone(missing['case_id'])

    def test_live_macro_uses_current_child_job_ids_for_progress_and_eta(self):
        now = 1_000.0
        first, running, waiting = self.case('one'), self.case('two'), self.case('three')
        (Path(running['_root']) / 'log.solver').write_text(
            'Time = 0\nExecutionTime = 0 s ClockTime = 0 s\n'
            'Time = 50\nExecutionTime = 50 s ClockTime = 50 s\n', encoding='utf-8')
        jobs = self.store.enqueue_batch([first, running, waiting], 'current-batch')
        self.store.update_job(jobs[0]['id'], status='succeeded', created=970,
                              started=970, finished=980)
        self.store.update_job(jobs[1]['id'], status='running', created=970, started=980)
        self.store.update_job(jobs[2]['id'], status='queued', created=970)
        jobs = self.store.jobs()
        macro = dict(name='batch', task_type='macro', _config=str(self.root / 'macro-batch.json'),
                     queue={'state': 'running'}, cases=[
                         dict(case_dir=first['_root'], state='finished', result='succeeded',
                              job_id=jobs[0]['id']),
                         dict(case_dir=running['_root'], state='running', job_id=jobs[1]['id']),
                         dict(case_dir=waiting['_root'], state='waiting', job_id=jobs[2]['id'])])

        result = running_macro_views([macro], [first, running, waiting], jobs, self.store, now)[0]

        self.assertEqual((result['completed'], result['target']), (1, 3))
        self.assertAlmostEqual(result['progress'], .5)
        self.assertEqual(result['elapsed_seconds'], 30)
        # The queued case has no own history, so the last successful child (10 s)
        # supplies the macro-level fallback after the running child ETA (50 s).
        self.assertEqual(result['remaining_seconds'], 60)
        self.assertEqual(result['active_case'], 'two')

        macro['cases'][1]['job_id'] = 'old-job-not-in-store'
        self.assertIsNone(running_macro_views(
            [macro], [first, running, waiting], jobs, self.store, now)[0]['remaining_seconds'])


if __name__ == '__main__':
    unittest.main()
