"""Startup regressions use synthetic drafts and temporary history only."""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.request import ProxyHandler, build_opener

from fakes import FakeOperations
from gift_registry.frontend_server import LocalHTTPServer, RowStore, make_server
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.registration import RegistrationQueue
from gift_registry.selection import migrate_legacy_selections

GIFTS = [{'id': 'SKU', 'name': 'Synthetic gift'}]


def row(identifier='current', label='Synthetic gift'):
    return dict(id=identifier, order_no='SYN-ORDER', gift=label, gift_ids=['SKU'], change_address=False)


class StartupLoadingTests(unittest.TestCase):
    def test_server_binding_does_not_wait_for_dns(self):
        with patch('socket.getfqdn', side_effect=AssertionError('Unexpected DNS lookup')):
            server = LocalHTTPServer(('127.0.0.1', 0), BaseHTTPRequestHandler)
            try:
                self.assertGreater(server.server_port, 0)
                self.assertEqual(server.server_name, '127.0.0.1')
                with self.assertRaises(OSError):
                    LocalHTTPServer(('127.0.0.1', server.server_port), BaseHTTPRequestHandler)
            finally:
                server.server_close()

    def test_blank_and_current_catalog_drafts_never_scan_history(self):
        jobs = Mock()
        jobs._paths.side_effect = AssertionError('Unnecessary history scan')
        rows = [row(), dict(id='blank', order_no='', gift='', gift_ids=[])]
        migrate_legacy_selections(rows, GIFTS, jobs)
        self.assertTrue(rows[0]['gift_selection']['confirmed'])
        self.assertEqual(rows[1]['gift_unmatched'], [])
        jobs._paths.assert_not_called()

    def test_renamed_legacy_gift_uses_matching_history_and_stops_when_proven(self):
        jobs = Mock()
        jobs._paths.return_value = ['first', 'unused']
        old = row(label='Old gift')
        jobs._load.return_value = dict(rows=[old], gift_catalog=[dict(id='SKU', name='Old gift')])
        rows = [old.copy()]
        migrate_legacy_selections(rows, GIFTS, jobs)
        self.assertTrue(rows[0]['gift_selection']['confirmed'])
        jobs._load.assert_called_once_with('first')
        rows = [row(label='Unmatched')]
        jobs._paths.return_value = []
        migrate_legacy_selections(rows, GIFTS, jobs)
        self.assertEqual(rows[0]['gift_unmatched'], ['Unmatched'])

    def test_states_cache_cannot_hide_file_changes_or_leak_mutations(self):
        with tempfile.TemporaryDirectory() as tmp:
            ops = FakeOperations(); ops.configured = False
            with RegistrationQueue(Path(tmp)/'jobs', operations=ops) as jobs:
                jobs.start([row()], GIFTS)
                original = jobs.states()
                with patch.object(jobs, '_load', side_effect=AssertionError('Unchanged history decoded again')):
                    cached = jobs.states()
                cached['current']['detail'] = 'client mutation'
                self.assertEqual(jobs.states(), original)
                path = jobs._paths()[0]
                job = json.loads(path.read_text(encoding='utf-8'))
                job['items'][0]['detail'] = 'external update with different length'
                path.write_text(json.dumps(job), encoding='utf-8')
                self.assertEqual(jobs.states()['current']['detail'], 'external update with different length')
                path.unlink()
                self.assertEqual(jobs.states(), {})

    def test_empty_sheet_skips_history_and_payload_only_contains_current_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            catalog = folder/'gifts.json'; catalog.write_text(json.dumps(GIFTS), encoding='utf-8')
            store = RowStore(folder/'rows.json')
            ops = FakeOperations(); ops.configured = False
            jobs = RegistrationQueue(folder/'jobs', operations=ops)
            jobs.start([row('old'), row('current')], GIFTS)
            server = make_server(0, store=store, queue=jobs, catalog=GiftCatalogStore(catalog))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            opener = build_opener(ProxyHandler({}))
            def read(endpoint):
                with opener.open(f'http://127.0.0.1:{server.server_port}{endpoint}', timeout=3) as response:
                    return json.load(response)
            try:
                with patch.object(jobs, 'states', side_effect=AssertionError('Empty sheet scanned history')):
                    for endpoint in ('/api/rows', '/api/registrations/status'):
                        self.assertEqual(read(endpoint)['row_states'], {})
                with store.lock:
                    store.document = dict(revision=3, rows=[row()])
                for endpoint in ('/api/rows', '/api/registrations/status'):
                    result = read(endpoint)
                    self.assertEqual(set(result['row_states']), {'current'})
                    self.assertEqual(result.get('draft_revision', result.get('revision')), 3)
                self.assertEqual(set(jobs.states()), {'current', 'old'})
            finally:
                server.shutdown(); server.server_close(); thread.join(3)
