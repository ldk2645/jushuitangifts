"""空白页浏览器启动诊断工具的自检：不启动浏览器、不读写业务数据。"""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / 'tools'


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


class BrowserStartupDiagnosticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.check = load_module('check_browser_startup', 'check_browser_startup.py')

    def test_integrity_probe_is_read_only_and_reports_a_level(self):
        integrity = self.check.process_integrity()
        self.assertIn('supported', integrity)
        if integrity['supported']:
            self.assertIn(integrity['level'], ('low', 'medium', 'high', 'system', 'unknown'))
            self.assertFalse(integrity['low'] and integrity['level'] != 'low')

    def test_low_integrity_hint_points_to_the_repair_entry(self):
        hint = self.check.LOW_INTEGRITY_HINT
        self.assertIn('fix-browser-integrity.bat', hint)
        self.assertIn('低完整性级别', hint)

    def test_repair_entry_and_script_are_shipped(self):
        self.assertTrue((ROOT / 'fix-browser-integrity.bat').is_file())
        self.assertTrue((TOOLS / 'fix_browser_integrity.ps1').is_file())
        script = (TOOLS / 'fix_browser_integrity.ps1').read_text(encoding='utf-8')
        self.assertIn('Low Mandatory Level', script)
        self.assertIn('setintegritylevel Medium', script)
        self.assertIn('/grant:r', script)

    def test_diagnose_batch_covers_both_supported_browsers(self):
        batch = (ROOT / 'diagnose-selenium.bat').read_text(encoding='utf-8')
        self.assertIn('--browser chrome', batch)
        self.assertIn('--browser edge', batch)


if __name__ == '__main__':
    unittest.main()
