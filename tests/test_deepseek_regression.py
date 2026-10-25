"""DeepSeek 审查回归：持久化队列、防重复提交与新增记录核验。

全部测试只使用临时任务目录和显式注入的模拟平台，不启动浏览器、不接触
真实草稿、任务记录、登录配置或业务平台。
"""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from fakes import FakeOperations
from gift_registry.registration import RegistrationQueue, business_key, load_gifts
from gift_registry.workflow import (GiftRecord, OrderSnapshot, SubmissionReceipt,
                                    request_from_row, verify_new_records)

GIFTS = ("N01B0L1LE91VZS8S01", "N01B0J1JDK1V007H08")
GIFT_NAMES = {
    "N01B0L1LE91VZS8S01": "米家保温杯口袋版",
    "N01B0J1JDK1V007H08": "小米小背包",
}


def sample(identifier="row-1", order="00009", gift_ids=GIFTS,
           recipient="测试姓名", phone="0013800000000", address="测试地址"):
    identifiers = list(gift_ids)
    return {
        "id": identifier,
        "order_no": order,
        "gift": "、".join(GIFT_NAMES[identifier] for identifier in identifiers),
        "gift_ids": identifiers,
        "change_address": False,
        "recipient_name": recipient,
        "phone": phone,
        "address": address,
    }


