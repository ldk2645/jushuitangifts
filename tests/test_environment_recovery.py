"""P1 共享环境故障的批次级回归：暂停、等待、显式恢复及防重。

只使用合成订单、临时队列目录和模拟平台，不连接真实平台、不触碰真实 data。
"""

import json
import tempfile
import threading
import unittest
from pathlib import Path

from fakes import FakeOperations
from gift_registry.authentication import AuthenticationRequired
from gift_registry.registration import RegistrationQueue, RegistrationStartError, load_gifts, task_key
from gift_registry.workflow import ManualInterventionRequired, PageRecoverableError


ORDERS = ("SYN-A", "SYN-B", "SYN-C")


def row(order_no, identifier=None):
    return {"id": identifier or f"row-{order_no}", "order_no": order_no,
            "gift": "赠品保温杯、赠品小背包",
            "gift_ids": ["N00A090GEU0DXM6O02", "N00A0L1LE90DXM5001"],
            "change_address": False}


class ScriptedOperations(FakeOperations):
    """按场景在 A 单的查询或提交阶段注入共享环境故障。"""

    def __init__(self, failure=None, recover="ok"):
        super().__init__()
        self.failure = failure
        self.recover_mode = recover
        self.recovered = False
        self.recover_calls = 0

    def recover_environment(self):
        self.recover_calls += 1
        if self.recover_mode == "fail":
            raise ManualInterventionRequired("SYNTHETIC: 登录仍未就绪，恢复检查失败")
        self.recovered = True

    def query_order(self, order_no):
        injected = not self.recovered and order_no == ORDERS[0]
        if injected and self.failure == "query_recoverable":
            self.trace.append(("query", order_no))
            self.queries[order_no] = self.queries.get(order_no, 0) + 1
            raise PageRecoverableError("SYNTHETIC: 工具自有页面需刷新")
        if injected and self.failure == "query_auth":
            self.trace.append(("query", order_no))
            self.queries[order_no] = self.queries.get(order_no, 0) + 1
            raise AuthenticationRequired("SYNTHETIC: 需要人工登录/验证码")
        return super().query_order(order_no)

    def submit_registration(self, request, before, *, on_submitted=None):
        if self.failure == "submit_uncertain" and request.order_no == ORDERS[0]:
            self.trace.append(("submit", request.order_no))
            error = ManualInterventionRequired("SYNTHETIC: 原生提交后结果不确定")
            error.click_attempted = True
            error.stage = "submitting"
            raise error
        return super().submit_registration(request, before, on_submitted=on_submitted)


