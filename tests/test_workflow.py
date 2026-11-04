import json
import tempfile
import time
import unittest
from pathlib import Path

from fakes import FakeOperations
from gift_registry.registration import RegistrationQueue, load_gifts
from gift_registry.workflow import (GiftRecord, OrderSnapshot, PlatformOperationError, Recipient, RegistrationRequest,
                                    SubmissionReceipt, Workflow, request_from_row, validate_snapshot,
                                    verify_new_records)


def sample(order="00009", identifier="row-1"):
    return {"id": identifier, "order_no": order, "gift": "赠品保温杯、赠品小背包", "gift_ids": ["N00A090GEU0DXM6O02", "N00A0L1LE90DXM5001"],
            "change_address": False, "recipient_name": "测试姓名", "phone": "0013800000000", "address": "测试地址"}


class UnconfiguredOperations(FakeOperations):
    configured = False


def wait_finished(jobs, row_id="row-1"):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = jobs.states().get(row_id, {})
        if state.get("state") in ("success", "failed"):
            return state
        time.sleep(.01)
    raise AssertionError("任务没有在测试期限内结束")


class WorkflowTests(unittest.TestCase):
    def execute(self, mode):
        operations = FakeOperations({"00009": mode})
        events = []
        Workflow(operations).execute(sample(), load_gifts(), lambda state, **updates: events.append({"state": state, **updates}))
        return operations, events

    def test_success_requires_query_submit_and_second_query_in_order(self):
        operations, events = self.execute("success")
        self.assertEqual(operations.trace, [("query", "00009"), ("submit", "00009"), ("query", "00009")])
        self.assertEqual([event["state"] for event in events], ["querying", "submitting", "verifying", "success"])
        self.assertEqual(events[-1]["evidence"]["verified_record_ids"], ["new-00009"])
        self.assertTrue(events[1]["submit_attempted"])

    def test_no_new_information_and_mismatches_fail_without_resubmit(self):
        for mode in ("no_new", "wrong_gift", "wrong_order", "verification_error", "submit_timeout", "rejected"):
            with self.subTest(mode=mode):
                operations, events = self.execute(mode)
                self.assertEqual(events[-1]["state"], "failed")
                self.assertEqual(len(operations.submissions), 1)

    def test_missing_order_or_first_query_error_never_submits(self):
        for mode in ("missing", "query_error"):
            with self.subTest(mode=mode):
                operations, events = self.execute(mode)
                self.assertEqual(events[-1]["state"], "failed")
                self.assertEqual(operations.submissions, [])

    def test_retry_check_failure_has_its_own_stage_and_never_submits(self):
        for error in (PlatformOperationError('班牛任务列表未就绪'), RuntimeError('private detail')):
            with self.subTest(error=type(error).__name__):
                operations, events = FakeOperations(), []

                def check(request, before):
                    raise error

                Workflow(operations).execute(sample(), load_gifts(),
                    lambda state, **updates: events.append({'state': state, **updates}), before_submit=check)
                self.assertEqual(operations.trace, [('query', '00009')])
                self.assertEqual(events[-1]['state'], 'failed')
                self.assertIn('失败订单重登安全核查未通过', events[-1]['detail'])
                self.assertNotIn('聚水潭首次查询失败', events[-1]['detail'])
                self.assertNotIn('private detail', events[-1]['detail'])
                self.assertEqual(events[-1]['submission_status'], 'not_submitted')
                self.assertFalse(any(event.get('submit_attempted') for event in events))

    def test_hidden_address_is_not_sent_to_the_business_adapter(self):
        request = request_from_row({**sample(), "change_address": False}, load_gifts())
        self.assertIsNone(request.recipient)
        self.assertEqual(request.order_no, "00009")
        self.assertEqual(request.gift_ids, ("N00A090GEU0DXM6O02", "N00A0L1LE90DXM5001"))

    def test_normal_registration_does_not_compare_original_recipient_information(self):
        request = request_from_row({**sample(), "change_address": False}, load_gifts())
        original = Recipient("原收件人", "0013800000000", "原订单地址")
        before = OrderSnapshot("00009", True, (), recipient=original)
        after = OrderSnapshot("00009", True, (GiftRecord("new", request.gift_ids, original, gift_quantities=(1,) * len(request.gift_ids)),))
        self.assertTrue(verify_new_records(request, before, after, SubmissionReceipt(True, created_record_ids=("new",)))[0])
        wrong = OrderSnapshot("00009", True, (GiftRecord("new", request.gift_ids, Recipient("错误", "错误", "错误"), gift_quantities=(1,) * len(request.gift_ids)),))
        self.assertTrue(verify_new_records(request, before, wrong, SubmissionReceipt(True, created_record_ids=("new",)))[0])
        missing = OrderSnapshot("00009", True, (GiftRecord("new", request.gift_ids, gift_quantities=(1,) * len(request.gift_ids)),))
        self.assertTrue(verify_new_records(request, before, missing, SubmissionReceipt(True, created_record_ids=("new",)))[0])

    def test_normal_workflow_ignores_recipient_differences(self):
        operations, events = self.execute("wrong_address")
        self.assertEqual(events[-1]["state"], "success")
        self.assertEqual(len(operations.submissions), 1)
        self.assertIsNone(operations.submissions[0].recipient)

    def test_change_address_is_rejected_before_query_or_submit(self):
        operations = FakeOperations()
        events = []
        Workflow(operations).execute({**sample(), "change_address": True}, load_gifts(),
                                     lambda state, **updates: events.append({"state": state, **updates}))
        self.assertEqual([event["state"] for event in events], ["failed"])
        self.assertIn("本版本不支持改地址", events[0]["detail"])
        self.assertIn("未查询或提交", events[0]["detail"])
        self.assertEqual(operations.trace, [])
        self.assertNotIn("submit_attempted", events[0])

    def test_explicit_recipient_is_still_checked_for_historical_manual_verification(self):
        request = RegistrationRequest("00009", tuple(sample()["gift_ids"]), ("合成礼品甲", "合成礼品乙"), Recipient("合成姓名", "SYNTHETIC-PHONE", "合成地址"))
        before = OrderSnapshot("00009", True)
        after = OrderSnapshot("00009", True, (GiftRecord("new", request.gift_ids, request.recipient, gift_quantities=(1,) * len(request.gift_ids)),))
        self.assertTrue(verify_new_records(request, before, after, SubmissionReceipt(True, created_record_ids=("new",)))[0])
        missing = OrderSnapshot("00009", True, (GiftRecord("new", request.gift_ids, gift_quantities=(1,) * len(request.gift_ids)),))
        self.assertFalse(verify_new_records(request, before, missing, SubmissionReceipt(True, created_record_ids=("new",)))[0])

    def test_changed_existing_record_is_never_counted_as_new(self):
        request = request_from_row(sample(), load_gifts())
        before = OrderSnapshot(request.order_no, True, (GiftRecord("same-id", ("different-gift",), gift_quantities=(1,) * len(("different-gift",))),))
        after = OrderSnapshot(request.order_no, True, (GiftRecord("same-id", request.gift_ids, request.recipient, gift_quantities=(1,) * len(request.gift_ids)),))
        self.assertFalse(verify_new_records(request, before, after, SubmissionReceipt(True, created_record_ids=("new",)))[0])

    def test_excess_quantity_and_repeated_sku_across_new_records_are_rejected(self):
        request = request_from_row(sample(), load_gifts())
        before = OrderSnapshot(request.order_no, True)
        scenarios = (
            (GiftRecord("new", request.gift_ids, request.recipient, gift_quantities=(2, 1)),),
            (GiftRecord("new", request.gift_ids, request.recipient, gift_quantities=(1,) * len(request.gift_ids)),
             GiftRecord("duplicate", (request.gift_ids[0],), request.recipient, gift_quantities=(1,) * len((request.gift_ids[0],)))),
        )
        for records in scenarios:
            with self.subTest(records=records):
                after = OrderSnapshot(request.order_no, True, records)
                success, detail, identifiers = verify_new_records(request, before, after, SubmissionReceipt(True, created_record_ids=tuple(r.record_id for r in records)))
                self.assertFalse(success)
                self.assertIn("数量", detail)
                self.assertEqual(identifiers, [])

    def test_original_quantity_greater_than_one_does_not_count_against_new_gifts(self):
        request = request_from_row(sample(), load_gifts())
        original = GiftRecord("old", request.gift_ids, request.recipient, gift_quantities=(3, 2))
        before = OrderSnapshot(request.order_no, True, (original,))
        new = GiftRecord("new", request.gift_ids, request.recipient, gift_quantities=(1, 1))
        after = OrderSnapshot(request.order_no, True, (original, new))
        success, _, identifiers = verify_new_records(request, before, after, SubmissionReceipt(True, created_record_ids=("new",)))
        self.assertTrue(success)
        self.assertEqual(identifiers, ["new"])

    def test_populated_quantities_require_positive_integers_matching_every_sku(self):
        for quantities in ((0,), (-1,), (True,), (1.0,), ("1",), (1, 1), None, 1):
            with self.subTest(quantities=quantities):
                record = GiftRecord("old", ("sku",), gift_quantities=quantities)
                with self.assertRaises(ValueError):
                    validate_snapshot(OrderSnapshot("00009", True, (record,)), "00009")

    def test_record_ids_and_reference_must_both_match_when_both_are_supplied(self):
        request = request_from_row(sample(), load_gifts())
        before = OrderSnapshot(request.order_no, True)
        after = OrderSnapshot(request.order_no, True,
                              (GiftRecord("new", request.gift_ids, request.recipient, "other-reference", gift_quantities=(1,) * len(request.gift_ids)),))
        receipt = SubmissionReceipt(True, "expected-reference", ("new",))
        self.assertFalse(verify_new_records(request, before, after, receipt)[0])

    def test_all_receipt_record_ids_must_be_new_and_present(self):
        request = request_from_row(sample(), load_gifts())
        old = GiftRecord("old", request.gift_ids, request.recipient, gift_quantities=(1,) * len(request.gift_ids))
        before = OrderSnapshot(request.order_no, True, (old,))
        after = OrderSnapshot(request.order_no, True,
                              (old, GiftRecord("new", request.gift_ids, request.recipient, gift_quantities=(1,) * len(request.gift_ids))))
        for identifiers in (("old",), ("new", "missing"), ("old", "new")):
            with self.subTest(identifiers=identifiers):
                self.assertFalse(verify_new_records(request, before, after,
                                                   SubmissionReceipt(True, created_record_ids=identifiers))[0])

    def test_invalid_baseline_or_unaccepted_receipt_cannot_pass_direct_verification(self):
        request = request_from_row(sample(), load_gifts())
        after = OrderSnapshot(request.order_no, True, (GiftRecord("new", request.gift_ids, request.recipient, gift_quantities=(1,) * len(request.gift_ids)),))
        self.assertFalse(verify_new_records(request, OrderSnapshot(request.order_no, False), after,
                                           SubmissionReceipt(True, created_record_ids=("new",)))[0])
        self.assertFalse(verify_new_records(request, OrderSnapshot(request.order_no, True), after,
                                           SubmissionReceipt(False))[0])
        with self.assertRaises(ValueError):
            verify_new_records(request, OrderSnapshot("another-order", True), after, SubmissionReceipt(True, created_record_ids=("new",)))

    def test_snapshot_rejects_numeric_ids_codes_and_partial_recipient(self):
        invalid = (
            OrderSnapshot(9, True),
            OrderSnapshot("00009", 1),
            OrderSnapshot("00009", True, (GiftRecord(9, ("gift",), gift_quantities=(1,) * len(("gift",))),)),
            OrderSnapshot("00009", True, (GiftRecord("id", (9,), gift_quantities=(1,) * len((9,))),)),
            OrderSnapshot("00009", True, (GiftRecord("id", "gift", gift_quantities=(1,) * len("gift")),)),
            OrderSnapshot("00009", True, (GiftRecord("id", (), gift_quantities=(1,) * len(())),)),
            OrderSnapshot("00009", True, (GiftRecord("id", ("gift", "gift"), gift_quantities=(1,) * len(("gift", "gift"))),)),
            OrderSnapshot("00009", True, (GiftRecord("same", ("gift",), gift_quantities=(1,) * len(("gift",))), GiftRecord("same", ("gift",), gift_quantities=(1,) * len(("gift",))))),
            OrderSnapshot("00009", True, recipient=Recipient("姓名", 13800000000, "地址")),
            OrderSnapshot("00009", True, recipient=Recipient("姓名", "0013800000000", "")),
        )
        for snapshot in invalid:
            with self.subTest(snapshot=snapshot):
                with self.assertRaises(ValueError):
                    validate_snapshot(snapshot, "00009")

    def test_failed_persistence_of_submit_marker_prevents_platform_call(self):
        operations = FakeOperations()
        events = []

        def report(state, **updates):
            if state == "submitting":
                raise OSError("simulated storage failure")
            events.append({"state": state, **updates})

        Workflow(operations).execute(sample(), load_gifts(), report)
        self.assertEqual(operations.submissions, [])
        self.assertEqual(events[-1]["state"], "failed")

    def test_optional_bounded_verification_never_resubmits(self):
        class DelayedOperations(FakeOperations):
            def wait_for_verification(self, request, before, receipt):
                self.wait_calls = getattr(self, "wait_calls", 0) + 1
                # 隔离模拟最终仍未出现新增数据；只能回传失败，不能重走提交阶段。
                for _ in range(3):
                    last = self.query_order(request.order_no)
                return last

        operations = DelayedOperations({"00009": "no_new"})
        events = []
        Workflow(operations).execute(sample(), load_gifts(), lambda state, **updates: events.append({"state": state, **updates}))
        self.assertEqual(events[-1]["state"], "failed")
        self.assertEqual(operations.wait_calls, 1)
        self.assertEqual(operations.queries["00009"], 4)
        self.assertEqual(len(operations.submissions), 1)

    def test_only_authored_platform_errors_are_exposed_to_frontend(self):
        for error, expected in ((PlatformOperationError("请先完成聚水潭登录"), "请先完成聚水潭登录"),
                                (RuntimeError("secret-cookie-value"), "RuntimeError")):
            with self.subTest(error=type(error).__name__):
                class ErrorOperations(FakeOperations):
                    def query_order(self, order_no):
                        raise error

                events = []
                Workflow(ErrorOperations()).execute(sample(), load_gifts(),
                                                    lambda state, **updates: events.append({"state": state, **updates}))
                self.assertIn(expected, events[-1]["detail"])
                self.assertNotIn("secret-cookie-value", events[-1]["detail"])
                self.assertEqual(events[-1]["state"], "failed")


