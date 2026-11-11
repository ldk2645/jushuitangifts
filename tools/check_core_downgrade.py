"""Verify old main refuses to retry a synthetic, unmerged submission overlay."""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root), str(root/'tests')]
from test_core_safety import CATALOG, row
from fakes import FakeOperations
from gift_registry.registration import RegistrationQueue

parser=argparse.ArgumentParser(); parser.add_argument('--legacy-repo',required=True); args=parser.parse_args()
legacy=Path(args.legacy_repo).resolve()
with tempfile.TemporaryDirectory(prefix='core-downgrade-') as folder:
    ops=FakeOperations(); ops.configured=False
    with RegistrationQueue(folder,operations=ops) as jobs:
        jobs.start([row()], CATALOG); path=jobs._paths()[0]; job=jobs._load(path)
        job['items'][0].update(state='queued'); jobs._save(path,job)
        item=jobs._current_item(path,0)
        item.update(state='submitting',submit_attempted=True,attempts=1,submission_status='uncertain')
        jobs._save_item(path,job,0,item)
    script='''
import json,sys
sys.path[:0]=[sys.argv[1],sys.argv[1]+'/tests']
from gift_registry.registration import RegistrationQueue
from fakes import FakeOperations
ops=FakeOperations()
with RegistrationQueue(sys.argv[2],operations=ops) as jobs:
 assert all(not f['eligible'] for f in jobs.retry_candidates()['failures'])
 assert jobs.states()['r0']['state']=='failed'
 assert ops.trace==[]
print('PASS: original main blocks retry of unmerged synthetic submission; no platform calls.')
'''
    subprocess.run([sys.executable,'-c',script,str(legacy),folder],cwd=legacy,check=True)
