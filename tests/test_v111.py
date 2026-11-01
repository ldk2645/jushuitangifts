"""1.1.1 分组、延迟只读复核及凌晨归档，全部使用隔离数据与可控时钟。"""
import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from fakes import FakeOperations
from gift_registry.frontend_server import RowStore
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.gift_groups import GiftGroupStore, GroupConflict
from gift_registry.maintenance import MaintenanceService, CHINA
from gift_registry.registration import RegistrationQueue, row_signature, now
from gift_registry.workflow import GiftRecord, OrderSnapshot, PlatformOperationError
from test_host_management import CATALOG, row


class GroupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name)
        self.catalog_path = path / "gifts.json"
        self.catalog_path.write_text(json.dumps(CATALOG), encoding="utf-8")
        self.groups = GiftGroupStore(path / "groups.local.json", GiftCatalogStore(self.catalog_path))

    def test_defaults_do_not_guess_memberships_or_change_catalog(self):
        content = self.catalog_path.read_bytes()
        result = self.groups.read()
        self.assertEqual([g["name"] for g in result["groups"]], ["小米生态链", "小米手机"])
        self.assertEqual(result["memberships"], {})
        self.assertFalse(self.groups.path.exists())
        self.assertEqual(self.catalog_path.read_bytes(), content)

    def test_bulk_multi_membership_new_group_and_ungrouping_persist(self):
        data = self.groups.update({"revision": 0, "action": "create", "name": "未来分组"})
        ids = [g["id"] for g in data["groups"]]
        data = self.groups.update({"revision": 1, "action": "assign", "gift_ids": ["SKU-A", "SKU-B"], "group_ids": ids})
        self.assertEqual(data["memberships"], {"SKU-A": ids, "SKU-B": ids})
        self.groups.update({"revision": 2, "action": "assign", "gift_ids": ["SKU-A"], "group_ids": []})
        self.assertEqual(self.groups.read()["memberships"], {"SKU-B": ids})

    def test_stale_revision_invalid_groups_and_duplicate_names_do_not_write(self):
        self.groups.update({"revision": 0, "action": "create", "name": "未来"})
        content = self.groups.path.read_bytes()
        for body in ({"revision": 0, "action": "create", "name": "旧请求"},
                     {"revision": 1, "action": "create", "name": "小米手机"},
                     {"revision": 1, "action": "assign", "gift_ids": ["SKU-A"], "group_ids": ["unknown"]},
                     {"revision": 1, "action": "assign", "gift_ids": [123], "group_ids": []}):
            with self.assertRaises(ValueError):
                self.groups.update(body)
            self.assertEqual(self.groups.path.read_bytes(), content)


