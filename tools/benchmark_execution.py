"""Synthetic execution / retry HTTP benchmark. --repo may point at an isolated main archive."""
import argparse
import json
import platform
import sys
import tempfile
import threading
import time
import tracemalloc
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack
import importlib.util

parser = argparse.ArgumentParser()
parser.add_argument('--repo', default=str(Path(__file__).resolve().parents[1]))
parser.add_argument('--rows', type=int, required=True)
parser.add_argument('--scenario', choices=['execute','retry'], required=True)
parser.add_argument('--output', required=True)
args = parser.parse_args()
sys.path[:0] = [args.repo, str(Path(args.repo)/'tests')]
from fakes import FakeOperations
from gift_registry.registration import RegistrationQueue, now
from gift_registry.frontend_server import RowStore, make_server
from gift_registry.gift_catalog import GiftCatalogStore
import gift_registry.registration as registration
try:
    import gift_registry.job_storage as storage
except ImportError:
    storage = None
try:
    import gift_registry.retry_batches as batches
except ImportError:
    batches = None
spec=importlib.util.spec_from_file_location('benchmark_metrics',Path(__file__).resolve().parents[1]/'gift_registry/queue_metrics.py')
timings=importlib.util.module_from_spec(spec); spec.loader.exec_module(timings)

class Ops(FakeOperations):
    def check_retry_safety(self, request, before, evidence): return True

counts = {'json_reads':0,'json_read_bytes':0,'json_writes':0,'json_write_bytes':0,'full_job_reads':0,'full_job_writes':0}
latencies = []; started = time.perf_counter(); finished = threading.Event()
result = {'environment': platform.platform(), 'python':sys.version, 'rows':args.rows,'scenario':args.scenario,'complete':False}
output = Path(args.output).resolve()
with tempfile.TemporaryDirectory(prefix='core-perf-') as folder:
    root = Path(folder); ops = Ops()
    jobs = RegistrationQueue(root/'jobs', operations=ops)
    if not hasattr(jobs.lock,'max_hold_seconds'): jobs.lock=timings.MeasuredRLock()
    rows = RowStore(root/'rows.json')
    catalog = GiftCatalogStore(root/'gifts.json')
    gifts = [{'id':'00001','name':'Synthetic','aliases':[]}]; catalog.path.write_text(json.dumps(gifts))
    data = [{'id':f'r{i}','order_no':f'SYNTHETIC-{i}','gift':'Synthetic','gift_ids':['00001'],'change_address':False} for i in range(args.rows)]
    rows.write(0,data)
    if args.scenario == 'retry':
        items = [jobs._item(row) for row in data]
        for item in items:
            item.update(state='failed',detail='Synthetic query failure',submission_status='not_submitted',
                        events=[{'state':'querying','at':now()},{'state':'failed','at':now()}])
        jobs._save(jobs.directory/'fixture.json', {'version':3,'id':'fixture','created_at':now(), 'rows':data, 'items':items,'gift_catalog':gifts})
        failure_ids = [r['failure_id'] for r in jobs.retry_candidates()['failures']]
    server = make_server(0, store=rows, queue=jobs, catalog=catalog)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}'
    token = json.load(urllib.request.urlopen(base+'/api/config'))['token']
    started = time.perf_counter()
    jobs.lock.max_hold_seconds = jobs.lock.max_wait_seconds = 0.0
    original_read = Path.read_text; original_write = registration.atomic_write
    def read(path,*a,**kw):
        value = original_read(path,*a,**kw)
        if jobs.directory in path.parents and path.suffix == '.json':
            counts['json_reads'] += 1; counts['json_read_bytes'] += len(value.encode())
            if path.parent == jobs.directory: counts['full_job_reads'] += 1
        return value
    def write(path, content):
        if jobs.directory in path.parents:
            counts['json_writes'] += 1; counts['json_write_bytes'] += len(content)
            if path.parent == jobs.directory: counts['full_job_writes'] += 1
        return original_write(path,content)
    def snapshot():
        result.update(counts, elapsed_seconds=time.perf_counter()-started,
                      submissions=len(ops.submissions), peak_python_bytes=tracemalloc.get_traced_memory()[1], http_ms=list(latencies))
        result['metrics'] = {'lock_max_wait_seconds':jobs.lock.max_wait_seconds,
                             'lock_max_hold_seconds':jobs.lock.max_hold_seconds,
                             'queue_max_wait_seconds':getattr(jobs.pending,'max_wait_seconds',None)}
        if latencies:
            ordered=sorted(latencies)
            result.update({p:ordered[min(len(ordered)-1,int(len(ordered)*q))] for p,q in [('p50',.5),('p95',.95),('p99',.99)]})
        output.write_text(json.dumps(result, indent=2),encoding='utf-8')
    def monitor():
        while not finished.wait(2):
            # Avoid acquiring the application's long lock while recording a timeout.
            result.update(counts, elapsed_seconds=time.perf_counter()-started, submissions=len(ops.submissions),
                          peak_python_bytes=tracemalloc.get_traced_memory()[1],
                          metrics={'lock_max_wait_seconds':jobs.lock.max_wait_seconds,'lock_max_hold_seconds':jobs.lock.max_hold_seconds})
            output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    tracemalloc.start(); threading.Thread(target=monitor,daemon=True).start()
    try:
        with patch.object(Path,'read_text',read), patch.object(registration,'atomic_write',write):
            with ExitStack() as stack:
                for module in [storage,batches]:
                    if module: stack.enter_context(patch.object(module,'atomic_write',write))
                if args.scenario == 'execute':
                    before=time.perf_counter(); jobs.start(data,gifts)
                    result['accept_ms']=(time.perf_counter()-before)*1000
                else:
                    payload=json.dumps({'failure_ids':failure_ids}).encode()
                    request=urllib.request.Request(base+'/api/registrations/retry-all',data=payload,
                        headers={'X-CSRF-Token':token,'Content-Type':'application/json'})
                    for _ in range(3):
                        before=time.perf_counter()
                        try:
                            response=json.load(urllib.request.urlopen(request,timeout=180))
                            result.setdefault('http_status',[]).append(200)
                        except urllib.error.HTTPError as exc:
                            response=json.load(exc)
                            result.setdefault('http_status',[]).append(exc.code)
                        latencies.append((time.perf_counter()-before)*1000)
                        result['http_ms'] = list(latencies)
                        if 'batch_id' in response: result['persistent_batch']=True
                jobs.pending.join()
                if args.scenario == 'execute':
                    for _ in range(20):
                        before=time.perf_counter(); urllib.request.urlopen(base+'/api/registrations/status').read()
                        latencies.append((time.perf_counter()-before)*1000)
                assert len(ops.submissions)==args.rows
                result['complete']=True
                snapshot()
    finally:
        finished.set(); tracemalloc.stop(); server.shutdown(); server.server_close()