def wait_finished(jobs, row_id, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = jobs.states().get(row_id, {})
        if state.get("state") in ("success", "failed"):
            return state
        time.sleep(0.01)
    raise AssertionError(f"{row_id} 没有在测试期限内结束")


class QueueReliabilityRegressionTests(unittest.TestCase):
    def test_duplicate_row_ids_are_rejected_before_any_query_or_submit(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            operations = FakeOperations()
            jobs = RegistrationQueue(folder, operations=operations)
            try:
                with self.assertRaisesRegex(ValueError, "行标识"):
                    jobs.start([sample("same"), sample("same")], load_gifts())
                self.assertEqual(operations.trace, [])
                self.assertEqual(list(Path(folder).glob("*.json")), [])
            finally:
                # 若防重校验被移除，也要等在途提交结束再释放目录，避免掩盖为清理错误。
                jobs.pending.join()
                jobs.close()

    def test_reordered_gifts_and_changed_hidden_address_reuse_verified_success(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            operations = FakeOperations()
            jobs = RegistrationQueue(folder, operations=operations)
            try:
                jobs.start([sample("first")], load_gifts())
                self.assertEqual(wait_finished(jobs, "first")["state"], "success")
                jobs.pending.join()
                second = sample("second", gift_ids=tuple(reversed(GIFTS)),
                                recipient="另一隐藏姓名", phone="0013900000000",
                                address="另一隐藏地址")
                result = jobs.start([second], load_gifts())
                self.assertEqual(result["state"], "success")
                self.assertEqual(jobs.states()["second"]["state"], "success")
                self.assertEqual(len(operations.submissions), 1)
                self.assertEqual(operations.queries["00009"], 2)
            finally:
                jobs.pending.join()
                jobs.close()

    def test_concurrent_repeated_start_batches_submit_once(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            operations = FakeOperations()
            jobs = RegistrationQueue(folder, operations=operations)
            barrier = threading.Barrier(6, timeout=5)
            errors = []

            def click_start():
                try:
                    barrier.wait()
                    jobs.start([sample("same")], load_gifts())
                except BaseException as exc:  # 测试线程内的失败要回传给主线程
                    errors.append(exc)

            threads = [threading.Thread(target=click_start) for _ in range(6)]
            try:
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                self.assertEqual(errors, [])
                self.assertEqual(wait_finished(jobs, "same")["state"], "success")
                jobs.pending.join()
                self.assertEqual(len(operations.submissions), 1)
                self.assertEqual(operations.queries["00009"], 2)
            finally:
                for thread in threads:
                    thread.join()
                jobs.pending.join()
                jobs.close()

    def test_submit_timeout_is_terminal_across_restart_and_new_row(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            operations = FakeOperations({"00009": "submit_timeout"})
            jobs = RegistrationQueue(folder, operations=operations)
            try:
                jobs.start([sample("first")], load_gifts())
                state = wait_finished(jobs, "first")
                self.assertEqual(state["state"], "failed")
                self.assertEqual(state["attempts"], 1)
            finally:
                jobs.pending.join()
                jobs.close()
            with RegistrationQueue(folder, operations=operations) as restarted:
                self.assertEqual(restarted.start([sample("first")], load_gifts())["state"], "failed")
                self.assertEqual(restarted.start([sample("copied")], load_gifts())["state"], "failed")
                self.assertEqual(restarted.states()["copied"]["attempts"], 1)
                self.assertEqual(len(operations.submissions), 1)
                self.assertEqual(operations.trace, [("query", "00009"), ("submit", "00009")])

    def test_failed_submit_marker_persistence_never_calls_platform_submit(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            operations = FakeOperations()
            jobs = RegistrationQueue(folder, operations=operations)
            original_save = jobs._save

            def failing_save(path, job):
                if any(item["state"] == "submitting" for item in job["items"]):
                    raise OSError("simulated storage failure")
                original_save(path, job)

            jobs._save = failing_save
            try:
                jobs.start([sample()], load_gifts())
                state = wait_finished(jobs, "row-1")
                self.assertEqual(state["state"], "failed")
                self.assertEqual(operations.submissions, [])
                self.assertEqual(operations.trace, [("query", "00009")])
                document = json.loads(next(Path(folder).glob("*.json")).read_text(encoding="utf-8"))
                self.assertFalse(document["items"][0]["submit_attempted"])
                self.assertEqual(document["items"][0]["attempts"], 0)
            finally:
                jobs.pending.join()
                jobs.close()

    def test_worker_processes_second_batch_only_after_first(self):
        started, release = threading.Event(), threading.Event()

        def pause_submission(_):
            started.set()
            if not release.wait(5):
                raise TimeoutError("test-only synchronization timeout")

        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            operations = FakeOperations(on_submit=pause_submission)
            jobs = RegistrationQueue(folder, operations=operations)
            try:
                jobs.start([sample("first")], load_gifts())
                self.assertTrue(started.wait(5))
                jobs.start([sample("second", order="00010")], load_gifts())
                # 第一个批次仍停在提交中时，第二个批次不能开始查询或提交。
                self.assertEqual(operations.trace, [("query", "00009"), ("submit", "00009")])
                self.assertEqual(len(operations.submissions), 1)
            finally:
                release.set()
                jobs.pending.join()
                jobs.close()
            self.assertEqual([request.order_no for request in operations.submissions],
                             ["00009", "00010"])
            self.assertEqual(jobs.states()["first"]["state"], "success")
            self.assertEqual(jobs.states()["second"]["state"], "success")

    def test_unverified_success_is_downgraded_and_blocks_new_row(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            seed_operations = FakeOperations()
            seed_operations.configured = False
            seed = RegistrationQueue(folder, operations=seed_operations)
            try:
                seed.start([sample("seed")], load_gifts())
                path = next(Path(folder).glob("*.json"))
                document = json.loads(path.read_text(encoding="utf-8"))
                # 模拟旧版本 / 损坏记录：只有点击成功状态，没有新增记录核验依据。
                document["items"][0].update(state="success", detail="点击成功",
                                             submit_attempted=True, attempts=1, evidence=None)
                path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            finally:
                seed.close()
            operations = FakeOperations()
            with RegistrationQueue(folder, operations=operations) as jobs:
                result = jobs.start([sample("copied")], load_gifts())
                self.assertEqual(result["state"], "failed")
                self.assertEqual(jobs.states()["seed"]["state"], "failed")
                self.assertIn("核验", jobs.states()["copied"]["detail"])
                self.assertEqual(operations.trace, [])

    def test_repeated_manual_hold_without_baseline_is_idempotent(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            operations = FakeOperations()
            baseline = OrderSnapshot("00009", True, (GiftRecord("old", ("original-sku",)),))
            with RegistrationQueue(folder, operations=operations) as jobs:
                first = jobs.record_external_submission(sample(), load_gifts(), baseline)
                second = jobs.record_external_submission(sample(), load_gifts(), None)
                self.assertEqual(second["state"], "failed")
                self.assertEqual(second["evidence"]["before_record_ids"], ["old"])
                self.assertEqual(first["attempts"], second["attempts"])
                self.assertEqual(operations.trace, [])

    def test_business_key_ignores_hidden_address_but_uses_explicit_address(self):
        base = sample()
        hidden = {**base, "recipient_name": "另一姓名", "phone": "0013900000000",
                  "address": "另一地址"}
        self.assertEqual(business_key(base), business_key(hidden))
        explicit = {**base, "change_address": True, "recipient_name": "收件人",
                    "phone": "0013800000000", "address": "地址 A"}
        changed = {**explicit, "address": "地址 B"}
        self.assertNotEqual(business_key(explicit), business_key(changed))


class VerificationRegressionTests(unittest.TestCase):
    def request(self):
        return request_from_row(sample(), load_gifts())

    def before(self):
        return OrderSnapshot("00009", True, (GiftRecord("old", ("original-sku",)),))

    def new_gift(self, record_id, gifts=GIFTS, reference="", quantities=(1, 1)):
        return GiftRecord(record_id, gifts, reference=reference, gift_quantities=quantities)

    def test_receipt_ids_must_cover_every_new_record(self):
        request = self.request()
        before = self.before()
        old = before.records[0]
        new = self.new_gift("new-1")
        success, _, identifiers = verify_new_records(
            request, before, OrderSnapshot("00009", True, (old, new)),
            SubmissionReceipt(True, created_record_ids=("new-1",)))
        self.assertTrue(success)
        self.assertEqual(identifiers, ["new-1"])

        extra = self.new_gift("new-2")
        success, detail, identifiers = verify_new_records(
            request, before, OrderSnapshot("00009", True, (old, new, extra)),
            SubmissionReceipt(True, created_record_ids=("new-1",)))
        self.assertFalse(success)
        self.assertIn("回执", detail)
        self.assertEqual(identifiers, [])

    def test_receipt_reference_must_cover_every_new_record(self):
        request = self.request()
        before = self.before()
        old = before.records[0]
        new = self.new_gift("new-1", reference="ref-1")
        success, _, identifiers = verify_new_records(
            request, before, OrderSnapshot("00009", True, (old, new)),
            SubmissionReceipt(True, "ref-1"))
        self.assertTrue(success)
        self.assertEqual(identifiers, ["new-1"])

        extra = self.new_gift("new-2", reference="ref-2")
        success, detail, identifiers = verify_new_records(
            request, before, OrderSnapshot("00009", True, (old, new, extra)),
            SubmissionReceipt(True, "ref-1"))
        self.assertFalse(success)
        self.assertIn("凭证", detail)
        self.assertEqual(identifiers, [])

    def test_missing_or_extra_gift_codes_fail_but_exact_set_succeeds(self):
        request = self.request()
        before = self.before()
        old = before.records[0]
        for gifts in ((GIFTS[0],), GIFTS + ("EXTRA-SKU",)):
            with self.subTest(gifts=gifts):
                record = self.new_gift("new", gifts=gifts, quantities=(1,) * len(gifts))
                success, detail, identifiers = verify_new_records(
                    request, before, OrderSnapshot("00009", True, (old, record)),
                    SubmissionReceipt(True))
                self.assertFalse(success)
                self.assertIn("礼品", detail)
                self.assertEqual(identifiers, [])
        record = self.new_gift("new")
        success, _, identifiers = verify_new_records(
            request, before, OrderSnapshot("00009", True, (old, record)),
            SubmissionReceipt(True))
        self.assertTrue(success)
        self.assertEqual(identifiers, ["new"])


if __name__ == "__main__":
    unittest.main()
