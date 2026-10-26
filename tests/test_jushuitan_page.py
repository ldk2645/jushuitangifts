import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from selenium.webdriver.common.keys import Keys

from gift_registry.jushuitan_page import JushuitanPage, ORDER_URL, page_count, parse_order_rows
from gift_registry.workflow import PlatformOperationError


def sample_row(identifier="000048", online="0000611991816470192", sku="000SKU001"):
    return {"index": "0", "internal_id": identifier, "order_no": online,
            "items": [{"title": f"STYLE\r\n{sku}\r\n测试商品\r\n测试规格\r\n￥0", "quantity": "1"}],
            "total_quantity": "1", "recipient": {"name": "(测试姓名)", "phone": "(0013800000000)", "address": "测试省测试地址"}}


class OrderRowsTests(unittest.TestCase):
    def test_ids_and_order_are_strings_without_collecting_recipient(self):
        row = sample_row()
        snapshot = parse_order_rows(row["order_no"], [row], "总共 1 页 1 条记录")
        self.assertTrue(snapshot.found)
        self.assertEqual(snapshot.order_no, "0000611991816470192")
        self.assertEqual(snapshot.records[0].record_id, "000048")
        self.assertEqual(snapshot.records[0].gift_ids, ("000SKU001",))
        self.assertEqual(snapshot.records[0].gift_quantities, (1,))
        self.assertEqual(snapshot.records[0].reference, "")
        self.assertIsNone(snapshot.recipient)
        self.assertIsNone(snapshot.records[0].recipient)

    def test_original_order_quantity_greater_than_one_is_preserved(self):
        row = sample_row()
        row["items"][0]["quantity"] = "3"
        row["total_quantity"] = "3"
        snapshot = parse_order_rows(row["order_no"], [row], "总共 1 页 1 条记录")
        self.assertEqual(snapshot.records[0].gift_quantities, (3,))

    def test_existing_and_added_internal_orders_are_separate_records(self):
        rows = [sample_row(), sample_row(identifier="000049", sku="000GIFT001")]
        rows[1]["index"] = "1"
        snapshot = parse_order_rows(rows[0]["order_no"], rows, "总共 1 页 2 条记录")
        self.assertEqual([record.record_id for record in snapshot.records], ["000048", "000049"])

    def test_recipient_values_are_ignored_in_current_original_address_version(self):
        rows = [sample_row(), sample_row(identifier="000049")]
        rows[1]["recipient"]["address"] = "新地址"
        snapshot = parse_order_rows(rows[0]["order_no"], rows, "总共 1 页 2 条记录")
        self.assertIsNone(snapshot.recipient)
        self.assertTrue(all(record.recipient is None for record in snapshot.records))

    def test_zero_result_is_only_missing_when_count_and_rows_are_both_empty(self):
        self.assertFalse(parse_order_rows("00001", [], "总共 0 页 0 条记录").found)
        with self.assertRaisesRegex(PlatformOperationError, "行数"):
            parse_order_rows("00001", [sample_row()], "总共 0 页 0 条记录")

    def test_incomplete_and_multi_page_results_are_rejected(self):
        for pager in ("总共 2 页 21 条记录", "正在加载", "总共 0 页 1 条记录"):
            with self.subTest(pager=pager), self.assertRaises(PlatformOperationError):
                page_count(pager)
        with self.assertRaisesRegex(PlatformOperationError, "行数"):
            parse_order_rows("00001", [], "总共 1 页 1 条记录")

    def test_wrong_online_order_duplicate_record_and_numeric_ids_are_rejected(self):
        row = sample_row()
        with self.assertRaisesRegex(PlatformOperationError, "线上订单号"):
            parse_order_rows("wrong-order", [row], "总共 1 页 1 条记录")
        with self.assertRaisesRegex(PlatformOperationError, "内部订单号"):
            parse_order_rows(row["order_no"], [row, row], "总共 1 页 2 条记录")
        row["internal_id"] = 48
        with self.assertRaisesRegex(PlatformOperationError, "内部订单号"):
            parse_order_rows(row["order_no"], [row], "总共 1 页 1 条记录")
        with self.assertRaisesRegex(PlatformOperationError, "非空文本"):
            parse_order_rows(123, [], "总共 0 页 0 条记录")

    def test_missing_tooltip_or_quantity_and_truncated_item_list_are_rejected(self):
        variants = [dict(title="STYLE\nSKU", quantity="1"),
                    dict(title="STYLE\nSKU\n商品", quantity="0"),
                    dict(title="STYLE\nSKU\n商品", quantity="?"),
                    dict(title="STYLE\nSKU\n商品", quantity="2")]
        for item in variants:
            row = sample_row()
            row["items"] = [item]
            with self.subTest(item=item), self.assertRaises(PlatformOperationError):
                parse_order_rows(row["order_no"], [row], "总共 1 页 1 条记录")
        row = sample_row()
        row["items"].append(copy.deepcopy(row["items"][0]))
        row["total_quantity"] = "2"
        with self.assertRaisesRegex(PlatformOperationError, "编码重复"):
            parse_order_rows(row["order_no"], [row], "总共 1 页 1 条记录")

    def test_masked_or_missing_recipient_does_not_block_order_item_verification(self):
        for field, value in (("name", "(陈*)"), ("phone", "(1**********)") ,
                             ("address", "省市区……"), ("address", None), ("name", "")):
            row = sample_row()
            row["recipient"][field] = value
            with self.subTest(field=field, value=value):
                snapshot = parse_order_rows(row["order_no"], [row], "总共 1 页 1 条记录")
                self.assertTrue(snapshot.found)
                self.assertIsNone(snapshot.records[0].recipient)
        row.pop("recipient")
        self.assertTrue(parse_order_rows(row["order_no"], [row], "总共 1 页 1 条记录").found)


