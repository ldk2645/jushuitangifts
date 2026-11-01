import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from gift_registry import lan_setup
from gift_registry.network import NetworkSettings
from gift_registry.network_monitor import NetworkProbeError


class LanSetupTests(unittest.TestCase):
    def run_refresh(self, connections, answer='1', previous=None):
        old = previous or NetworkSettings(True, '10.1.1.2', '10.1.1.0/24', 'ldkldk.win')
        with patch.object(lan_setup, 'load_network', return_value=old), \
             patch.object(lan_setup, 'probe_network', return_value=connections), \
             patch.object(lan_setup, 'save_network') as save, \
             patch('builtins.input', return_value=answer), patch('builtins.print'):
            result = lan_setup.refresh_network()
        return result, save

    def test_changed_network_preserves_domain(self):
        current, save = self.run_refresh([{'ip':'192.168.51.240','subnet':'192.168.51.0/24'}])
        self.assertEqual(current.domain, 'ldkldk.win')
        self.assertEqual(current.lan_ip, '192.168.51.240')
        save.assert_called_once_with(current)

    def test_same_configuration_is_not_rewritten(self):
        _, save = self.run_refresh([{'ip':'10.1.1.2','subnet':'10.1.1.0/24'}])
        save.assert_not_called()

    def test_multiple_adapters_require_valid_selection(self):
        options = [{'ip':'10.2.1.2','subnet':'10.2.1.0/24','name':'A'},
                   {'ip':'192.168.1.2','subnet':'192.168.1.0/24','name':'B'}]
        current, _ = self.run_refresh(options, '2')
        self.assertEqual(current.lan_ip, '192.168.1.2')
        with patch.object(lan_setup, 'save_network') as save:
            with self.assertRaises(ValueError):
                self.run_refresh(options, '0')
            save.assert_not_called()

    def test_disconnected_or_disabled_cannot_enable_lan(self):
        for connections, previous in (([], None), ([], NetworkSettings())):
            with self.assertRaises(ValueError):
                self.run_refresh(connections, previous=previous)

    def test_probe_timeout_preserves_configuration(self):
        with patch.object(lan_setup, 'load_network', return_value=NetworkSettings(True, '10.1.1.2', '10.1.1.0/24')), \
             patch.object(lan_setup, 'probe_network', side_effect=NetworkProbeError('网络检测超时')), \
             patch.object(lan_setup, 'save_network') as save:
            with self.assertRaisesRegex(NetworkProbeError, '网络检测超时'):
                lan_setup.refresh_network()
        save.assert_not_called()

    def test_diagnose_mismatched_network_does_not_change_configuration(self):
        output = io.StringIO()
        with patch.object(sys, 'argv', ['lan_setup', '--diagnose']), \
             patch.object(lan_setup, 'load_network', return_value=NetworkSettings(True, '10.1.1.2', '10.1.1.0/24')), \
             patch.object(lan_setup, 'probe_network', return_value=[
                 {'name': 'test-wifi', 'ip': '192.168.2.10', 'subnet': '192.168.2.0/24'}]), \
             patch.object(lan_setup, 'save_network') as save, redirect_stdout(output):
            lan_setup.main()
        save.assert_not_called()
        self.assertIn('与配置不同', output.getvalue())
        self.assertIn('只读检查', output.getvalue())

    def test_cli_reports_specific_timeout_and_returns_failure(self):
        output = io.StringIO()
        with patch.object(sys, 'argv', ['lan_setup']), \
             patch.object(lan_setup, 'refresh_network', side_effect=NetworkProbeError('网络检测超时（超过 25 秒）')), \
             redirect_stdout(output):
            with self.assertRaises(SystemExit) as caught:
                lan_setup.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn('网络检测超时', output.getvalue())

    def test_existing_check_contract_remains_json_and_never_probes(self):
        settings = NetworkSettings(True, '10.1.1.2', '10.1.1.0/24')
        output = io.StringIO()
        with patch.object(sys, 'argv', ['lan_setup', '--check']), \
             patch.object(lan_setup, 'load_network', return_value=settings), \
             patch.object(lan_setup, 'probe_network') as probe, redirect_stdout(output):
            lan_setup.main()
        probe.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), vars(settings))
