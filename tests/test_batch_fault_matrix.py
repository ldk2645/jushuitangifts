"""批次级故障矩阵：A 单在登记各阶段失败时，B/C 不得被批量写成失败。

使用合成订单和模拟平台，不连接真实平台、不触碰真实 data。
"""

import tempfile
import unittest
from pathlib import Path

from fakes import FakeOperations
from gift_registry.authentication import AuthenticationRequired
from gift_registry.registration import RegistrationQueue, load_gifts
from gift_registry.workflow import (ManualInterventionRequired, PageRecoverableError,
                                    RegistrationNotSubmittedError)


ORDERS = ("SYN-A", "SYN-B", "SYN-C")


def row(order_no):
    return {"id": f"row-{order_no}", "order_no": order_no, "gift": "赠品保温杯、赠品小背包",
            "gift_ids": ["N00A090GEU0DXM6O02", "N00A0L1LE90DXM5001"], "change_address": False}


class StageFaultOperations(FakeOperations):
    """stage: query_auth/query_page/open_preserved/order_pull/gift_select/quantity/
    pre_submit/native_submit/form_close/receipt/verify。
    """

    def __init__(self, stage, recover="ok"):
        super().__init__()
        self.stage = stage
        self.recover_mode = recover
        self.recovered = False
        self.recover_calls = 0
        self.verify_failed = False

    def recover_environment(self):
        self.recover_calls += 1
        if self.recover_mode == "fail":
            raise ManualInterventionRequired("SYNTHETIC: 站点仍未就绪")
        self.recovered = True

    def _count(self, order_no):
        self.trace.append(("query", order_no))
        self.queries[order_no] = self.queries.get(order_no, 0) + 1

    def query_order(self, order_no):
        if order_no != ORDERS[0]:
            return super().query_order(order_no)
        if self.stage == "query_auth" and not self.recovered:
            self._count(order_no)
            raise AuthenticationRequired("SYNTHETIC: 需要人工登录")
        if self.stage == "query_page" and not self.recovered:
            self._count(order_no)
            raise PageRecoverableError("SYNTHETIC: 工具自有页面需恢复")
        if self.stage == "verify" and self.queries.get(order_no, 0) >= 1:
            self._count(order_no)
            raise ManualInterventionRequired("SYNTHETIC: 聚水潭复查页面连接中断，需人工确认")
        return super().query_order(order_no)

    def submit_registration(self, request, before, *, on_submitted=None):
        if request.order_no == ORDERS[0]:
            if self.stage == "open_preserved":
                error = RegistrationNotSubmittedError("SYNTHETIC: 表单内容无法确认，已保留")
                error.pause_class = "manual"
                error.click_attempted = False
                raise error
            if self.stage in {"order_pull", "gift_select", "quantity", "pre_submit"}:
                error = RegistrationNotSubmittedError(f"SYNTHETIC: {self.stage} 页面失败，未点击")
                error.pause_class = "recoverable"
                error.click_attempted = False
                raise error
            if self.stage == "open_form":
                error = RegistrationNotSubmittedError("SYNTHETIC: 打开表单失败，未点击")
                error.pause_class = "recoverable"
                error.click_attempted = False
                raise error
            if self.stage in {"native_submit", "form_close", "receipt"}:
                error = ManualInterventionRequired(f"SYNTHETIC: {self.stage} 结果不确定")
                error.click_attempted = True
                error.stage = "submitting"
                raise error
        return super().submit_registration(request, before, on_submitted=on_submitted)


RECOVERABLE = {"query_page", "open_form", "order_pull", "gift_select", "quantity", "pre_submit"}
MANUAL = {"query_auth", "open_preserved", "native_submit", "form_close", "receipt", "verify"}


class BatchFaultMatrixTests(unittest.TestCase):
    def run_case(self, stage):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            operations = StageFaultOperations(stage)
            queue = RegistrationQueue(Path(folder), operations=operations)
            try:
                queue.start([row(order) for order in ORDERS], load_gifts())
                queue.pending.join()
                states = queue.states()
                self.assertEqual(states["row-SYN-A"]["state"], "failed", stage)
                expected = "success" if stage in RECOVERABLE else "waiting"
                self.assertEqual(states["row-SYN-B"]["state"], expected, stage)
                self.assertEqual(states["row-SYN-C"]["state"], expected, stage)
                # A 绝不自动重提；verify 阶段 A 先提交一次后再复查失败，查询共两次。
                self.assertEqual(operations.queries.get("SYN-A"), 2 if stage == "verify" else 1, stage)
                if stage in RECOVERABLE:
                    self.assertFalse(queue.paused, stage)
                    self.assertEqual(operations.recover_calls, 1, stage)
                    self.assertEqual([entry.order_no for entry in operations.submissions], ["SYN-B", "SYN-C"], stage)
                else:
                    self.assertTrue(queue.paused, stage)
                    self.assertEqual(operations.recover_calls, 0, stage)
                    # verify 阶段 A 已提交（结果不确定），其它阶段 A 从未进入提交。
                    self.assertEqual([entry.order_no for entry in operations.submissions],
                                     ["SYN-A"] if stage == "verify" else [], stage)
                    self.assertEqual(queue.pending.unfinished_tasks, 0, stage)
                    result = queue.pause_snapshot()
                    self.assertEqual(result["waiting_count"], 2, stage)
                    self.assertEqual(result["class"], "manual", stage)
                    # 主机处理并明确恢复后 B/C 各执行一次。
                    operations.recovered = True
                    operations.recover_mode = "ok"
                    self.assertTrue(queue.resume()["resumed"], stage)
                    queue.pending.join()
                    self.assertEqual(queue.states()["row-SYN-B"]["state"], "success", stage)
                    self.assertEqual(queue.states()["row-SYN-C"]["state"], "success", stage)
                    self.assertEqual([entry.order_no for entry in operations.submissions],
                                     (["SYN-A"] if stage == "verify" else []) + ["SYN-B", "SYN-C"], stage)
            finally:
                queue.close()

    def test_fault_matrix_all_stages(self):
        for stage in sorted(RECOVERABLE | MANUAL):
            with self.subTest(stage=stage):
                self.run_case(stage)


if __name__ == "__main__":
    unittest.main()
