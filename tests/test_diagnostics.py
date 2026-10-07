"""Observability must preserve return values, failures, streams and transactions."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import warnings

from cfd_bot import diagnostics as d


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(d.close)
        d.configure({'diagnostic_logging': {'level': 'detailed', 'directory': str(self.root)}}, active=True)

    def records(self):
        d.flush()
        return [record for path in self.root.glob('*.jsonl*') for record in d.read_records(path)]

    def test_calls_preserve_identity_and_correlate_parent_and_thread(self):
        value = object()
        @d.trace
        def child(x):
            return x
        @d.trace
        def parent(x):
            thread = threading.Thread(target=d.inherit_context(lambda: child(x)))
            thread.start()
            thread.join()
            return child(x)
        self.assertIs(parent(value), value)
        records = self.records()
        parent_call = next(r for r in records if r['event']=='function.call' and r['function'].endswith('.parent'))
        children = [r for r in records if r['event']=='function.call' and r['function'].endswith('.child')]
        self.assertEqual(len(children), 2)
        self.assertTrue(all(r['parent_call_id']==parent_call['call_id'] and r['trace_id']==parent_call['trace_id'] for r in children))
        self.assertIsNone(d._current.get())

    def test_exception_identity_chain_warning_and_secrets(self):
        secret='unit-test-secret-not-a-telegram-pattern'
        d.remember_secret(secret)
        cause=ValueError('token='+secret)
        error=RuntimeError('failed '+secret)
        @d.trace
        def fail(token):
            warnings.warn('warning '+token, RuntimeWarning)
            raise error from cause
        with contextlib.redirect_stderr(io.StringIO()), warnings.catch_warnings():
            warnings.simplefilter('always')
            with self.assertRaises(RuntimeError) as caught:
                fail(secret)
        self.assertIs(caught.exception,error)
        records=self.records()
        self.assertNotIn(secret,json.dumps(records))
        self.assertTrue(any(r['event']=='python.warning' for r in records))
        raised=next(r for r in records if r['event']=='function.raise')
        self.assertEqual([e['type'] for e in raised['exception']],['RuntimeError','ValueError'])

    def test_generator_send_throw_close_and_context(self):
        closed=[]
        @d.trace
        def generator():
            try:
                value=yield 'ready'
                try:
                    yield value
                except ValueError:
                    yield 'caught'
                return 7
            finally:
                closed.append(True)
        iterator=generator()
        self.assertEqual(next(iterator),'ready')
        self.assertIsNone(d._current.get())
        self.assertEqual(iterator.send(42),42)
        self.assertEqual(iterator.throw(ValueError('expected')),'caught')
        with self.assertRaises(StopIteration) as end:
            next(iterator)
        self.assertEqual(end.exception.value,7)
        second=generator();next(second);second.close()
        self.assertEqual(closed,[True,True])
        self.assertIsNone(d._current.get())
        self.assertTrue(any(r['event']=='function.cancelled' for r in self.records()))

    def test_off_does_not_summarize_or_write_and_propagates_to_child(self):
        d.configure({'diagnostic_logging': {'directory': str(self.root/'off')}}, active=False)
        @d.trace
        def identity(value):return value
        with patch.object(d,'summary',side_effect=AssertionError('OFF summarized')):
            self.assertEqual(identity(5),5)
            d.event('ignored',body='not serialized')
        self.assertFalse((self.root/'off').exists())
        self.assertEqual(d.child_environment({})['CFD_BOT_DIAGNOSTICS'],'0')

    def test_batches_keep_every_branch_and_rotation_keeps_source_header(self):
        d.configure({'diagnostic_logging': {'level': 'detailed', 'directory': str(self.root), 'max_bytes': 1024, 'backups': 3}}, active=True)
        @d.trace
        def work():
            for i in range(180):d.step('synthetic.loop', index=i)
        work()
        d.event('rotation.trigger')
        d.flush()
        records=self.records()
        steps=[step for record in records for step in record.get('steps',[])]
        self.assertEqual([step[3]['index'] for step in steps],list(range(180)))
        files=list(self.root.glob('*.jsonl*'))
        self.assertTrue(files)
        self.assertTrue(any(str(path).endswith('.1') for path in files))
        for path in files:
            header=json.loads(path.read_text().splitlines()[0])
            self.assertEqual(header['event'],'log.file')
            self.assertEqual(len(header['source_map_sha256']),64)

    def test_new_secret_invalidates_cached_values(self):
        value='another-credential-value'
        self.assertEqual(d.summary(value,'path'),value)
        d.remember_secret(value)
        self.assertEqual(d.summary(value,'path'),'[redacted]')

    def test_old_batches_still_decode(self):
        path=self.root/'legacy.jsonl'
        header={'event':'log.file','pid':123,'instance':'old','component':'legacy'}
        batch={'event':'log.batch','contexts':[{'thread':7,'trace_id':'t','initiator':None}],
               'records':[['function.return',100,8,0,'123:2',None,{'function':'old.work','result':False}]]}
        path.write_text(json.dumps(header)+'\n'+json.dumps(batch)+'\n')
        records=list(d.read_records(path))
        self.assertEqual(records[1]['function'],'old.work')
        self.assertIs(records[1]['result'],False)
        self.assertEqual(records[1]['trace_id'],'t')

    def test_numeric_codec_preserves_types_and_exception_propagation(self):
        failure=RuntimeError('same original failure')
        scopes=[]
        @d.trace
        def leaf(flag):
            if flag=='raise':raise failure
            return flag
        @d.trace
        def middle():return leaf('raise')
        @d.trace
        def outer():
            scopes.append(d._current.get()['errors'])
            return middle()
        leaf(True);leaf(1);leaf(False);leaf(0)
        with self.assertRaises(RuntimeError) as raised:outer()
        self.assertIs(raised.exception,failure)
        self.assertEqual(scopes,[{}])
        records=self.records()
        returned=[r['result'] for r in records if r['event']=='function.return' and r.get('function','').endswith('.leaf')]
        self.assertEqual([type(v) for v in returned],[bool,int,bool,int])
        errors=[r['exception'][0] for r in records if r['event']=='function.raise']
        self.assertEqual(len(errors),3)
        self.assertEqual(len({e['error_id'] for e in errors}),1)
        self.assertLess(len(errors[0]['frames']),len(errors[-1]['frames']))
        raw=[json.loads(line) for p in self.root.glob('*.jsonl') for line in p.read_text().splitlines()]
        batches=[r for r in raw if r.get('event')=='log.batch.v2']
        self.assertTrue(batches)
        self.assertTrue(any(e[1] is not None for b in batches for e in b['errors']))

    def test_readonly_sqlite_context_is_not_reported_as_commit(self):
        db=d.connect_sqlite(self.root/'readonly.sqlite')
        try:
            with db:db.execute('SELECT 1').fetchone()
        finally:db.close()
        events=[r['event'] for r in self.records()]
        self.assertIn('db.context.exit',events)
        self.assertNotIn('db.transaction.commit',events)

    def test_long_lived_request_bounds_exception_references(self):
        @d.trace
        def observe():
            maximum=0
            for i in range(600):
                d.exception_info(ValueError('failure '+str(i)))
                maximum=max(maximum,len(d._current.get()['errors']))
            return maximum
        self.assertEqual(observe(),256)

    def test_invalid_logging_options_are_not_new_business_validation(self):
        d.configure({'telegram': {'token_env': []}, 'diagnostic_logging': {
            'directory': {}, 'max_bytes': 'invalid', 'backups': -1}}, state_dir=str(self.root), active=True)
        self.assertTrue(d.enabled)
        self.assertEqual(d._sink.max_bytes,20*1024*1024)

    def test_logging_output_failure_does_not_replace_application_result(self):
        @d.trace
        def work():return 'original-result'
        with patch.object(d._sink,'_drain',side_effect=OSError('disk full')),contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(work(),'original-result')
        self.assertGreater(d._sink.errors,0)

    def test_sqlite_rollback_and_lock_error_are_original(self):
        db=d.connect_sqlite(self.root/'work.sqlite',timeout=0)
        self.addCleanup(db.close)
        with db:db.execute('CREATE TABLE sample (value INTEGER)')
        with self.assertRaisesRegex(ValueError,'original'):
            with db:
                db.execute('INSERT INTO sample VALUES (1)')
                raise ValueError('original')
        self.assertEqual(db.execute('SELECT count(*) FROM sample').fetchone()[0],0)
        other=sqlite3.connect(self.root/'work.sqlite',timeout=0)
        try:
            other.execute('BEGIN IMMEDIATE')
            with self.assertRaises(sqlite3.OperationalError):
                with db:db.execute('BEGIN IMMEDIATE')
        finally:other.close()
        events=[r['event'] for r in self.records()]
        self.assertIn('db.transaction.rollback',events)
        self.assertIn('db.statement.error',events)

    def test_subprocess_keeps_streams_status_and_trace(self):
        @d.trace
        def run():
            return d.run_process(['python3','-c',
                'import os,sys;print(os.environ["CFD_BOT_TRACE_ID"]);print("a warning",file=sys.stderr);sys.exit(7)'],
                capture_output=True,text=True)
        result=run()
        self.assertEqual(result.returncode,7)
        self.assertEqual(result.stderr,'a warning\n')
        request=next(r for r in self.records() if r['event']=='subprocess.request')
        self.assertEqual(result.stdout.strip(),request['trace_id'])

    def test_shell_diagnostics_preserve_stdout_status_and_pipeline(self):
        helper=Path(__file__).resolve().parents[1]/'bin/ofps-diagnostics.sh'
        script='''source "$1"
good() { local _cfd_diag_call=good _cfd_diag_result=''; _cfd_diag_write shell.call 0 argc=0 good; printf 'unchanged\\n'; _cfd_diag_result=7; return "$_cfd_diag_result"; }
good; status=$?
printf 'status=%s\\n' "$status"
false | true
printf 'pipeline=%s\\n' "$?"
'''
        env=dict(os.environ,CFD_BOT_DIAGNOSTICS='1',CFD_BOT_DIAGNOSTICS_LEVEL='detailed',CFD_BOT_DIAGNOSTICS_DIR=str(self.root/'shell'))
        result=subprocess.run(['bash','-o','pipefail','-c',script,'test',str(helper)],env=env,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout,'unchanged\nstatus=7\npipeline=1\n')
        records=[record for p in (self.root/'shell').glob('*.jsonl') for record in d.read_records(p)]
        self.assertTrue(any(r['event']=='shell.return' and r['function']=='good' and r['status']==7 for r in records))
        self.assertTrue(any(r['event']=='shell.error' and r['status']==1 for r in records))


if __name__=='__main__':unittest.main()
