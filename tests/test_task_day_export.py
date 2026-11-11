import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from gift_registry.business_day import business_day, CHINA
from gift_registry.frontend_server import RowStore
from gift_registry.registration import RegistrationQueue
from gift_registry.task_day_export import TaskDayExports, SourceChanged, build_report, summarize
from gift_registry.maintenance import MaintenanceService
from test_verification_lane import CATALOG, row

class Offline:
    configured=False

def fixture(jobs, name, n, created, completed, done=None):
    rows=[row(f'{name}-{i}',order='000'+str(1000000000000000000+i)) for i in range(n)]
    items=[]
    for i,r in enumerate(rows):
        item=jobs._item(r)
        terminal=done is None or i<done
        item.update(state='success' if terminal else 'verification_pending',state_version=1,
                    submit_attempted=True,attempts=1,submission_status='submitted',
                    evidence=dict(submit_clicked=True,verified_record_ids=[f'{name}-record-{i}'] if terminal else []),
                    started_at=created,submitted_at=created,updated_at=completed if terminal else created)
        if terminal: item.update(first_terminal_at=completed,completed_at=completed,verified_at=completed)
        items.append(item)
    job=dict(id=name,version=4,created_at=created,rows=rows,items=items,gift_catalog=CATALOG)
    path=jobs.directory/(name+'.json')
    with jobs.lock: jobs._save(path,job)
    return path

def cells(payload,sheet=4):
    with ZipFile(BytesIO(payload)) as archive:
        root=ET.fromstring(archive.read(f'xl/worksheets/sheet{sheet}.xml'))
        return [[next(iter(c)).text if c.attrib.get('t')=='n' else ''.join(c.itertext()) for c in r]
                for r in root.findall('.//{*}sheetData/{*}row')]

class TaskDayTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.jobs=RegistrationQueue(self.root/'jobs',Offline()); self.addCleanup(self.jobs.close)
        self.rows=RowStore(self.root/'rows.json'); self.clock=datetime.fromisoformat('2026-11-10T01:00:00+08:00')
        self.service=TaskDayExports(self.jobs,self.rows,self.root,clock=lambda:self.clock)

    def test_thousand_cross_midnight_final_and_independent_next_day(self):
        p=fixture(self.jobs,'original-1000',1000,'2026-11-09T20:00:00+08:00','2026-11-09T23:00:00+08:00',600)
        self.service.tick()
        self.assertFalse((self.service.exports/'gift-registration-2026-11-09.xlsx').exists())
        partial=self.service.publish('2026-11-09',partial=True)
        self.assertIn('partial',partial.name); self.assertEqual(len(cells(partial.read_bytes()))-1,1000)
        fixture(self.jobs,'new-day-still-running',4,'2026-11-10T03:00:00+08:00','2026-11-10T03:01:00+08:00',1)
        self.clock=datetime.fromisoformat('2026-11-10T04:00:00+08:00')
        with self.jobs.lock:
            job=self.jobs._load(p)
            for i,item in enumerate(job['items'][600:],600):
                item.update(state='success',completed_at=self.clock.isoformat(),first_terminal_at=self.clock.isoformat(),verified_at=self.clock.isoformat())
                item['evidence']['verified_record_ids']=[f'original-1000-record-{i}']
            self.jobs._save(p,job)
        self.service.tick(); final=self.service.exports/'gift-registration-2026-11-09.xlsx'
        self.assertTrue(final.exists()); rows=cells(final.read_bytes())
        self.assertEqual(len(rows)-1,1000); self.assertEqual(rows[1][4],'0001000000000000000000')
        self.assertTrue(all(r[0]=='2026-11-09' for r in rows[1:])); self.assertEqual(rows[-1][14],self.clock.isoformat())
        summary=cells(final.read_bytes(),3)[1]; self.assertEqual(summary[4:6],['1000','1000'])
        self.assertEqual(self.service.read_manifest('2026-11-09')['export_state'],'final')
        self.assertFalse((self.service.exports/'gift-registration-2026-11-10.xlsx').exists())
        self.assertEqual(next(d for d in self.service.inventory()['days'] if d['business_day']=='2026-11-10')['input'],4)
        oldhash=self.service.read_manifest('2026-11-09')['sha256']; self.service.tick()
        self.assertEqual(self.service.read_manifest('2026-11-09')['sha256'],oldhash)

    def test_same_day_second_batch_delays_final_but_batch_download_allowed(self):
        fixture(self.jobs,'finished',2,'2026-11-09T20:00:00+08:00','2026-11-09T21:00:00+08:00')
        fixture(self.jobs,'running',2,'2026-11-09T22:00:00+08:00','2026-11-09T23:00:00+08:00',1)
        self.assertIsNone(self.service.publish('2026-11-09'))
        file=self.service.publish('2026-11-09',batch_id='finished'); self.assertIn('batch-',file.name)
        self.assertEqual(len(cells(file.read_bytes()))-1,2)

    def test_active_next_day_changes_during_build_do_not_block_old_day(self):
        fixture(self.jobs,'old-done',2,'2026-11-09T20:00:00+08:00','2026-11-10T04:00:00+08:00')
        newer=fixture(self.jobs,'new-open',2,'2026-11-10T03:00:00+08:00','2026-11-10T03:00:00+08:00',1)
        def build(*args):
            with self.jobs.lock:
                job=self.jobs._load(newer); job['items'][1]['detail']='still working'; self.jobs._save(newer,job)
            return build_report(*args)
        with patch('gift_registry.task_day_export.build_report',side_effect=build):
            self.assertTrue(self.service.publish('2026-11-09').exists())

    def test_revision_same_second_and_manual_overlay_keep_platform_evidence(self):
        p=fixture(self.jobs,'first',2,'2026-11-09T20:00:00+08:00','2026-11-10T04:00:00+08:00')
        self.service.tick(); old=self.service.read_manifest('2026-11-09')
        job=self.jobs._load(p); item=job['items'][0]; item['review_result']=dict(success=True,at=self.clock.isoformat(),detail='review')
        item['reviewed_at']=self.clock.isoformat(); item['state_version']+=1
        with self.jobs.lock: self.jobs._save(p,job)
        self.service.tick(); new=self.service.read_manifest('2026-11-09')
        self.assertEqual(new['version'],old['version']+1); self.assertEqual(len(new['history']),2)
        self.assertTrue((self.service.exports/old['history_file']).exists()); self.assertEqual(summarize(job)['actual_submissions'],2)
        # A second change with exactly the same wall time must still revise.
        job['items'][1]['review_result']=dict(success=True,at=self.clock.isoformat(),detail='review')
        with self.jobs.lock: self.jobs._save(p,job)
        self.service.tick(); self.assertEqual(self.service.read_manifest('2026-11-09')['version'],3)

    def test_locked_destination_and_source_race_preserve_prior_report(self):
        p=fixture(self.jobs,'small',3,'2026-11-09T20:00:00+08:00','2026-11-09T21:00:00+08:00')
        file=self.service.publish('2026-11-09'); original=file.read_bytes()
        job=self.jobs._load(p); job['items'][0]['detail']='changed'
        with self.jobs.lock: self.jobs._save(p,job)
        from gift_registry.sessions import atomic_write
        def locked(path,payload):
            if Path(path)==file: raise PermissionError('Excel open')
            return atomic_write(path,payload)
        with patch('gift_registry.task_day_export.atomic_write',side_effect=locked):
            with self.assertRaises(PermissionError): self.service.publish('2026-11-09')
        self.assertEqual(file.read_bytes(),original); self.assertEqual(self.service.read_manifest('2026-11-09')['export_state'],'error')
        original_builder=build_report
        def racing(*args):
            payload=original_builder(*args)
            with self.jobs.lock:
                update=self.jobs._load(p); update['items'][1]['detail']='race'; self.jobs._save(p,update)
            return payload
        with patch('gift_registry.task_day_export.build_report',side_effect=racing):
            with self.assertRaises(SourceChanged): self.service.publish('2026-11-09')
        self.assertEqual(file.read_bytes(),original); self.assertEqual(self.service.read_manifest('2026-11-09')['export_state'],'needs_refresh')
        self.service.tick(); self.assertEqual(self.service.read_manifest('2026-11-09')['export_state'],'final')

    def test_legacy_dates_fixed_offset_and_unknown(self):
        self.assertEqual(business_day('2026-11-09T16:00:00+00:00'),'2026-11-10')
        self.assertEqual(business_day('2026-11-09T11:00:00-05:00'),'2026-11-10')
        for bad in (None,'','2026-11-09T20:00:00'):
            self.assertIsNone(business_day(bad))
        fixture(self.jobs,'unknown',2,'2026-11-09T20:00:00','2026-11-10T04:00:00+08:00')
        inventory=self.service.inventory(); self.assertEqual(inventory['days'],[])
        self.assertEqual(inventory['needs_manual_reconciliation'][0]['batch_id'],'unknown')

    def test_every_original_row_and_unknown_is_not_success(self):
        p=fixture(self.jobs,'mixed',7,'2026-11-09T20:00:00+08:00','2026-11-10T04:00:00+08:00')
        job=self.jobs._load(p)
        for item,state in zip(job['items'],['success','failed','skipped','cancelled','verification_pending','manual_required','success']):
            item['state']=state
            if state=='cancelled': item.update(submit_attempted=False,attempts=0,submission_status='not_submitted',cancelled_by='host',evidence={})
            if state!='success': item['evidence'].pop('verified_record_ids',None)
        job['items'][-1]['reused_result']=True
        with self.jobs.lock:self.jobs._save(p,job)
        file=self.service.publish('2026-11-09',partial=True)
        self.assertEqual(len(cells(file.read_bytes()))-1,7)
        self.assertEqual(len(cells(file.read_bytes(),1))-1,2)
        self.assertEqual(self.service.read_manifest('2026-11-09')['remaining'],1)

    def test_maintenance_ignores_busy_next_day_and_damaged_legacy_cursor(self):
        fixture(self.jobs,'old',1,'2026-11-09T20:00:00+08:00','2026-11-10T04:00:00+08:00')
        self.rows.path.with_name('maintenance.json').write_bytes(b'\0'*191)
        service=MaintenanceService(self.jobs,self.rows,self.root,clock=lambda:self.clock)
        with self.jobs.pending.all_tasks_done:self.jobs.pending.unfinished_tasks+=1
        try: service.tick()
        finally:self.jobs.pending.task_done()
        self.assertTrue((self.service.exports/'gift-registration-2026-11-09.xlsx').exists())
        self.assertEqual(service.path.read_bytes(),b'\0'*191)
        self.assertEqual(self.rows.read()['revision'],0)

if __name__=='__main__': unittest.main()
