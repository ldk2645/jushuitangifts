import json
import tempfile
import threading
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from gift_registry.registration import RegistrationQueue
from gift_registry.workflow import OrderSnapshot, GiftRecord, SubmissionReceipt

CATALOG = [{'id':'X', 'name':'Gift'}, {'id':'Y', 'name':'Other'}]
def row(i, order=None, sku='X'):
    return dict(id=str(i), order_no=order or f'SYNTHETIC-{i}', gift_ids=[sku],
                gift='Gift' if sku=='X' else 'Other', change_address=False)

class Main:
    configured = True
    def __init__(self): self.submitted=[]; self.owners=set()
    def query_order(self, order):
        self.owners.add(threading.get_ident())
        if order in self.submitted:
            return OrderSnapshot(order, True, (GiftRecord('record-'+order, ('X',), reference='ref-'+order, gift_quantities=(1,)),))
        return OrderSnapshot(order, True)
    def submit_registration_tracked(self, request, before, on_submitted):
        self.owners.add(threading.get_ident()); self.submitted.append(request.order_no)
        on_submitted()
        return SubmissionReceipt(True, 'ref-'+request.order_no, ('record-'+request.order_no,))

class Review:
    def __init__(self, main, block=False, fail=False):
        self.main=main; self.entered=threading.Event(); self.release=threading.Event(); self.owners=set(); self.fail=fail
        if not block: self.release.set()
    def query_order(self, order):
        self.owners.add(threading.get_ident()); self.entered.set()
        if not self.release.wait(10): raise TimeoutError()
        if self.fail: raise TimeoutError('sensitive platform details')
        return OrderSnapshot(order, True, (GiftRecord('record-'+order, ('X',), reference='ref-'+order, gift_quantities=(1,)),))

