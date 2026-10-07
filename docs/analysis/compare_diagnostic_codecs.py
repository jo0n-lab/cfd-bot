"""Sequential, identical fixtures for previous ON/current OFF/current ON."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

root=Path(__file__).resolve().parents[2]
parser=argparse.ArgumentParser()
parser.add_argument('--previous-source',type=Path,required=True)
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--samples',type=int,default=5)
args=parser.parse_args()
results=[]
for ready in (False,True):
    for label,source,mode in [('v1',args.previous_source,'on'),('off',root,'off'),('v2',root,'on')]:
        command=[sys.executable,str(root/'docs/analysis/benchmark_diagnostics.py'),'--source',str(source),
                 '--mode',mode,'--members','921','--samples',str(args.samples)]
        if ready:command+=['--valid-cpu','--scenario','web_overview']
        result=json.loads(subprocess.check_output(command,text=True,cwd=root))
        result['codec']=label;results.append(result)
        args.output.write_text(json.dumps(results,indent=2)+'\n')
        print(label,'ready' if ready else 'missing',[(r['scenario'],round(r['p50_ms'],2)) for r in result['results']],flush=True)
