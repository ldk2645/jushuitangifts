import sys
import unittest
from unittest.mock import patch

import portable
from gift_registry.network import NetworkSettings


class PortableLanTests(unittest.TestCase):
    def test_selected_network_is_saved_without_domain_and_uses_portable_port(self):
        connections = [{'name':'wifi','ip':'192.168.2.10','subnet':'192.168.2.0/24'},
                       {'name':'ethernet','ip':'10.8.0.5','subnet':'10.8.0.0/24'}]
        with patch.object(portable, 'configure', return_value={'port':8766}), \
             patch.object(sys, 'argv', ['portable.py','lan-setup']), \
             patch('gift_registry.network_monitor.probe_network', return_value=connections), \
             patch('builtins.input', return_value='2'), patch('builtins.print'), \
             patch('gift_registry.network.LanAccessStore') as store, \
             patch('gift_registry.network.save_network') as save, \
             patch('gift_registry.network.show_lan_info') as show:
            portable.main()
        expected = NetworkSettings(True, '10.8.0.5', '10.8.0.0/24')
        save.assert_called_once_with(expected)
        show.assert_called_once_with(expected, 8766)
        store.return_value.ensure.assert_called_once()

    def test_invalid_selection_never_changes_configuration_or_password(self):
        with patch.object(portable, 'configure', return_value={'port':8766}), \
             patch.object(sys, 'argv', ['portable.py','lan-setup']), \
             patch('gift_registry.network_monitor.probe_network', return_value=[
                 {'name':'wifi','ip':'192.168.2.10','subnet':'192.168.2.0/24'}]), \
             patch('builtins.input', return_value='0'), patch('builtins.print'), \
             patch('gift_registry.network.LanAccessStore') as store, \
             patch('gift_registry.network.save_network') as save:
            portable.main()
        save.assert_not_called()
        store.assert_not_called()
