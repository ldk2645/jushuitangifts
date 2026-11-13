"""Presentation evidence must not change registration outcomes or retry rules."""
import json
import tempfile
import unittest
from pathlib import Path

from fakes import FakeOperations
from gift_registry.registration import RegistrationQueue, submitted_to_banniu
from gift_registry.workflow import PlatformOperationError, RegistrationNotSubmittedError

CATALOG = [{'id': 'SYN-01', 'name': '合成礼品'}]
MODES = ['query_error', 'form_error', 'unknown_error', 'submit_timeout', 'no_new', 'verification_error', 'rejected', 'success']
ROWS = [dict(id=mode, order_no='SYN-'+mode, gift='合成礼品', gift_ids=['SYN-01'], change_address=False) for mode in MODES]
SUBMITTED = {'submit_timeout', 'no_new', 'verification_error', 'rejected', 'success'}


class TrackedOperations(FakeOperations):
    def __init__(self):
        super().__init__({'SYN-'+mode: mode for mode in MODES})

    def submit_registration_tracked(self, request, before, callback):
        mode = self.modes[request.order_no]
        if mode == 'form_error':
            raise RegistrationNotSubmittedError('合成填表失败，未点击提交')
        if mode == 'unknown_error':
            raise PlatformOperationError('合成提交前异常，无点击证据')
        callback()
        return self.submit_registration(request, before)


class PendingReviewTests(unittest.TestCase):
    def test_state_payload_distinguishes_click_from_attempt_without_mutating_history_or_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            ops = TrackedOperations()
            with RegistrationQueue(directory, operations=ops) as jobs:
                jobs.start(ROWS, CATALOG); jobs.pending.join()
                before = {p: p.read_bytes() for p in Path(directory).glob('*.json')}
                states = jobs.states()
                for mode in MODES:
                    self.assertEqual(states[mode]['submitted_to_banniu'], mode in SUBMITTED, mode)
                    self.assertEqual(states[mode]['state'], 'success' if mode == 'success' else 'failed')
                self.assertEqual(states['form_error']['attempts'], 1)
                self.assertEqual(states['unknown_error']['attempts'], 1)
                failures = {entry['order_no']: entry for entry in jobs.retry_candidates()['failures']}
                for mode in MODES[:-1]:
                    self.assertEqual(failures['SYN-'+mode]['submitted_to_banniu'], mode in SUBMITTED)
                    self.assertEqual(failures['SYN-'+mode]['eligible'], mode in {'query_error', 'form_error'})
                self.assertEqual(before, {p: p.read_bytes() for p in before})
                for data in before.values():
                    self.assertNotIn('submitted_to_banniu', data.decode())
                count = len(ops.submissions)
                jobs.start(ROWS, CATALOG); jobs.pending.join()
                self.assertEqual(len(ops.submissions), count)
            with RegistrationQueue(directory, operations=TrackedOperations()) as restarted:
                for mode in MODES:
                    self.assertEqual(restarted.states()[mode]['submitted_to_banniu'], mode in SUBMITTED)

    def test_old_verification_and_explicit_external_submission_have_evidence(self):
        self.assertTrue(submitted_to_banniu({'events': [{'state': 'verifying'}, {'state': 'failed'}]}))
        self.assertTrue(submitted_to_banniu({'evidence': {'receipt': {'accepted': True}}}))
        self.assertTrue(submitted_to_banniu({'source': 'manual', 'submit_attempted': True}))
        self.assertFalse(submitted_to_banniu({'attempts': 1, 'submit_attempted': True,
                                            'submission_status': 'uncertain', 'evidence': {'receipt': None}}))


if __name__ == '__main__':
    unittest.main()