class BatchEnvironmentPauseTests(unittest.TestCase):
    def run_queue(self, folder, operations):
        queue = RegistrationQueue(Path(folder), operations=operations)
        try:
            queue.start([row(order) for order in ORDERS], load_gifts())
            queue.pending.join()
        finally:
            pass
        return queue

    def test_recoverable_page_failure_is_recovered_once_between_orders(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            operations = ScriptedOperations("query_recoverable")
            with self.run_queue(folder, operations) as queue:
                states = queue.states()
                self.assertEqual(states["row-SYN-A"]["state"], "failed")
                self.assertEqual(states["row-SYN-B"]["state"], "success")
                self.assertEqual(states["row-SYN-C"]["state"], "success")
                self.assertFalse(queue.paused)
                self.assertEqual(operations.recover_calls, 1)
                # A 只查询一次、不重提；B/C 各执行一次（每单提交一次）。
                self.assertEqual(operations.queries.get("SYN-A"), 1)
                self.assertEqual(operations.queries.get("SYN-B"), 2)
                self.assertEqual(operations.queries.get("SYN-C"), 2)
                self.assertEqual([entry.order_no for entry in operations.submissions], ["SYN-B", "SYN-C"])

    def test_auth_failure_pauses_unexecuted_rows_and_resume_runs_them_once(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            operations = ScriptedOperations("query_auth")
            with self.run_queue(folder, operations) as queue:
                states = queue.states()
                self.assertEqual(states["row-SYN-A"]["state"], "failed")
                self.assertEqual(states["row-SYN-B"]["state"], "waiting")
                self.assertEqual(states["row-SYN-C"]["state"], "waiting")
                self.assertTrue(queue.paused)
                snapshot = queue.pause_snapshot()
                self.assertTrue(snapshot["paused"])
                self.assertEqual(snapshot["class"], "manual")
                self.assertTrue(snapshot["reason"])
                self.assertTrue(snapshot["recovery_condition"])
                self.assertEqual(snapshot["waiting_count"], 2)
                # 暂停期间：未执行行不进入失败历史、不占用在途计数、不能开始新任务。
                self.assertEqual(queue.pending.unfinished_tasks, 0)
                failure_orders = {entry["order_no"] for entry in queue.retry_candidates()["failures"]}
                self.assertNotIn("SYN-B", failure_orders)
                self.assertNotIn("SYN-C", failure_orders)
                with self.assertRaises(RegistrationStartError):
                    queue.start([row("SYN-D")], load_gifts())
                # 恢复前必须验证站点就绪：模拟主机完成登录后恢复。
                operations.recovered = True
                result = queue.resume()
                self.assertTrue(result["resumed"])
                queue.pending.join()
                states = queue.states()
                self.assertEqual(states["row-SYN-A"]["state"], "failed")
                self.assertEqual(states["row-SYN-B"]["state"], "success")
                self.assertEqual(states["row-SYN-C"]["state"], "success")
                self.assertFalse(queue.paused)
                self.assertEqual([entry.order_no for entry in operations.submissions], ["SYN-B", "SYN-C"])

    def test_uncertain_submission_pauses_and_never_repeats_failed_order(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            operations = ScriptedOperations("submit_uncertain")
            with self.run_queue(folder, operations) as queue:
                states = queue.states()
                self.assertEqual(states["row-SYN-A"]["state"], "failed")
                self.assertEqual(states["row-SYN-B"]["state"], "waiting")
                self.assertEqual(states["row-SYN-C"]["state"], "waiting")
                first = next(record for record in queue._latest_attempts()
                             if record["row"]["order_no"] == ORDERS[0])
                self.assertEqual(first["item"]["submission_status"], "uncertain")
                self.assertTrue(queue.paused)
                operations.recovered = True
                queue.resume()
                queue.pending.join()
                states = queue.states()
                self.assertEqual(states["row-SYN-A"]["state"], "failed")
                self.assertEqual(states["row-SYN-B"]["state"], "success")
                self.assertEqual(states["row-SYN-C"]["state"], "success")
                # A 的防重资格仍保留：再次明确开始同订单不会重提。
                with self.assertRaises(ValueError):
                    queue.retry_failed(next(record["failure_id"] for record in queue._latest_attempts()
                                            if record["row"]["order_no"] == ORDERS[0]))

    def test_recovery_failure_keeps_queue_paused_until_host_ready(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            operations = ScriptedOperations("query_recoverable", recover="fail")
            with self.run_queue(folder, operations) as queue:
                self.assertTrue(queue.paused)
                snapshot = queue.pause_snapshot()
                self.assertEqual(snapshot["class"], "manual")
                self.assertEqual(queue.states()["row-SYN-B"]["state"], "waiting")
                self.assertEqual(operations.recover_calls, 1)
                # 就绪验证失败时恢复被拒绝，暂停与 waiting 不变。
                with self.assertRaises(ManualInterventionRequired):
                    queue.resume()
                self.assertTrue(queue.paused)
                self.assertEqual(queue.states()["row-SYN-B"]["state"], "waiting")
                operations.recovered, operations.recover_mode = True, "ok"
                self.assertTrue(queue.resume()["resumed"])
                queue.pending.join()
                self.assertEqual(queue.states()["row-SYN-B"]["state"], "success")
                self.assertEqual(queue.states()["row-SYN-C"]["state"], "success")

    def test_concurrent_start_requests_cannot_bypass_pause(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            operations = ScriptedOperations("query_auth")
            queue = RegistrationQueue(Path(folder), operations=operations)
            try:
                queue.start([row(order) for order in ORDERS], load_gifts())
                queue.pending.join()
                self.assertTrue(queue.paused)
                errors, barrier = [], threading.Barrier(3)

                def attempt():
                    barrier.wait()
                    try:
                        queue.start([row("SYN-D")], load_gifts())
                    except RegistrationStartError as exc:
                        errors.append(str(exc))
                    except Exception as exc:  # 任何非“暂停拒绝”都需要显式失败。
                        errors.append("unexpected:" + type(exc).__name__)

                threads = [threading.Thread(target=attempt) for _ in range(3)]
                for thread in threads: thread.start()
                for thread in threads: thread.join(timeout=5)
                self.assertEqual(len(errors), 3)
                self.assertTrue(all("已暂停" in message for message in errors))
                self.assertFalse((Path(folder) / "jobs" / "SYN-D.json").exists())
            finally:
                queue.close()

    def test_pause_survives_restart_and_host_can_cancel_waiting_rows(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            operations = ScriptedOperations("query_auth")
            with self.run_queue(folder, operations) as queue:
                self.assertTrue(queue.paused)
                # 暂停期间关闭：pending 已排空，不悬挂、不阻塞。
                self.assertEqual(queue.pending.unfinished_tasks, 0)
                pause_file = Path(folder) / ".queue-pause.state"
                self.assertTrue(pause_file.exists())
                persisted = json.loads(pause_file.read_text(encoding="utf-8"))
                self.assertTrue(persisted["paused"])
            # 重启后仍保持暂停，未执行行保留 waiting，不会自动派发。
            operations.recovered = True
            with RegistrationQueue(Path(folder), operations=operations) as queue:
                self.assertTrue(queue.paused)
                self.assertEqual(queue.states()["row-SYN-B"]["state"], "waiting")
                with self.assertRaises(RegistrationStartError):
                    queue.start([row("SYN-D")], load_gifts())
                keys = {task_key(row(order)) for order in ("SYN-B", "SYN-C")}
                cancelled, count = queue.cancel_queued(keys)
                self.assertEqual(count, 2)
                self.assertEqual(cancelled, keys)
                self.assertEqual(queue.states()["row-SYN-B"]["state"], "cancelled")
                self.assertEqual(queue.states()["row-SYN-B"]["detail"].count("未提交"), 1)
                # 取消全部 waiting 后恢复仅解除暂停，不再派发任何未执行任务。
                queue.resume()
                queue.pending.join()
                self.assertFalse(queue.paused)
                self.assertEqual(queue.states()["row-SYN-B"]["state"], "cancelled")
                self.assertEqual(operations.queries.get("SYN-B", 0), 0)


if __name__ == "__main__":
    unittest.main()
