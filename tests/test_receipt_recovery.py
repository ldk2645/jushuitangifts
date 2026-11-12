"""Focused missing-receipt regressions; all writes are in temporary directories."""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

from fakes import FakeOperations
from test_host_management import CATALOG, row
from test_banniu_receipt import BEFORE, REQUEST, TASK, detail, task
from gift_registry.banniu_receipt import BanniuReceiptReader
from gift_registry.receipt_recovery import recovery_context
from gift_registry.registration import RegistrationQueue
from gift_registry.workflow import GiftRecord, OrderSnapshot, PlatformOperationError


class RecoveryTests(unittest.TestCase):
    def test_baseline_and_legacy_window_require_one_task_and_validate_full_detail(self):
        legacy = {'submission_status': 'uncertain', 'evidence': {'submit_clicked': True}, 'events': [
            {'state': 'submitting', 'at': '2026-10-23T10:00:01+08:00'},
            {'state': 'failed', 'at': '2026-10-23T10:00:30+08:00'}]}
        reader = BanniuReceiptReader(Mock())
        reader.query_tasks = Mock(return_value=[task()])
        reader.open_task = Mock()
        reader.page._wait.return_value = detail()
        for context in ({'previous_ids': []}, recovery_context(legacy)):
            result = reader.recover_receipt(REQUEST, BEFORE, context)
            self.assertEqual(result['receipt']['created_record_ids'], ('000049',))
            self.assertEqual(result['banniu_recovered_task_id'], TASK)
        for context in ({'previous_ids': [TASK]}, {'created_from': '2026-10-24T00:00:00',
                                                'created_to': '2026-10-24T00:05:00'}):
            with self.assertRaises(PlatformOperationError):
                reader.recover_receipt(REQUEST, BEFORE, context)
        reader.query_tasks.return_value = [task(), task(identifier='53')]
        with self.assertRaises(PlatformOperationError):
            reader.recover_receipt(REQUEST, BEFORE, {'previous_ids': []})
        reader.query_tasks.return_value = [task()]
        broken = detail()
        broken['fields']['订单号'] = 'OTHER'
        reader.page._wait.return_value = broken
        with self.assertRaises(PlatformOperationError):
            reader.recover_receipt(REQUEST, BEFORE, {'previous_ids': []})
        reader.page.submit.assert_not_called()

    def test_no_click_rejected_receipt_or_bad_baseline_cannot_be_adopted(self):
        for item in ({'evidence': {'banniu_before_task_ids': []}},
                     {'evidence': {'submit_clicked': True, 'receipt': {'accepted': False}}},
                     {'submission_status': 'not_submitted', 'evidence': {'submit_clicked': True}},
                     {'evidence': {'submit_clicked': True, 'banniu_before_task_ids': ['bad']}},
                     {'evidence': {'submit_clicked': True}, 'events': []}):
            with self.subTest(item=item), self.assertRaises(PlatformOperationError):
                recovery_context(item)

    def test_180_second_recovery_persists_success_in_both_lanes_without_resubmit(self):
        for dual in (False, True):
            with self.subTest(dual=dual), tempfile.TemporaryDirectory() as temp:
                ops = FakeOperations()
                verifier = Mock()
                verifier.recover_receipt.return_value = {
                    'receipt': {'accepted': True, 'created_record_ids': ['000049']},
                    'banniu_recovered_task_id': TASK}
                verifier.query_verification_order.return_value = OrderSnapshot(
                    row()['order_no'], True, (GiftRecord('000049', ()),))
                calls = []
                def submitted(request, before, callback):
                    calls.append(request.order_no)
                    callback({'banniu_before_task_ids': []})
                    raise PlatformOperationError('receipt not yet available')
                ops.submit_registration_tracked = submitted
                ops.recover_receipt = verifier.recover_receipt
                ops.query_verification_order = verifier.query_verification_order
                with RegistrationQueue(Path(temp)/'jobs', operations=ops, dual_verification=dual,
                                       verification_operations=verifier) as jobs:
                    jobs.start([row()], CATALOG)
                    jobs.pending.join()
                    item = jobs._latest_attempts()[0]['item']
                    due = datetime.fromisoformat(item['review_due_at'])
                    failed = datetime.fromisoformat(item['events'][-1]['at'])
                    self.assertLess(abs((due-failed).total_seconds()-180), 1)
                    self.assertEqual(jobs.enqueue_due_reviews(due-timedelta(seconds=1)), 0)
                    self.assertEqual(jobs.enqueue_due_reviews(due), 1)
                    if dual:
                        jobs.verification.join()
                    else:
                        jobs.pending.join()
                    self.assertEqual(jobs.states()[row()['id']]['state'], 'success')
                    saved = jobs._latest_attempts()[0]['item']
                    self.assertEqual(saved['evidence']['verified_record_ids'], ['000049'])
                    self.assertEqual(saved['evidence']['banniu_recovered_task_id'], TASK)
                    self.assertEqual(saved['submission_status'], 'submitted')
                    self.assertEqual(calls, [row()['order_no']])
                    self.assertEqual(jobs.enqueue_due_reviews(due+timedelta(days=1)), 0)
                    verifier.submit_registration.assert_not_called()
                with RegistrationQueue(Path(temp)/'jobs', operations=FakeOperations(), dual_verification=False) as jobs:
                    self.assertEqual(jobs.states()[row()['id']]['state'], 'success')


if __name__ == '__main__':
    unittest.main()
