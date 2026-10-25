import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from gift_registry.frontend_server import RevisionConflict, RowStore, make_server, validate_rows
from gift_registry.registration import RegistrationQueue, load_gifts, registration_rows
from fakes import FakeOperations


class UnconfiguredOperations(FakeOperations):
    configured = False


def sample():
    return {"id": "row-1", "order_no": "00012345678901234567890", "gift": "米家保温杯口袋版", "gift_ids": ["N01B0L1LE91VZS8S01"],
            "change_address": True, "recipient_name": "测试姓名", "phone": "0013800000000", "address": "测试地址"}


class RowStoreTests(unittest.TestCase):
    def test_long_identifiers_and_address_are_preserved_after_reload(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path = Path(folder) / "rows.json"
            store = RowStore(path)
            self.assertEqual(store.write(0, [sample()]), {"revision": 1})
            self.assertEqual(RowStore(path).read(), {"revision": 1, "rows": [sample()]})

    def test_conflicting_window_cannot_overwrite_saved_data(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            store = RowStore(Path(folder) / "rows.json")
            store.write(0, [sample()])
            with self.assertRaises(RevisionConflict):
                store.write(0, [])
            self.assertEqual(store.read()["rows"], [sample()])

    def test_invalid_types_and_duplicate_identifiers_are_rejected(self):
        for rows in ([{**sample(), "order_no": 123}], [{**sample(), "change_address": "是"}], [sample(), sample()]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                validate_rows(rows)

    def test_client_cannot_forge_completed_status(self):
        self.assertNotIn("status", validate_rows([{**sample(), "status": "已完成"}])[0])


class FrontendHTTPTests(unittest.TestCase):
    def test_server_close_freezes_queue_and_closes_operations_before_releasing_directory(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            directory = Path(folder) / "jobs"
            calls = []
            case = self

            class Operations:
                configured = False

                def close(self):
                    with case.assertRaisesRegex(ValueError, "正在关闭"):
                        jobs.start([sample()], load_gifts())
                    with case.assertRaisesRegex(ValueError, "任务目录"):
                        RegistrationQueue(directory, operations=UnconfiguredOperations())
                    calls.append("operations_closed")

            jobs = RegistrationQueue(directory, operations=Operations())
            server = make_server(0, store=RowStore(Path(folder) / "rows.json"), queue=jobs)
            server.server_close()
            self.assertEqual(calls, ["operations_closed"])
            self.assertIsNone(server.registration_queue)
            with RegistrationQueue(directory, operations=UnconfiguredOperations()):
                pass
            server.server_close()
            self.assertEqual(calls, ["operations_closed"])

    def test_operation_close_error_still_releases_idle_queue(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            directory = Path(folder) / "jobs"

            class Operations:
                configured = False

                def close(self):
                    raise RuntimeError("test-only close failure")

            jobs = RegistrationQueue(directory, operations=Operations())
            server = make_server(0, store=RowStore(Path(folder) / "rows.json"), queue=jobs)
            with self.assertRaisesRegex(RuntimeError, "test-only close failure"):
                server.server_close()
            self.assertIsNone(server.registration_queue)
            with RegistrationQueue(directory, operations=UnconfiguredOperations()):
                pass

    def test_second_process_cannot_bind_to_existing_frontend_port(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            store = RowStore(Path(folder) / "rows.json")
            jobs = RegistrationQueue(Path(folder) / "jobs", operations=UnconfiguredOperations())
            first = make_server(0, store=store, queue=jobs)
            try:
                with self.assertRaises(OSError):
                    second = make_server(first.server_port, store=store, queue=jobs)
                    second.server_close()
            finally:
                first.server_close()

    def test_static_page_and_authorized_save(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            store = RowStore(Path(folder) / "rows.json")
            server = make_server(0, store=store, queue=RegistrationQueue(
                Path(folder) / "jobs", operations=UnconfiguredOperations()))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(url + "/") as response:
                    self.assertIn("礼赠登记表", response.read().decode())
                with urlopen(url + "/api/config") as response:
                    token = json.load(response)["token"]
                payload = json.dumps({"revision": 0, "rows": [sample()]}).encode()
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(url + "/api/rows", data=payload, method="PUT"))
                self.assertEqual(error.exception.code, 403)
                self.assertEqual(store.read()["revision"], 0)
                request = Request(url + "/api/rows", data=payload, method="PUT", headers={"X-CSRF-Token": token})
                with urlopen(request) as response:
                    self.assertEqual(json.load(response), {"revision": 1})
                start = Request(url + "/api/registrations/start", data=json.dumps({"revision": 1}).encode(), method="POST", headers={"X-CSRF-Token": token})
                with urlopen(start) as response:
                    result = json.load(response)
                    self.assertEqual(result["state"], "waiting_for_workflow")
                    self.assertEqual(result["count"], 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


class RegistrationTests(unittest.TestCase):
    def test_task_preserves_snapshot_and_repeated_click_is_idempotent(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            jobs = RegistrationQueue(Path(folder) / "jobs", operations=UnconfiguredOperations())
            row = sample()
            first = jobs.start([row], load_gifts())
            row["order_no"] = "modified-after-submit"
            second = jobs.start([sample()], load_gifts())
            self.assertEqual(first["task_id"], second["task_id"])
            self.assertTrue(second["reused"])
            self.assertEqual(len(list(jobs.directory.glob("*.json"))), 1)
            snapshot = json.loads(next(jobs.directory.glob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual(snapshot["rows"][0]["order_no"], sample()["order_no"])
            self.assertEqual(snapshot["state"], "waiting_for_workflow")
            jobs.close()

    def test_incomplete_or_unmatched_draft_is_rejected(self):
        for row in ({**sample(), "gift": "未知礼品"}, {**sample(), "gift_ids": []}, {**sample(), "phone": ""}):
            with self.subTest(row=row), self.assertRaises(ValueError):
                registration_rows([row], load_gifts())

    def test_legacy_string_draft_is_kept_until_frontend_matches_it(self):
        legacy = sample()
        del legacy["gift_ids"]
        loaded = validate_rows([legacy])[0]
        self.assertEqual(loaded["gift"], "米家保温杯口袋版")
        self.assertEqual(loaded["gift_ids"], [])


if __name__ == "__main__":
    unittest.main()
