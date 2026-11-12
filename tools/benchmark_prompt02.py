"""Fake lanes and XLSX only: no real platform/session access."""
import json
import platform
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
from datetime import datetime
from pathlib import Path
from urllib.request import build_opener, ProxyHandler

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from gift_registry.frontend_server import RowStore,make_server
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.registration import RegistrationQueue
from gift_registry.task_day_export import TaskDayExports
from gift_registry.workflow import OrderSnapshot,GiftRecord,SubmissionReceipt
from test_task_day_export import fixture,Offline
from test_verification_lane import row,CATALOG

class Meter:
    def __init__(self):
        self.lock=threading.Lock(); self.active=0; self.max_active=0; self.by_thread={}; self.submitted={}; self.owners={}
    def call(self,lane,delay):
        with self.lock:
            self.active+=1; self.max_active=max(self.active,self.max_active)
            self.by_thread[lane]=self.by_thread.get(lane,0)+1
            self.owners.setdefault(lane,set()).add(threading.get_ident())
        time.sleep(delay)
        with self.lock:self.active-=1

class Ops:
    configured=True
    def __init__(self,meter,lane):self.meter=meter; self.lane=lane
    def query_order(self,order):
        self.meter.call(self.lane,.002)
        if order in self.meter.submitted:
            return OrderSnapshot(order,True,(GiftRecord('R-'+order,('X',),reference='REF-'+order,gift_quantities=(1,)),))
        return OrderSnapshot(order,True)
    def wait_for_verification(self,request,before,receipt):
        self.meter.call(self.lane,.008)
        return self.query_order(request.order_no)

class Registration(Ops):
    def submit_registration_tracked(self,request,before,callback):
        self.meter.call(self.lane,.002)
        if request.order_no in self.meter.submitted:raise AssertionError('duplicate submit')
        self.meter.submitted[request.order_no]=1; callback()
        return SubmissionReceipt(True,'REF-'+request.order_no,('R-'+request.order_no,))

def lane_bench(size,dual):
    with tempfile.TemporaryDirectory(prefix='prompt02-lanes-') as folder:
        root=Path(folder); meter=Meter(); ops=Registration(meter,'registration'); verifier=Ops(meter,'verification')
        jobs=RegistrationQueue(root/'jobs',ops,dual_verification=dual,verification_operations=verifier)
        rows=RowStore(root/'rows.json'); records=[row(i) for i in range(size)]; rows.write(0,records)
        catalog=GiftCatalogStore(root/'gifts.json'); catalog.path.write_text(json.dumps(CATALOG))
        server=make_server(0,store=rows,queue=jobs,catalog=catalog); threading.Thread(target=server.serve_forever,daemon=True).start()
        url=f'http://127.0.0.1:{server.server_port}'; opener=build_opener(ProxyHandler({}))
        poll=[]; stop=threading.Event(); backlog=[]
        def monitor():
            while not stop.wait(.5):
                before=time.perf_counter()
                with opener.open(url+'/api/registrations/status',timeout=30) as response:response.read()
                poll.append((time.perf_counter()-before)*1000); backlog.append(jobs.verification.backlog())
        tracemalloc.start(); started=time.perf_counter(); thread=threading.Thread(target=monitor); thread.start()
        try:
            jobs.start(records,CATALOG); jobs.pending.join()
            if dual:jobs.verification.join()
            elapsed=time.perf_counter()-started
            states=jobs.states(); errors=sum(s['state']!='success' for s in states.values())
            assert errors==0 and len(meter.submitted)==size
            result=dict(rows=size,dual=dual,seconds=elapsed,errors=errors,submissions=len(meter.submitted),
                        peak_python_bytes=tracemalloc.get_traced_memory()[1],max_platform_concurrency=meter.max_active,
                        thread_count=len(set().union(*meter.owners.values())),max_backlog=max(backlog,default=0),
                        poll_ms=poll,metrics=jobs.health())
        finally:
            stop.set(); thread.join(35); tracemalloc.stop(); server.shutdown(); server.server_close()
            jobs.close()
        return result

def export_bench(size):
    with tempfile.TemporaryDirectory(prefix='prompt02-export-') as folder:
        root=Path(folder); jobs=RegistrationQueue(root/'jobs',Offline()); rows=RowStore(root/'rows.json')
        fixture(jobs,'synthetic-batch',size,'2026-11-09T20:00:00+08:00','2026-11-10T04:00:00+08:00')
        service=TaskDayExports(jobs,rows,root,clock=lambda:datetime.fromisoformat('2026-11-10T04:00:00+08:00'))
        jobs.lock.max_hold_seconds=0; tracemalloc.start(); started=time.perf_counter()
        file=service.publish('2026-11-09'); elapsed=time.perf_counter()-started
        peak=tracemalloc.get_traced_memory()[1]; tracemalloc.stop(); started=time.perf_counter(); service.inventory(); api=time.perf_counter()-started
        result=dict(rows=size,seconds=elapsed,peak_python_bytes=peak,lock_max_hold_seconds=jobs.lock.max_hold_seconds,
                    inventory_ms=api*1000,xlsx_bytes=file.stat().st_size)
        # Deliverable samples are created by generate_prompt02_sample.py;
        # benchmark reruns must not overwrite the reviewed 600/400 workbook.
        jobs.close(); return result

def main():
    output=ROOT/'docs'/'benchmark-prompt02.json'
    report=dict(environment=platform.platform(),python=sys.version,
                source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                simulated_query_ms=2,simulated_submit_ms=2,
                simulated_verify_wait_ms=8,lanes=[],exports=[])
    def save():output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    for size in (100,500,1000):
        for dual in (False,True):
            result=lane_bench(size,dual); report['lanes'].append(result);save();print(json.dumps({k:v for k,v in result.items() if k not in ('poll_ms','metrics')}),flush=True)
    for size in (100,500,1000,2000):
        result=export_bench(size); report['exports'].append(result);save();print(json.dumps(result),flush=True)

if __name__=='__main__':main()
