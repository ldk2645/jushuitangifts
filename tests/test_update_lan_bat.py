"""在临时目录运行真实 BAT 分支；Python 配置和防火墙脚本均为替身。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'nt', 'Windows BAT regression')
class UpdateLanBatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'tests')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        scripts = self.folder / '.venv' / 'Scripts'
        scripts.mkdir(parents=True)
        shutil.copy2(sys.executable, scripts / 'python.exe')
        (scripts.parent / 'pyvenv.cfg').write_text(
            f'home = {sys.base_prefix}\ninclude-system-site-packages = false\n', encoding='utf-8')
        shutil.copy2(ROOT / 'update-lan.bat', self.folder / 'update-lan.bat')
        package = self.folder / 'gift_registry'
        package.mkdir()
        (package / '__init__.py').write_text('', encoding='utf-8')
        (package / 'lan_setup.py').write_text(
            'import json, os, sys\nfrom pathlib import Path\n'
            'Path("setup-called.json").write_text(json.dumps(sys.argv[1:]))\n'
            'raise SystemExit(int(os.environ.get("TEST_LAN_SETUP_EXIT", "0")))\n', encoding='utf-8')
        # 仅写临时标记，绝不调用 NetFirewall / RunAs。
        (self.folder / 'allow-lan.ps1').write_text(
            'param([int]$Port)\n'
            'Set-Content -LiteralPath (Join-Path $PSScriptRoot "firewall-called.txt") -Value $Port -Encoding ascii\n'
            'exit ([int]$env:TEST_LAN_FIREWALL_EXIT)\n', encoding='ascii')

    def run_bat(self, *arguments, setup_exit=0, firewall_exit=0):
        env = dict(os.environ, TEST_LAN_SETUP_EXIT=str(setup_exit), TEST_LAN_FIREWALL_EXIT=str(firewall_exit))
        env.pop('PYTHONPATH', None)
        return subprocess.run(['cmd.exe', '/d', '/c', 'update-lan.bat', *arguments],
                              cwd=self.folder, env=env, input='\n', capture_output=True,
                              encoding='utf-8', errors='replace', timeout=30)

    def test_read_only_branch_never_calls_firewall(self):
        result = self.run_bat('--check')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads((self.folder / 'setup-called.json').read_text()), ['--diagnose'])
        self.assertFalse((self.folder / 'firewall-called.txt').exists())

    def test_setup_failure_stops_before_firewall_and_retains_exit_code(self):
        result = self.run_bat(setup_exit=1)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('Firewall update was NOT started', result.stdout)
        self.assertFalse((self.folder / 'firewall-called.txt').exists())

    def test_firewall_failure_is_identified_as_second_stage(self):
        result = self.run_bat(firewall_exit=1)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('firewall update failed or was cancelled', result.stdout)
        self.assertEqual((self.folder / 'firewall-called.txt').read_text(encoding='ascii').strip(), '8765')

    def test_read_only_failure_does_not_fall_through(self):
        result = self.run_bat('--check', setup_exit=1)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse((self.folder / 'firewall-called.txt').exists())

    def test_unknown_argument_does_not_change_settings(self):
        result = self.run_bat('--typo')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse((self.folder / 'setup-called.json').exists())
        self.assertFalse((self.folder / 'firewall-called.txt').exists())
