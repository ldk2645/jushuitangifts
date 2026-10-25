"""启动前资源检查；只使用明确的模拟平台、临时目录和本机随机 HTTP 端口。"""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fakes import FakeOperations
from gift_registry.frontend_server import RowStore, make_server
from gift_registry.registration import RegistrationQueue, RegistrationStartError, load_gifts
from gift_registry.workflow import PlatformOperationError


BLOCKED_DETAIL = "登录助手正在占用浏览器配置，请保存并关闭登录助手后再次点击开始登记"


def sample(identifier="row-1", order="00009", *, change_address=False):
    return {"id": identifier, "order_no": order, "gift": "米家保温杯口袋版、小米小背包",
            "gift_ids": ["N01B0L1LE91VZS8S01", "N01B0J1JDK1V007H08"],
            "change_address": change_address, "recipient_name": "测试姓名",
            "phone": "0013800000000", "address": "测试地址"}


class PreflightOperations(FakeOperations):
    def __init__(self, *, blocked=False, modes=None, on_submit=None):
        super().__init__(modes=modes, on_submit=on_submit)
        self.blocked = blocked
        self.preflight_calls = 0
        self.release_calls = 0
        self.reserved = False

    def prepare_start(self):
        self.preflight_calls += 1
        if self.blocked:
            raise PlatformOperationError(BLOCKED_DETAIL)
        self.reserved = True

    def release_unused_start(self):
        self.release_calls += 1
        self.reserved = False


