"""平台登录诊断工具的自检：导入模块但不启动浏览器、不读取真实凭据。"""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / 'tools'


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


class LoginDiagnosticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.check = load_module('check_login_startup', 'check_login_startup.py')

    def test_error_summary_keeps_only_type_and_first_line(self):
        summary = self.check._safe_error(RuntimeError('Message: synthetic\nStacktrace:\nsecret-cookie'))
        self.assertEqual(summary['error_type'], 'RuntimeError')
        self.assertEqual(summary['error_line'], 'Message: synthetic')
        long = self.check._safe_error(RuntimeError('x' * 1000))
        self.assertLessEqual(len(long['error_line']), 400)

    def test_profile_state_reports_leftover_browser_lock_files(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            profile = data / 'browser' / 'banniu'
            profile.mkdir(parents=True)
            (profile / 'SingletonLock').write_text('stale', encoding='utf-8')
            (profile / 'Cookies').write_text('x', encoding='utf-8')
            with patch.object(self.check, 'DATA_DIR', data), \
                    patch.dict('os.environ', {'GIFT_BROWSER': 'chrome'}):
                state = self.check._profile_state(self.check.SITES[1])
        self.assertTrue(state['exists'])
        self.assertIn('SingletonLock', state['lock_like_files'])
        self.assertNotIn('Cookies', state['lock_like_files'])

    def test_credential_probe_reports_booleans_without_password(self):
        class FakeStore:
            def settings(self):
                return {'banniu': {'username': 'user', 'headless': False}}

            def load(self, key):
                return type('Credentials', (), {'username': 'user', 'password': 'secret'})()

        with patch.object(self.check, 'CredentialStore', FakeStore):
            state = self.check._credential_state(self.check.SITES[1])
        self.assertTrue(state['username_present'])
        self.assertTrue(state['dpapi_decrypt_ok'])
        self.assertNotIn('secret', str(state))

    def test_live_devtools_port_marks_profile_in_use(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, size=-1):
                return b'{"Browser": "Chrome/156"}'

        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp)
            (profile / 'DevToolsActivePort').write_text('59201\n/devtools/browser/x', encoding='utf-8')
            with patch.object(self.check.urllib.request, 'urlopen', return_value=Response()):
                alive = self.check._devtools_alive(profile)
            self.assertEqual(alive, {'port': 59201, 'browser': 'Chrome/156'})
            with patch.object(self.check.urllib.request, 'urlopen', side_effect=OSError('closed')):
                self.assertIsNone(self.check._devtools_alive(profile))
            (profile / 'DevToolsActivePort').write_text('not-a-port', encoding='utf-8')
            self.assertIsNone(self.check._devtools_alive(profile))

    def test_diagnose_batch_and_report_are_shipped(self):
        batch = (ROOT / 'diagnose-login.bat').read_text(encoding='utf-8')
        self.assertIn('check_login_startup.py', batch)
        self.assertIn('login-diagnostic.json', batch)
        script = (TOOLS / 'check_login_startup.py').read_text(encoding='utf-8')
        self.assertIn("'submitted': False", script)
        # 报告结构只记录布尔状态，不得序列化账号密码。
        self.assertNotIn("'password': credentials.password", script)
        self.assertNotIn('print(credentials', script)

    def test_batch_files_support_venv_and_portable_runtime(self):
        for name in ('diagnose-login.bat', 'diagnose-selenium.bat', 'verify-honor.bat', 'update-gifts.bat'):
            with self.subTest(name=name):
                text = (ROOT / name).read_text(encoding='utf-8')
                self.assertIn(r'.venv\Scripts\python.exe', text)
                self.assertIn(r'runtime\python.exe', text)

    def test_portable_build_ships_login_diagnostics(self):
        source = (TOOLS / 'build_portable.py').read_text(encoding='utf-8')
        for name in ('diagnose-login.bat', 'check_login_startup.py', 'diagnose-selenium.bat',
                     'update-gifts.bat', 'check_browser_startup.py'):
            self.assertIn(name, source)


if __name__ == '__main__':
    unittest.main()
