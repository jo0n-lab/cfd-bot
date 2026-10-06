"""Isolated documentation benchmark: synthetic snapshots, temporary files/DB only.
Run from the repository root: python3 docs/analysis/benchmark_architecture.py
No Telegram calls, real ofps scan, scheduler or solver is started.
"""
from collections import Counter
from contextlib import ExitStack
import json
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from cfd_bot.bot import Bot
from cfd_bot.config import load_bot, load_case
from cfd_bot.storage import Store
from cfd_bot.ticket_run import TicketRunner
from cfd_bot.web import WebApp


def main():
    results=[]
    for count in (1, 10, 40):
        with tempfile.TemporaryDirectory(prefix='cfd-doc-benchmark-') as directory:
            root=Path(directory);(root/'tickets').mkdir()
            records={}
            for i in range(count):
                case=root/f'case-{i}';(case/'system').mkdir(parents=True)
                (case/'system/controlDict').write_text('startTime 0; stopAt endTime; endTime 10;\n')
                (case/'log.solver').write_text('Time = 1\nExecutionTime = 1 s  ClockTime = 1 s\nTime = 2\nExecutionTime = 2 s  ClockTime = 2 s\n')
                (root/'tickets'/f'alone-{i}.json').write_text(json.dumps(dict(version=1,case_dir=str(case),name=f'fixture-{i}',watcher={'logs':['log.solver']})))
                records[str(case)]={'root':str(case),'processes':[],'supervisors':[], 'actual_cores':1,'actual_cpu_list':'0','owner':'fixture'}
            (root/'bot.json').write_text(json.dumps(dict(version=1,state_dir='state',case_globs=['tickets/*.json'],ofps_command=['NEVER-RUN-DOC-FIXTURE'],scheduler={'enabled':False})))
            config=load_bot(root/'bot.json');store=Store(config['state_dir']);app=WebApp(config,store);bot=Bot(config,store,None)
            def scanner(*args):
                return {'raw':'synthetic fixture','cases':records.copy()}
            def stat():
                snap,runs,error=bot.fresh_runs()
                return bot.status(snap,runs,error)
            with ExitStack() as stack:
                for name in ('cfd_bot.bot.process_snapshot','cfd_bot.ticket_run.snapshot'):
                    stack.enter_context(patch(name,side_effect=scanner))
                stack.enter_context(patch('subprocess.run',side_effect=AssertionError('Real subprocess disabled')))
                stack.enter_context(patch('subprocess.Popen',side_effect=AssertionError('Worker disabled')))
                stack.enter_context(patch('urllib.request.urlopen',side_effect=AssertionError('Network disabled')))
                for name,operation in [('telegram_stat_backend',stat),('web_overview_backend',app.overview)]:
                    for _ in range(3):operation()
                    times=[]
                    for _ in range(20):
                        start=time.perf_counter();operation();times.append((time.perf_counter()-start)*1000)
                    counts=Counter()
                    functions={load_case.__code__:'load_case',Store.jobs.__code__:'Store.jobs',Store.get.__code__:'Store.get',Store.runtime_history.__code__:'Store.runtime_history',TicketRunner.state.__code__:'TicketRunner.state'}
                    def profile(frame,event,arg):
                        if event=='call' and frame.f_code in functions:counts[functions[frame.f_code]]+=1
                        if event=='c_call' and arg is sqlite3.connect:counts['sqlite3.connect']+=1
                    sys.setprofile(profile)
                    try:operation()
                    finally:sys.setprofile(None)
                    results.append(dict(path=name,cases=count,samples=20,p50_ms=round(statistics.median(times),3),p95_ms=round(sorted(times)[18],3),min_ms=round(min(times),3),max_ms=round(max(times),3),calls=dict(sorted(counts.items()))))
    document=dict(date='2026-10-04',python=platform.python_version(),platform=platform.system(),snapshot='synthetic; no subprocess or API',jobs=0,macro=0,log='2 short Time/ClockTime pairs per case',warmup=3,results=results)
    output=Path(__file__).with_name('benchmark-results.json');output.write_text(json.dumps(document,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(document,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