class DurableQueueTests(unittest.TestCase):
    def test_unsupported_address_request_is_rejected_across_new_row_and_restart(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            operations = FakeOperations()
            row = {**sample(), "change_address": True}
            with RegistrationQueue(folder, operations=operations) as jobs:
                with self.assertRaisesRegex(ValueError, '本版本不支持'):
                    jobs.start([row], load_gifts())
                self.assertEqual(jobs.states(), {})
            with RegistrationQueue(folder, operations=operations) as restarted:
                for record in (row, {**row, 'id': 'copied-row'}):
                    with self.assertRaises(ValueError): restarted.start([record], load_gifts())
                self.assertEqual(restarted.states(), {})
                self.assertEqual(operations.trace, [])

    def test_submit_attempt_is_durable_before_platform_call(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path = Path(folder)

            def inspect_before_submit(_):
                job = json.loads(next(path.glob("*.json")).read_text(encoding="utf-8"))
                item = job["items"][0]
                self.assertEqual(item["state"], "submitting")
                self.assertTrue(item["submit_attempted"])
                self.assertEqual(item["attempts"], 1)

            operations = FakeOperations(on_submit=inspect_before_submit)
            jobs = RegistrationQueue(path, operations=operations)
            jobs.start([sample()], load_gifts())
            self.assertEqual(wait_finished(jobs)["state"], "success")
            jobs.pending.join()
            jobs.close()

    def test_failure_is_not_retried_by_repeated_start_restart_or_new_row(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            operations = FakeOperations({"00009": "no_new"})
            jobs = RegistrationQueue(folder, operations=operations)
            jobs.start([sample()], load_gifts())
            self.assertEqual(wait_finished(jobs)["state"], "failed")
            for _ in range(3):
                jobs.start([sample()], load_gifts())
            jobs.pending.join()
            jobs.close()
            restarted = RegistrationQueue(folder, operations=operations)
            restarted.start([sample()], load_gifts())
            copied = sample(identifier="copied-row")
            restarted.start([copied], load_gifts())
            self.assertEqual(wait_finished(restarted, "copied-row")["state"], "failed")
            self.assertEqual(len(operations.submissions), 1)
            self.assertEqual(operations.queries["00009"], 2)
            restarted.pending.join()
            restarted.close()

    def test_duplicate_rows_register_once_and_other_orders_continue_after_failure(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            operations = FakeOperations({"00009": "no_new"})
            jobs = RegistrationQueue(folder, operations=operations)
            jobs.start([sample(), sample(identifier="copy"), sample("00010", "another-order")], load_gifts())
            self.assertEqual(wait_finished(jobs, "another-order")["state"], "success")
            self.assertEqual(jobs.states()["copy"]["state"], "failed")
            self.assertEqual([request.order_no for request in operations.submissions], ["00009", "00010"])
            jobs.pending.join()
            jobs.close()

    def test_interrupted_submission_is_failed_and_never_resumed(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            seed = RegistrationQueue(folder, operations=UnconfiguredOperations())
            seed.start([sample()], load_gifts())
            path = next(Path(folder).glob("*.json"))
            document = json.loads(path.read_text(encoding="utf-8"))
            document["items"][0].update(state="verifying", submit_attempted=True, attempts=1)
            path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            seed.close()
            operations = FakeOperations()
            recovered = RegistrationQueue(folder, operations=operations)
            self.assertEqual(recovered.states()["row-1"]["state"], "failed")
            recovered.start([sample()], load_gifts())
            self.assertEqual(operations.trace, [])
            recovered.close()

    def test_unconfigured_operations_wait_without_simulating_success(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            jobs = RegistrationQueue(folder, operations=UnconfiguredOperations())
            result = jobs.start([sample()], load_gifts())
            self.assertFalse(result["workflow_configured"])
            self.assertEqual(jobs.states()["row-1"]["state"], "waiting_for_workflow")
            self.assertIsNone(jobs.worker)
            jobs.close()

    def test_uncertain_prior_submission_blocks_duplicate_in_another_batch(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            jobs = RegistrationQueue(folder, operations=UnconfiguredOperations())
            jobs.start([sample()], load_gifts())
            path = next(Path(folder).glob("*.json"))
            document = json.loads(path.read_text(encoding="utf-8"))
            document["items"][0].update(state="submitting", submit_attempted=True, attempts=1)
            path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            operations = FakeOperations()
            jobs.operations = operations
            jobs.start([sample(identifier="new-row")], load_gifts())
            self.assertEqual(jobs.states()["new-row"]["state"], "failed")
            self.assertEqual(operations.submissions, [])
            jobs.pending.join()
            jobs.close()


if __name__ == "__main__":
    unittest.main()
