"""Deliver the exact 600/400 cross-midnight synthetic acceptance example."""
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from gift_registry.registration import RegistrationQueue
from gift_registry.frontend_server import RowStore
from gift_registry.task_day_export import TaskDayExports
from test_task_day_export import fixture,Offline

def main():
    with tempfile.TemporaryDirectory(prefix='prompt02-sample-') as folder:
        root=Path(folder);jobs=RegistrationQueue(root/'jobs',Offline());rows=RowStore(root/'rows.json')
        p=fixture(jobs,'synthetic-1000-cross-midnight',1000,'2026-11-09T20:00:00+08:00','2026-11-09T23:00:00+08:00',600)
        job=jobs._load(p)
        for n,item in enumerate(job['items'][600:],600):
            item.update(state='success',first_terminal_at='2026-11-10T04:00:00+08:00',completed_at='2026-11-10T04:00:00+08:00',
                        verified_at='2026-11-10T04:00:00+08:00',updated_at='2026-11-10T04:00:00+08:00')
            item['evidence']['verified_record_ids']=[f'synthetic-record-{n}']
        with jobs.lock:jobs._save(p,job)
        service=TaskDayExports(jobs,rows,root,clock=lambda:datetime.fromisoformat('2026-11-10T04:00:00+08:00'))
        file=service.publish('2026-11-09');destination=ROOT/'docs/samples';destination.mkdir(exist_ok=True,parents=True)
        (destination/file.name).write_bytes(file.read_bytes())
        (destination/'batch-day-example.json').write_text(json.dumps(dict(batch_id=job['id'],business_day='2026-11-09',
            created_at=job['created_at'],completed_2026_11_09=600,completed_2026_11_10=400,input_rows=1000,
            actual_submissions=1000,final_at='2026-11-10T04:00:00+08:00',source='deterministic synthetic fixture; no real orders'),indent=2),encoding='utf-8')
        jobs.close();print(f'Validated sample: {destination/file.name}')

if __name__=='__main__':main()
