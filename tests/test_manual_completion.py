"""人工演示提交的持久化防重；全部使用隔离目录和模拟平台。"""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch

from fakes import FakeOperations
from gift_registry.registration import RegistrationQueue, business_key, load_gifts, row_signature
from gift_registry.workflow import GiftRecord, OrderSnapshot, Recipient, SubmissionReceipt, request_from_row


def sample(identifier="manual-example"):
    return {"id": identifier, "order_no": "00009", "gift": "米家保温杯口袋版、小米小背包",
            "gift_ids": ["N01B0L1LE91VZS8S01", "N01B0J1JDK1V007H08"], "change_address": True,
            "recipient_name": "隔离测试姓名", "phone": "0013800000000", "address": "隔离测试地址"}


def snapshots(row=None):
    request = request_from_row(row or sample(), load_gifts())
    old = GiftRecord("old-00009", ("original-sku",))
    before = OrderSnapshot("00009", True, (old,))
    after = OrderSnapshot("00009", True,
                          (old, GiftRecord("new-00009", request.gift_ids, request.recipient,
                                           gift_quantities=(1, 1))))
    return before, after, SubmissionReceipt(True, created_record_ids=("new-00009",))


class ManualCompletionTests(unittest.TestCase):
    def test_equal_wall_clock_values_keep_explicit_start_order(self):
        operations = FakeOperations()
        operations.configured = False
        row = {**sample('same-clock-row'), 'change_address': False}
        later = {**row, 'order_no': '00010'}
        before, after, receipt = snapshots(row)
        with patch('gift_registry.registration.datetime', wraps=datetime) as clock, \
                patch('gift_registry.registration._last_now', None):
            clock.now.return_value = datetime(2026, 1, 1, tzinfo=timezone.utc)
            with RegistrationQueue(self.folder(), operations=operations) as jobs:
                jobs.record_external_submission({**row, 'id': 'same-clock-evidence'}, load_gifts(), before, after, receipt)
                jobs.start([row], load_gifts())
                first = jobs.states()[row['id']]['updated_at']
                jobs.start([later], load_gifts())
                self.assertEqual(jobs.states()[row['id']]['signature'], row_signature(later))
                second = jobs.states()[row['id']]['updated_at']
                self.assertGreater(datetime.fromisoformat(second), datetime.fromisoformat(first))
                jobs.start([row], load_gifts())
                self.assertEqual(jobs.states()[row['id']]['signature'], row_signature(row))
                self.assertIsNone(jobs.worker)
                self.assertEqual(operations.trace, [])

    def folder(self):
        folder = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(folder.cleanup)
        return Path(folder.name)

    def test_uncertain_manual_submission_is_durable_without_starting_worker(self):
        folder = self.folder()
        operations = FakeOperations()
        before, _, _ = snapshots()
        with RegistrationQueue(folder, operations=operations) as jobs:
            result = jobs.record_external_submission(sample(), load_gifts(), before, source_reference="bn-00001")
            self.assertEqual(result["state"], "failed")
            self.assertEqual(result["detail"], "已人工提交，结果待核查；禁止重复登记")
            self.assertTrue(result["submit_attempted"])
            self.assertEqual(result["attempts"], 1)
            self.assertEqual(result["source"], "manual")
            self.assertIsNone(jobs.worker)
            self.assertEqual(jobs.pending.unfinished_tasks, 0)
            self.assertEqual(operations.trace, [])
        path = folder / f"external-{business_key(sample())}.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(document["items"][0]["evidence"],
                         {"before_record_ids": ["old-00009"], "verified_record_ids": [],
                          "source_reference": "bn-00001"})

    def test_hold_can_be_recorded_even_if_baseline_is_unavailable(self):
        with RegistrationQueue(self.folder(), operations=FakeOperations()) as jobs:
            item = jobs.record_external_submission(sample(), load_gifts(), None)
            self.assertEqual(item["state"], "failed")
            self.assertTrue(item["submit_attempted"])
            self.assertEqual(item["evidence"]["before_record_ids"], [])

    def test_verified_manual_result_blocks_repeated_start_new_row_and_restart(self):
        folder = self.folder()
        operations = FakeOperations()
        before, after, receipt = snapshots()
        with RegistrationQueue(folder, operations=operations) as jobs:
            result = jobs.record_external_submission(sample(), load_gifts(), before, after, receipt, "bn-00001")
            self.assertEqual(result["state"], "success")
            self.assertEqual(result["evidence"]["verified_record_ids"], ["new-00009"])
            for _ in range(3):
                self.assertEqual(jobs.start([sample("draft-row")], load_gifts())["state"], "success")
            self.assertIsNone(jobs.worker)
        with RegistrationQueue(folder, operations=operations) as jobs:
            self.assertEqual(jobs.start([sample("copied-row")], load_gifts())["state"], "success")
            self.assertEqual(operations.trace, [])
            self.assertEqual(jobs.states()["copied-row"]["attempts"], 1)

    def test_manual_hold_blocks_duplicates_after_restart(self):
        folder = self.folder()
        operations = FakeOperations()
        before, _, _ = snapshots()
        with RegistrationQueue(folder, operations=operations) as jobs:
            jobs.record_external_submission(sample(), load_gifts(), before)
        with RegistrationQueue(folder, operations=operations) as jobs:
            for identifier in ("draft-row", "draft-row", "copied-row"):
                self.assertEqual(jobs.start([sample(identifier)], load_gifts())["state"], "failed")
            self.assertEqual(operations.trace, [])

    def test_upgrade_only_external_file_and_preserve_existing_jobs_and_draft(self):
        folder = self.folder()
        draft = folder / "draft.json"
        draft.write_text('{"revision":7,"rows":[]}', encoding="utf-8")
        saved_draft = draft.read_bytes()
        operations = FakeOperations()
        operations.configured = False
        before, after, receipt = snapshots()
        with RegistrationQueue(folder / "jobs", operations=operations) as jobs:
            jobs.start([sample("user-draft-row")], load_gifts())
            old_path = next((folder / "jobs").glob("*.json"))
            saved_job = old_path.read_bytes()
            jobs.record_external_submission(sample(), load_gifts(), before, source_reference="bn-00001")
            result = jobs.record_external_submission(sample(), load_gifts(), before, after, receipt)
            self.assertEqual(result["state"], "success")
            self.assertEqual(result["attempts"], 1)
            self.assertEqual(result["evidence"]["source_reference"], "bn-00001")
            self.assertEqual([event["state"] for event in result["events"]], ["failed", "success"])
            self.assertEqual(old_path.read_bytes(), saved_job)
            self.assertEqual(draft.read_bytes(), saved_draft)
            self.assertEqual(len(list((folder / "jobs").glob("external-*.json"))), 1)
            self.assertEqual(operations.trace, [])

    def test_success_is_idempotent_and_cannot_be_downgraded(self):
        folder = self.folder()
        before, after, receipt = snapshots()
        with RegistrationQueue(folder, operations=FakeOperations()) as jobs:
            initial = jobs.record_external_submission(sample(), load_gifts(), before, after, receipt, "bn-00001")
            path = next(folder.glob("external-*.json"))
            saved = path.read_bytes()
            for arguments in ((before,), (before, after, receipt)):
                self.assertEqual(jobs.record_external_submission(sample(), load_gifts(), *arguments), initial)
            self.assertEqual(path.read_bytes(), saved)

    def test_existing_matching_gifts_wrong_quantity_or_address_never_verify_success(self):
        before, after, _ = snapshots()
        scenarios = (
            before,
            OrderSnapshot("00009", True, before.records + (GiftRecord("new", ("wrong-sku",)),)),
            OrderSnapshot("00009", True, before.records +
                          (GiftRecord("new", after.records[-1].gift_ids, after.records[-1].recipient,
                                      gift_quantities=(2, 1)),)),
            OrderSnapshot("00009", True, before.records +
                          (GiftRecord("new", after.records[-1].gift_ids, Recipient("wrong", "wrong", "wrong")),)),
        )
        for observed in scenarios:
            with self.subTest(after=observed), RegistrationQueue(self.folder(), operations=FakeOperations()) as jobs:
                result = jobs.record_external_submission(sample(), load_gifts(), before, observed, SubmissionReceipt(True))
                self.assertEqual(result["state"], "failed")
                self.assertTrue(result["submit_attempted"])
                self.assertEqual(result["evidence"]["verified_record_ids"], [])

    def test_upgrade_rejects_changed_baseline(self):
        before, after, receipt = snapshots()
        with RegistrationQueue(self.folder(), operations=FakeOperations()) as jobs:
            jobs.record_external_submission(sample(), load_gifts(), before)
            with self.assertRaisesRegex(ValueError, "基线"):
                jobs.record_external_submission(sample(), load_gifts(), OrderSnapshot("00009", True), after, receipt)
            self.assertEqual(jobs.states()["manual-example"]["state"], "failed")

    def test_hidden_address_is_not_stored_in_manual_record(self):
        row = {**sample(), "change_address": False}
        folder = self.folder()
        with RegistrationQueue(folder, operations=FakeOperations()) as jobs:
            jobs.record_external_submission(row, load_gifts(), None)
        stored = next(folder.glob("external-*.json")).read_text(encoding="utf-8")
        for value in (row["recipient_name"], row["phone"], row["address"]):
            self.assertNotIn(value, stored)

    def test_manual_original_address_verification_requires_no_recipient_readback(self):
        row = {**sample(), "change_address": False}
        before, after, receipt = snapshots(row)
        before = OrderSnapshot(before.order_no, before.found, before.records,
                               Recipient("原姓名", "0013700000000", "原地址"))
        with RegistrationQueue(self.folder(), operations=FakeOperations()) as jobs:
            result = jobs.record_external_submission(row, load_gifts(), before, after, receipt)
            self.assertEqual(result["state"], "success")
            self.assertEqual(result["evidence"]["verified_record_ids"], ["new-00009"])
            self.assertIsNone(after.records[-1].recipient)

    def test_latest_result_uses_updated_time_not_filename_order(self):
        folder = self.folder()
        before, after, receipt = snapshots()
        with RegistrationQueue(folder, operations=FakeOperations()) as jobs:
            jobs.record_external_submission(sample(), load_gifts(), before)
            jobs.start([sample("older-copy")], load_gifts())
            older_path = next(path for path in folder.glob("*.json") if not path.name.startswith("external-"))
            older = json.loads(older_path.read_text(encoding="utf-8"))
            older["items"][0]["updated_at"] = "2000-01-01T00:00:00+00:00"
            older_path.unlink()
            (folder / "zzzz-older.json").write_text(json.dumps(older), encoding="utf-8")
            jobs.record_external_submission(sample(), load_gifts(), before, after, receipt)
            self.assertEqual(jobs.start([sample("latest-copy")], load_gifts())["state"], "success")

    def test_old_waiting_snapshot_displays_reused_success_after_later_row_revision(self):
        operations = FakeOperations()
        operations.configured = False
        row = {**sample("shared-row"), "change_address": False}
        manual = {**row, "id": "manual-evidence"}
        before, after, receipt = snapshots(row)
        with RegistrationQueue(self.folder(), operations=operations) as jobs:
            jobs.start([row], load_gifts())
            jobs.record_external_submission(manual, load_gifts(), before, after, receipt)
            jobs.start([{**row, "order_no": "00010"}], load_gifts())
            operations.configured = True
            jobs.start([row], load_gifts())
            jobs.pending.join()
            state = jobs.states()[row["id"]]
            self.assertEqual(state["signature"], row_signature(row))
            self.assertEqual(state["state"], "success")
            self.assertEqual(operations.trace, [])

    def test_explicit_start_of_old_terminal_snapshot_preserves_its_selected_signature(self):
        operations = FakeOperations()
        operations.configured = False
        row = {**sample("shared-row"), "change_address": False}
        before, after, receipt = snapshots(row)
        with RegistrationQueue(self.folder(), operations=operations) as jobs:
            jobs.record_external_submission({**row, "id": "manual-evidence"}, load_gifts(), before, after, receipt)
            jobs.start([row], load_gifts())
            later = {**row, "order_no": "00010"}
            jobs.start([later], load_gifts())
            self.assertEqual(jobs.states()[row["id"]]["signature"], row_signature(later))
            result = jobs.start([row], load_gifts())
            self.assertTrue(result["reused"])
            self.assertEqual(result["row_states"][row["id"]]["signature"], row_signature(row))
            self.assertEqual(result["row_states"][row["id"]]["state"], "success")
            self.assertIsNone(jobs.worker)
            self.assertEqual(operations.trace, [])

    def test_verified_manual_success_wins_over_later_crash_recovery_failure(self):
        folder = self.folder()
        operations = FakeOperations()
        operations.configured = False
        row = {**sample("waiting-row"), "change_address": False}
        before, after, receipt = snapshots(row)
        with RegistrationQueue(folder, operations=operations) as jobs:
            jobs.start([row], load_gifts())
            queued_path = next(folder.glob("*.json"))
            jobs.record_external_submission({**row, "id": "manual-evidence"}, load_gifts(), before, after, receipt)
            # 模拟 start 已写入 queued，进程尚未读取人工结果就中断的磁盘状态。
            document = json.loads(queued_path.read_text(encoding="utf-8"))
            document["items"][0]["state"] = "queued"
            queued_path.write_text(json.dumps(document), encoding="utf-8")
        operations.configured = True
        with RegistrationQueue(folder, operations=operations) as recovered:
            self.assertEqual(recovered.states()[row["id"]]["state"], "failed")
            copied = recovered.start([{**row, "id": "copied-row"}], load_gifts())
            self.assertEqual(copied["state"], "success")
            original = recovered.start([row], load_gifts())
            self.assertEqual(original["state"], "success")
            self.assertEqual(original["row_states"][row["id"]]["attempts"], 1)
            self.assertEqual(operations.trace, [])
            self.assertIsNone(recovered.worker)

    def test_success_without_valid_verified_record_ids_gets_no_priority_over_failure(self):
        for evidence in (None, {}, {"verified_record_ids": []}, {"verified_record_ids": "new"},
                         {"verified_record_ids": [""]}):
            with self.subTest(evidence=evidence):
                folder = self.folder()
                operations = FakeOperations()
                with RegistrationQueue(folder, operations=operations) as jobs:
                    jobs.record_external_submission(sample(), load_gifts(), None)
                    jobs.start([sample("unverified-copy")], load_gifts())
                    path = next(path for path in folder.glob("*.json") if not path.name.startswith("external-"))
                    document = json.loads(path.read_text(encoding="utf-8"))
                    document["items"][0].update(state="success", evidence=evidence,
                                                 updated_at="2000-01-01T00:00:00+00:00")
                    path.write_text(json.dumps(document), encoding="utf-8")
                    self.assertEqual(jobs.start([sample("new-copy")], load_gifts())["state"], "failed")
                    self.assertEqual(operations.trace, [])

    def test_inflight_closed_and_stopping_queues_reject_recording(self):
        started, release = threading.Event(), threading.Event()

        def pause(_):
            started.set()
            if not release.wait(5):
                raise TimeoutError("test synchronization")

        folder = self.folder()
        operations = FakeOperations(on_submit=pause)
        jobs = RegistrationQueue(folder, operations=operations)
        try:
            jobs.start([{**sample("active-row"), "change_address": False}], load_gifts())
            self.assertTrue(started.wait(5))
            with self.assertRaisesRegex(RuntimeError, "仍在执行"):
                jobs.record_external_submission(sample(), load_gifts(), None)
            self.assertEqual(list(folder.glob("external-*.json")), [])
        finally:
            release.set()
            jobs.pending.join()
            jobs.close()
        with self.assertRaisesRegex(RuntimeError, "已关闭"):
            jobs.record_external_submission(sample(), load_gifts(), None)
        with RegistrationQueue(folder, operations=operations) as jobs:
            jobs.stop_accepting()
            with self.assertRaisesRegex(ValueError, "正在关闭"):
                jobs.record_external_submission(sample(), load_gifts(), None)


if __name__ == "__main__":
    unittest.main()
