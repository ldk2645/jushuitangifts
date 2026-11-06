"""Evidence regressions use synthetic data; no production queue or browser."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from gift_registry.registration import RegistrationQueue, row_signature, task_key
from gift_registry.workflow import (GiftRecord, OrderSnapshot, RegistrationRequest,
    SubmissionReceipt, PlatformOperationError, verify_new_records)

CATALOG = [{'id': 'SYN-01', 'name': '合成礼品'}]
ROW = {'id': 'synthetic-row', 'order_no': 'SYN-ORDER', 'gift': '合成礼品',
       'gift_ids': ['SYN-01'], 'change_address': False, 'recipient_name': '', 'phone': '', 'address': ''}


class ClickWithoutReceipt:
    configured = True
    def __init__(self):
        self.submits = 0
        self.queries = 0
    def query_order(self, order):
        self.queries += 1
        return OrderSnapshot(order, True, () if self.queries == 1 else (
            GiftRecord('OTHER-ACTORS-DOCUMENT', ('SYN-01',), gift_quantities=(1,)),))
    def submit_registration_tracked(self, request, before, callback):
        self.submits += 1
        callback()
        raise PlatformOperationError('合成回执超时')


class StatusEvidenceTests(unittest.TestCase):
    def test_missing_quantity_cannot_be_assumed_one(self):
        request = RegistrationRequest('SYN-ORDER', ('SYN-01',), ('合成礼品',))
        before = OrderSnapshot(request.order_no, True)
        after = OrderSnapshot(request.order_no, True, (GiftRecord('SYN-DOC', request.gift_ids),))
        try:
            success, _, _ = verify_new_records(request, before, after, SubmissionReceipt(True, created_record_ids=('SYN-DOC',)))
        except ValueError:
            success = False
        self.assertFalse(success, 'missing quantity must not become evidence of one item')

    def test_accepted_without_link_cannot_attribute_another_actors_new_record(self):
        request = RegistrationRequest('SYN-ORDER', ('SYN-01',), ('合成礼品',))
        before = OrderSnapshot(request.order_no, True)
        after = OrderSnapshot(request.order_no, True, (GiftRecord('SYN-DOC', request.gift_ids, gift_quantities=(1,)),))
        self.assertFalse(verify_new_records(request, before, after, SubmissionReceipt(True))[0])

    def test_delayed_review_without_receipt_remains_uncertain_and_never_resubmits(self):
        with tempfile.TemporaryDirectory() as tmp:
            ops = ClickWithoutReceipt()
            with RegistrationQueue(Path(tmp) / 'jobs', operations=ops) as jobs:
                jobs.start([ROW], CATALOG)
                jobs.pending.join()
                state = jobs.states()[ROW['id']]
                jobs.enqueue_due_reviews(datetime.fromisoformat(state['review_due_at']))
                jobs.pending.join()
                self.assertEqual(jobs.states()[ROW['id']]['state'], 'failed')
                self.assertEqual(ops.submits, 1)

    def test_display_rename_and_hidden_fields_do_not_change_row_binding(self):
        renamed = {**ROW, 'gift': '合成新名称', 'recipient_name': 'SYNTHETIC-IGNORED'}
        self.assertEqual(row_signature(ROW), row_signature(renamed))
        self.assertEqual(task_key(ROW), task_key(renamed))
        self.assertNotEqual(row_signature(ROW), row_signature({**ROW, 'gift_ids': ['OTHER-SKU']}))


if __name__ == '__main__':
    unittest.main()
