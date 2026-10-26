import unittest
from unittest.mock import Mock

from gift_registry.platform_operations import SeleniumOperations
from gift_registry.workflow import GiftRecord, OrderSnapshot, PlatformOperationError, Recipient, RegistrationRequest, SubmissionReceipt


class PlatformOperationsTests(unittest.TestCase):
    def test_replacement_driver_rebuilds_adapter_without_replaying_submit(self):
        from types import SimpleNamespace
        browsers = Mock()
        first, second = object(), object()
        browsers.driver.side_effect = [first, first, second]
        pages = []
        def factory(driver):
            page = SimpleNamespace(driver=driver, submit=Mock())
            pages.append(page)
            return page
        operations = SeleniumOperations(browsers=browsers, form_factory=factory)
        original = operations._banniu()
        self.assertIs(operations._banniu(), original)
        replacement = operations._banniu()
        self.assertIsNot(original, replacement)
        self.assertEqual(len(pages), 2)
        for page in pages:
            page.submit.assert_not_called()

    def setUp(self):
        self.recipient = Recipient("测试姓名", "001234567890", "测试地址")
        self.request = RegistrationRequest("00001", ("SKU-A",), ("测试礼品",))
        self.before = OrderSnapshot("00001", True, (GiftRecord("old", ("OLD",)),))
        self.after = OrderSnapshot("00001", True, self.before.records + (GiftRecord("new", ("SKU-A",)),))
        self.browsers = Mock()
        self.query = Mock()
        self.form = Mock()
        self.sleep = Mock()
        self.operations = SeleniumOperations(browsers=self.browsers, query_factory=lambda _: self.query,
                                             form_factory=lambda _: self.form, sleep=self.sleep)

    def test_prepare_start_reserves_profiles_without_opening_or_querying_browser(self):
        self.operations.prepare_start()
        self.browsers.reserve_for_start.assert_called_once_with()
        self.browsers.driver.assert_not_called()
        self.query.query_order.assert_not_called()
        self.form.submit.assert_not_called()

    def test_prepare_start_reports_safe_reservation_error_without_platform_action(self):
        self.browsers.reserve_for_start.side_effect = PlatformOperationError("请先保存并关闭登录助手")
        with self.assertRaisesRegex(PlatformOperationError, "登录助手"):
            self.operations.prepare_start()
        self.browsers.driver.assert_not_called()
        self.query.query_order.assert_not_called()
        self.form.submit.assert_not_called()

    def test_release_unused_start_only_delegates_reservation_cleanup(self):
        self.browsers.release_unused_reservation.return_value = True
        self.assertTrue(self.operations.release_unused_start())
        self.browsers.release_unused_reservation.assert_called_once_with()
        self.browsers.close.assert_not_called()
        self.browsers.driver.assert_not_called()

    def test_disabled_submission_stops_before_any_browser_action(self):
        self.operations.configured = False
        self.assertFalse(self.operations.configured)
        with self.assertRaises(PlatformOperationError):
            self.operations.submit_registration(self.request, self.before)
        self.browsers.driver.assert_not_called()
        self.form.submit.assert_not_called()

    def test_prepare_does_not_submit_and_reuses_browser(self):
        self.operations.prepare_registration(self.request, self.before)
        self.operations.prepare_registration(self.request, self.before)
        self.assertEqual(self.browsers.driver.call_count, 2)
        self.assertEqual(self.form.prepare.call_count, 2)
        self.form.submit.assert_not_called()

    def test_missing_order_stops_before_form(self):
        with self.assertRaises(PlatformOperationError):
            self.operations.prepare_registration(self.request, OrderSnapshot("00001", False))
        self.browsers.driver.assert_not_called()

    def test_original_address_prepare_does_not_require_recipient_data(self):
        self.operations.prepare_registration(self.request, self.before)
        self.form.prepare.assert_called_once_with(self.request, self.before)
        self.form.submit.assert_not_called()

    def test_changed_address_stops_before_prepare_or_submit(self):
        request = RegistrationRequest("00001", ("SKU-A",), ("测试礼品",), self.recipient)
        self.operations.configured = True
        for action in (self.operations.prepare_registration, self.operations.submit_registration):
            with self.subTest(action=action.__name__), self.assertRaisesRegex(PlatformOperationError, "暂不支持改地址"):
                action(request, self.before)
        self.browsers.driver.assert_not_called()
        self.form.prepare.assert_not_called()
        self.form.submit.assert_not_called()

    def test_delayed_new_record_only_repeats_read_queries(self):
        self.query.query_order.side_effect = [self.before, self.before, self.after]
        result = self.operations.wait_for_verification(self.request, self.before, SubmissionReceipt(True))
        self.assertEqual(result, self.after)
        self.assertEqual(self.query.query_order.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)
        self.form.submit.assert_not_called()

    def test_no_new_record_has_a_finite_read_limit(self):
        self.query.query_order.return_value = self.before
        self.assertEqual(self.operations.wait_for_verification(self.request, self.before, SubmissionReceipt(True)), self.before)
        self.assertEqual(self.query.query_order.call_count, 4)
        self.assertEqual(self.sleep.call_count, 3)
        self.form.submit.assert_not_called()

    def test_mismatched_new_record_or_read_error_is_not_hidden_by_retry(self):
        wrong = OrderSnapshot("00001", True, self.before.records + (GiftRecord("new", ("WRONG",), self.recipient),))
        self.query.query_order.return_value = wrong
        self.assertEqual(self.operations.wait_for_verification(self.request, self.before, SubmissionReceipt(True)), wrong)
        self.query.query_order.assert_called_once()
        self.sleep.assert_not_called()
        self.query.query_order.side_effect = PlatformOperationError("测试读取失败")
        with self.assertRaises(PlatformOperationError):
            self.operations.wait_for_verification(self.request, self.before, SubmissionReceipt(True))
        self.form.submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
