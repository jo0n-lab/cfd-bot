"""Basic mode drops normal helper work, preserving causal errors and domain events."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import warnings
from cfd_bot import diagnostics as d
from cfd_bot.config import cpu_set
from cfd_bot.logs import read_log
from cfd_bot.storage import Store
from cfd_bot.tickets import atomic_json


class DiagnosticLevelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(d.close)
        self.environment = patch.dict(os.environ, {}, clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        os.environ.pop('CFD_BOT_DIAGNOSTICS_LEVEL', None)
        d.configure({'diagnostic_logging': {'directory': str(self.root/'logs')}}, active=True)

    def records(self):
        d.flush()
        return [r for p in (self.root/'logs').glob('*.jsonl*') for r in d.read_records(p)]

    def test_default_basic_skips_normal_helpers_before_summarizing(self):
        self.assertEqual(d.settings()['level'], 'basic')
        self.assertFalse(d.detailed)
        before=d._sink.records
        with patch.object(d, 'call_value', side_effect=AssertionError('helper summary')):
            self.assertEqual(cpu_set('0-2'), {0,1,2})
            d.step('unused:loop', index=1)
        self.assertEqual(d._sink.records,before)
        d.configure({'diagnostic_logging': {'directory': str(self.root/'logs'), 'level':'detailed'}}, active=True)
        cpu_set('0-2')
        self.assertTrue(any(r['event']=='function.call' and r['function']=='config.cpu_set' for r in self.records()))

    def test_helpers_keep_exception_identity_chain_catch_and_warning(self):
        failure=ValueError('fixture failure')
        @d.trace
        def leaf():raise failure
        @d.trace
        def outer():return leaf()
        with self.assertRaises(ValueError) as caught:outer()
        self.assertIs(caught.exception, failure)
        try:outer()
        except ValueError:d.step('test:except')
        with warnings.catch_warnings(), contextlib.redirect_stderr(io.StringIO()):
            warnings.simplefilter('always');warnings.warn('fixture warning')
        records=self.records()
        self.assertFalse(any(r['event']=='function.call' for r in records))
        self.assertEqual(sum(r['event']=='function.error' for r in records),4)
        self.assertTrue(any(r['event']=='sequence.step' and r['exception'] for r in records))
        self.assertTrue(any(r['event']=='python.warning' for r in records))
        self.assertIsNone(d._current.get())

    def test_basic_generator_send_throw_close_are_unchanged(self):
        closed=[]
        @d.trace
        def iterator():
            try:
                value=yield 1
                try:yield value
                except ValueError:yield 3
                return 4
            finally:closed.append(True)
        item=iterator();self.assertEqual(next(item),1);self.assertEqual(item.send(2),2)
        self.assertEqual(item.throw(ValueError()),3)
        with self.assertRaises(StopIteration) as ended:next(item)
        self.assertEqual(ended.exception.value,4)
        item=iterator();next(item);item.close()
        self.assertEqual(closed,[True,True])
        self.assertFalse(any(r['event']=='function.error' for r in self.records()))

    def test_ticket_job_and_solver_errors_survive_basic(self):
        case=self.root/'case';case.mkdir();(case/'Allrun').write_text('#!/bin/sh\nexit 0\n')
        atomic_json(self.root/'ticket.json',{'version':1,'case_dir':str(case)})
        store=Store(self.root/'state')
        job=store.enqueue({'_root':str(case),'name':'fixture'}, request_key='test-only')
        store.update_job(job['id'],status='cancelled')
        path=case/'log.solver';path.write_text('Time = 1\nWarning fixture\nFOAM FATAL ERROR fixture\n')
        state=read_log(path)
        self.assertEqual(state['time'],1);self.assertTrue(state['errors'])
        records=self.records();events={r['event'] for r in records}
        self.assertTrue({'ticket.file.replaced','job.persisted','db.transaction.commit','solver.output.warning','solver.output.error'} <= events)
        self.assertFalse(any(r.get('function')=='logs.feed' and r['event']=='function.call' for r in records))
        self.assertTrue(any(r.get('job_id')==job['id'] for r in records))

    def test_three_adapters_keep_business_boundaries_and_actor(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from cfd_bot.bot import Bot
        from cfd_bot.gui import TicketEditor
        from cfd_bot.web import WebApp
        # Actual adapter methods with UI/service dependencies replaced; no display or network.
        bot=SimpleNamespace(config={'telegram':{'allowed_user_ids':[10],'chat_ids':[20]}},
                            ticket_ui=SimpleNamespace(handle=lambda update:False), dispatch=Mock())
        update={'update_id':123,'message':{'from':{'id':10},'chat':{'id':20},'text':'/stat'}}
        Bot.handle(bot,update)
        bot.dispatch.assert_called_once_with(20,'status','123',10)
        gui=SimpleNamespace(confirm_switch=lambda:False)
        TicketEditor.new(gui)
        service=SimpleNamespace(new=Mock(return_value={'id':'draft'}))
        app=SimpleNamespace(service=service)
        self.assertEqual(WebApp.post(app,'/api/new',{'kind':'single'}),{'id':'draft'})
        records=self.records()
        calls={r.get('function'):r for r in records if r['event']=='function.call'}
        self.assertTrue({'bot.Bot.handle','gui.TicketEditor.new','web.WebApp.post'} <= calls.keys())
        self.assertEqual(calls['bot.Bot.handle']['initiator']['id'],d.actor_id(10))
        self.assertEqual(calls['gui.TicketEditor.new']['initiator']['platform'],'gui')
        self.assertTrue(any(r['event']=='ui.telegram.received' and r['update_id']==123 for r in records))

    def test_env_reload_children_and_off(self):
        os.environ['CFD_BOT_DIAGNOSTICS_LEVEL']='detailed'
        config={'diagnostic_logging':{'directory':str(self.root/'logs'),'level':'basic'}}
        d.configure(config,active=True)
        self.assertTrue(d.detailed)
        self.assertEqual(d.child_environment({})['CFD_BOT_DIAGNOSTICS_LEVEL'],'detailed')
        os.environ['CFD_BOT_DIAGNOSTICS_LEVEL']='invalid'
        d.configure(config,active=True)
        self.assertEqual(d.level,'basic');self.assertFalse(d.detailed)
        d.configure(config,active=False)
        self.assertFalse(d.enabled);self.assertFalse(d.detailed)

    def test_shell_basic_omits_only_normal_helper_records(self):
        helper=Path(__file__).resolve().parents[1]/'bin/ofps-diagnostics.sh'
        script='''source "$1"
_cfd_diag_write shell.call 0 '' cpu_count
_cfd_diag_write shell.return 0 '' cpu_count
_cfd_diag_write shell.return 7 '' cpu_count
_cfd_diag_write shell.call 0 '' scan_once
false | true
printf 'pipeline=%s\\n' "$?"
'''
        env=dict(os.environ,CFD_BOT_DIAGNOSTICS='1',CFD_BOT_DIAGNOSTICS_LEVEL='basic',CFD_BOT_DIAGNOSTICS_DIR=str(self.root/'shell'))
        result=subprocess.run(['bash','-o','pipefail','-c',script,'test',str(helper)],env=env,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(result.stdout,'pipeline=1\n')
        records=[r for p in (self.root/'shell').glob('*.jsonl') for r in d.read_records(p)]
        self.assertFalse(any(r['event']=='shell.error' and r.get('function','').startswith('_cfd_diag_') for r in records))
        helpers=[r for r in records if r.get('function')=='cpu_count']
        self.assertEqual([(r['event'],r['status']) for r in helpers],[('shell.return',7)])
        self.assertTrue(any(r['event']=='shell.call' and r.get('function')=='scan_once' for r in records))
        self.assertTrue(any(r['event']=='shell.error' and r['status']==1 for r in records))