class VerificationTests(unittest.TestCase):
    def test_parallel_different_order_blocks_same_order(self):
        with tempfile.TemporaryDirectory() as folder:
            main=Main(); review=Review(main, block=True)
            jobs=RegistrationQueue(folder, main, dual_verification=True, verification_operations=review)
            jobs.start([row('a','ORDER-A'),row('b','ORDER-B'),row('c','ORDER-A','Y')], CATALOG)
            self.assertTrue(review.entered.wait(5)); jobs.pending.join()
            self.assertEqual(main.submitted, ['ORDER-A','ORDER-B'])
            self.assertEqual(jobs.states()['c']['submission_status'], 'not_submitted')
            self.assertEqual(jobs.states()['a']['state'], 'verifying')
            review.release.set(); jobs.verification.join()
            self.assertEqual(jobs.states()['a']['state'],'success')
            self.assertTrue(main.owners.isdisjoint(review.owners)); jobs.close()

    def test_query_fault_retains_hold_and_never_resubmits(self):
        with tempfile.TemporaryDirectory() as folder:
            main=Main(); jobs=RegistrationQueue(folder,main,dual_verification=True,verification_operations=Review(main,fail=True))
            jobs.start([row('a')],CATALOG); jobs.pending.join(); jobs.verification.join()
            self.assertEqual(jobs.states()['a']['state'],'manual_required'); jobs.close()
            jobs=RegistrationQueue(folder,main,dual_verification=False)
            jobs.start([row('b','SYNTHETIC-a','Y')],CATALOG); jobs.pending.join()
            self.assertEqual(len(main.submitted),1); jobs.close()

    def test_durable_handoff_recovered_in_serial_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            main=Main(); review=Review(main)
            jobs=RegistrationQueue(folder,main,dual_verification=True,verification_operations=review)
            with patch.object(jobs.verification,'wake'):
                jobs.start([row('a')],CATALOG); jobs.pending.join()
            self.assertEqual(jobs.states()['a']['state'],'verification_pending')
            # Simulate process death without draining; only synthetic directory.
            jobs._directory_lock.close()
            jobs=RegistrationQueue(folder,main,dual_verification=False)
            jobs.pending.join(); self.assertEqual(jobs.states()['a']['state'],'success')
            self.assertEqual(len(main.submitted),1); jobs.close()

    def test_receipt_crash_never_replayed(self):
        with tempfile.TemporaryDirectory() as folder:
            main=Main()
            def broken(request,before,callback):
                main.submitted.append(request.order_no); callback(); raise RuntimeError('receipt lost')
            main.submit_registration_tracked=broken
            jobs=RegistrationQueue(folder,main,dual_verification=True,verification_operations=Review(main))
            jobs.start([row('a')],CATALOG); jobs.pending.join(); jobs.close()
            jobs=RegistrationQueue(folder,main,dual_verification=True,verification_operations=Review(main))
            jobs.start([row('a')],CATALOG); jobs.pending.join()
            self.assertEqual(len(main.submitted),1); self.assertNotEqual(jobs.states()['a']['state'],'success'); jobs.close()

    def test_result_committed_claim_crash_reconciles(self):
        with tempfile.TemporaryDirectory() as folder:
            main=Main(); jobs=RegistrationQueue(folder,main,dual_verification=True,verification_operations=Review(main))
            jobs.start([row('a')],CATALOG); jobs.pending.join(); jobs.verification.join(); jobs.close()
            p=next((Path(folder)/'verification_tasks').glob('*.json')); record=json.loads(p.read_text(encoding='utf-8')); record['status']='running'; p.write_text(json.dumps(record), encoding='utf-8')
            jobs=RegistrationQueue(folder,main,dual_verification=False)
            self.assertEqual(json.loads(p.read_text(encoding='utf-8'))['status'],'done'); self.assertEqual(jobs.states()['a']['state'],'success'); jobs.close()

    def test_serial_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            main=Main(); jobs=RegistrationQueue(folder,main,dual_verification=False)
            jobs.start([row('a'),row('b')],CATALOG); jobs.pending.join()
            self.assertEqual([v['state'] for v in jobs.states().values()],['success','success']); jobs.close()

    def test_delayed_claim_limit_does_not_overshoot_when_63_are_queued(self):
        with tempfile.TemporaryDirectory() as folder:
            main=Main();jobs=RegistrationQueue(folder,main,dual_verification=True,verification_operations=Review(main))
            directory=jobs.directory/'auto_reviews';directory.mkdir()
            for i in range(63):(directory/f'old-{i}.json').write_text(json.dumps(dict(execution_id=f'old-{i}',status='queued')))
            r=row('limit');item=jobs._item(r);stamp=datetime.now().astimezone().isoformat()
            item.update(state='failed',submission_status='submitted',review_due_at=stamp,review_delay_seconds=180,
                        evidence=dict(before_snapshot=asdict(OrderSnapshot(r['order_no'],True)),submit_clicked=True,
                                      receipt=asdict(SubmissionReceipt(True,'ref-'+r['order_no'],('record-'+r['order_no'],)))))
            records=[dict(failure_id=f'new-{i}',row=r,item=item,catalog=CATALOG,created_at=stamp,completed_at=stamp) for i in range(4)]
            with patch.object(jobs,'_latest_attempts',return_value=records),patch.object(jobs.verification,'wake'):
                self.assertEqual(jobs.enqueue_due_reviews(datetime.now().astimezone()+timedelta(seconds=1)),1)
                self.assertEqual(jobs.health()['review_claims']['queued'],64)
            # No execution exists for these synthetic scheduling-only records;
            # the worker marks them skipped without accessing either adapter.
            jobs.verification.wake();jobs.verification.join();jobs.close()

    def test_thousand_rows_bounded_backpressure_and_fair_delayed_review(self):
        with tempfile.TemporaryDirectory() as folder:
            main=Main(); review=Review(main,block=True); order=[]; original=review.query_order
            def query(identifier):
                order.append(identifier); return original(identifier)
            review.query_order=query
            jobs=RegistrationQueue(folder,main,dual_verification=True,verification_operations=review)
            jobs.verification.LIMIT=4
            seed=row('seed'); item=jobs._item(seed); stamp=datetime.now().astimezone().isoformat()
            item.update(state='failed',submit_attempted=True,attempts=1,submission_status='submitted',
                        evidence=dict(before_snapshot=asdict(OrderSnapshot(seed['order_no'],True)),submit_clicked=True,
                                      receipt=asdict(SubmissionReceipt(True,'ref-'+seed['order_no'],('record-'+seed['order_no'],)))),
                        review_due_at=stamp,review_delay_seconds=180)
            jobs._save(jobs.directory/'seed.json',dict(id='seed',created_at=stamp,rows=[seed],items=[item],gift_catalog=CATALOG))
            with patch.object(jobs.verification,'wake'):
                self.assertEqual(jobs.enqueue_due_reviews(datetime.now().astimezone()+timedelta(seconds=1)),1)
            observed=[]; handoff=jobs.verification.handoff; capacity_reached=threading.Event()
            def measured(*args):
                handoff(*args); observed.append(jobs.verification.backlog())
                if jobs.verification.backlog()==4: capacity_reached.set()
            jobs.verification.handoff=measured
            try:
                jobs.start([row(i) for i in range(1000)],CATALOG)
                self.assertTrue(review.entered.wait(5))
                self.assertTrue(capacity_reached.wait(5))
                # The primary can accept only LIMIT outstanding handoffs while
                # the verifier is held; it cannot build an unbounded queue.
                self.assertEqual(jobs.verification.backlog(),4)
                self.assertEqual(len(main.submitted),4)
                review.release.set(); jobs.pending.join(); jobs.verification.join()
                self.assertLessEqual(max(observed),4); self.assertEqual(len(main.submitted),1000)
                self.assertLessEqual(order.index(seed['order_no']),3)
                self.assertEqual(jobs.states()['seed']['state'],'success')
                states=jobs.states()
                self.assertTrue(all(states[str(i)]['state']=='success' for i in range(1000)))
            finally:
                review.release.set(); jobs.pending.join(); jobs.verification.join(); jobs.close()

    def test_corrupt_delayed_claim_stops_without_hanging_or_changing_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            main=Main(); jobs=RegistrationQueue(folder,main,dual_verification=True,verification_operations=Review(main))
            path=jobs.directory/'auto_reviews'/'damaged.json'; path.parent.mkdir()
            original=b'{incomplete'; path.write_bytes(original)
            try:
                jobs.verification.wake(); jobs.verification.thread.join(5)
                self.assertFalse(jobs.verification.thread.is_alive())
                with self.assertRaises(RuntimeError): jobs.verification.join()
                self.assertEqual(path.read_bytes(),original)
                self.assertEqual(main.submitted,[])
            finally:
                # Failed shutdown intentionally retains the production lock;
                # this isolated corruption fixture has no running worker.
                jobs._directory_lock.close()

    def test_handoff_before_item_write_crash_repaired_and_stale_completion_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            main=Main(); review=Review(main,block=True)
            jobs=RegistrationQueue(folder,main,dual_verification=True,verification_operations=review)
            with patch.object(jobs.verification,'wake'):
                jobs.start([row('a')],CATALOG);jobs.pending.join()
            path,record=jobs.verification.records()[0]; jp=jobs.directory/record['path']
            # Reconstruct the pre-projection crash boundary from durable data.
            job=jobs._load(jp); item=job['items'][0]; item['state']='submitting'; item['state_version']=record['state_version']-1
            with jobs.lock: jobs._save(jp,job)
            jobs._directory_lock.close()
            jobs=RegistrationQueue(folder,main,dual_verification=True,verification_operations=review)
            self.assertTrue(review.entered.wait(5))
            with jobs.lock:
                current=jobs._current_item(jp,0); current.update(state='manual_required',detail='new authoritative state')
                jobs._save_item(jp,jobs._load(jp),0,current)
            review.release.set();jobs.verification.join()
            self.assertEqual(jobs.states()['a']['state'],'manual_required')
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['status'],'interrupted');self.assertEqual(len(main.submitted),1);jobs.close()

if __name__=='__main__': unittest.main()