class FakeElement:
    def __init__(self, text="", on_keys=None):
        self.text = text
        self.value = ""
        self.keys = []
        self.clicks = 0
        self.on_keys = on_keys

    def clear(self):
        self.value = ""

    def send_keys(self, value):
        self.keys.append(value)
        if self.on_keys:
            self.on_keys(value)
        if value != Keys.ENTER:
            self.value += value

    def get_attribute(self, name):
        return self.value if name == "value" else None

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

    def click(self):
        self.clicks += 1


class FakeDriver:
    def __init__(self, *, refreshed=True, pager="总共 1 页 1 条记录"):
        self.refreshed = refreshed
        self.events = []
        self.elements = {"_jt_body": FakeElement(), "s_filter_frame": FakeElement(),
                         "so_id": FakeElement(on_keys=lambda value: self.events.append(("input", value))),
                         "_jt_pagebar": FakeElement(pager)}
        self.switch_to = SimpleNamespace(default_content=lambda: None, frame=lambda _: None,
                                         parent_frame=lambda: None)
        self.urls = []
        self.current_url = "about:blank"
        self.scripts = []
        self.grid_states = []
        self.grid_reads = 0

    def get(self, url):
        self.urls.append(url)
        self.current_url = url

    def find_element(self, by, value):
        return self.elements[value]

    def execute_script(self, script, *args):
        self.scripts.append(script)
        if "gift-order-page-ready" in script:
            return True
        if "gift-query-observer-install" in script:
            self.events.append(("observer", "install"))
            return True
        if "gift-query-observer-read" in script:
            return {"changed": self.refreshed, "age": 1000}
        if "gift-grid-state" in script:
            self.grid_reads += 1
            if self.grid_states:
                return self.grid_states.pop(0)
            row = sample_row(online=self.elements["so_id"].value)
            row.pop("recipient")
            return {"rows": [row], "top": 0, "max": 0, "height": 100,
                    "pager": self.elements["_jt_pagebar"].text}
        if "gift-grid-scroll" in script:
            return True
        return None


