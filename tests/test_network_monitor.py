"""网络变化回归；使用合成网卡，不修改实际 DNS、网络或共享配置。"""

import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from gift_registry.network import NetworkSettings
from gift_registry.network_monitor import NetworkMonitor, normalize_connections, probe_network

SETTINGS = NetworkSettings(True, '192.168.1.91', '192.168.1.0/24', 'gift.example.test')


def connection(ip='192.168.1.91', *, name='test-wifi', adapter='physical-one', gateway='192.168.1.1'):
    return normalize_connections([{'adapter': adapter, 'name': name, 'ip': ip, 'prefix': 24, 'gateway': gateway}])[0]


class NetworkMonitorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'network.json'
        self.connections = [connection()]
        self.monitor = NetworkMonitor(SETTINGS, path=self.path, probe=lambda: self.connections, notify=None)

    def test_first_matching_network_establishes_persistent_baseline(self):
        self.monitor.check()
        self.assertEqual(self.monitor.snapshot()['state'], 'ok')
        baseline = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(baseline['connection'], connection())
        self.assertEqual(baseline['configuration']['lan_ip'], SETTINGS.lan_ip)

    def test_changed_ip_is_detected_even_without_existing_baseline(self):
        self.connections = [connection('192.168.51.240', gateway='192.168.51.112')]
        self.monitor.check()
        status = self.monitor.snapshot()
        self.assertEqual(status['state'], 'changed')
        self.assertIn('Cloudflare', status['message'])
        self.assertIn('192.168.51.240', status['message'])
        self.assertIn('gift.example.test', status['message'])
        self.assertFalse(self.path.exists())
        with self.assertRaises(ValueError):
            self.monitor.acknowledge(status['change_id'])

    def test_runtime_ip_change_does_not_replace_first_baseline(self):
        self.monitor.check()
        baseline = self.path.read_bytes()
        self.connections = [connection('192.168.1.92')]
        self.monitor.check()
        self.assertEqual(self.monitor.snapshot()['state'], 'changed')
        self.assertEqual(self.path.read_bytes(), baseline)

    def test_restart_remembers_network_identity_and_detects_same_ip_on_new_wifi(self):
        self.monitor.check()
        restarted = NetworkMonitor(SETTINGS, path=self.path, probe=lambda: [connection(name='new-wifi')], notify=None)
        restarted.check()
        self.assertEqual(restarted.snapshot()['state'], 'changed')

    def test_gateway_and_adapter_changes_are_detected_with_same_ip(self):
        self.monitor.check()
        for entry in (connection(gateway='192.168.1.2'), connection(adapter='physical-two')):
            self.connections = [entry]
            self.monitor.check()
            self.assertEqual(self.monitor.snapshot()['state'], 'changed')

    def test_acknowledgement_requires_current_change_id_then_survives_restart(self):
        self.monitor.check()
        self.connections = [connection(name='new-wifi')]
        self.monitor.check()
        with self.assertRaises(ValueError):
            self.monitor.acknowledge('stale-change')
        status = self.monitor.acknowledge(self.monitor.snapshot()['change_id'])
        self.assertEqual(status['state'], 'ok')
        self.monitor.check()
        self.assertEqual(self.monitor.snapshot()['state'], 'ok')

    def test_updating_shared_configuration_establishes_the_new_baseline(self):
        self.monitor.check()
        settings = NetworkSettings(True, '192.168.51.240', '192.168.51.0/24', SETTINGS.domain)
        updated = NetworkMonitor(settings, path=self.path,
                                 probe=lambda: [connection('192.168.51.240', gateway='192.168.51.112')], notify=None)
        updated.check()
        self.assertEqual(updated.snapshot()['state'], 'ok')
        self.assertEqual(json.loads(self.path.read_text())['configuration']['lan_ip'], settings.lan_ip)

    def test_offline_and_probe_failure_do_not_erase_baseline_or_clear_known_changes(self):
        self.monitor.check()
        baseline = self.path.read_bytes()
        self.connections = []
        self.monitor.check()
        self.assertEqual(self.monitor.snapshot()['state'], 'offline')
        self.monitor.probe = lambda: (_ for _ in ()).throw(OSError('synthetic failure'))
        self.monitor.check()
        self.assertEqual(self.monitor.snapshot()['state'], 'unavailable')
        self.monitor.probe = lambda: [connection('192.168.1.92')]
        self.monitor.check()
        self.monitor.probe = lambda: (_ for _ in ()).throw(OSError('synthetic failure'))
        self.monitor.check()
        self.assertEqual(self.monitor.snapshot()['state'], 'changed')
        self.assertEqual(self.path.read_bytes(), baseline)
        with self.assertRaises(ValueError):
            self.monitor.acknowledge(self.monitor.snapshot()['change_id'])

    def test_corrupt_baseline_is_reported_without_overwrite(self):
        self.path.write_text('invalid-json')
        self.monitor.check()
        self.assertEqual(self.monitor.snapshot()['state'], 'unavailable')
        self.assertEqual(self.path.read_text(), 'invalid-json')

    def test_extra_adapter_does_not_trigger_false_change_for_selected_lan(self):
        self.monitor.check()
        self.connections.append(connection('192.168.2.10', adapter='physical-two', gateway='192.168.2.1'))
        self.monitor.check()
        self.assertEqual(self.monitor.snapshot()['state'], 'ok')

    def test_snapshot_is_a_copy_and_disabled_monitor_creates_nothing(self):
        self.monitor.check()
        self.monitor.snapshot()['current_addresses'].clear()
        self.assertEqual(len(self.monitor.snapshot()['current_addresses']), 1)
        unused = self.path.with_name('disabled.json')
        monitor = NetworkMonitor(NetworkSettings(), path=unused,
                                 probe=lambda: self.fail('Disabled monitor must not probe'), notify=None)
        monitor.check()
        self.assertFalse(unused.exists())

    def test_changed_network_notifies_once_without_blocking_monitor(self):
        notified = threading.Event()
        messages = []
        self.monitor.notify = lambda message: (messages.append(message), notified.set())
        self.connections = [connection('192.168.1.92')]
        self.monitor.check()
        self.assertTrue(notified.wait(1))
        self.monitor.check()
        self.assertEqual(len(messages), 1)

    def test_monitor_rechecks_while_running_and_stops_cleanly(self):
        checked = threading.Event()
        self.monitor.probe = lambda: (checked.set(), self.connections)[1]
        self.monitor.interval = .01
        self.monitor.start()
        self.assertTrue(checked.wait(1))
        self.monitor.close()
        self.assertFalse(self.monitor.thread.is_alive())

    def test_probe_has_bounded_timeout_hides_window_and_excludes_non_private_ips(self):
        data = [{'adapter': 'physical', 'name': 'synthetic', 'ip': '192.168.1.91', 'prefix': 24, 'gateway': '192.168.1.1'},
                {'adapter': 'virtual', 'name': 'synthetic', 'ip': '198.18.0.1', 'prefix': 30, 'gateway': '198.18.0.2'}]
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps(data), stderr='')
        with patch('gift_registry.network_monitor.subprocess.run', return_value=result) as run:
            self.assertEqual(probe_network()[0]['ip'], SETTINGS.lan_ip)
        self.assertEqual(run.call_args.kwargs['timeout'], 12)
        self.assertIn('HardwareInterface', run.call_args.args[0][-1])
        self.assertEqual(len(normalize_connections(data)), 1)


if __name__ == '__main__':
    unittest.main()
