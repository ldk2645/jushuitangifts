"""班牛回执隔离测试：仅构造 DOM 读取结果，不连接真实平台。"""

import copy
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

from gift_registry.banniu_page import GIFT_COLUMNS
from gift_registry.banniu_receipt import BanniuReceiptReader, complete_task_rows, receipt_from_detail
from gift_registry.workflow import GiftRecord, OrderSnapshot, PlatformOperationError, RegistrationRequest, SubmissionReceipt


ORDER = "0000611991816470192"
TASK = "000052"
SKU = "000SKU01"
NAME = "测试礼品"
REQUEST = RegistrationRequest(ORDER, (SKU,), (NAME,))
BEFORE = OrderSnapshot(ORDER, True, (GiftRecord("000048", ("OLD",)),))
SUBMITTED_AT = datetime(2026, 10, 23, 10, 0, 0, 123456)


def task(identifier=TASK, **updates):
    return {"task_id": identifier, "order_no": ORDER, "created_at": "2026-10-23 10:00:00", **updates}


def detail():
    return {"fields": {"工单编号": TASK, "创建时间": "2026-10-23 10:00:00", "同步状态": "成功",
                       "售后类型": "补发", "补发类型": "非原单原地址", "订单号": ORDER,
                       "问题类型": "赠品补发", "是否自动确认售后单": "是", "售后单内部单号": "000049"},
            "headers": list(GIFT_COLUMNS), "rows": [["补发", SKU, NAME, "0", "1"]]}


class TaskListTests(unittest.TestCase):
    def test_complete_list_preserves_string_ids_and_deduplicates_fixed_column_copies(self):
        row = task()
        self.assertEqual(complete_task_rows({"pager": "共 1 条", "rows": [row, copy.deepcopy(row)]}, ORDER), [row])
        self.assertEqual(complete_task_rows({"pager": "共 0 条", "rows": []}, ORDER), [])

    def test_loading_partial_wrong_order_and_invalid_id_results_are_not_ready(self):
        snapshots = [None, {"pager": "加载中", "rows": []},
                     {"pager": "共 2 条", "rows": [task()]},
                     {"pager": "共 0 条", "rows": [task()]},
                     {"pager": "共 1 条", "rows": [task(order_no="other")]},
                     {"pager": "共 1 条", "rows": [task(identifier=52)]},
                     {"pager": "共 1 条", "rows": [task(identifier="invalid")]}]
        for snapshot in snapshots:
            with self.subTest(snapshot=snapshot):
                self.assertIsNone(complete_task_rows(snapshot, ORDER))

    def test_conflicting_duplicate_task_rows_fail_closed(self):
        snapshot = {"pager": "共 1 条", "rows": [task(), task(created_at="2026-10-23 10:00:01")]}
        with self.assertRaisesRegex(PlatformOperationError, "读取中发生变化"):
            complete_task_rows(snapshot, ORDER)


