"""v1.1 主机重登、按订单防重与 Excel 导出的隔离回归测试。"""

import json
import tempfile
import threading
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from fakes import FakeOperations
from gift_registry.daily_export import export_daily, NS
from gift_registry.frontend_server import RowStore, make_server
from gift_registry.registration import RegistrationQueue
from gift_registry.workflow import RegistrationNotSubmittedError, PlatformOperationError
from test_network import SETTINGS, PASSWORD, authorization

CATALOG = [{"id": "SKU-A", "name": "礼品甲"}, {"id": "SKU-B", "name": "礼品乙"}]


def row(order="00012345678901234567890", identifier="row-1", gift=0):
    return {"id": identifier, "order_no": order, "gift": CATALOG[gift]["name"], "gift_ids": [CATALOG[gift]["id"]],
            "change_address": False}


class RetryOperations(FakeOperations):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.checks = []
        self.safe = True

    def check_retry_safety(self, request, before, evidence):
        self.checks.append((request, before, deepcopy(evidence)))
        if isinstance(self.safe, Exception):
            raise self.safe
        return self.safe


class HostQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "jobs"
        self.operations = RetryOperations()
        self.jobs = RegistrationQueue(self.directory, operations=self.operations)

    def tearDown(self):
        self.jobs.pending.join()
        self.jobs.close()
        self.temp.cleanup()

    def run_rows(self, rows):
        result = self.jobs.start(rows, CATALOG)
        self.jobs.pending.join()
        return result

    def failed(self, mode="query_error"):
        self.operations.modes[row()["order_no"]] = mode
        self.run_rows([row()])
        return self.jobs.retry_candidates()["failures"][0]

    def test_same_gifts_new_rows_and_whitespace_skip_successful_task(self):
        self.run_rows([row()])
        self.run_rows([row(" " + row()["order_no"] + " ", "new")])
        self.assertEqual(self.jobs.states()["new"]["state"], "success")
        self.assertEqual(len(self.operations.submissions), 1)
        self.assertEqual(len(self.operations.trace), 3)

    def test_overlapping_batches_with_same_gifts_still_submit_once(self):
        entered, release = threading.Event(), threading.Event()
        original_query = self.operations.query_order
        def query(order_no):
            entered.set()
            release.wait(5)
            return original_query(order_no)
        with patch.object(self.operations, "query_order", side_effect=query):
            self.jobs.start([row()], CATALOG)
            try:
                self.assertTrue(entered.wait(3))
                self.jobs.start([row(identifier="concurrent")], CATALOG)
            finally:
                release.set()
                self.jobs.pending.join()
        self.assertEqual(len(self.operations.submissions), 1)
        self.assertEqual(self.jobs.states()["concurrent"]["state"], "success")

    def test_legacy_unknown_failure_identity_is_stable_and_read_only(self):
        from gift_registry.registration import now
        path = self.directory / "legacy.json"
        content = json.dumps({"version": 2, "id": "legacy", "created_at": now(), "state": "failed",
                              "rows": [row()], "gift_catalog": CATALOG}).encode()
        path.write_bytes(content)
        first = self.jobs.retry_candidates()
        self.assertEqual(first, self.jobs.retry_candidates())
        self.assertFalse(first["failures"][0]["eligible"])
        self.assertEqual(path.read_bytes(), content)

    def test_restart_of_interrupted_retry_blocks_further_submission(self):
        from gift_registry.registration import now
        import hashlib
        failure = self.failed()
        self.jobs.close()
        item = self.jobs._item(row())
        item.update(state="submitting", retry_of=failure["failure_id"], submission_status="uncertain",
                    submit_attempted=True, attempts=1)
        path = self.directory / ("retry-" + hashlib.sha256(failure["failure_id"].encode()).hexdigest() + ".json")
        path.write_text(json.dumps({"version": 3, "id": "interrupted-retry", "created_at": now(),
                                   "rows": [row()], "gift_catalog": CATALOG, "items": [item]}), encoding="utf-8")
        self.operations = RetryOperations()
        self.jobs = RegistrationQueue(self.directory, operations=self.operations)
        latest = self.jobs.retry_candidates()["failures"][0]
        self.assertFalse(latest["eligible"])
        self.assertTrue(self.jobs.retry_failed(failure["failure_id"])["reused"])
        with self.assertRaises(ValueError):
            self.jobs.retry_failed(latest["failure_id"])
        self.run_rows([row(identifier="copy")])
        self.assertEqual(self.operations.trace, [])

    def test_same_gifts_skip_failed_task_even_after_restart(self):
        self.failed()
        self.jobs.close()
        self.operations = RetryOperations()
        self.jobs = RegistrationQueue(self.directory, operations=self.operations)
        self.run_rows([row(identifier="new")])
        self.assertEqual(self.jobs.states()["new"]["state"], "failed")
        self.assertEqual(self.operations.trace, [])

    def test_same_batch_duplicate_order_with_other_gifts_submits_twice(self):
        self.run_rows([row(), row(identifier="other", gift=1)])
        self.assertEqual(len(self.operations.submissions), 2)
        self.assertEqual(self.jobs.states()["other"]["state"], "success")

    def test_explicit_retry_is_idempotent_and_retains_original_failure(self):
        failure = self.failed()
        original_path = next(self.directory.glob("*.json"))
        original = original_path.read_bytes()
        self.operations.modes.clear()
        first = self.jobs.retry_failed(failure["failure_id"])
        again = self.jobs.retry_failed(failure["failure_id"])
        self.jobs.pending.join()
        self.assertEqual(first["task_id"], again["task_id"])
        self.assertTrue(again["reused"])
        self.assertEqual(original_path.read_bytes(), original)
        self.assertEqual(len(self.operations.checks), 1)
        self.assertEqual(len(self.operations.submissions), 1)
        self.assertEqual(self.jobs.states()["row-1"]["state"], "success")
        self.assertEqual(self.jobs.retry_candidates()["failures"], [])
        self.run_rows([row()])
        self.run_rows([row(identifier="copy")])
        day, records = self.jobs.daily_records()
        self.assertEqual(sorted(record["state"] for record in records), ["failed", "success"])
        self.assertEqual([record["internal_order_ids"] for record in records if record["state"] == "success"],
                         [["new-" + row()["order_no"]]])
        self.jobs.close()
        self.operations = RetryOperations()
        self.jobs = RegistrationQueue(self.directory, operations=self.operations)
        self.assertTrue(self.jobs.retry_failed(failure["failure_id"])["reused"])
        self.run_rows([row(identifier="restart")])
        self.assertEqual(self.operations.trace, [])
        self.assertEqual(len(self.jobs.daily_records()[1]), 2)

    def test_second_explicit_retry_requires_new_failure_identity(self):
        failure = self.failed()
        self.jobs.retry_failed(failure["failure_id"])
        self.jobs.pending.join()
        latest = self.jobs.retry_candidates()["failures"][0]
        self.assertNotEqual(failure["failure_id"], latest["failure_id"])
        self.assertTrue(self.jobs.retry_failed(failure["failure_id"])["reused"])
        self.operations.modes.clear()
        self.jobs.retry_failed(latest["failure_id"])
        self.jobs.pending.join()
        self.assertEqual(len(self.operations.submissions), 1)
        self.assertEqual(len(self.jobs.daily_records()[1]), 3)

    def test_attempted_uncertain_rejected_and_verification_failures_cannot_retry(self):
        for mode in ("submit_timeout", "rejected", "no_new", "wrong_gift", "verification_error"):
            order = "000" + mode
            self.operations.modes[order] = mode
            self.run_rows([row(order, mode)])
        failures = self.jobs.retry_candidates()["failures"]
        self.assertEqual(len(failures), 5)
        for failure in failures:
            self.assertFalse(failure["eligible"])
            with self.assertRaisesRegex(ValueError, "提交尝试"):
                self.jobs.retry_failed(failure["failure_id"])
        self.assertEqual(len(self.operations.submissions), 5)

    def test_structured_not_submitted_error_can_retry_with_original_baseline(self):
        def fail(*_):
            raise RegistrationNotSubmittedError("合成的填写失败")
        with patch.object(self.operations, "submit_registration", side_effect=fail):
            self.run_rows([row()])
        failure = self.jobs.retry_candidates()["failures"][0]
        self.assertTrue(failure["eligible"])
        self.jobs.retry_failed(failure["failure_id"])
        self.jobs.pending.join()
        self.assertEqual(self.operations.checks[0][2]["before_record_ids"], ["old-" + row()["order_no"]])
        self.assertEqual(len(self.operations.submissions), 1)

    def test_guard_failure_and_false_result_never_submit(self):
        failure = self.failed()
        self.operations.modes.clear()
        self.operations.safe = PlatformOperationError("班牛已有任务")
        self.jobs.retry_failed(failure["failure_id"])
        self.jobs.pending.join()
        failure = self.jobs.retry_candidates()["failures"][0]
        self.operations.safe = False
        self.jobs.retry_failed(failure["failure_id"])
        self.jobs.pending.join()
        self.assertEqual(self.operations.submissions, [])
        self.assertEqual(len(self.operations.checks), 2)

    def test_retry_guard_failure_keeps_baseline_for_next_explicit_retry(self):
        with patch.object(self.operations, "submit_registration", side_effect=RegistrationNotSubmittedError("未提交")):
            self.run_rows([row()])
        for _ in range(2):
            failure = self.jobs.retry_candidates()["failures"][0]
            self.operations.safe = PlatformOperationError("基线变化")
            self.jobs.retry_failed(failure["failure_id"])
            self.jobs.pending.join()
        self.assertEqual([check[2]["before_record_ids"] for check in self.operations.checks],
                         [["old-" + row()["order_no"]]] * 2)
        self.assertEqual(self.operations.submissions, [])

    def test_manual_submission_and_legacy_unknown_never_become_retryable(self):
        self.jobs.record_external_submission(row(), CATALOG, None)
        failure = self.jobs.retry_candidates()["failures"][0]
        self.assertFalse(failure["eligible"])
        with self.assertRaises(ValueError):
            self.jobs.retry_failed(failure["failure_id"])
        unknown = self.jobs._item(row("000legacy", "legacy"))
        unknown.update(state="failed")
        self.assertTrue(self.jobs._retry_reason(unknown))
        unknown.update(submit_attempted=True, attempts=1, detail="未提交")
        self.assertTrue(self.jobs._retry_reason(unknown))

    def test_busy_queue_accepts_retry_but_runs_it_serially_and_only_once(self):
        failure = self.failed()
        entered, release = threading.Event(), threading.Event()
        def blocked_query(order_no):
            entered.set()
            release.wait(5)
            raise PlatformOperationError("合成阻塞查询失败")
        with patch.object(self.operations, "query_order", side_effect=blocked_query):
            self.jobs.start([row("000busy", "busy")], CATALOG)
            try:
                self.assertTrue(entered.wait(3))
                result = self.jobs.retry_failed(failure["failure_id"])
                self.assertFalse(result["reused"])
                self.assertTrue(self.jobs.retry_failed(failure["failure_id"])["reused"])
                self.assertEqual(self.jobs.pending.unfinished_tasks, 2)
                for _ in range(3):
                    self.jobs.retry_candidates()
                    self.jobs.states()
                    self.jobs.daily_records()
                self.assertEqual(self.operations.submissions, [])
            finally:
                release.set()
                self.jobs.pending.join()

    def test_today_export_deduplicates_reused_results_and_uses_event_date(self):
        yesterday = datetime.now().astimezone() - timedelta(days=1)
        with patch("gift_registry.registration.now", side_effect=lambda: yesterday.isoformat()):
            self.run_rows([row()])
        self.run_rows([row(identifier="copy")])
        self.assertEqual(self.jobs.daily_records()[1], [])
        self.assertEqual(len(self.jobs.daily_records(yesterday.date())[1]), 1)

    def test_restored_success_remains_protected_from_different_gift_history(self):
        self.run_rows([row()])
        self.run_rows([row(identifier="copy", gift=1)])
        for _ in range(3):
            self.run_rows([row()])
            self.run_rows([row(identifier="copy", gift=1)])
        self.assertEqual(self.jobs.states()["row-1"]["state"], "success")
        self.assertEqual(len(self.operations.submissions), 2)


