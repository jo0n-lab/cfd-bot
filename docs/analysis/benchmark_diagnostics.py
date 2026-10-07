"""Baseline/OFF/ON benchmark. Temporary fixtures; no Telegram or solver execution.

python3 docs/analysis/benchmark_diagnostics.py --source PATH --mode on --members 921
Run each mode in a fresh interpreter, with the same --members and --samples.
"""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import resource
import statistics
import sys
import tempfile
import time
from unittest.mock import patch


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',required=True)
    parser.add_argument('--mode',choices=('baseline','off','on'),required=True)
    parser.add_argument('--members',type=int,default=40)
    parser.add_argument('--samples',type=int,default=20)
    args=parser.parse_args()
    os.environ['CFD_BOT_DIAGNOSTICS']='1' if args.mode=='on' else '0'
    sys.path.insert(0,args.source)
    from cfd_bot.bot import Bot
    from cfd_bot.catalog import ticket_index
    from cfd_bot.config import load_bot,cases_for
    from cfd_bot.logs import recent_log
    from cfd_bot.storage import Store
    from cfd_bot.web import WebApp
    with tempfile.TemporaryDirectory(prefix='cfd-diagnostic-bench-') as temp:
        root=Path(temp);folder=root/'tickets';folder.mkdir()
        records={}
        for i in range(args.members):
            case=root/f'case-{i}';(case/'system').mkdir(parents=True)
            (case/'system/controlDict').write_text('startTime 0; stopAt endTime; endTime 10;\n')
            (case/'log.solver').write_text('Time = 1\nExecutionTime = 1 s  ClockTime = 1 s\nTime = 2\nExecutionTime = 2 s  ClockTime = 2 s\n')
            (folder/f'alone-{i}.json').write_text(json.dumps(dict(version=1,case_dir=str(case),name=f'fixture-{i}',watcher={'logs':['log.solver']})))
            if i<40:
                records[str(case)]={'root':str(case),'processes':[],'supervisors':[], 'actual_cores':1,'actual_cpu_list':'0','owner':'fixture'}
        (root/'bot.json').write_text(json.dumps(dict(version=1,state_dir='state',case_globs=['tickets/*.json'],ofps_command=['NEVER-RUN'],scheduler={'enabled':False})))
        start=time.perf_counter();config=load_bot(root/'bot.json');store=Store(config['state_dir']);app=WebApp(config,store);bot=Bot(config,store,None)
        cold_ms=(time.perf_counter()-start)*1000
        def scanner(*args):return {'raw':'synthetic','cases':records.copy()}
        def stat():
            snap,runs,error=bot.fresh_runs();return bot.status(snap,runs,error)
        huge=root/'long.log';huge.write_text('Time = 1\nExecutionTime = 1 s  ClockTime = 1 s\n'*1000)
        result=[]
        with ExitStack() as stack:
            for name in ('cfd_bot.bot.process_snapshot','cfd_bot.ticket_run.snapshot'):
                stack.enter_context(patch(name,side_effect=scanner))
            stack.enter_context(patch('subprocess.Popen',side_effect=AssertionError('no worker')))
            stack.enter_context(patch('urllib.request.urlopen',side_effect=AssertionError('no network')))
            for name,operation in [('warm_catalog',lambda:cases_for(config)),('one_ticket',lambda:app.service.open('alone-0.json')),
                                   ('telegram_stat',stat),('web_overview',app.overview),('log_tail_2000_lines',lambda:recent_log(huge))]:
                for _ in range(3):operation()
                elapsed=[];cpu=time.process_time()
                for _ in range(args.samples):
                    start=time.perf_counter();operation();elapsed.append((time.perf_counter()-start)*1000)
                result.append(dict(scenario=name,p50_ms=statistics.median(elapsed),p95_ms=sorted(elapsed)[int(.95*(len(elapsed)-1))],cpu_ms=(time.process_time()-cpu)*1000,samples=args.samples))
        if args.mode!='baseline':
            from cfd_bot import diagnostics
            diagnostics.flush()
            count=diagnostics._sink.records if diagnostics._sink else 0
            errors=diagnostics._sink.errors if diagnostics._sink else 0
            written=diagnostics._sink.bytes_written if diagnostics._sink else 0
            diagnostics.close()
        else:count=errors=written=0
        print(json.dumps(dict(mode=args.mode,members=args.members,active=len(records),cold_ms=cold_ms,results=result,
                              rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,records=count,log_errors=errors,
                              bytes_written=written,
                              log_bytes=sum(p.stat().st_size for p in (root/'state/diagnostics').glob('*')),python=sys.version.split()[0]),indent=2))


if __name__=='__main__':main()
