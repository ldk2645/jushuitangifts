"""认证接入隔离测试；认证器和浏览器均显式 fake，不读取本机账号配置。"""
import unittest
from unittest.mock import Mock, patch

from gift_registry.authentication import AuthenticationRequired, ENTRY_URLS
from gift_registry.browser import SiteBrowser
from gift_registry.config import SITES, Site
from gift_registry.platform_operations import SeleniumOperations


class BrowserAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.auth = Mock()
        self.auth.ensure_authenticated.return_value = False
        self.auth.is_authenticated.return_value = True
        self.store = Mock()
        self.store.load.return_value = None
        self.browser = SiteBrowser(SITES[1], self.store, authenticator=self.auth)
        self.browser.driver = Mock()
        self.browser.capture = Mock(return_value={"cookie_count": 0, "last_saved": "00:00:00"})

    def test_open_uses_protected_entry_and_retains_window_for_manual_auth(self):
        self.auth.ensure_authenticated.side_effect = AuthenticationRequired("需要人工验证码")
        driver = Mock()
        with patch("gift_registry.browser.create_driver", return_value=driver):
            self.browser.open()
        driver.get.assert_called_once_with(ENTRY_URLS["banniu"])
        self.assertIs(self.browser.driver, driver)
        driver.quit.assert_not_called()
        self.assertEqual(self.browser.auth_warning, "需要人工验证码")

    def test_failed_auth_save_preserves_previous_snapshot_without_capture(self):
        self.browser.snapshot = {"cookies": [{"name": "synthetic"}]}
        self.auth.ensure_authenticated.side_effect = AuthenticationRequired("自动登录未通过")
        result = self.browser.save()
        self.assertFalse(result["confirmed"])
        self.assertEqual(result["cookie_count"], 1)
        self.browser.capture.assert_not_called()
        self.store.save.assert_not_called()

    def test_recovered_auth_captures_once_and_removes_stale_restore_script(self):
        self.auth.ensure_authenticated.return_value = True
        self.browser.restore_script = "synthetic-script"
        self.assertTrue(self.browser.ensure_authenticated(target_url=ENTRY_URLS["banniu"]))
        self.assertTrue(self.browser.confirmed)
        self.assertTrue(self.browser.auto_logged_in)
        self.browser.capture.assert_called_once()
        self.browser.driver.execute_cdp_cmd.assert_called_once_with(
            "Page.removeScriptToEvaluateOnNewDocument", {"identifier": "synthetic-script"})

    def test_failed_snapshot_update_does_not_repeat_login_or_claim_capture(self):
        self.auth.ensure_authenticated.return_value = True
        self.browser.capture.side_effect = OSError("synthetic-error")
        self.assertTrue(self.browser.ensure_authenticated())
        self.auth.ensure_authenticated.assert_called_once()
        self.assertIn("保留上次", self.browser.restore_warning)

    def test_capture_while_login_visible_never_writes_session(self):
        browser = SiteBrowser(SITES[1], self.store, authenticator=self.auth)
        browser.driver = Mock()
        browser.driver.current_url = ENTRY_URLS["banniu"]
        self.auth.is_authenticated.return_value = False
        self.assertIsNone(browser.capture())
        browser.driver.execute_script.assert_not_called()
        self.store.save.assert_not_called()
        self.auth.ensure_authenticated.assert_not_called()

    def test_shutdown_never_attempts_login(self):
        with patch("gift_registry.browser.quit_owned_driver", return_value={"closed": True, "forced": False}):
            self.browser.close()
        self.auth.ensure_authenticated.assert_not_called()

    def test_later_successful_snapshot_clears_stale_automatic_login_warning(self):
        browser = SiteBrowser(SITES[1], self.store, authenticator=self.auth)
        browser.driver = Mock()
        browser.driver.current_url = ENTRY_URLS["banniu"]
        browser.driver.execute_script.return_value = {"local": {}, "session": {}}
        browser.driver.execute_cdp_cmd.return_value = {"cookies": []}
        browser.auto_logged_in = True
        browser.restore_warning = "自动登录已恢复，本次会话快照未能更新"
        self.assertIsNotNone(browser.capture())
        self.assertEqual(browser.restore_warning, "")

    def test_custom_test_site_has_no_production_authenticator(self):
        browser = SiteBrowser(Site("isolated", "测试", "http://127.0.0.1/", ("127.0.0.1",)), self.store)
        self.assertIsNone(browser.authenticator)
        self.assertFalse(browser.ensure_authenticated())


class AuthenticationFactoryTests(unittest.TestCase):
    def test_default_page_factories_get_bound_auth_guards(self):
        browsers = Mock()
        with patch("gift_registry.platform_operations.JushuitanPage") as factory:
            operations = SeleniumOperations(browsers=browsers, query_factory=factory)
            operations._jushuitan()
            factory.assert_called_once_with(browsers.driver.return_value,
                                            authenticate=browsers.get.return_value.ensure_authenticated)
        with patch("gift_registry.banniu_page.BanniuPage") as factory:
            operations = SeleniumOperations(browsers=browsers)
            operations._banniu()
            factory.assert_called_once_with(browsers.driver.return_value,
                                            authenticate=browsers.get.return_value.ensure_authenticated)

    def test_custom_factories_keep_single_driver_argument_and_never_open_auth_store(self):
        browsers, query, form = Mock(), Mock(), Mock()
        operations = SeleniumOperations(browsers=browsers, query_factory=query, form_factory=form)
        operations._jushuitan()
        operations._banniu()
        query.assert_called_once_with(browsers.driver.return_value)
        form.assert_called_once_with(browsers.driver.return_value)
        browsers.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
