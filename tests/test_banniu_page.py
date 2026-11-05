"""班牛表单隔离测试；不连接真实浏览器或业务平台。"""

import unittest
from datetime import datetime
from itertools import chain, repeat
from unittest.mock import Mock, patch

from selenium.common.exceptions import ElementClickInterceptedException, StaleElementReferenceException, NoSuchWindowException, TimeoutException

from gift_registry.banniu_page import BanniuPage, GIFT_COLUMNS, gifts_match, table_records
from gift_registry.workflow import OrderSnapshot, PlatformOperationError, Recipient, RegistrationRequest, SubmissionReceipt


SKU = "N00A0X070C0DXM5J01"
NAME = "平板蓝牙鼠标键盘套装"


def gift(**updates):
    return {"退换补类型": "补发", "商家编码": SKU, "货品名称": NAME,
            "退换补商品金额": "0", "数量": "1", **updates}


class BanniuPageTests(unittest.TestCase):
    def test_requeried_names_keep_old_snapshot_compatibility_only_for_same_sku(self):
        pairs = (
            ('XS24ANA35002168001', 'K歌神器', '话筒音响一体无线麦克'),
            ('N01B03070C1VZS7B01', '赠品保护套', '小米平板7/7 Pro 赠品保护套'),
            ('N00A0A0I1I0DXM5D01', '胶囊雨伞（带胶囊盒）米白', '胶囊雨伞（带胶囊盒）'),
            ('XS24A7A23002J0Y001', '智能手表', 'S20智能手表'),
        )
        for code, old, actual in pairs:
            with self.subTest(code=code):
                self.assertTrue(gifts_match([gift(商家编码=code, 货品名称=actual)], (code,), (old,)))
                self.assertFalse(gifts_match([gift(商家编码=code, 货品名称=actual)], (SKU,), (old,)))
                self.assertFalse(gifts_match([gift(商家编码=code, 货品名称=actual, 数量='2')], (code,), (old,)))

    def test_only_order_loading_uses_longer_wait_and_enter_is_not_repeated(self):
        page = BanniuPage(Mock())
        for name in ('_click_once', '_one', '_dialog', '_track_form', '_choose_radio', '_set_text', '_send_keys', '_validate_honor_context'):
            setattr(page, name, Mock())
        page._value = Mock(return_value='')
        page._wait = Mock(side_effect=PlatformOperationError('loading timed out'))
        request = RegistrationRequest('001', (SKU,), (NAME,))
        with self.assertRaisesRegex(PlatformOperationError, 'loading timed out'):
            page._fill_prepared(request)
        self.assertEqual(page._wait.call_args.kwargs['timeout'], 60)
        page._send_keys.assert_called_once()
        self.assertEqual(page.timeout, 20)

    def test_text_input_relocates_after_clear_and_returns_current_field_for_enter(self):
        page = BanniuPage(Mock())
        old, empty, populated = Mock(), Mock(), Mock()
        empty.get_attribute.return_value = ''
        page._field = Mock(side_effect=[old, empty, populated])
        page._value = Mock(return_value='0001')
        self.assertIs(page._set_text('#synthetic input', '0001'), populated)
        old.clear.assert_called_once()
        old.send_keys.assert_not_called()
        empty.send_keys.assert_called_once_with('0001')
        empty.clear.assert_not_called()

    def test_maps_observed_headers_instead_of_fixed_column_numbers(self):
        headers = ["", "*退换补类型", "商家编码", "货品名称", "退换补商品金额", "数量", ""]
        values = [["", "补发", SKU, NAME, "0", "1", ""]]
        self.assertEqual(table_records(headers, values), [gift()])
        reordered = ["数量", "商家编码", "货品名称", "退换补类型", "退换补商品金额"]
        self.assertEqual(table_records(reordered, [["1", SKU, NAME, "补发", "0"]]), [gift()])

    def test_missing_duplicate_and_incomplete_table_columns_fail_closed(self):
        for headers, rows in ((GIFT_COLUMNS[:-1], []), ((*GIFT_COLUMNS, "商家编码"), []), (GIFT_COLUMNS, [["补发"]])):
            with self.subTest(headers=headers), self.assertRaises(PlatformOperationError):
                table_records(headers, rows)

    def test_exact_gift_set_names_quantities_and_zero_amount_are_required(self):
        self.assertTrue(gifts_match([gift()], (SKU,), (NAME,)))
        self.assertTrue(gifts_match([gift(数量="1.0", 退换补商品金额="0.00")], (SKU,), (NAME,)))
        for records in ([gift(数量="2")], [gift(退换补商品金额="1")], [gift(数量="NaN")],
                        [gift(货品名称="其他礼品")], [gift(商家编码="other")], [gift(退换补类型="换货")],
                        [gift(), gift()], []):
            with self.subTest(records=records):
                self.assertFalse(gifts_match(records, (SKU,), (NAME,)))

    def search_page(self, search_records, selected):
        page = BanniuPage(Mock(), timeout=.5)
        page.driver.execute_script.return_value = True
        page._close_search = Mock()
        page._value = Mock(return_value="商家编码")
        page._set_text = Mock()
        page._dialog = Mock()
        filter_button = Mock()
        page._one = Mock(return_value=filter_button)
        rows = [Mock() for _ in search_records]
        page._table = Mock(return_value=(rows, search_records))
        page._selected_gifts = Mock(return_value=selected)
        return page, filter_button, rows

    def test_observed_earphone_platform_name_is_accepted_only_for_its_exact_sku(self):
        earphone = gift(商家编码='XS24AMA350021GK001', 货品名称='OWS31蓝牙耳机')
        self.assertTrue(gifts_match([earphone], ('XS24AMA350021GK001',), ('椰球夹耳式耳机',)))
        self.assertFalse(gifts_match([earphone], ('XS24AMA350021GK001',), ('其它耳机',)))
        self.assertFalse(gifts_match([gift(货品名称='OWS31蓝牙耳机')], (SKU,), ('椰球夹耳式耳机',)))
        self.assertFalse(gifts_match([earphone], ('OTHER-SKU',), ('椰球夹耳式耳机',)))

    def test_observed_earphone_search_name_is_added_once_and_quantities_still_checked(self):
        records = [gift(商家编码='XS24AMA350021GK001', 货品名称='OWS31蓝牙耳机')]
        page, button, rows = self.search_page(records, records)
        page._add_gift('XS24AMA350021GK001', '椰球夹耳式耳机', 1, ('XS24AMA350021GK001',), ('椰球夹耳式耳机',), (1,))
        button.click.assert_called_once()
        rows[0].click.assert_called_once()
        self.assertFalse(gifts_match([{**records[0], '数量':'2'}], ('XS24AMA350021GK001',), ('椰球夹耳式耳机',)))

    def test_matching_later_search_result_is_clicked_when_earlier_rows_do_not_match(self):
        page, filter_button, rows = self.search_page(
            [gift(商家编码="wrong"), gift(货品名称="平台旧名称"), gift()], [gift()])
        page._add_gift(SKU, NAME, 1, (SKU,), (NAME,), (1,))
        filter_button.click.assert_called_once()
        rows[0].click.assert_not_called()
        rows[1].click.assert_not_called()
        rows[2].click.assert_called_once()

    def test_same_code_duplicate_names_choose_row_matching_requested_name(self):
        records = [gift(货品名称="平台新名称"), gift(货品名称="平台旧名称")]
        expected_name = "平台旧名称"
        page, filter_button, rows = self.search_page(records, [gift(货品名称=expected_name)])
        page._add_gift(SKU, expected_name, 1, (SKU,), (expected_name,), (1,))
        filter_button.click.assert_called_once()
        rows[0].click.assert_not_called()
        rows[1].click.assert_called_once()

    def test_no_row_matches_code_and_name_so_nothing_is_clicked(self):
        page, filter_button, rows = self.search_page(
            [gift(商家编码="wrong"), gift(货品名称="其它礼品")], [gift()])
        with self.assertRaises(PlatformOperationError):
            page._add_gift(SKU, NAME, 1, (SKU,), (NAME,), (1,))
        filter_button.click.assert_called_once()
        for row in rows:
            row.click.assert_not_called()

    def test_first_matching_result_is_clicked_once_then_selected_values_are_checked(self):
        page, filter_button, rows = self.search_page([gift(), gift()], [gift()])
        page._add_gift(SKU, NAME, 1, (SKU,), (NAME,), (1,))
        filter_button.click.assert_called_once()
        rows[0].click.assert_called_once()
        rows[1].click.assert_not_called()
        page._selected_gifts.assert_called()

    def test_stale_previous_search_result_is_not_clicked_while_refreshing(self):
        page, filter_button, rows = self.search_page([gift()], [gift()])
        page.timeout = 1
        stale = Mock()
        page._table.side_effect = chain([([stale], [gift(商家编码="previous-sku")])], repeat((rows, [gift()])))
        page._add_gift(SKU, NAME, 1, (SKU,), (NAME,), (1,))
        filter_button.click.assert_called_once()
        stale.click.assert_not_called()
        rows[0].click.assert_called_once()

    def test_gift_identity_is_rechecked_after_search_row_is_replaced(self):
        page, filter_button, rows = self.search_page([gift()], [gift()])
        replacement = Mock()
        page._table.side_effect = chain([(rows, [gift()])], repeat(([replacement], [gift(商家编码="other-sku")])))
        with self.assertRaises(PlatformOperationError):
            page._add_gift(SKU, NAME, 1, (SKU,), (NAME,), (1,))
        filter_button.click.assert_called_once()
        rows[0].click.assert_not_called()
        replacement.click.assert_not_called()

    def test_clipped_table_values_are_read_from_one_dom_snapshot(self):
        driver = Mock()
        page = BanniuPage(driver)
        table, row = Mock(), Mock()
        page._field = Mock(return_value=table)
        driver.execute_script.return_value = {
            "headers": ["", "*退换补类型", "商家编码", "货品名称", "退换补商品金额", "数量", ""],
            "rows": [row], "values": [["", "补发", SKU, NAME, "0", "1", ""]], "ambiguousCell": False,
        }
        elements, records = page._table("observed-table-selector")
        self.assertEqual(elements, [row])
        self.assertEqual(records, [gift()])
        table.find_elements.assert_not_called()
        driver.execute_script.assert_called_once()
        self.assertIs(driver.execute_script.call_args.args[1], table)

    def test_ambiguous_dom_cell_is_rejected(self):
        driver = Mock()
        page = BanniuPage(driver)
        page._field = Mock()
        driver.execute_script.return_value = {"ambiguousCell": True}
        with self.assertRaisesRegex(PlatformOperationError, "单元格结构"):
            page._table("observed-table-selector")

    def test_bad_selected_quantity_does_not_trigger_second_add(self):
        page, filter_button, rows = self.search_page([gift()], [gift(数量="2")])
        with self.assertRaises(PlatformOperationError):
            page._add_gift(SKU, NAME, 1, (SKU,), (NAME,), (1,))
        filter_button.click.assert_called_once()
        rows[0].click.assert_called_once()

    def test_gifts_match_uses_explicit_piece_counts(self):
        self.assertTrue(gifts_match([gift(数量="2")], (SKU,), (NAME,), (2,)))
        self.assertFalse(gifts_match([gift(数量="2")], (SKU,), (NAME,), (1,)))
        self.assertTrue(gifts_match([gift(数量="1.0")], (SKU,), (NAME,), (1,)))
        self.assertFalse(gifts_match([gift(数量="2.5")], (SKU,), (NAME,), (2,)))
        self.assertFalse(gifts_match([gift(数量="2")], (SKU,), (NAME,), (2, 2)))
        self.assertFalse(gifts_match([gift(数量="2")], (SKU,), (NAME,), (0,)))

    def test_multi_piece_gift_sets_quantity_then_checks_selected_values(self):
        page, filter_button, rows = self.search_page([gift()], [gift(数量="2")])
        page._set_gift_quantity = Mock()
        page._add_gift(SKU, NAME, 2, (SKU,), (NAME,), (2,))
        filter_button.click.assert_called_once()
        rows[0].click.assert_called_once()
        page._set_gift_quantity.assert_called_once_with(SKU, 2)

    def test_prepare_rejects_invalid_gift_quantities(self):
        page = BanniuPage(Mock())
        for quantities in ((0,), (100,), (1, 2), (True,), ("2",)):
            request = RegistrationRequest("001", (SKU,), (NAME,), None, quantities)
            with self.subTest(quantities=quantities), self.assertRaisesRegex(PlatformOperationError, "礼品信息"):
                page.prepare(request, OrderSnapshot("001", True))

    def submission_page(self):
        driver = Mock()
        driver.execute_script.return_value = True
        page = BanniuPage(driver, timeout=.01)
        page.prepare = Mock()
        page._validate_prepared = Mock()
        page._dialog = Mock()
        page._visible = Mock(return_value=[])
        button = Mock()
        button.is_enabled.return_value = True
        page._one = Mock(return_value=button)
        reader = Mock()
        reader.query_tasks.return_value = [{"task_id": "0001"}]
        reader.wait_for_receipt.return_value = SubmissionReceipt(True, created_record_ids=("0002",))
        return page, button, reader

    def test_click_readiness_failure_before_native_click_is_not_submitted(self):
        from gift_registry.workflow import RegistrationNotSubmittedError
        page, button, reader = self.submission_page()
        page._click_once = Mock(side_effect=PlatformOperationError("班牛目标控件仍被遮罩覆盖，未点击"))
        page._discard_unsubmitted_form = Mock(return_value='closed')
        with patch('gift_registry.banniu_receipt.BanniuReceiptReader', return_value=reader):
            with self.assertRaises(RegistrationNotSubmittedError) as error:
                page.submit(RegistrationRequest('0009', (SKU,), (NAME,)), OrderSnapshot('0009', True))
        self.assertIn('未触发提交按钮', str(error.exception))
        self.assertIn('已关闭本次未提交表单', str(error.exception))
        self.assertFalse(getattr(error.exception, 'click_attempted'))
        self.assertEqual(error.exception.stage, '提交前可点击性检查')
        button.click.assert_not_called()

    def test_native_click_exception_is_uncertain_and_token_is_recorded_before_click(self):
        from gift_registry.workflow import ManualInterventionRequired
        page, button, reader = self.submission_page()
        request = RegistrationRequest('0009', (SKU,), (NAME,))
        page._owned_form = (Mock(), request, 'owned-token')
        page._discard_unsubmitted_form = Mock(return_value='closed')

        def click(element, *, before_click=None, resolve=None):
            before_click()
            raise NoSuchWindowException('private browser payload')

        page._click_once = Mock(side_effect=click)
        with patch('gift_registry.banniu_receipt.BanniuReceiptReader', return_value=reader):
            with self.assertRaises(ManualInterventionRequired) as error:
                page.submit(request, OrderSnapshot('0009', True))
        self.assertIn('结果可能不确定', str(error.exception))
        self.assertNotIn('private browser payload', str(error.exception))
        self.assertTrue(getattr(error.exception, 'click_attempted'))
        self.assertEqual(error.exception.stage, '点击提交或读取回执')
        self.assertEqual(page._submitted_form_token, 'owned-token')
        page._discard_unsubmitted_form.assert_not_called()  # 原生 click 已发起，不能清理成“未提交”。
        button.click.assert_not_called()

    def test_secondary_auth_failure_preserves_proven_pre_submit_failure(self):
        from gift_registry.authentication import AuthenticationRequired
        from gift_registry.workflow import RegistrationNotSubmittedError
        page, button, reader = self.submission_page()
        page.prepare.side_effect = NoSuchWindowException('private browser payload')
        page.authenticate = Mock(side_effect=AuthenticationRequired('safe auth warning'))
        with patch('gift_registry.banniu_receipt.BanniuReceiptReader', return_value=reader):
            with self.assertRaises(RegistrationNotSubmittedError) as error:
                page.submit(RegistrationRequest('0009', (SKU,), (NAME,)), OrderSnapshot('0009', True))
        self.assertIn('填写登记表单', str(error.exception))
        self.assertIn('NoSuchWindowException', str(error.exception))
        self.assertIn('未触发提交按钮', str(error.exception))
        self.assertNotIn('private browser payload', str(error.exception))
        button.click.assert_not_called()

    def test_secondary_auth_failure_after_click_stays_uncertain_and_never_repeats(self):
        from gift_registry.authentication import AuthenticationRequired
        from gift_registry.workflow import RegistrationNotSubmittedError
        page, button, reader = self.submission_page()
        page.authenticate = Mock(side_effect=[False, False, AuthenticationRequired('safe auth warning')])
        reader.wait_for_receipt.side_effect = NoSuchWindowException('private browser payload')
        request = RegistrationRequest('0009', (SKU,), (NAME,))
        with patch('gift_registry.banniu_receipt.BanniuReceiptReader', return_value=reader):
            with self.assertRaises(PlatformOperationError) as error:
                page.submit(request, OrderSnapshot('0009', True))
            self.assertNotIsInstance(error.exception, RegistrationNotSubmittedError)
            self.assertIn('结果可能不确定', str(error.exception))
            self.assertNotIn('private browser payload', str(error.exception))
            with self.assertRaisesRegex(PlatformOperationError, '禁止再次提交'):
                page.submit(request, OrderSnapshot('0009', True))
        button.click.assert_called_once()

    def test_all_gifts_are_prepared_and_submitted_once_then_receipt_is_read(self):
        page, button, reader = self.submission_page()
        request = RegistrationRequest("0009", (SKU, "000OTHER"), (NAME, "另一礼品"))
        before = OrderSnapshot("0009", True)
        submitted_at = datetime(2026, 10, 23, 10, 0, 0)
        with patch("gift_registry.banniu_receipt.BanniuReceiptReader", return_value=reader), \
                patch("gift_registry.banniu_page.datetime") as clock:
            clock.now.return_value = submitted_at
            receipt = page.submit(request, before)
            reordered = RegistrationRequest("0009", tuple(reversed(request.gift_ids)), tuple(reversed(request.gift_names)))
            with self.assertRaisesRegex(PlatformOperationError, "禁止再次提交"):
                page.submit(reordered, before)
        self.assertEqual(receipt.created_record_ids, ("0002",))
        reader.query_tasks.assert_called_once_with("0009")
        page.prepare.assert_called_once_with(request, before)
        page._validate_prepared.assert_called_once_with(request)
        button.click.assert_called_once()
        reader.wait_for_receipt.assert_called_once_with(request, before, {"0001"}, submitted_at)

    def test_receipt_time_guard_uses_attempt_start_captured_before_preparing_form(self):
        page, button, reader = self.submission_page()
        request = RegistrationRequest("0009", (SKU,), (NAME,))
        before = OrderSnapshot("0009", True)
        attempt_started_at = datetime(2026, 10, 23, 9, 59, 58)
        with patch("gift_registry.banniu_receipt.BanniuReceiptReader", return_value=reader), \
                patch("gift_registry.banniu_page.datetime") as clock:
            clock.now.return_value = attempt_started_at
            page.submit(request, before)
        # 回执时间下限在填写表单之前取一次；提交按钮点击时的瞬时时间不能代替它，
        # 否则班牛秒级创建时间与本地时钟的±秒偏差会把本单误判为旧任务。
        clock.now.assert_called_once()
        reader.wait_for_receipt.assert_called_once_with(request, before, {"0001"}, attempt_started_at)

    def test_explicit_retry_rechecks_empty_task_baseline_before_opening_form(self):
        from gift_registry.workflow import RegistrationNotSubmittedError
        page, button, reader = self.submission_page()
        request = RegistrationRequest("0009", (SKU,), (NAME,))
        with patch("gift_registry.banniu_receipt.BanniuReceiptReader", return_value=reader):
            with self.assertRaisesRegex(RegistrationNotSubmittedError, "出现了该订单任务"):
                page.submit(request, OrderSnapshot("0009", True), require_empty_baseline=True)
        page.prepare.assert_not_called()
        button.click.assert_not_called()
        reader.wait_for_receipt.assert_not_called()

    def test_retry_checks_old_gift_sets_before_preparing_or_clicking(self):
        from gift_registry.workflow import RegistrationNotSubmittedError
        for allowed in (True, False):
            with self.subTest(allowed=allowed):
                page, button, reader = self.submission_page()
                reader.check_distinct_gifts = Mock(return_value=allowed)
                request = RegistrationRequest('0009', (SKU,), (NAME,))
                with patch('gift_registry.banniu_receipt.BanniuReceiptReader', return_value=reader):
                    if allowed:
                        page.submit(request, OrderSnapshot('0009', True), require_distinct_gifts=True)
                        button.click.assert_called_once()
                    else:
                        with self.assertRaises(RegistrationNotSubmittedError):
                            page.submit(request, OrderSnapshot('0009', True), require_distinct_gifts=True)
                        page.prepare.assert_not_called()
                        button.click.assert_not_called()
                reader.check_distinct_gifts.assert_called_once_with(request, reader.query_tasks.return_value)

    def test_submit_observer_runs_only_after_native_click_returns(self):
        for failing in (False, True):
            page, button, reader = self.submission_page()
            if failing:
                button.click.side_effect = ElementClickInterceptedException("test")
            observed = Mock(side_effect=lambda: self.assertEqual(button.click.call_count, 1))
            with patch("gift_registry.banniu_receipt.BanniuReceiptReader", return_value=reader):
                try:
                    page.submit(RegistrationRequest("0009", (SKU,), (NAME,)), OrderSnapshot("0009", True), on_submitted=observed)
                except PlatformOperationError:
                    self.assertTrue(failing)
            self.assertEqual(observed.call_count, 0 if failing else 1)

    def test_click_exception_dialog_timeout_and_receipt_timeout_never_retry(self):
        request = RegistrationRequest("0009", (SKU,), (NAME,))
        before = OrderSnapshot("0009", True)
        for failure in ("click", "dialog", "receipt"):
            page, button, reader = self.submission_page()
            if failure == "click":
                button.click.side_effect = TimeoutException("test-only uncertain click")
            elif failure == "dialog":
                page._visible.return_value = [Mock()]
            else:
                reader.wait_for_receipt.side_effect = PlatformOperationError("回执超时；禁止再次提交")
            with self.subTest(failure=failure), patch(
                    "gift_registry.banniu_receipt.BanniuReceiptReader", return_value=reader):
                with self.assertRaises(PlatformOperationError):
                    page.submit(request, before)
                with self.assertRaisesRegex(PlatformOperationError, "禁止再次提交"):
                    page.submit(request, before)
            button.click.assert_called_once()
            page.prepare.assert_called_once()
            reader.query_tasks.assert_called_once()
            if failure != "receipt":
                reader.wait_for_receipt.assert_not_called()

    def test_baseline_prepare_and_disabled_button_failures_never_submit(self):
        request = RegistrationRequest("0009", (SKU,), (NAME,))
        before = OrderSnapshot("0009", True)
        for failure in ("baseline", "prepare", "validation", "button"):
            page, button, reader = self.submission_page()
            if failure == "baseline":
                reader.query_tasks.side_effect = PlatformOperationError("列表未完整刷新")
            elif failure == "prepare":
                page.prepare.side_effect = PlatformOperationError("表单准备失败")
            elif failure == "validation":
                page._validate_prepared.side_effect = PlatformOperationError("礼品不一致")
            else:
                button.is_enabled.return_value = False
            with self.subTest(failure=failure), patch(
                    "gift_registry.banniu_receipt.BanniuReceiptReader", return_value=reader):
                with self.assertRaises(PlatformOperationError):
                    page.submit(request, before)
                with self.assertRaisesRegex(PlatformOperationError, "禁止再次提交"):
                    page.submit(request, before)
            button.click.assert_not_called()
            reader.wait_for_receipt.assert_not_called()
            reader.query_tasks.assert_called_once()

    def test_pre_submit_click_interception_reports_stage_without_sensitive_exception_text(self):
        page, button, reader = self.submission_page()
        reader.query_tasks.side_effect = ElementClickInterceptedException('secret-cookie-placeholder')
        request = RegistrationRequest('0009', (SKU,), (NAME,))
        with patch('gift_registry.banniu_receipt.BanniuReceiptReader', return_value=reader):
            with self.assertRaises(PlatformOperationError) as error:
                page.submit(request, OrderSnapshot('0009', True))
            detail = str(error.exception)
            self.assertIn('查询提交前任务基线', detail)
            self.assertIn('未触发提交按钮', detail)
            self.assertIn('ElementClickInterceptedException', detail)
            self.assertNotIn('secret-cookie-placeholder', detail)
            with self.assertRaisesRegex(PlatformOperationError, '禁止再次提交'):
                page.submit(request, OrderSnapshot('0009', True))
        button.click.assert_not_called()
        page.prepare.assert_not_called()
        reader.query_tasks.assert_called_once()

    def test_submit_click_failure_remains_uncertain_and_is_never_repeated(self):
        page, button, reader = self.submission_page()
        button.click.side_effect = ElementClickInterceptedException('secret-token-placeholder')
        request = RegistrationRequest('0009', (SKU,), (NAME,))
        with patch('gift_registry.banniu_receipt.BanniuReceiptReader', return_value=reader):
            with self.assertRaisesRegex(PlatformOperationError, '结果可能不确定') as error:
                page.submit(request, OrderSnapshot('0009', True))
            self.assertNotIn('secret-token-placeholder', str(error.exception))
            with self.assertRaisesRegex(PlatformOperationError, '禁止再次提交'):
                page.submit(request, OrderSnapshot('0009', True))
        button.click.assert_called_once()

    def test_click_waits_for_actual_hit_target_then_clicks_once(self):
        driver, element = Mock(), Mock()
        driver.execute_script.side_effect = [None, False, True]
        page = BanniuPage(driver, timeout=1)
        page._click_once(element)
        element.click.assert_called_once()
        self.assertEqual(driver.execute_script.call_count, 3)

    def test_permanent_transparent_overlay_stops_without_click(self):
        driver, element = Mock(), Mock()
        driver.execute_script.return_value = False
        page = BanniuPage(driver, timeout=.01)
        with self.assertRaisesRegex(PlatformOperationError, '遮罩覆盖'):
            page._click_once(element)
        element.click.assert_not_called()

    def test_virtual_row_is_resolved_again_after_scroll_before_first_click(self):
        driver, previous, current = Mock(), Mock(), Mock()
        driver.execute_script.side_effect = [StaleElementReferenceException(), None, True]
        resolve = Mock(side_effect=chain([previous], repeat(current)))
        page = BanniuPage(driver, timeout=1)
        page._click_once(previous, resolve=resolve)
        previous.click.assert_not_called()
        current.click.assert_called_once()
        self.assertEqual(resolve.call_count, 3)

    def test_resolved_native_click_error_is_never_retried(self):
        driver, element = Mock(), Mock()
        driver.execute_script.return_value = True
        element.click.side_effect = StaleElementReferenceException()
        resolve = Mock(return_value=element)
        with self.assertRaises(StaleElementReferenceException):
            BanniuPage(driver, timeout=1)._click_once(element, resolve=resolve)
        element.click.assert_called_once()
        self.assertEqual(resolve.call_count, 2)

    def test_changed_address_submission_stops_before_browser_or_receipt_query(self):
        page, button, reader = self.submission_page()
        request = RegistrationRequest("0009", (SKU,), (NAME,), Recipient("测试", "0001", "地址"))
        with patch("gift_registry.banniu_receipt.BanniuReceiptReader", return_value=reader) as factory:
            with self.assertRaisesRegex(PlatformOperationError, "暂不支持改地址"):
                page.submit(request, OrderSnapshot("0009", True))
        factory.assert_not_called()
        page.prepare.assert_not_called()
        button.click.assert_not_called()

    def test_existing_user_form_is_never_overwritten(self):
        driver = Mock()
        page = BanniuPage(driver)
        page._visible = Mock(return_value=[Mock()])
        with self.assertRaisesRegex(PlatformOperationError, "已有未关闭"):
            page.prepare(RegistrationRequest("0009", (SKU,), (NAME,)), OrderSnapshot("0009", True))
        driver.get.assert_not_called()
        driver.find_elements.assert_not_called()

    def test_invalid_request_or_missing_baseline_never_opens_form(self):
        for request, before in ((RegistrationRequest(9, (SKU,), (NAME,)), OrderSnapshot("9", True)),
                                (RegistrationRequest("0009", (SKU, SKU), (NAME, NAME)), OrderSnapshot("0009", True)),
                                (RegistrationRequest("0009", (SKU,), (NAME,)), OrderSnapshot("0009", False))):
            driver = Mock()
            with self.subTest(request=request), self.assertRaises(PlatformOperationError):
                BanniuPage(driver).prepare(request, before)
            self.assertEqual(driver.mock_calls, [])

    def test_reconstructed_address_must_equal_full_input(self):
        page = BanniuPage(Mock())
        page._dialog = Mock()
        page._visible = Mock(return_value=[])
        values = {"省": "浙江省", "市": "杭州市", "区": "西湖区"}
        page._value = Mock(side_effect=lambda selector: "测试路01号" if "textarea" in selector
                           else next(value for key, value in values.items() if f"选择{key}" in selector))
        self.assertTrue(page._address_matches("浙江省杭州市西湖区测试路01号"))
        self.assertFalse(page._address_matches("浙江省杭州市西湖区测试路02号"))


if __name__ == "__main__":
    unittest.main()
