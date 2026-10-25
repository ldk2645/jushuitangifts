"""缺少执行明细的旧快照恢复；仅显式模拟平台与临时任务目录。"""

import json
import tempfile
import unittest
from pathlib import Path

from fakes import FakeOperations
from gift_registry.registration import RegistrationQueue, load_gifts


ABSENT = object()


def sample(identifier="saved-row"):
    return {"id": identifier, "order_no": "00009", "gift": "米家保温杯口袋版、小米小背包",
            "gift_ids": ["N01B0L1LE91VZS8S01", "N01B0J1JDK1V007H08"], "change_address": False,
            "recipient_name": "", "phone": "", "address": ""}


def seed_snapshot(folder, *, version=ABSENT, state="waiting_for_workflow"):
    operations = FakeOperations()
    operations.configured = False
    with RegistrationQueue(folder, operations=operations) as jobs:
        jobs.start([sample()], load_gifts())
    path = next(Path(folder).glob("*.json"))
    document = json.loads(path.read_text(encoding="utf-8"))
    document.pop("items")
    if version is ABSENT:
        document.pop("version")
    else:
        document["version"] = version
    if state is ABSENT:
        document.pop("state")
    else:
        document["state"] = state
    document["updated_at"] = "2020-01-01T00:00:00+00:00"
    document["retained_metadata"] = "must-remain"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


class MissingExecutionDetailsTests(unittest.TestCase):
    def test_only_explicit_legacy_waiting_snapshots_can_resume_on_user_start(self):
        for version in (ABSENT, 1):
            with self.subTest(version=version), tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
                path = seed_snapshot(folder, version=version)
                saved = path.read_bytes()
                operations = FakeOperations()
                with RegistrationQueue(folder, operations=operations) as jobs:
                    self.assertEqual(jobs.states()["saved-row"]["state"], "waiting_for_workflow")
                    self.assertEqual(jobs.states()["saved-row"]["updated_at"], "2020-01-01T00:00:00+00:00")
                    self.assertEqual(path.read_bytes(), saved)
                    self.assertIsNone(jobs.worker)
                    self.assertEqual(operations.trace, [])
                    jobs.start([sample()], load_gifts())
                    jobs.pending.join()
                    self.assertEqual(jobs.states()["saved-row"]["state"], "success")
                    self.assertEqual(len(operations.submissions), 1)
                    jobs.start([sample("copied-row")], load_gifts())
                    self.assertEqual(len(operations.submissions), 1)
                    document = json.loads(path.read_text(encoding="utf-8"))
                    self.assertEqual(document["retained_metadata"], "must-remain")

    def assert_blocked_across_restart_and_copy(self, *, version, state):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path = seed_snapshot(folder, version=version, state=state)
            saved = path.read_bytes()
            operations = FakeOperations()
            with RegistrationQueue(folder, operations=operations) as jobs:
                for _ in range(2):
                    observed = jobs.states()["saved-row"]
                    self.assertEqual(observed["state"], "failed")
                    self.assertIn("历史提交状态无法确认", observed["detail"])
                    self.assertEqual(observed["updated_at"], "2020-01-01T00:00:00+00:00")
                self.assertEqual(path.read_bytes(), saved)
                self.assertIsNone(jobs.worker)
                for row in (sample(), sample(), sample("copied-row")):
                    self.assertEqual(jobs.start([row], load_gifts())["state"], "failed")
                self.assertEqual(operations.trace, [])
                self.assertIsNone(jobs.worker)
            with RegistrationQueue(folder, operations=operations) as restarted:
                for row in (sample(), sample("copied-row"), sample("another-copy")):
                    self.assertEqual(restarted.start([row], load_gifts())["state"], "failed")
                self.assertEqual(operations.trace, [])
                self.assertIsNone(restarted.worker)
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(document["retained_metadata"], "must-remain")
            self.assertEqual(document["rows"], [sample()])
            self.assertEqual(document["items"][0]["state"], "failed")
            self.assertEqual(document["items"][0]["attempts"], 0)
            self.assertNotIn("evidence", document["items"][0])

    def test_legacy_success_failed_and_active_snapshots_never_resubmit_without_details(self):
        for version in (ABSENT, 1):
            for state in ("success", "failed", "partial_failure", "queued", "querying", "submitting", "verifying"):
                with self.subTest(version=version, state=state):
                    self.assert_blocked_across_restart_and_copy(version=version, state=state)

    def test_version_two_including_waiting_is_not_treated_as_legacy_without_details(self):
        for state in ("waiting_for_workflow", "success", "failed", "queued", "verifying"):
            with self.subTest(state=state):
                self.assert_blocked_across_restart_and_copy(version=2, state=state)

    def test_unknown_or_missing_state_and_ambiguous_versions_fail_closed(self):
        for version, state in ((ABSENT, ABSENT), (1, None), (1, "unknown"),
                               (None, "waiting_for_workflow"), (True, "waiting_for_workflow"),
                               ("1", "waiting_for_workflow"), (3, "waiting_for_workflow")):
            with self.subTest(version=version, state=state):
                self.assert_blocked_across_restart_and_copy(version=version, state=state)


if __name__ == "__main__":
    unittest.main()
