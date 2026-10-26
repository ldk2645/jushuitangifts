"""认证边界回归；显式 fake 页面及回调，不读账号、不访问真实平台。"""
import unittest
from datetime import datetime
from unittest.mock import Mock, call, patch

from selenium.common.exceptions import TimeoutException

from gift_registry.banniu_page import BANNIU_REGISTRATION_URL, BanniuPage
from gift_registry.banniu_receipt import BanniuReceiptReader
from gift_registry.jushuitan_page import ORDER_URL, JushuitanPage
from gift_registry.workflow import OrderSnapshot, PlatformOperationError, RegistrationRequest, SubmissionReceipt


ORDER = "000009"
REQUEST = RegistrationRequest(ORDER, ("000SKU",), ("合成礼品",))
BEFORE = OrderSnapshot(ORDER, True)


class ReadOnlyAuthenticationGuardsTests(unittest.TestCase):
    def test_jst_checks_authentication_after_navigation_before_page_wait(self):
        driver, guard = Mock(), Mock(return_value=False)
        page = JushuitanPage(driver, authenticate=guard)
        page._wait = Mock(side_effect=PlatformOperationError("stop after navigation"))
        with self.assertRaises(PlatformOperationError):
            page._query(ORDER)
        driver.get.assert_called_once_with(ORDER_URL)
        guard.assert_called_once_with(target_url=ORDER_URL)

    def test_jst_repeats_whole_read_only_when_login_was_recovered(self):
        page = JushuitanPage(Mock(), authenticate=Mock(return_value=True))
        page._query_order_once = Mock(side_effect=[PlatformOperationError("expired"), BEFORE])
        self.assertIs(page.query_order(ORDER), BEFORE)
        self.assertEqual(page._query_order_once.call_args_list, [call(ORDER), call(ORDER)])
        page.authenticate.assert_called_once_with()

    def test_jst_does_not_repeat_unrelated_failure_or_second_failed_read(self):
        for guard, expected_calls in ((Mock(return_value=False), 1), (Mock(return_value=True), 2), (None, 1)):
            with self.subTest(guard=guard):
                page = JushuitanPage(Mock(), authenticate=guard)
                page._query_order_once = Mock(side_effect=PlatformOperationError("read failed"))
                with self.assertRaises(PlatformOperationError):
                    page.query_order(ORDER)
                self.assertEqual(page._query_order_once.call_count, expected_calls)
                if guard is not None:
                    guard.assert_called_once_with()

    def test_invalid_order_never_calls_authentication(self):
        page = JushuitanPage(Mock(), authenticate=Mock())
        with self.assertRaises(PlatformOperationError):
            page.query_order(9)
        page.authenticate.assert_not_called()

    def test_banniu_read_checks_authentication_after_navigation(self):
        driver, guard = Mock(), Mock(return_value=False)
        page = BanniuPage(driver, authenticate=guard)
        page._visible = Mock(return_value=[])
        page._one = Mock(side_effect=PlatformOperationError("stop after navigation"))
        reader = BanniuReceiptReader(page)
        with self.assertRaises(PlatformOperationError):
            reader._query_tasks_once(ORDER)
        driver.get.assert_called_once_with(BANNIU_REGISTRATION_URL)
        guard.assert_called_once_with(target_url=BANNIU_REGISTRATION_URL)

    def test_banniu_query_repeats_once_only_for_actual_auth_recovery(self):
        for recovered, calls in ((True, 2), (False, 1)):
            with self.subTest(recovered=recovered):
                guard = Mock(return_value=recovered)
                reader = BanniuReceiptReader(BanniuPage(Mock(), authenticate=guard))
                reader._query_tasks_once = Mock(side_effect=PlatformOperationError("read failed"))
                with self.assertRaises(PlatformOperationError):
                    reader.query_tasks(ORDER)
                self.assertEqual(reader._query_tasks_once.call_count, calls)
                guard.assert_called_once_with()

    def test_mock_page_without_explicit_callback_does_not_create_authentication(self):
        reader = BanniuReceiptReader(Mock())
        self.assertIsNone(reader.authenticate)
        reader._query_tasks_once = Mock(side_effect=PlatformOperationError("read failed"))
        with self.assertRaises(PlatformOperationError):
            reader.query_tasks(ORDER)
        reader._query_tasks_once.assert_called_once_with(ORDER)


