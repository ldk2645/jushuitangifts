"""GIFT_CHROME_BINARY 固定便携 Chrome 版本；不启动真实浏览器。"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gift_registry import browser
from gift_registry.config import SITES


class CreateDriverBinaryTests(unittest.TestCase):
    def test_explicit_portable_driver_skips_manager(self):
        with patch.dict(os.environ, {'GIFT_CHROMEDRIVER': str(self.chrome)}):
            _, chrome = self.build(str(self.chrome))
        self.assertEqual(chrome.call_args.kwargs['service'].path, str(self.chrome))

    def test_missing_explicit_driver_is_rejected(self):
        with patch.dict(os.environ, {'GIFT_CHROMEDRIVER': str(self.folder / 'missing.exe')}):
            with self.assertRaisesRegex(ValueError, 'GIFT_CHROMEDRIVER'):
                self.build(str(self.chrome))

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        self.chrome = self.folder / "chrome.exe"
        self.chrome.write_bytes(b"stub")

    def build(self, value):
        driver = Mock()
        chrome = Mock(return_value=driver)
        with patch.dict(os.environ, {"GIFT_CHROME_BINARY": value or ""}, clear=False), \
                patch.object(browser.webdriver, "Chrome", chrome), \
                patch.object(browser, "track_owned_browser"), \
                patch.object(browser, "configure_transport"):
            result = browser.create_driver(SITES[0], profile=self.folder / "profile")
        return result, chrome

    def test_configured_binary_is_passed_to_selenium(self):
        driver, chrome = self.build(str(self.chrome))
        self.assertIs(driver, driver)
        options = chrome.call_args.kwargs["options"]
        self.assertEqual(options.binary_location, str(self.chrome))

    def test_absent_or_blank_binary_keeps_selenium_manager_default(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                _, chrome = self.build(value)
                options = chrome.call_args.kwargs["options"]
                self.assertFalse(options.binary_location)

    def test_missing_binary_is_rejected_before_browser_start(self):
        missing = self.folder / "missing" / "chrome.exe"
        with patch.dict(os.environ, {"GIFT_CHROME_BINARY": str(missing)}, clear=False), \
                patch.object(browser.webdriver, "Chrome") as chrome:
            with self.assertRaisesRegex(ValueError, "GIFT_CHROME_BINARY"):
                browser.create_driver(SITES[0], profile=self.folder / "profile")
        chrome.assert_not_called()


if __name__ == "__main__":
    unittest.main()