class JushuitanQueryTests(unittest.TestCase):
    def test_virtual_rows_are_collected_across_scrolling(self):
        class GridDriver(FakeDriver):
            position = 0

            def execute_script(self, script, *args):
                if "gift-grid-scroll" in script:
                    self.position = args[1]
                    return True
                if "gift-grid-state" in script:
                    row = sample_row(identifier="000048" if self.position == 0 else "000049")
                    row["index"] = "0" if self.position == 0 else "1"
                    return {"rows": [row], "top": self.position, "max": 100, "height": 100,
                            "pager": "总共 1 页 2 条记录"}
                return super().execute_script(script, *args)

        page = JushuitanPage(GridDriver(), timeout=.01)
        rows = page._collect("identity", 2, "总共 1 页 2 条记录")
        self.assertEqual(set(rows), {"0", "1"})
        self.assertEqual(rows["1"]["internal_id"], "000049")

    def test_decrypt_timeout_does_not_click_the_button_again(self):
        driver = FakeDriver()
        cell, button = FakeElement(), FakeElement()

        def find_element(by, selector):
            return button if "h_btn_decrypt_all" in selector else cell

        driver.find_element = find_element
        driver.execute_script = lambda *_: {"rows": [{"index": "0", "recipient": {
            "name": "陈*", "phone": "1**********", "address": "地址***"}}]}
        page = JushuitanPage(driver, timeout=.01)
        with patch("gift_registry.jushuitan_page.ActionChains") as actions:
            with self.assertRaisesRegex(PlatformOperationError, "解密未完成"):
                page._reveal_recipient(driver, "0")
        actions.return_value.move_to_element.assert_called_once_with(cell)
        self.assertEqual(button.clicks, 1)

    def test_query_uses_observed_page_field_and_one_enter_before_reading(self):
        driver = FakeDriver()
        page = JushuitanPage(driver, timeout=.01)
        row = sample_row()
        identity = {key: value for key, value in row.items() if key != "recipient"}
        # 已核对的 DOM pager 是读取依据，后续 WebElement.text 可处于短暂空值。
        driver.elements["_jt_pagebar"].text = ""
        driver.grid_states = [{"rows": [identity], "pager": "总共 1 页 1 条记录"}]
        calls = []

        def collect(mode, count, pager):
            calls.append(mode)
            return {"0": identity if mode == "identity" else {"index": "0", "recipient": row["recipient"]}}

        page._collect = collect
        result = page.query_order(row["order_no"])
        self.assertEqual(driver.urls, [ORDER_URL])
        self.assertEqual(driver.elements["so_id"].keys, [row["order_no"], Keys.ENTER])
        self.assertEqual(calls, ["identity", "identity"])
        self.assertEqual(result.records[0].record_id, "000048")
        self.assertIsNone(result.recipient)
        self.assertIsNone(result.records[0].recipient)
        self.assertEqual(driver.events, [("input", row["order_no"]), ("observer", "install"),
                                         ("input", Keys.ENTER)])

    def test_query_never_reads_or_reveals_recipient_fields(self):
        driver = FakeDriver()
        page = JushuitanPage(driver, timeout=.01,
                             reveal_recipient=lambda *_: self.fail("本版本不应解密收件资料"))
        execute = driver.execute_script

        def read_identity_only(script, *args):
            if "gift-grid-state" in script or "gift-grid-scroll" in script:
                self.assertEqual(args[0], "identity")
            return execute(script, *args)

        driver.execute_script = read_identity_only
        snapshot = page.query_order("0000611991816470192")
        self.assertTrue(snapshot.found)
        self.assertIsNone(snapshot.recipient)
        self.assertIsNone(snapshot.records[0].recipient)

    def test_refresh_waits_for_pager_and_target_rows_without_repeating_enter(self):
        driver = FakeDriver()
        order_no = "0000611991816470192"
        wrong = sample_row(online="another-order")
        correct = sample_row(online=order_no)
        driver.grid_states = [
            {"rows": [], "pager": ""},
            {"rows": [], "pager": "正在加载"},
            {"rows": [wrong], "pager": "总共 1 页 1 条记录"},
            {"rows": [correct], "pager": "单元格数：1 求和：0 总共 1 页 1 条记录 每页记录数 100"},
        ]
        page = JushuitanPage(driver, timeout=2)
        with patch("selenium.webdriver.support.wait.time.sleep"):
            state = page._query(order_no)
        self.assertEqual(driver.grid_reads, 4)
        self.assertEqual(state["rows"][0]["order_no"], order_no)
        self.assertEqual(driver.elements["so_id"].keys.count(Keys.ENTER), 1)

    def test_loading_pager_times_out_without_reading_or_entering_again(self):
        driver = FakeDriver(pager="正在加载")
        page = JushuitanPage(driver, timeout=.01)
        page._collect = lambda *_: self.fail("分页未就绪时不能读取结果")
        with self.assertRaisesRegex(PlatformOperationError, "未确认刷新"):
            page.query_order("0000611991816470192")
        self.assertEqual(driver.elements["so_id"].keys.count(Keys.ENTER), 1)
        self.assertTrue(any("gift-query-observer-stop" in script for script in driver.scripts))

    def test_auxiliary_pager_text_changes_do_not_change_result_count(self):
        driver = FakeDriver()
        row = sample_row()
        driver.grid_states = [{"rows": [row], "top": 0, "max": 0, "height": 100,
                               "pager": "单元格数：1 求和：0 总共 1 页 1 条记录 每页记录数 100"}]
        page = JushuitanPage(driver, timeout=.01)
        self.assertEqual(page._collect("identity", 1, "总共 1 页 1 条记录"), {"0": row})

    def test_result_count_change_during_collection_is_rejected(self):
        driver = FakeDriver()
        driver.grid_states = [{"rows": [sample_row()], "top": 0, "max": 0, "height": 100,
                               "pager": "总共 1 页 2 条记录"}]
        with self.assertRaisesRegex(PlatformOperationError, "结果数量发生变化"):
            JushuitanPage(driver, timeout=.01)._collect("identity", 1, "总共 1 页 1 条记录")

    def test_matching_old_rows_are_not_read_without_refresh_observation(self):
        driver = FakeDriver(refreshed=False)
        page = JushuitanPage(driver, timeout=.01)
        page._collect = lambda *_: self.fail("未刷新时不能读取旧结果")
        with self.assertRaisesRegex(PlatformOperationError, "未确认刷新"):
            page.query_order("0000611991816470192")
        self.assertEqual(driver.elements["so_id"].keys.count(Keys.ENTER), 1)
        self.assertTrue(any("gift-query-observer-stop" in script for script in driver.scripts))

    def test_rows_that_change_during_identity_reread_are_rejected(self):
        driver = FakeDriver()
        page = JushuitanPage(driver, timeout=.01)
        row = sample_row()
        results = iter([{"0": row}, {"0": sample_row(identifier="different-order")}])
        page._collect = lambda *_: next(results)
        with self.assertRaisesRegex(PlatformOperationError, "订单行不一致"):
            page.query_order(row["order_no"])


if __name__ == "__main__":
    unittest.main()
