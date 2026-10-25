"""会话保存异常和关闭时限；仅使用显式 fake，不访问真实浏览器与会话。"""
import unittest
from unittest.mock import Mock, patch

from urllib3.exceptions import ReadTimeoutError

from gift_registry.browser import SiteBrowser
from gift_registry.config import SITES
from gift_registry.driver_shutdown import BrowserShutdownError


class SiteBrowserCloseTests(unittest.TestCase):
    def setUp(self):
        self.browser = SiteBrowser(SITES[1], Mock(), authenticator=False)
        self.driver = Mock()
        self.browser.driver = self.driver

    def test_transport_error_during_capture_still_closes_without_leaking_details(self):
        self.browser.capture = Mock(side_effect=ReadTimeoutError(None, None, "private-token"))
        progress = []
        with patch("gift_registry.browser.quit_owned_driver", return_value={"closed": True, "forced": False}) as close:
            result = self.browser.close(progress=progress.append)
        close.assert_called_once_with(self.driver)
        self.assertTrue(result["closed"])
        self.assertIn("保留上次", result["warning"])
        self.assertNotIn("private-token", result["warning"])
        self.assertIsNone(self.browser.driver)
        self.assertEqual(progress, ["正在保存会话", "正在关闭浏览器"])

    def test_failed_quit_retains_driver_for_manual_close_retry(self):
        self.browser.capture = Mock()
        with patch("gift_registry.browser.quit_owned_driver", side_effect=BrowserShutdownError("尚未关闭")):
            with self.assertRaises(BrowserShutdownError):
                self.browser.close()
        self.assertIs(self.browser.driver, self.driver)

    def test_successful_capture_and_force_close_are_reported(self):
        self.browser.capture = Mock()
        with patch("gift_registry.browser.quit_owned_driver", return_value={"closed": True, "forced": True}):
            result = self.browser.close()
        self.assertTrue(result["forced"])
        self.assertIn("退出超时", result["warning"])
        self.assertIsNone(self.browser.driver)

    def test_progress_callback_failure_does_not_block_cleanup(self):
        self.browser.capture = Mock(side_effect=OSError("private-file-path"))
        with patch("gift_registry.browser.quit_owned_driver", return_value={"closed": True, "forced": False}) as close:
            result = self.browser.close(progress=Mock(side_effect=RuntimeError("private-ui-error")))
        close.assert_called_once()
        self.assertTrue(result["closed"])

    def test_already_closed_is_idempotent(self):
        self.browser.driver = None
        self.browser.capture = Mock()
        with patch("gift_registry.browser.quit_owned_driver") as close:
            self.assertTrue(self.browser.close()["closed"])
        close.assert_not_called()
        self.browser.capture.assert_not_called()

    def test_capture_deadline_expires_before_any_browser_command(self):
        with patch("gift_registry.browser.time.monotonic", return_value=10):
            with self.assertRaises(TimeoutError):
                self.browser.capture(deadline=9)
        self.driver.execute_script.assert_not_called()
        self.driver.execute_cdp_cmd.assert_not_called()

    def test_save_failure_does_not_mutate_previous_snapshot(self):
        previous = {"version": 1, "site": "banniu", "origins": {}, "cookies": [{"name": "old"}]}
        self.browser.snapshot = previous
        self.driver.current_url = SITES[1].url
        self.driver.execute_script.return_value = {"local": {}, "session": {}}
        self.driver.execute_cdp_cmd.return_value = {"cookies": []}
        self.browser.store.save.side_effect = OSError("test storage failure")
        with self.assertRaises(OSError):
            self.browser.capture()
        self.assertEqual(previous["origins"], {})
        self.assertEqual(previous["cookies"], [{"name": "old"}])


if __name__ == "__main__":
    unittest.main()