class ExcelExportTests(unittest.TestCase):
    def test_empty_workbook_has_two_sheets_and_long_ids_formulas_are_text(self):
        records = [{"state": "success", "order_no": "00012345678901234567890", "internal_order_ids": ["0000123", "0000456"],
                    "detail": "", "gift": "礼品", "completed_at": "2026-10-26T12:00:00+08:00", "execution_id": "abc"},
                   {"state": "failed", "order_no": "000002", "internal_order_ids": [], "detail": '=HYPERLINK("test")\x01',
                    "gift": "礼品", "completed_at": "2026-10-26T12:00:01+08:00", "execution_id": "def"}]
        for values in ([], records):
            with ZipFile(BytesIO(export_daily("2026-10-26", values))) as archive:
                for name in archive.namelist():
                    ET.fromstring(archive.read(name))
                first = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
                second = ET.fromstring(archive.read("xl/worksheets/sheet2.xml"))
                if values:
                    self.assertIn("00012345678901234567890", [el.text for el in first.iter(f"{{{NS}}}t")])
                    self.assertIn("0000123、0000456", [el.text for el in first.iter(f"{{{NS}}}t")])
                    self.assertIn('=HYPERLINK("test")', [el.text for el in second.iter(f"{{{NS}}}t")])
                for tree in (first, second):
                    self.assertEqual(list(tree.iter(f"{{{NS}}}f")), [])
                    self.assertTrue(all(cell.attrib["t"] == "inlineStr" for cell in tree.iter(f"{{{NS}}}c")))


class HostHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        folder = Path(self.temp.name)
        self.operations = RetryOperations({row()["order_no"]: "query_error"})
        self.jobs = RegistrationQueue(folder / "jobs", operations=self.operations)
        self.jobs.start([row()], CATALOG)
        self.jobs.pending.join()
        from gift_registry.gift_catalog import GiftCatalogStore
        catalog_path = folder / "gifts.json"
        catalog_path.write_text(json.dumps(CATALOG), encoding="utf-8")
        store = RowStore(folder / "rows.json")
        store.write(0, [row()])
        self.server = make_server(0, store=store, queue=self.jobs,
                                  network=SETTINGS, access_password=PASSWORD, catalog=GiftCatalogStore(catalog_path))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.jobs.pending.join()
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def request(self, path, body=None, headers=None):
        return self.opener.open(Request(self.url + path, data=None if body is None else json.dumps(body).encode(),
                                        headers=headers or {}), timeout=5)

    def test_host_can_list_retry_and_download_but_post_requires_csrf(self):
        with self.request("/api/config") as response:
            config = json.load(response)
        self.assertEqual(config["version"], "1.3.0")
        self.assertTrue(config["host_actions_available"])
        with self.request("/api/registrations/failures") as response:
            failure = json.load(response)["failures"][0]
        with self.assertRaises(HTTPError) as denied:
            self.request("/api/registrations/retry", {"failure_id": failure["failure_id"]})
        self.assertEqual(denied.exception.code, 403)
        self.operations.modes.clear()
        with self.request("/api/registrations/retry", {"failure_id": failure["failure_id"]},
                          {"X-CSRF-Token": config["token"], "Origin": self.url}) as response:
            self.assertFalse(json.load(response)["reused"])
        self.jobs.pending.join()
        with self.request("/api/registrations/export-today") as response:
            self.assertIn(".xlsx", response.headers["Content-Disposition"])
            self.assertTrue(response.read().startswith(b"PK"))

    def test_domain_and_lan_peer_cannot_access_host_actions_even_with_password_and_forwarded_headers(self):
        with self.request("/api/config") as response:
            token = json.load(response)["token"]
        for use_domain in (True, False):
            headers = {"Authorization": authorization(), "X-CSRF-Token": token,
                       "X-Forwarded-For": "127.0.0.1", "X-Forwarded-Host": self.url[7:]}
            if use_domain:
                headers["Host"] = f"{SETTINGS.domain}:{self.server.server_port}"
            with patch.object(self.server.RequestHandlerClass, "is_local_client", return_value=use_domain):
                with self.request("/api/config", headers=headers) as response:
                    self.assertFalse(json.load(response)["host_actions_available"])
                for path, body in (("/failures", None), ("/export-today", None), ("/retry", {"failure_id": "fake"})):
                    with self.assertRaises(HTTPError) as denied:
                        self.request("/api/registrations" + path, body, headers)
                    self.assertEqual(denied.exception.code, 403)
        self.assertEqual(self.operations.submissions, [])

    def test_groups_are_readable_on_lan_but_only_host_can_mutate_with_csrf(self):
        with self.request("/api/config") as response:
            token = json.load(response)["token"]
        body = {"revision": 0, "action": "assign", "gift_ids": ["SKU-A"], "group_ids": ["xiaomi-phone"]}
        def put(headers):
            return self.opener.open(Request(self.url + "/api/gift-groups", method="PUT",
                                           data=json.dumps(body).encode(), headers=headers), timeout=5)
        with self.assertRaises(HTTPError) as denied:
            put({})
        self.assertEqual(denied.exception.code, 403)
        headers = {"Authorization": authorization(), "X-CSRF-Token": token,
                   "Host": f"{SETTINGS.domain}:{self.server.server_port}", "X-Forwarded-For": "127.0.0.1"}
        with self.request("/api/gift-groups", headers=headers) as response:
            self.assertEqual(len(json.load(response)["groups"]), 2)
        with self.assertRaises(HTTPError) as denied:
            put(headers)
        self.assertEqual(denied.exception.code, 403)
        with put({"X-CSRF-Token": token, "Origin": self.url}) as response:
            self.assertEqual(json.load(response)["memberships"], {"SKU-A": ["xiaomi-phone"]})
        with self.assertRaises(HTTPError) as stale:
            put({"X-CSRF-Token": token})
        self.assertEqual(stale.exception.code, 409)


if __name__ == "__main__":
    unittest.main()
