import json
import tempfile
import threading
import unittest
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
            p=next((Path(folder)/'verification_tasks').glob('*.json')); record=json.loads(p.read_text()); record['status']='running'; p.write_text(json.dumps(record))
            jobs=RegistrationQueue(folder,main,dual_verification=False)
            self.assertEqual(json.loads(p.read_text())['status'],'done'); self.assertEqual(jobs.states()['a']['state'],'success'); jobs.close()

    def test_serial_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            main=Main(); jobs=RegistrationQueue(folder,main,dual_verification=False)
            jobs.start([row('a'),row('b')],CATALOG); jobs.pending.join()
            self.assertEqual([v['state'] for v in jobs.states().values()],['success','success']); jobs.close()

if __name__=='__main__': unittest.main()