class FormAuthenticationGuardsTests(unittest.TestCase):
    def make_prepare_page(self, guard):
        driver = Mock()
        driver.current_url = "https://work.bytenew.com/"
        driver.execute_script.return_value = True
        page = BanniuPage(driver, authenticate=guard)
        page._visible = Mock(return_value=[])
        page._fill_prepared = Mock()
        return page

    def test_initial_login_recovery_can_precede_one_form_fill(self):
        guard = Mock(return_value=True)
        page = self.make_prepare_page(guard)
        page.prepare(REQUEST, BEFORE)
        guard.assert_called_once_with(target_url=BANNIU_REGISTRATION_URL)
        page._fill_prepared.assert_called_once_with(REQUEST)

    def test_mid_form_login_recovery_does_not_fill_form_again(self):
        guard = Mock(side_effect=[False, True])
        page = self.make_prepare_page(guard)
        page._fill_prepared.side_effect = PlatformOperationError("expired during fill")
        with self.assertRaisesRegex(PlatformOperationError, "未重新填写或提交"):
            page.prepare(REQUEST, BEFORE)
        page._fill_prepared.assert_called_once_with(REQUEST)
        self.assertEqual(guard.call_args_list, [call(target_url=BANNIU_REGISTRATION_URL), call()])

    def make_submit_page(self, guard):
        page = BanniuPage(Mock(), authenticate=guard, timeout=.01)
        page.driver.execute_script.return_value = True
        page.prepare = Mock()
        page._validate_prepared = Mock()
        page._dialog = Mock()
        page._visible = Mock(return_value=[])
        button = Mock()
        button.is_enabled.return_value = True
        page._one = Mock(return_value=button)
        reader = Mock()
        reader.query_tasks.return_value = [{"task_id": "00001"}]
        reader.wait_for_receipt.return_value = SubmissionReceipt(True, created_record_ids=("00002",))
        return page, button, reader

    def test_recovery_before_click_invalidates_prepared_form_without_click(self):
        guard = Mock(side_effect=[False, True, False])
        page, button, reader = self.make_submit_page(guard)
        with patch("gift_registry.banniu_receipt.BanniuReceiptReader", return_value=reader):
            with self.assertRaisesRegex(PlatformOperationError, "原登记表单已失效"):
                page.submit(REQUEST, BEFORE)
            with self.assertRaisesRegex(PlatformOperationError, "禁止再次提交"):
                page.submit(REQUEST, BEFORE)
        page.prepare.assert_called_once()
        button.click.assert_not_called()
        reader.wait_for_receipt.assert_not_called()
        self.assertEqual(guard.call_count, 3)

    def test_uncertain_click_recovers_login_but_never_clicks_or_submits_twice(self):
        guard = Mock(side_effect=[False, False, True])
        page, button, reader = self.make_submit_page(guard)
        button.click.side_effect = TimeoutException("synthetic uncertain click")
        with patch("gift_registry.banniu_receipt.BanniuReceiptReader", return_value=reader):
            with self.assertRaisesRegex(PlatformOperationError, "禁止再次提交"):
                page.submit(REQUEST, BEFORE)
            with self.assertRaisesRegex(PlatformOperationError, "禁止再次提交"):
                page.submit(REQUEST, BEFORE)
        button.click.assert_called_once()
        page.prepare.assert_called_once()
        reader.query_tasks.assert_called_once()
        reader.wait_for_receipt.assert_not_called()

    def test_normal_submission_keeps_the_unique_submit_click(self):
        guard = Mock(return_value=False)
        page, button, reader = self.make_submit_page(guard)
        with patch("gift_registry.banniu_receipt.BanniuReceiptReader", return_value=reader):
            result = page.submit(REQUEST, BEFORE)
        self.assertEqual(result.created_record_ids, ("00002",))
        self.assertEqual(guard.call_count, 2)
        button.click.assert_called_once()
        reader.wait_for_receipt.assert_called_once()

    def test_receipt_detail_login_recovery_preserves_original_baseline_and_time(self):
        guard = Mock(return_value=True)
        page = BanniuPage(Mock(), authenticate=guard)
        page.submit = Mock()
        reader = BanniuReceiptReader(page)
        new_task = {"task_id": "00002"}
        reader.query_tasks = Mock(return_value=[new_task])
        reader.open_task = Mock()
        reader.read_detail = Mock(side_effect=[PlatformOperationError("expired detail"),
                                              SubmissionReceipt(True, created_record_ids=("00010",))])
        original_ids = {"00001"}
        submitted_at = datetime(2026, 10, 25, 10, 0, 0)
        with patch("gift_registry.banniu_receipt.time.sleep"):
            result = reader.wait_for_receipt(REQUEST, BEFORE, original_ids, submitted_at)
        self.assertEqual(result.created_record_ids, ("00010",))
        self.assertEqual(original_ids, {"00001"})
        self.assertEqual(reader.query_tasks.call_args_list, [call(ORDER), call(ORDER)])
        self.assertEqual(reader.read_detail.call_args_list, [call(REQUEST, BEFORE, "00002", not_before=submitted_at)] * 2)
        page.submit.assert_not_called()
        guard.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