class ReceiptDetailTests(unittest.TestCase):
    def test_matching_saved_task_returns_only_new_jst_id_with_leading_zeros(self):
        receipt = receipt_from_detail(detail(), REQUEST, BEFORE, TASK, not_before=SUBMITTED_AT)
        self.assertTrue(receipt.accepted)
        self.assertEqual(receipt.created_record_ids, ("000049",))
        self.assertEqual(receipt.reference, "")

    def test_each_business_field_must_match_the_same_task(self):
        for key in ("工单编号", "售后类型", "补发类型", "订单号", "问题类型", "是否自动确认售后单"):
            snapshot = detail()
            snapshot["fields"][key] = "错误值"
            with self.subTest(key=key), self.assertRaisesRegex(PlatformOperationError, "与本次登记不一致"):
                receipt_from_detail(snapshot, REQUEST, BEFORE, TASK)

    def test_saved_gift_identity_name_quantity_amount_and_type_must_match(self):
        for index, value in ((0, "换货"), (1, "other"), (2, "其他礼品"), (3, "1"), (4, "2")):
            snapshot = detail()
            snapshot["rows"][0][index] = value
            with self.subTest(index=index), self.assertRaisesRegex(PlatformOperationError, "礼品、数量或金额"):
                receipt_from_detail(snapshot, REQUEST, BEFORE, TASK)
        snapshot = detail()
        snapshot["rows"].append(copy.deepcopy(snapshot["rows"][0]))
        with self.assertRaises(PlatformOperationError):
            receipt_from_detail(snapshot, REQUEST, BEFORE, TASK)

    def test_old_or_unparseable_creation_time_cannot_confirm_current_submission(self):
        for value in ("2026-10-23 09:59:59", "", "未知时间"):
            snapshot = detail()
            snapshot["fields"]["创建时间"] = value
            with self.subTest(value=value), self.assertRaises(PlatformOperationError):
                receipt_from_detail(snapshot, REQUEST, BEFORE, TASK, not_before=SUBMITTED_AT)

    def test_blank_sync_or_jst_id_is_pending_but_failure_or_old_jst_id_is_rejected(self):
        self.assertIsNone(receipt_from_detail(None, REQUEST, BEFORE, TASK))
        for key in ("同步状态", "售后单内部单号"):
            snapshot = detail()
            snapshot["fields"][key] = ""
            with self.subTest(key=key):
                self.assertIsNone(receipt_from_detail(snapshot, REQUEST, BEFORE, TASK))
        for key, value in (("同步状态", "失败"), ("售后单内部单号", "000048"), ("售后单内部单号", "abc")):
            snapshot = detail()
            snapshot["fields"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(PlatformOperationError):
                receipt_from_detail(snapshot, REQUEST, BEFORE, TASK)


class ReceiptPollingTests(unittest.TestCase):
    def reader(self):
        reader = BanniuReceiptReader(Mock())
        reader.query_tasks = Mock()
        reader.open_task = Mock()
        reader.read_detail = Mock()
        return reader

    def test_waits_only_with_read_queries_for_unique_new_task_and_delayed_receipt(self):
        reader = self.reader()
        old = task(identifier="000051")
        reader.query_tasks.side_effect = [[old], [old, task()], [old, task()]]
        expected = SubmissionReceipt(True, created_record_ids=("000049",))
        reader.read_detail.side_effect = [None, expected]
        with patch("gift_registry.banniu_receipt.time.sleep") as sleep:
            result = reader.wait_for_receipt(REQUEST, BEFORE, {"000051"}, SUBMITTED_AT)
        self.assertEqual(result, expected)
        self.assertEqual(reader.query_tasks.call_count, 3)
        self.assertEqual(reader.open_task.call_count, 2)
        reader.open_task.assert_called_with(ORDER, TASK)
        reader.read_detail.assert_called_with(REQUEST, BEFORE, TASK, not_before=SUBMITTED_AT)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(reader.page.mock_calls, [])

    def test_no_new_task_or_missing_receipt_has_bounded_reads(self):
        for rows in ([task(identifier="000051")], [task()]):
            reader = self.reader()
            reader.query_tasks.return_value = rows
            reader.read_detail.return_value = None
            with self.subTest(rows=rows), patch("gift_registry.banniu_receipt.time.sleep") as sleep:
                with self.assertRaisesRegex(PlatformOperationError, "未取得明确回执"):
                    reader.wait_for_receipt(REQUEST, BEFORE, {"000051"}, SUBMITTED_AT)
            self.assertEqual(reader.query_tasks.call_count, 4)
            self.assertEqual(sleep.call_count, 3)
            self.assertEqual(reader.page.mock_calls, [])

    def test_ambiguous_new_tasks_and_mismatched_detail_stop_without_retry(self):
        reader = self.reader()
        reader.query_tasks.return_value = [task(), task(identifier="000053")]
        with patch("gift_registry.banniu_receipt.time.sleep") as sleep:
            with self.assertRaisesRegex(PlatformOperationError, "多个新增任务"):
                reader.wait_for_receipt(REQUEST, BEFORE, set(), SUBMITTED_AT)
        reader.query_tasks.assert_called_once()
        reader.open_task.assert_not_called()
        sleep.assert_not_called()
        reader = self.reader()
        reader.query_tasks.return_value = [task()]
        reader.read_detail.side_effect = PlatformOperationError("不一致；禁止再次提交")
        with patch("gift_registry.banniu_receipt.time.sleep") as sleep:
            with self.assertRaisesRegex(PlatformOperationError, "不一致"):
                reader.wait_for_receipt(REQUEST, BEFORE, set(), SUBMITTED_AT)
        reader.query_tasks.assert_called_once()
        sleep.assert_not_called()

    def test_polling_limits_are_validated_before_any_read(self):
        for queries, interval in ((0, 2), (5, 2), (4, -1), (4, 6)):
            reader = self.reader()
            with self.subTest(queries=queries, interval=interval), self.assertRaises(ValueError):
                reader.wait_for_receipt(REQUEST, BEFORE, set(), SUBMITTED_AT, queries=queries, interval=interval)
            reader.query_tasks.assert_not_called()


class ReceiptQueryTests(unittest.TestCase):
    def query_reader(self, page, field):
        reader = BanniuReceiptReader(page)
        reader._order_filter = Mock(return_value=field)
        return reader

    def test_filter_is_clicked_once_and_old_results_wait_for_refresh(self):
        page = Mock()
        field, button = Mock(), Mock()
        field.get_attribute.return_value = ORDER
        page._visible.side_effect = lambda _, selector: [] if selector in ('.w-edit-task-dialog', '.el-loading-mask') else [field]
        page._one.side_effect = lambda selector, **_: field if selector == '#list_1891 textarea' else button
        fresh = iter([False, True, True])
        snapshots = iter([{"pager": "共 1 条", "rows": [task(order_no="old-order")]},
                          {"pager": "共 1 条", "rows": [task()]}])
        observing = False

        def execute(script):
            nonlocal observing
            if "gift-banniu-observe" in script:
                observing = True
            if "gift-banniu-fresh" in script:
                return next(fresh)
            if "gift-banniu-list" in script:
                return next(snapshots) if observing else {"pager": "共 1 条", "rows": [task(order_no="initial")]}

        page.driver.execute_script.side_effect = execute

        def wait(predicate, detail):
            for _ in range(6):
                result = predicate(page.driver)
                if result:
                    return result
            raise AssertionError(detail)

        page._wait.side_effect = wait
        self.assertEqual(self.query_reader(page, field).query_tasks(ORDER), [task()])
        field.clear.assert_called_once()
        field.send_keys.assert_called_once_with(ORDER)
        button.click.assert_called_once()
        scripts = [call.args[0] for call in page.driver.execute_script.call_args_list]
        self.assertTrue(any("gift-banniu-observe" in script for script in scripts))
        self.assertIn("delete window.__giftBanniuObserver", scripts[-1])

    def test_open_user_form_blocks_navigation(self):
        page = Mock()
        page._visible.return_value = [Mock()]
        with self.assertRaisesRegex(PlatformOperationError, "未关闭的任务表单"):
            self.query_reader(page, Mock()).query_tasks(ORDER)
        page.driver.get.assert_not_called()
        page._one.assert_not_called()

    def test_query_timeout_stops_observer_without_second_filter_click(self):
        page = Mock()
        field, button = Mock(), Mock()
        field.get_attribute.return_value = ORDER
        page._visible.side_effect = lambda _, selector: [] if selector in ('.w-edit-task-dialog', '.el-loading-mask') else [field]
        page._one.side_effect = lambda selector, **_: field if selector == '#list_1891 textarea' else button
        page._wait.side_effect = [True, PlatformOperationError("查询超时")]
        with self.assertRaises(PlatformOperationError):
            self.query_reader(page, field).query_tasks(ORDER)
        button.click.assert_called_once()
        self.assertIn("delete window.__giftBanniuObserver", page.driver.execute_script.call_args.args[0])

    def test_initial_list_and_loading_mask_finish_before_filter_toggle(self):
        page = Mock()
        field, toggle, button = Mock(), Mock(), Mock()
        field.get_attribute.return_value = ORDER
        toggle.get_attribute.return_value = "worksheet-main-content-bar-item-set"
        initial_reads = 0
        observing = False

        def execute(script):
            nonlocal initial_reads, observing
            if "gift-banniu-observe" in script:
                observing = True
            if "gift-banniu-fresh" in script:
                return True
            if "gift-banniu-list" in script:
                if not observing:
                    initial_reads += 1
                    return None if initial_reads == 1 else {"pager": "共 0 条", "rows": []}
                return {"pager": "共 1 条", "rows": [task()]}

        def visible(_, selector):
            if selector == '.el-loading-mask':
                return [Mock()] if initial_reads == 2 else []
            return []

        def one(selector, **_):
            if selector == '#list_1891 textarea':
                return field
            if selector == 'button.worksheet-main-content-bar-item-set':
                self.assertEqual(initial_reads, 3)
                return toggle
            return button

        def wait(predicate, message):
            for _ in range(6):
                result = predicate(page.driver)
                if result:
                    return result
            raise AssertionError(message)

        page.driver.execute_script.side_effect = execute
        page._visible.side_effect = visible
        page._one.side_effect = one
        page._wait.side_effect = wait
        self.assertEqual(self.query_reader(page, field).query_tasks(ORDER), [task()])
        self.assertEqual(initial_reads, 3)
        toggle.click.assert_called_once()
        button.click.assert_called_once()
        field.send_keys.assert_called_once_with(ORDER)

    def test_open_filter_with_late_field_is_not_toggled_closed(self):
        page = Mock()
        field, toggle, button = Mock(), Mock(), Mock()
        field.get_attribute.return_value = ORDER
        toggle.get_attribute.return_value = "worksheet-main-content-bar-item-set open-style"
        page._visible.return_value = []

        def one(selector, **_):
            if selector == '#list_1891 textarea':
                toggle.click.assert_not_called()
                return field  # _one waits until the already expanding field is ready.
            return toggle if selector == 'button.worksheet-main-content-bar-item-set' else button

        def execute(script):
            if "gift-banniu-list" in script:
                return {"pager": "共 1 条", "rows": [task()]}
            if "gift-banniu-fresh" in script:
                return True

        page._one.side_effect = one
        page.driver.execute_script.side_effect = execute
        page._wait.side_effect = lambda predicate, _: predicate(page.driver)
        reader = self.query_reader(page, field)
        reader._order_filter.side_effect = lambda: (toggle.click.assert_not_called(), field)[1]
        self.assertEqual(reader.query_tasks(ORDER), [task()])
        toggle.click.assert_not_called()
        button.click.assert_called_once()
        field.send_keys.assert_called_once_with(ORDER)


class OrderFilterTests(unittest.TestCase):
    def filter_reader(self, *, order_present=True, operator_text="等于", other_count=0):
        page = Mock()
        controls = {name: Mock() for name in ("field", "title", "add", "search", "option", "operator", "equals")}
        controls["operator"].text = operator_text
        state = {"order_present": order_present, "menu": False,
                 "others": [Mock() for _ in range(other_count)]}
        originals = list(state["others"])
        for item in originals:
            item.delete.click.side_effect = lambda item=item: state["others"].remove(item)
        controls["add"].click.side_effect = lambda: state.update(menu=True)
        controls["option"].click.side_effect = lambda: state.update(order_present=True)
        controls["title"].click.side_effect = lambda: state.update(menu=False)
        controls["equals"].click.side_effect = lambda: setattr(controls["operator"], "text", "等于")

        def visible(_, selector):
            if selector == '.advancedFilter-top-item:not(#list_1891)':
                return list(state["others"])
            if selector == '#list_1891 textarea':
                return [controls["field"]] if state["order_present"] else []
            if selector == '.height-search-bottom-right-input input':
                return [controls["search"]] if state["menu"] else []
            self.fail("未预期的筛选读取：" + selector)

        def one(selector, *, root=None, text=None):
            if selector == '.advancedFilter':
                return Mock()
            if selector == '.bnRule-del':
                return root.delete
            mapping = {'.height-search-bottom-right-add': "add",
                       '.height-search-bottom-right-input input': "search",
                       '.height-search-bottom-right-drop[title="订单号"]': "option",
                       '#list_1891 .advancedFilter-title[title="订单号"]': "title",
                       '#list_1891 textarea': "field", '#list_1891 .bnRule-down': "operator",
                       '.bnDropdown-drop-title[title="等于"]': "equals"}
            self.assertIn(selector, mapping)  # No save button or task mutation belongs in this flow.
            if selector == '#list_1891 textarea':
                self.assertTrue(state["order_present"])
            return controls[mapping[selector]]

        def wait(predicate, message):
            result = predicate(page.driver)
            if not result:
                raise PlatformOperationError(message)
            return result

        page._visible.side_effect = visible
        page._one.side_effect = one
        page._wait.side_effect = wait
        return BanniuReceiptReader(page), controls, state, originals

    def test_removes_all_non_order_conditions_without_saving_or_readding_order(self):
        reader, controls, state, originals = self.filter_reader(other_count=2)
        self.assertIs(reader._order_filter(), controls["field"])
        self.assertEqual(state["others"], [])
        for item in originals:
            item.delete.click.assert_called_once()
        controls["add"].click.assert_not_called()
        controls["operator"].click.assert_not_called()
        controls["equals"].click.assert_not_called()
        controls["title"].click.assert_not_called()

    def test_missing_order_is_added_once_and_multiselect_menu_is_closed_before_exact_match(self):
        reader, controls, state, _ = self.filter_reader(order_present=False, other_count=2)
        self.assertIs(reader._order_filter(), controls["field"])
        controls["add"].click.assert_called_once()
        controls["search"].clear.assert_called_once()
        controls["search"].send_keys.assert_called_once_with("订单号")
        controls["option"].click.assert_called_once()
        self.assertTrue(state["order_present"])
        self.assertFalse(state["menu"])
        controls["title"].click.assert_called_once()

    def test_non_equal_operator_is_changed_once_and_verified(self):
        reader, controls, _, _ = self.filter_reader(operator_text="不等于")
        self.assertIs(reader._order_filter(), controls["field"])
        controls["operator"].click.assert_called_once()
        controls["equals"].click.assert_called_once()
        self.assertEqual(controls["operator"].text, "等于")

    def test_delete_failure_does_not_click_again_or_continue_to_order_field(self):
        reader, controls, _, originals = self.filter_reader(other_count=1)
        originals[0].delete.click.side_effect = None
        with self.assertRaisesRegex(PlatformOperationError, "其他筛选条件未清除"):
            reader._order_filter()
        originals[0].delete.click.assert_called_once()
        controls["add"].click.assert_not_called()
        controls["operator"].click.assert_not_called()

    def test_operator_that_does_not_change_fails_without_repeat_click(self):
        reader, controls, _, _ = self.filter_reader(operator_text="不等于")
        controls["equals"].click.side_effect = None
        with self.assertRaisesRegex(PlatformOperationError, "未设置为精确匹配"):
            reader._order_filter()
        controls["operator"].click.assert_called_once()
        controls["equals"].click.assert_called_once()


if __name__ == "__main__":
    unittest.main()
