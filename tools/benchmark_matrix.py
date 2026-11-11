import json
import subprocess
import sys
import argparse
from pathlib import Path

root=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser();parser.add_argument('--label',choices=['before','after']); args=parser.parse_args()
for label, repo in [('before',root/'.tmp/core-baseline'),('after',root)]:
    if args.label and args.label != label: continue
    for scenario,sizes in [('execute',[100,500,1000,2000]),('retry',[100,500,1000])]:
        for size in sizes:
            output=root/f'docs/perf-{label}-{scenario}-{size}.json'
            command=[sys.executable,str(root/'tools/benchmark_execution.py'),'--repo',str(repo),
                     '--scenario',scenario,'--rows',str(size),'--output',str(output)]
            with (root/f'.tmp/perf-{label}-{scenario}-{size}.log').open('w',encoding='utf-8') as log:
                try:
                    done=subprocess.run(command,stdout=log,stderr=log,timeout=60 if label=='before' else 180)
                    status={'exit_code':done.returncode}
                except subprocess.TimeoutExpired:
                    status={'censored_after_seconds':60 if label=='before' else 180}
            if output.exists(): status={**json.loads(output.read_text(encoding='utf-8')),**status}
            output.write_text(json.dumps(status,indent=2),encoding='utf-8')
            print(label,scenario,size,status.get('complete',False),flush=True)