class IsolatedQueue(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.operations = FakeOperations()
        self.jobs = RegistrationQueue(self.folder / "jobs", operations=self.operations)

    def tearDown(self):
        self.jobs.pending.join()
        self.jobs.close()
        self.temp.cleanup()

    def fail(self, mode="no_new"):
        self.operations.modes[row()["order_no"]] = mode
        self.jobs.start([row()], CATALOG)
        self.jobs.pending.join()
        record = self.jobs._latest_attempts()[0]
        return datetime.fromisoformat(record["item"]["review_due_at"])


class AutoReviewTests(IsolatedQueue):
    def test_new_schedule_is_exactly_three_minutes_and_never_reset(self):
        fixed = '2026-10-30T10:00:00+08:00'
        item = {}
        with patch('gift_registry.registration.now', return_value=fixed):
            RegistrationQueue._schedule_review(item, row())
        self.assertEqual(datetime.fromisoformat(item['review_due_at']) - datetime.fromisoformat(fixed), timedelta(minutes=3))
        self.assertEqual(item['review_delay_seconds'], 180)
        original = deepcopy(item)
        RegistrationQueue._schedule_review(item, row())
        self.assertEqual(item, original)

    def test_legacy_ten_minute_pending_schedule_uses_three_minutes_without_replaying(self):
        due = self.fail()
        for path in self.jobs.directory.rglob('*.json'):
            document = json.loads(path.read_text(encoding='utf-8'))
            items = document.get('items', [document['item']] if 'item' in document else [])
            for item in items:
                if item.get('review_due_at'):
                    item.pop('review_delay_seconds', None)
                    item['review_due_at'] = (due + timedelta(minutes=7)).isoformat()
            path.write_text(json.dumps(document), encoding='utf-8')
        before = {p:p.read_bytes() for p in self.jobs.directory.rglob('*.json')}
        self.assertEqual(datetime.fromisoformat(self.jobs.states()['row-1']['review_due_at']), due)
        self.assertEqual({p:p.read_bytes() for p in before}, before)
        self.assertEqual(self.jobs.enqueue_due_reviews(due - timedelta(seconds=1)), 0)
        self.assertEqual(self.jobs.enqueue_due_reviews(due), 1)
        self.jobs.pending.join()
        self.assertEqual(self.jobs.enqueue_due_reviews(due + timedelta(days=1)), 0)
        self.assertEqual(len(self.operations.submissions), 1)

    def test_completed_review_is_not_replayed_when_claim_file_is_missing(self):
        due = self.fail()
        self.jobs.enqueue_due_reviews(due)
        self.jobs.pending.join()
        self.assertFalse(self.jobs.states()['row-1']['review_result']['success'])
        for path in (self.jobs.directory / 'auto_reviews').glob('*.json'):
            path.unlink()  # 仅临时测试目录，模拟旧备份缺失领取文件。
        trace = list(self.operations.trace)
        self.assertEqual(self.jobs.enqueue_due_reviews(due + timedelta(days=1)), 0)
        self.assertEqual(self.operations.trace, trace)

    def test_review_does_not_steal_row_from_newer_order_and_explicit_reselect_works(self):
        due = self.fail()
        current = row('ORDER-B', 'row-1')
        self.jobs.start([current], CATALOG)
        self.jobs.pending.join()
        self.assertEqual(self.jobs.states()['row-1']['signature'], row_signature(current))
        self.operations.modes.clear()
        self.jobs.enqueue_due_reviews(due)
        self.jobs.pending.join()
        self.assertEqual(self.jobs.states()['row-1']['signature'], row_signature(current))
        self.assertEqual(self.jobs.states()['row-1']['state'], 'success')
        self.jobs.start([row()], CATALOG)
        self.jobs.pending.join()
        self.assertEqual(self.jobs.states()['row-1']['signature'], row_signature(row()))
        self.assertEqual(self.jobs.states()['row-1']['state'], 'success')
        self.assertEqual(len(self.operations.submissions), 2)

    def test_legacy_failure_review_preserves_row_binding(self):
        due = self.fail()
        for path in self.jobs.directory.glob('*.json'):
            job = json.loads(path.read_text(encoding='utf-8'))
            for item in job['items']:
                item.pop('display_updated_at', None)
            path.write_text(json.dumps(job), encoding='utf-8')
        current = row('ORDER-B', 'row-1')
        self.jobs.start([current], CATALOG)
        self.jobs.pending.join()
        self.operations.modes.clear()
        self.jobs.enqueue_due_reviews(due)
        self.jobs.pending.join()
        self.assertEqual(self.jobs.states()['row-1']['signature'], row_signature(current))

    def test_interrupted_registration_schedules_one_read_after_recovery(self):
        record = row()
        item = self.jobs._item(record)
        item.update(state='verifying', attempts=1, submit_attempted=True, submission_status='submitted')
        self.jobs._save(self.jobs.directory / 'interrupted.json', {
            'id':'interrupted', 'version':3, 'created_at':now(),
            'rows':[record], 'items':[item], 'gift_catalog':CATALOG})
        self.jobs.close()
        self.jobs = RegistrationQueue(self.folder / 'jobs', operations=self.operations)
        state = self.jobs.states()['row-1']
        due = datetime.fromisoformat(state['review_due_at'])
        self.assertEqual(state['state'], 'failed')
        self.assertGreaterEqual((due - datetime.fromisoformat(state['updated_at'])).total_seconds(), 180)
        self.assertEqual(self.jobs.enqueue_due_reviews(due - timedelta(seconds=1)), 0)
        self.assertEqual(self.jobs.enqueue_due_reviews(due), 1)
        self.jobs.pending.join()
        self.assertEqual(self.operations.trace, [('query', record['order_no'])])
        self.assertEqual(self.jobs.enqueue_due_reviews(due + timedelta(days=1)), 0)
        self.assertEqual(self.jobs.states()['row-1']['state'], 'failed')  # 无基线不冒充成功

    def test_three_minutes_exactly_one_read_no_submit_copies_upgrade_and_daily_log_updates(self):
        due = self.fail()
        self.jobs.start([row(identifier="copy")], CATALOG)
        self.jobs.pending.join()
        self.assertEqual(self.jobs.enqueue_due_reviews(due - timedelta(seconds=1)), 0)
        trace = list(self.operations.trace)
        self.operations.modes.clear()
        self.assertEqual(self.jobs.enqueue_due_reviews(due), 1)
        self.jobs.pending.join()
        self.assertEqual(self.operations.trace[len(trace):], [("query", row()["order_no"])])
        self.assertEqual(len(self.operations.submissions), 1)
        self.assertEqual({s["state"] for s in self.jobs.states().values()}, {"success"})
        self.assertEqual(self.jobs.enqueue_due_reviews(due + timedelta(days=1)), 0)
        records = self.jobs.daily_records()[1]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["state"], "success")
        self.assertEqual(records[0]["internal_order_ids"], ["new-" + row()["order_no"]])
        self.jobs.close()
        self.operations = FakeOperations()
        self.jobs = RegistrationQueue(self.folder / "jobs", operations=self.operations)
        self.assertEqual(self.jobs.enqueue_due_reviews(due + timedelta(days=2)), 0)
        self.assertEqual(self.operations.trace, [])

    def test_mismatch_no_new_missing_and_query_exception_stay_failed_once(self):
        for mode in ("wrong_gift", "wrong_order", "no_new", "query_error", "missing"):
            with self.subTest(mode=mode):
                order = "000" + mode
                self.operations.modes[order] = "no_new"
                self.jobs.start([row(order, order)], CATALOG)
                self.jobs.pending.join()
                self.operations.modes[order] = mode
        due = max(datetime.fromisoformat(r["item"]["review_due_at"]) for r in self.jobs._latest_attempts())
        original = len(self.operations.submissions)
        self.assertEqual(self.jobs.enqueue_due_reviews(due), 5)
        self.jobs.pending.join()
        self.assertEqual({s["state"] for s in self.jobs.states().values()}, {"failed"})
        self.assertEqual(self.jobs.enqueue_due_reviews(due + timedelta(hours=1)), 0)
        self.assertEqual(len(self.operations.submissions), original)

    def test_native_click_evidence_allows_delayed_match_without_inventing_receipt(self):
        def tracked(request, before, callback):
            callback()
            return self.operations.submit_registration(request, before)
        self.operations.submit_registration_tracked = tracked
        due = self.fail("submit_timeout")
        self.operations.modes.clear()
        self.jobs.enqueue_due_reviews(due)
        self.jobs.pending.join()
        self.assertEqual(self.jobs.states()["row-1"]["state"], "success")
        self.assertEqual(len(self.operations.submissions), 1)

    def test_timeout_without_click_evidence_and_query_failure_never_upgrade_from_other_new_record(self):
        due = self.fail("submit_timeout")
        self.operations.modes.clear()
        self.jobs.enqueue_due_reviews(due)
        self.jobs.pending.join()
        self.assertEqual(self.jobs.states()["row-1"]["state"], "failed")
        self.assertIn("提交依据", self.jobs.states()["row-1"]["detail"])

    def test_rejected_receipt_cannot_be_overridden_by_click_marker(self):
        def tracked(request, before, callback):
            callback()
            return self.operations.submit_registration(request, before)
        self.operations.submit_registration_tracked = tracked
        due = self.fail("rejected")
        self.operations.modes.clear()
        self.jobs.enqueue_due_reviews(due)
        self.jobs.pending.join()
        self.assertEqual(self.jobs.states()["row-1"]["state"], "failed")

    def test_old_matching_gift_is_not_new_information(self):
        self.operations.query_order = lambda order: OrderSnapshot(order, True, (GiftRecord("old", ("SKU-A",)),))
        due = self.fail()
        self.jobs.enqueue_due_reviews(due)
        self.jobs.pending.join()
        self.assertEqual(self.jobs.states()["row-1"]["state"], "failed")

    def test_missing_baseline_is_read_once_but_never_marks_success(self):
        due = self.fail("query_error")
        self.operations.modes.clear()
        self.operations.query_order = lambda order: OrderSnapshot(order, True, (GiftRecord("new", ("SKU-A",)),))
        self.jobs.enqueue_due_reviews(due)
        self.jobs.pending.join()
        self.assertEqual(self.jobs.states()["row-1"]["state"], "failed")
        self.assertIn("完整原始基线", self.jobs.states()["row-1"]["detail"])

    def test_legacy_failures_without_schedule_do_not_run(self):
        due = self.fail()
        for path in self.jobs.directory.rglob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            for item in data.get("items", [data.get("item", {})]):
                item.pop("review_due_at", None)
            path.write_text(json.dumps(data), encoding="utf-8")
        trace = list(self.operations.trace)
        self.assertEqual(self.jobs.enqueue_due_reviews(due), 0)
        self.assertEqual(self.operations.trace, trace)

    def test_crashed_read_claim_is_not_replayed_after_restart(self):
        due = self.fail()
        identity = self.jobs._latest_attempts()[0]["failure_id"]
        path = self.jobs.directory / "auto_reviews" / (hashlib.sha256(identity.encode()).hexdigest() + ".json")
        path.parent.mkdir()
        path.write_text(json.dumps({"execution_id": identity, "status": "running"}), encoding="utf-8")
        self.jobs.close()
        self.operations = FakeOperations()
        self.jobs = RegistrationQueue(self.folder / "jobs", operations=self.operations)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "interrupted")
        self.assertEqual(self.jobs.enqueue_due_reviews(due), 0)
        self.assertEqual(self.operations.trace, [])

    def test_newer_manual_hold_for_other_gifts_does_not_hide_original_review(self):
        due = self.fail()
        self.jobs.record_external_submission(row(gift=1), CATALOG, None)
        self.assertEqual(self.jobs.enqueue_due_reviews(due), 1)