class QueueStartPreflightTests(unittest.TestCase):
    def test_blocked_new_start_is_value_error_before_any_save_or_dispatch(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            operations = PreflightOperations(blocked=True)
            with RegistrationQueue(folder, operations=operations) as jobs:
                with patch.object(jobs, "_save", wraps=jobs._save) as save:
                    with self.assertRaises(RegistrationStartError) as error:
                        jobs.start([sample()], load_gifts())
                    self.assertIsInstance(error.exception, ValueError)
                    self.assertIn(BLOCKED_DETAIL, str(error.exception))
                    save.assert_not_called()
                self.assertEqual(operations.preflight_calls, 1)
                self.assertEqual(operations.trace, [])
                self.assertEqual(list(Path(folder).glob("*.json")), [])
                self.assertEqual(jobs.states(), {})
                self.assertEqual(jobs.pending.unfinished_tasks, 0)
                self.assertIsNone(jobs.worker)

    def test_blocked_existing_waiting_snapshot_is_not_modified(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            operations = PreflightOperations(blocked=True)
            operations.configured = False
            with RegistrationQueue(folder, operations=operations) as jobs:
                jobs.start([sample()], load_gifts())
                path = next(Path(folder).glob("*.json"))
                saved = path.read_bytes()
                operations.configured = True
                with patch.object(jobs, "_save", wraps=jobs._save) as save:
                    with self.assertRaises(RegistrationStartError):
                        jobs.start([sample()], load_gifts())
                    save.assert_not_called()
                self.assertEqual(path.read_bytes(), saved)
                self.assertEqual(jobs.states()["row-1"]["state"], "waiting_for_workflow")
                self.assertEqual(operations.preflight_calls, 1)
                self.assertEqual(operations.trace, [])
                self.assertIsNone(jobs.worker)

    def test_releasing_block_requires_another_explicit_start_for_first_execution(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            operations = PreflightOperations(blocked=True)
            with RegistrationQueue(folder, operations=operations) as jobs:
                with self.assertRaises(RegistrationStartError):
                    jobs.start([sample()], load_gifts())
                operations.blocked = False
                self.assertEqual(jobs.states(), {})
                self.assertEqual(operations.trace, [])
                self.assertEqual(operations.preflight_calls, 1)
                self.assertIsNone(jobs.worker)
                jobs.start([sample()], load_gifts())
                jobs.pending.join()
                self.assertEqual(jobs.states()["row-1"]["state"], "success")
                self.assertEqual(jobs.states()["row-1"]["attempts"], 1)
                self.assertEqual(operations.preflight_calls, 2)
                self.assertEqual(len(operations.submissions), 1)

    def test_successful_preflight_precedes_every_initial_task_save(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            operations = PreflightOperations()
            with RegistrationQueue(folder, operations=operations) as jobs:
                original_save = jobs._save

                def checked_save(path, job):
                    self.assertEqual(operations.preflight_calls, 1)
                    self.assertTrue(operations.reserved)
                    return original_save(path, job)

                with patch.object(jobs, "_save", side_effect=checked_save) as save:
                    jobs.start([sample()], load_gifts())
                    jobs.pending.join()
                    self.assertGreaterEqual(save.call_count, 1)
                self.assertEqual(jobs.states()["row-1"]["state"], "success")
                self.assertEqual(len(operations.submissions), 1)

    def test_idle_save_failure_releases_unused_reservation_without_dispatch(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            operations = PreflightOperations()
            with RegistrationQueue(folder, operations=operations) as jobs:
                with patch.object(jobs, "_save", side_effect=OSError("test-only storage failure")):
                    with self.assertRaises(OSError):
                        jobs.start([sample()], load_gifts())
                self.assertEqual(operations.preflight_calls, 1)
                self.assertEqual(operations.release_calls, 1)
                self.assertFalse(operations.reserved)
                self.assertEqual(operations.trace, [])
                self.assertEqual(list(Path(folder).glob("*.json")), [])
                self.assertIsNone(jobs.worker)
                jobs.start([sample()], load_gifts())
                jobs.pending.join()
                self.assertEqual(operations.preflight_calls, 2)
                self.assertEqual(len(operations.submissions), 1)

    def test_second_batch_save_failure_does_not_release_inflight_resources(self):
        started, release = threading.Event(), threading.Event()

        def pause_submission(_):
            started.set()
            if not release.wait(5):
                raise TimeoutError("test-only synchronization timeout")

        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            operations = PreflightOperations(on_submit=pause_submission)
            jobs = RegistrationQueue(folder, operations=operations)
            try:
                jobs.start([sample()], load_gifts())
                self.assertTrue(started.wait(5))
                original_save = jobs._save

                def fail_second_batch(path, job):
                    if job["rows"][0]["order_no"] == "00010":
                        raise OSError("test-only second batch storage failure")
                    return original_save(path, job)

                with patch.object(jobs, "_save", side_effect=fail_second_batch):
                    with self.assertRaises(OSError):
                        jobs.start([sample("second", "00010")], load_gifts())
                self.assertEqual(operations.release_calls, 0)
                self.assertTrue(operations.reserved)
                self.assertEqual(len(list(Path(folder).glob("*.json"))), 1)
            finally:
                release.set()
                jobs.pending.join()
                jobs.close()
            self.assertEqual([request.order_no for request in operations.submissions], ["00009"])
            self.assertEqual(jobs.states()["row-1"]["state"], "success")

    def test_unused_reservation_is_released_only_after_all_accepted_batches_finish(self):
        first_started, first_release = threading.Event(), threading.Event()
        second_started, second_release = threading.Event(), threading.Event()

        def pause_submission(request):
            started, release = ((first_started, first_release) if request.order_no == "00009"
                                else (second_started, second_release))
            started.set()
            if not release.wait(5):
                raise TimeoutError("test-only synchronization timeout")

        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            operations = PreflightOperations(on_submit=pause_submission)
            jobs = RegistrationQueue(folder, operations=operations)
            try:
                jobs.start([sample()], load_gifts())
                self.assertTrue(first_started.wait(5))
                jobs.start([sample("second", "00010")], load_gifts())
                first_release.set()
                self.assertTrue(second_started.wait(5))
                self.assertEqual(operations.release_calls, 0)
                self.assertTrue(operations.reserved)
            finally:
                first_release.set()
                second_release.set()
                jobs.pending.join()
                jobs.close()
            self.assertEqual(operations.release_calls, 1)
            self.assertFalse(operations.reserved)
            self.assertEqual([request.order_no for request in operations.submissions], ["00009", "00010"])

    def test_verified_terminal_reuse_does_not_check_current_resource_availability(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            operations = PreflightOperations()
            with RegistrationQueue(folder, operations=operations) as jobs:
                jobs.start([sample()], load_gifts())
                jobs.pending.join()
                operations.blocked = True
                calls, trace = operations.preflight_calls, list(operations.trace)
                for row in (sample(), sample("copied-row")):
                    self.assertEqual(jobs.start([row], load_gifts())["state"], "success")
                self.assertEqual(operations.preflight_calls, calls)
                self.assertEqual(operations.trace, trace)

    def test_previous_failed_zero_attempts_remains_terminal_after_resource_release(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            operations = PreflightOperations(modes={"00009": "query_error"})
            with RegistrationQueue(folder, operations=operations) as jobs:
                jobs.start([sample()], load_gifts())
                jobs.pending.join()
                self.assertEqual(jobs.states()["row-1"]["state"], "failed")
                self.assertEqual(jobs.states()["row-1"]["attempts"], 0)
                operations.modes.clear()
                calls, trace = operations.preflight_calls, list(operations.trace)
                for row in (sample(), sample("copied-row")):
                    self.assertEqual(jobs.start([row], load_gifts())["state"], "failed")
                self.assertEqual(operations.preflight_calls, calls)
                self.assertEqual(operations.trace, trace)
                self.assertEqual(operations.submissions, [])

    def test_only_address_change_requests_fail_without_preflight_or_platform_calls(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            operations = PreflightOperations(blocked=True)
            with RegistrationQueue(folder, operations=operations) as jobs:
                jobs.start([sample(change_address=True), sample("second", "00010", change_address=True)], load_gifts())
                jobs.pending.join()
                self.assertEqual({state["state"] for state in jobs.states().values()}, {"failed"})
                self.assertEqual(operations.preflight_calls, 0)
                self.assertEqual(operations.trace, [])

    def test_unconfigured_workflow_does_not_acquire_start_resources(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            operations = PreflightOperations(blocked=True)
            operations.configured = False
            with RegistrationQueue(folder, operations=operations) as jobs:
                result = jobs.start([sample()], load_gifts())
                self.assertEqual(result["state"], "waiting_for_workflow")
                self.assertEqual(operations.preflight_calls, 0)
                self.assertEqual(operations.trace, [])


class StartPreflightHTTPTests(unittest.TestCase):
    def test_conflict_preserves_draft_status_polling_never_starts_and_explicit_start_can_follow(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            directory = Path(folder)
            operations = PreflightOperations(blocked=True)
            jobs = RegistrationQueue(directory / "jobs", operations=operations)
            store = RowStore(directory / "rows.json")
            server = make_server(0, store=store, queue=jobs)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(url + "/api/config", timeout=5) as response:
                    token = json.load(response)["token"]
                headers = {"X-CSRF-Token": token}
                payload = json.dumps({"revision": 0, "rows": [sample()]}).encode("utf-8")
                with urlopen(Request(url + "/api/rows", data=payload, method="PUT", headers=headers), timeout=5) as response:
                    revision = json.load(response)["revision"]
                draft = store.path.read_bytes()
                payload = json.dumps({"revision": revision}).encode("utf-8")
                with self.assertRaises(HTTPError) as denied:
                    urlopen(Request(url + "/api/registrations/start", data=payload, method="POST"), timeout=5)
                self.assertEqual(denied.exception.code, 403)
                denied.exception.close()
                self.assertEqual(operations.preflight_calls, 0)
                with self.assertRaises(HTTPError) as blocked:
                    urlopen(Request(url + "/api/registrations/start", data=payload, method="POST", headers=headers), timeout=5)
                self.assertEqual(blocked.exception.code, 409)
                self.assertIn(BLOCKED_DETAIL, json.load(blocked.exception)["error"])
                blocked.exception.close()
                operations.blocked = False
                for _ in range(2):
                    with urlopen(url + "/api/registrations/status", timeout=5) as response:
                        self.assertEqual(json.load(response)["row_states"], {})
                self.assertEqual(operations.preflight_calls, 1)
                self.assertEqual(operations.trace, [])
                self.assertEqual(store.path.read_bytes(), draft)
                self.assertEqual(list(jobs.directory.glob("*.json")), [])
                with urlopen(Request(url + "/api/registrations/start", data=payload, method="POST", headers=headers), timeout=5) as response:
                    self.assertEqual(response.status, 200)
                jobs.pending.join()
                with urlopen(url + "/api/registrations/status", timeout=5) as response:
                    self.assertEqual(json.load(response)["row_states"]["row-1"]["state"], "success")
                self.assertEqual(operations.preflight_calls, 2)
                self.assertEqual(len(operations.submissions), 1)
                self.assertEqual(store.path.read_bytes(), draft)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_http_conflict_leaves_existing_waiting_snapshot_and_draft_unchanged(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            directory = Path(folder)
            operations = PreflightOperations(blocked=True)
            operations.configured = False
            jobs = RegistrationQueue(directory / "jobs", operations=operations)
            jobs.start([sample()], load_gifts())
            task_path = next(jobs.directory.glob("*.json"))
            saved_task = task_path.read_bytes()
            operations.configured = True
            store = RowStore(directory / "rows.json")
            store.write(0, [sample()])
            saved_draft = store.path.read_bytes()
            server = make_server(0, store=store, queue=jobs)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(url + "/api/config", timeout=5) as response:
                    token = json.load(response)["token"]
                request = Request(url + "/api/registrations/start", data=b'{"revision":1}', method="POST",
                                  headers={"X-CSRF-Token": token})
                with self.assertRaises(HTTPError) as blocked:
                    urlopen(request, timeout=5)
                self.assertEqual(blocked.exception.code, 409)
                blocked.exception.close()
                with urlopen(url + "/api/registrations/status", timeout=5) as response:
                    self.assertEqual(json.load(response)["row_states"]["row-1"]["state"], "waiting_for_workflow")
                self.assertEqual(task_path.read_bytes(), saved_task)
                self.assertEqual(store.path.read_bytes(), saved_draft)
                self.assertEqual(operations.preflight_calls, 1)
                self.assertEqual(operations.trace, [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
