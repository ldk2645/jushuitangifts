import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gift_registry import launcher
from gift_registry.network import NetworkSettings
from gift_registry.registration import RegistrationQueue

HEALTH = {'app': 'gift_registry', 'lan_enabled': False, 'lan_ip': '', 'domain': ''}


class LauncherTests(unittest.TestCase):
    def test_lock_probe_never_recovers_jobs_or_changes_history(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory)
            (path / 'preserved.bin').write_bytes(b'synthetic-history')
            self.assertFalse(launcher.queue_is_locked(path))
            self.assertFalse((path / '.queue.lock').exists())
            with RegistrationQueue(path, operations=Mock(configured=False)):
                before = {p.name: p.read_bytes() for p in path.glob('*') if p.is_file() and p.name != '.queue.lock'}
                self.assertTrue(launcher.queue_is_locked(path))
                self.assertEqual(before, {p.name: p.read_bytes() for p in path.glob('*') if p.is_file() and p.name != '.queue.lock'})
            self.assertFalse(launcher.queue_is_locked(path))

    def test_healthy_existing_service_is_reused_without_spawning(self):
        notify, spawn, locked = Mock(), Mock(), Mock()
        launcher.supervise(8765, notify, threading.Event(), health=Mock(return_value=HEALTH),
                           spawn=spawn, locked=locked, settings=NetworkSettings)
        spawn.assert_not_called()
        locked.assert_not_called()
        notify.assert_called_once_with('ready', 'http://127.0.0.1:8765/')

    def test_old_background_queue_is_waited_out_before_starting_exactly_one_child(self):
        health=Mock(side_effect=[None, None, None, HEALTH])
        locked=Mock(side_effect=[True, True, False])
        child=Mock()
        child.poll.return_value=None
        spawn=Mock(return_value=child)
        notify=Mock()
        cancelled=Mock()
        cancelled.is_set.return_value=False
        launcher.supervise(8765,notify,cancelled,health=health,locked=locked,spawn=spawn,settings=NetworkSettings)
        spawn.assert_called_once()
        self.assertIn('上一工作台', notify.call_args_list[0].args[1])
        self.assertEqual(notify.call_args_list[-1].args, ('ready','http://127.0.0.1:8765/'))
        child.terminate.assert_not_called()

    def test_stopping_http_service_is_waited_out_even_if_network_configuration_changed(self):
        child=Mock()
        child.poll.return_value=None
        cancelled=Mock()
        cancelled.is_set.return_value=False
        spawn=Mock(return_value=child)
        launcher.supervise(8765,Mock(),cancelled,
                           health=Mock(side_effect=[{**HEALTH,'stopping':True,'lan_ip':'192.168.2.10'},None,HEALTH]),
                           locked=Mock(side_effect=[True,False]),spawn=spawn,settings=NetworkSettings)
        spawn.assert_called_once()

    def test_cancelling_wait_does_not_kill_child(self):
        child=Mock()
        child.poll.return_value=None
        cancelled=threading.Event()
        with patch.object(cancelled,'wait',side_effect=lambda _:cancelled.set()):
            launcher.supervise(8765,Mock(),cancelled,health=Mock(return_value=None),
                               locked=Mock(return_value=False),spawn=Mock(return_value=child))
        child.terminate.assert_not_called()
        child.kill.assert_not_called()

    def test_failed_start_is_reported_without_raw_exception_or_credentials(self):
        child=Mock()
        child.poll.return_value=1
        child.communicate.return_value=(b'',b'Traceback\nRuntimeError: private-password-and-token')
        cancelled=Mock()
        cancelled.is_set.return_value=False
        with self.assertRaises(launcher.StartupError) as error:
            launcher.supervise(8765,Mock(),cancelled,health=Mock(return_value=None),
                               locked=Mock(return_value=False),spawn=Mock(return_value=child))
        self.assertIn('RuntimeError',str(error.exception))
        self.assertNotIn('private-',str(error.exception))

    def test_health_check_bypasses_system_proxy(self):
        response=Mock()
        response.__enter__=Mock(return_value=response)
        response.__exit__=Mock(return_value=None)
        response.read.return_value=b'{"app":"gift_registry"}'
        opener=Mock()
        opener.open.return_value=response
        with patch.object(launcher,'build_opener',return_value=opener) as factory:
            self.assertEqual(launcher.read_health(8765)['app'],'gift_registry')
        self.assertEqual(factory.call_args.args[0].proxies,{})

    def test_network_mismatch_reports_error_without_launching_second_server(self):
        spawn=Mock()
        with self.assertRaisesRegex(launcher.StartupError,'共享配置'):
            launcher.supervise(8765,Mock(),threading.Event(),health=Mock(return_value=HEALTH),spawn=spawn,
                               settings=lambda:NetworkSettings(True,'192.168.2.10','192.168.2.0/24'))
        spawn.assert_not_called()
