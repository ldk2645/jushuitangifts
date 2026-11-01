import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import portable
from gift_registry import browser
from gift_registry.config import SITES


class EdgePortableTests(unittest.TestCase):
    def test_edge_ignores_chrome_and_uses_separate_profile_with_headless(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            root = Path(folder)
            edge = root / 'msedge.exe'
            edge.touch()
            with patch.dict(os.environ, {'GIFT_BROWSER': 'edge', 'GIFT_CHROME_BINARY': 'missing',
                                       'GIFT_CHROMEDRIVER': 'missing', 'GIFT_EDGEDRIVER': ''}), \
                 patch.object(browser, 'installed_edge', return_value=str(edge)), \
                 patch.object(browser, 'DATA_DIR', root), \
                 patch.object(browser.webdriver, 'Edge', return_value=Mock()) as create, \
                 patch.object(browser.webdriver, 'Chrome') as chrome, \
                 patch.object(browser, 'track_owned_browser'), patch.object(browser, 'configure_transport'):
                browser.create_driver(SITES[0], headless=True)
            options = create.call_args.kwargs['options']
            self.assertIsInstance(options, browser.webdriver.EdgeOptions)
            self.assertEqual(options.binary_location, str(edge))
            self.assertIn('--headless=new', options.arguments)
            self.assertIn(f'--user-data-dir={root / "browser_edge" / SITES[0].key}', options.arguments)
            chrome.assert_not_called()

    def test_edge_install_search_and_missing_browser(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder, patch.dict(os.environ, {
            'ProgramFiles(x86)': folder, 'ProgramFiles': folder, 'LOCALAPPDATA': folder}):
            with self.assertRaisesRegex(ValueError, 'Edge'):
                browser.installed_edge()
            path = Path(folder) / 'Microsoft/Edge/Application/msedge.exe'
            path.parent.mkdir(parents=True)
            path.touch()
            self.assertEqual(browser.installed_edge(), str(path))

    def test_portable_edge_configuration_needs_no_chrome_files(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder, patch.dict(os.environ, {
            'GIFT_CHROME_BINARY': 'old', 'GIFT_CHROMEDRIVER': 'old', 'GIFT_EDGEDRIVER': 'old',
            'SE_EDGEDRIVER': 'old'}):
            root = Path(folder)
            (root / 'portable.json').write_text(json.dumps({'browser': 'edge', 'port': 8766}))
            with patch.object(portable, 'ROOT', root), patch.object(portable.os, 'chdir'):
                self.assertEqual(portable.configure()['port'], 8766)
            self.assertEqual(os.environ['GIFT_BROWSER'], 'edge')
            self.assertEqual(os.environ['SE_SKIP_DRIVER_IN_PATH'], 'true')
            self.assertEqual(os.environ['SE_AVOID_BROWSER_DOWNLOAD'], 'true')
            self.assertNotIn('GIFT_CHROME_BINARY', os.environ)
            self.assertNotIn('SE_EDGEDRIVER', os.environ)

    def test_legacy_portable_manifest_still_selects_chrome(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder, patch.dict(os.environ, {}):
            root = Path(folder)
            (root / 'chrome.exe').touch()
            (root / 'chromedriver.exe').touch()
            (root / 'portable.json').write_text(json.dumps({
                'chrome': 'chrome.exe', 'driver': 'chromedriver.exe', 'port': 8766}))
            with patch.object(portable, 'ROOT', root), patch.object(portable.os, 'chdir'):
                portable.configure()
            self.assertEqual(os.environ['GIFT_BROWSER'], 'chrome')
            self.assertEqual(os.environ['GIFT_CHROMEDRIVER'], str(root / 'chromedriver.exe'))