class NightlyTests(IsolatedQueue):
    def setUp(self):
        super().setUp()
        self.clock = datetime(2026, 10, 27, 0, 59, tzinfo=CHINA)
        self.store = RowStore(self.folder / "rows.json")
        self.service = MaintenanceService(self.jobs, self.store, self.folder, clock=lambda: self.clock)

    def complete(self, records, timestamp):
        with patch("gift_registry.registration.now", return_value=timestamp.isoformat()):
            self.jobs.start(records, CATALOG)
            self.jobs.pending.join()

    def test_export_then_clear_only_unchanged_verified_success_preserve_history_and_backup(self):
        success, failure = row(), row("000failed", "fail")
        self.operations.modes[failure["order_no"]] = "query_error"
        self.complete([success, failure], self.clock - timedelta(hours=3))
        self.operations.configured = False
        modified = {**success, "id": "other", "gift_ids": ["SKU-B"], "gift": "礼品乙"}
        self.store.write(0, [success, failure, modified])
        jobs_before = {p.name: p.read_bytes() for p in self.jobs.directory.glob("*.json")}
        self.service.tick()
        self.assertFalse(self.service.exports.exists())
        self.clock += timedelta(minutes=1)
        self.service.tick()
        output = self.service.exports / "gift-registration-2026-10-26.xlsx"
        self.assertTrue(output.exists())
        self.assertEqual(self.store.read()["rows"], [failure, modified])
        self.assertEqual(self.store.read()["revision"], 2)
        backup = json.loads((self.folder / "draft_archive/before-cleanup-2026-10-27.json").read_text(encoding="utf-8"))
        self.assertEqual(backup["rows"], [success, failure, modified])
        self.assertEqual(jobs_before, {p.name: p.read_bytes() for p in self.jobs.directory.glob("*.json")})
        self.service.tick()
        self.assertEqual(self.store.read()["revision"], 2)
        with ZipFile(output) as archive:
            self.assertEqual(len(ET.fromstring(archive.read("xl/worksheets/sheet1.xml")).findall('.//{*}row')), 2)
            self.assertEqual(len(ET.fromstring(archive.read("xl/worksheets/sheet2.xml")).findall('.//{*}row')), 2)

    def test_export_failure_preserves_table_and_retries_only_export(self):
        self.complete([row()], self.clock - timedelta(hours=3))
        self.store.write(0, [row()])
        self.service.tick()
        self.clock += timedelta(minutes=1)
        with patch.object(self.service, "_export", side_effect=OSError("test")):
            self.service.tick()
        self.assertEqual(self.store.read()["rows"], [row()])
        self.assertIn("OSError", self.service.status()["last_error"])
        self.service.tick()
        self.assertEqual(self.store.read()["rows"], [])
        self.assertEqual(len(self.operations.submissions), 1)

    def test_signature_changed_row_and_success_after_cutoff_are_preserved(self):
        self.complete([row()], self.clock - timedelta(hours=3))
        changed = {**row(), "gift_ids": ["SKU-B"], "gift": "礼品乙"}
        self.store.write(0, [changed])
        self.service.tick()
        self.clock += timedelta(hours=3)
        newer = row("000new", "new")
        self.complete([newer], self.clock)
        self.store.write(1, [changed, newer])
        self.service.tick()
        self.assertEqual(self.store.read()["rows"], [changed, newer])

    def test_first_enable_after_one_waits_until_next_night_restart_catches_up(self):
        self.clock = self.clock.replace(hour=15)
        self.service.tick()
        self.assertEqual(self.service.status()["next_run"], "2026-10-28T01:00:00+08:00")
        self.assertFalse(self.service.exports.exists())
        self.clock += timedelta(days=3)
        restarted = MaintenanceService(self.jobs, self.store, self.folder, clock=lambda: self.clock)
        for _ in range(3):
            restarted.tick()
        self.assertEqual(len(list(self.service.exports.glob("*.xlsx"))), 3)
        self.assertEqual(restarted.status()["next_run"], "2026-10-31T01:00:00+08:00")

    def test_busy_queue_defers_export_and_cleanup_until_idle(self):
        self.complete([row()], self.clock - timedelta(hours=3))
        self.store.write(0, [row()])
        self.service.tick()
        self.clock += timedelta(minutes=1)
        # 占用原队列的计数，但不给工作线程派发任何额外业务。
        with self.jobs.pending.all_tasks_done:
            self.jobs.pending.unfinished_tasks += 1
        try:
            self.service.tick()
            self.assertEqual(self.store.read()["rows"], [row()])
            self.assertFalse(self.service.exports.exists())
        finally:
            self.jobs.pending.task_done()
        self.service.tick()
        self.assertEqual(self.store.read()["rows"], [])

    def test_backup_failure_preserves_success_rows_and_history(self):
        self.complete([row()], self.clock - timedelta(hours=3))
        self.store.write(0, [row()])
        self.service.tick()
        self.clock += timedelta(minutes=1)
        from gift_registry.sessions import atomic_write
        def write(path, content):
            if "draft_archive" in Path(path).parts:
                raise OSError("isolated backup failure")
            return atomic_write(path, content)
        with patch("gift_registry.maintenance.atomic_write", side_effect=write):
            self.service.tick()
        self.assertTrue((self.service.exports / "gift-registration-2026-10-26.xlsx").exists())
        self.assertEqual(self.store.read()["rows"], [row()])
        self.assertEqual(self.store.read()["revision"], 1)
        self.assertEqual(self.jobs.states()["row-1"]["state"], "success")

    def test_late_review_refreshes_already_exported_original_day(self):
        self.operations.modes[row()["order_no"]] = "no_new"
        self.complete([row()], self.clock - timedelta(hours=3))
        self.operations.configured = False
        self.store.write(0, [row()])
        self.service.tick()
        self.clock += timedelta(minutes=1)
        self.service.tick()
        output = self.service.exports / "gift-registration-2026-10-26.xlsx"
        self.operations.configured = True
        self.operations.modes.clear()
        self.clock += timedelta(minutes=1)
        self.service.tick()
        self.jobs.pending.join()
        self.service.tick()
        with ZipFile(output) as archive:
            self.assertEqual(len(ET.fromstring(archive.read("xl/worksheets/sheet1.xml")).findall('.//{*}row')), 2)
            self.assertEqual(len(ET.fromstring(archive.read("xl/worksheets/sheet2.xml")).findall('.//{*}row')), 1)

    def test_cleanup_retry_backs_up_changed_draft_without_overwriting_first_backup(self):
        first, second = row('ORDER-1','one'), row('ORDER-2','two')
        self.complete([first, second], self.clock - timedelta(hours=3))
        self.store.write(0, [first])
        self.service.tick()
        self.clock += timedelta(minutes=1)
        with patch.object(self.store, 'write', side_effect=OSError('test')):
            self.service.tick()
        old_backup = self.folder / 'draft_archive/before-cleanup-2026-10-27.json'
        original = old_backup.read_bytes()
        self.store.write(1, [first, second])
        self.service.tick()
        self.assertEqual(self.store.read()['rows'], [])
        self.assertEqual(old_backup.read_bytes(), original)
        backups = [json.loads(p.read_text(encoding='utf-8')) for p in (self.folder / 'draft_archive').glob('*.json')]
        self.assertEqual(len(backups), 2)
        self.assertTrue(any(b['revision'] == 2 and b['rows'] == [first, second] for b in backups))


if __name__ == "__main__":
    unittest.main()
