"""局域网访问隔离测试；不用真实账号、域名服务或业务目录。"""

import base64
import json
import os
from http.client import HTTPConnection
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from gift_registry.frontend_server import RowStore, make_server
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.network import LanAccessStore, NetworkSettings, authorized, load_network, save_network, valid_access_password
from gift_registry.network_monitor import NetworkMonitor
from gift_registry.registration import RegistrationQueue

SETTINGS = NetworkSettings(True, '192.168.1.91', '192.168.1.0/24', 'gift.example.test')
PASSWORD = 'isolated-LAN-test-password-only'


def authorization(password=PASSWORD):
    return 'Basic ' + base64.b64encode(('gift:' + password).encode()).decode()


class NetworkSettingsTests(unittest.TestCase):
    def test_default_remains_loopback_and_creates_no_configuration(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / 'network.local.json'
            settings = load_network(path)
            self.assertEqual(settings.bind_host, '127.0.0.1')
            self.assertFalse(path.exists())
            self.assertTrue(settings.allows_client('127.0.0.1'))
            self.assertFalse(settings.allows_client('192.168.1.10'))
            self.assertFalse(settings.allows_host('gift.example.test:8765', 8765))

    def test_configuration_roundtrips_without_access_password(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / 'network.local.json'
            save_network(SETTINGS, path)
            self.assertEqual(load_network(path), SETTINGS)
            self.assertNotIn('password', path.read_text())

    def test_client_is_limited_to_selected_subnet_and_loopback(self):
        for address in ('192.168.1.2', '192.168.1.91', '127.0.0.1'):
            self.assertTrue(SETTINGS.allows_client(address), address)
        for address in ('192.168.2.2', '198.18.0.2', '8.8.8.8', 'not-an-ip'):
            self.assertFalse(SETTINGS.allows_client(address), address)

    def test_host_validation_matches_exact_name_and_port(self):
        for host in ('gift.example.test:8765', '192.168.1.91:8765', '127.0.0.1:8765', 'localhost:8765'):
            self.assertTrue(SETTINGS.allows_host(host, 8765), host)
        for host in ('evil.test:8765', 'gift.example.test.evil:8765', 'gift.example.test:80',
                     'gift.example.test', 'user@gift.example.test:8765', 'gift.example.test:8765/path',
                     'gift.example.test:8765#fragment', 'gift.example.test:8765?query',
                     'gift.example.test:8765 ', 'gift.example.test:wrong', None):
            self.assertFalse(SETTINGS.allows_host(host, 8765), host)

    def test_invalid_or_public_configuration_is_rejected(self):
        for values in ((True, '8.8.8.8', '8.8.8.0/24', 'gift.example.test'),
                       (True, '192.168.1.91', '0.0.0.0/0', 'gift.example.test'),
                       (True, '192.168.1.91', '192.168.2.0/24', 'gift.example.test'),
                       (True, '192.168.1.91', '192.168.1.0/24', 'https://gift.example.test'),
                       (True, '192.168.1.91', '192.168.1.0/24', '*.example.test'),
                       (True, '192.168.1.0', '192.168.1.0/24', 'gift.example.test'),
                       ('yes', '', '', '')):
            with self.subTest(values=values), self.assertRaises(ValueError):
                NetworkSettings(*values)

    def test_corrupt_configuration_fails_closed_without_overwriting(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / 'network.local.json'
            for data in ('not-json', '{"version":2}', '{"version":true}', '{"version":1,"unknown":true}'):
                path.write_text(data)
                with self.assertRaises(ValueError):
                    load_network(path)
                self.assertEqual(path.read_text(), data)


class LanAccessStoreTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI test')
    def test_real_dpapi_password_restores_from_an_isolated_file(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / 'lan_access.dpapi'
            password = LanAccessStore(path).ensure()
            self.assertNotIn(password.encode(), path.read_bytes())
            self.assertEqual(LanAccessStore(path).ensure(), password)

    def test_generated_access_password_is_encrypted_and_survives_reload(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / 'lan_access.dpapi'
            cipher = {}

            def protect(value):
                cipher[b'opaque-test-cipher'] = value
                return b'opaque-test-cipher'

            store = LanAccessStore(path, protect=protect, unprotect=lambda value: cipher[value])
            password = store.ensure()
            self.assertRegex(password, r'^[0-9]{6}$')
            self.assertNotIn(password.encode(), path.read_bytes())
            self.assertEqual(store.ensure(), password)

    def test_old_password_is_preserved_until_explicit_simplification(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / 'lan_access.dpapi'
            old = 'legacy-password-24-chars'
            self.assertEqual(len(old), 24)
            path.write_bytes(b'GIFT-LAN-ACCESS-1\nopaque-old')
            cipher = {b'opaque-old': old.encode()}

            def protect(value):
                cipher[b'opaque-new'] = value
                return b'opaque-new'

            store = LanAccessStore(path, protect=protect, unprotect=lambda data: cipher[data])
            self.assertEqual(store.ensure(), old)
            with patch('gift_registry.network.secrets.randbelow', return_value=42):
                password = store.simplify()
            self.assertEqual(password, '000042')
            self.assertEqual(store.ensure(), password)
            ciphertext = path.read_bytes()
            self.assertEqual(store.simplify(), password)
            self.assertEqual(path.read_bytes(), ciphertext)
            self.assertNotIn(password.encode(), ciphertext)

    def test_password_migration_failure_preserves_old_ciphertext(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / 'lan_access.dpapi'
            data = b'GIFT-LAN-ACCESS-1\nopaque-old'
            path.write_bytes(data)
            store = LanAccessStore(path, unprotect=lambda _: b'legacy-password-24-chars',
                                   protect=lambda _: (_ for _ in ()).throw(OSError('test failure')))
            with self.assertRaises(ValueError):
                store.simplify()
            self.assertEqual(path.read_bytes(), data)

    def test_simple_password_policy_preserves_leading_zeroes_and_rejects_other_short_values(self):
        for value in ('000042', '123456', PASSWORD):
            self.assertTrue(valid_access_password(value))
        for value in ('', '12345', '1234567', 'abcdef', '１２３４５６', None, 123456):
            self.assertFalse(valid_access_password(value))
        self.assertTrue(authorized(authorization('000042'), '000042'))
        self.assertFalse(authorized(authorization('42'), '000042'))

    def test_corrupt_credentials_are_not_silently_regenerated(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / 'lan_access.dpapi'
            path.write_bytes(b'invalid-test-data')
            with self.assertRaises(ValueError):
                LanAccessStore(path).ensure()
            self.assertEqual(path.read_bytes(), b'invalid-test-data')

    def test_authorization_checks_username_and_full_password(self):
        self.assertTrue(authorized(authorization(), PASSWORD))
        for value in (None, '', 'Bearer test', 'Basic ???', authorization('wrong'),
                      'Basic ' + base64.b64encode(('other:' + PASSWORD).encode()).decode(),
                      'Basic ' + 'a' * 2050):
            self.assertFalse(authorized(value, PASSWORD))


class LanHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.directory.cleanup)
        folder = Path(self.directory.name)
        self.store = RowStore(folder / 'rows.json')
        catalog_path = folder / 'gifts.json'
        catalog_path.write_text(json.dumps([{'id': '000SKU', 'name': '合成礼品'}]), encoding='utf-8')
        self.jobs = RegistrationQueue(folder / 'jobs', operations=type('Unconfigured', (), {'configured': False})())
        self.monitor = NetworkMonitor(SETTINGS, path=folder / 'network-baseline.json', notify=None, probe=lambda: [])
        self.server = make_server(0, store=self.store, queue=self.jobs, catalog=GiftCatalogStore(catalog_path),
                                  network=SETTINGS, access_password=PASSWORD, network_monitor=self.monitor)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.host = f'gift.example.test:{self.server.server_port}'
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, *, method='GET', data=None, headers=None, auth=True):
        values = {'Host': self.host}
        if auth:
            values['Authorization'] = authorization()
        values.update(headers or {})
        payload = json.dumps(data).encode() if data is not None else None
        return self.opener.open(Request(self.url + path, data=payload, method=method, headers=values), timeout=3)

    def config(self):
        with self.request('/api/config') as response:
            return json.load(response)

    def assert_status(self, status, path, **kwargs):
        with self.assertRaises(HTTPError) as error:
            self.request(path, **kwargs)
        self.assertEqual(error.exception.code, status)
        return error.exception

    def row(self):
        return {'id': 'synthetic-row', 'order_no': '00001', 'gift': '合成礼品', 'gift_ids': ['000SKU'],
                'change_address': False, 'recipient_name': '', 'phone': '', 'address': ''}

    def test_unauthorized_clients_cannot_read_any_draft_config_or_static_page(self):
        for path in ('/', '/api/config', '/api/rows', '/api/gifts', '/api/registrations/status', '/app.js'):
            error = self.assert_status(401, path, auth=False)
            self.assertIn('Basic', error.headers['WWW-Authenticate'])
        self.assertEqual(self.store.read()['revision'], 0)

    def test_local_urls_allow_static_page_and_api_without_authentication(self):
        for name in ('127.0.0.1', 'localhost'):
            for path in ('/', '/api/config', '/api/rows', '/api/registrations/status'):
                with self.subTest(name=name, path=path), self.request(path, auth=False,
                        headers={'Host': f'{name}:{self.server.server_port}'}) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIsNone(response.headers.get('WWW-Authenticate'))

    def test_local_write_still_requires_csrf_and_matching_origin(self):
        host = f'localhost:{self.server.server_port}'
        with self.request('/api/config', auth=False, headers={'Host': host}) as response:
            token = json.load(response)['token']
        body = {'revision': 0, 'rows': [self.row()]}
        self.assert_status(403, '/api/rows', auth=False, method='PUT', data=body, headers={'Host': host})
        self.assert_status(403, '/api/rows', auth=False, method='PUT', data=body,
                           headers={'Host': host, 'X-CSRF-Token': token, 'Origin': 'http://evil.test'})
        self.assertEqual(self.store.read()['revision'], 0)
        with self.request('/api/rows', auth=False, method='PUT', data=body,
                          headers={'Host': host, 'X-CSRF-Token': token, 'Origin': 'http://' + host}) as response:
            self.assertEqual(json.load(response)['revision'], 1)

    def test_domain_and_lan_host_still_require_password_from_loopback_peer(self):
        for host in (self.host, f'192.168.1.91:{self.server.server_port}'):
            self.assert_status(401, '/api/config', auth=False, headers={'Host': host})

    def test_remote_peer_cannot_bypass_password_with_loopback_host_or_forwarded_headers(self):
        self.server.RequestHandlerClass.is_local_client = lambda _: False
        self.assert_status(401, '/api/config', auth=False, headers={
            'Host': f'127.0.0.1:{self.server.server_port}', 'X-Forwarded-For': '127.0.0.1',
            'X-Real-IP': '127.0.0.1'})

    def test_cached_wrong_credentials_do_not_challenge_local_access(self):
        with self.request('/api/config', headers={'Host': f'localhost:{self.server.server_port}',
                                                  'Authorization': authorization('wrong')}) as response:
            self.assertEqual(response.status, 200)
            self.assertIsNone(response.headers.get('WWW-Authenticate'))

    def test_health_discloses_no_token_or_business_data(self):
        with self.request('/api/health', auth=False) as response:
            self.assertEqual(set(json.load(response)), {'app', 'lan_enabled', 'domain', 'lan_ip'})

    def test_duplicate_host_headers_are_rejected(self):
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            connection.putrequest('GET', '/api/config', skip_host=True)
            connection.putheader('Host', self.host)
            connection.putheader('Host', 'evil.test')
            connection.putheader('Authorization', authorization())
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
        finally:
            connection.close()

    def test_unauthorized_start_cannot_create_a_task_even_with_valid_csrf(self):
        token = self.config()['token']
        self.assert_status(401, '/api/registrations/start', method='POST', auth=False,
                           data={'revision': 0}, headers={'X-CSRF-Token': token})
        self.assertEqual(list(self.jobs.directory.glob('*.json')), [])

    def test_host_and_subnet_denial_precedes_authentication(self):
        self.assert_status(403, '/api/config', headers={'Host': 'evil.test:' + str(self.server.server_port)})
        with patch.object(NetworkSettings, 'allows_client', return_value=False):
            self.assert_status(403, '/api/config')

    def test_wrong_password_cannot_use_an_otherwise_valid_csrf_token(self):
        token = self.config()['token']
        self.assert_status(401, '/api/rows', method='PUT', data={'revision': 0, 'rows': [self.row()]},
                           headers={'Authorization': authorization('wrong'), 'X-CSRF-Token': token})
        self.assertEqual(self.store.read()['revision'], 0)

    def test_cross_origin_requests_are_rejected_even_with_auth_and_token(self):
        token = self.config()['token']
        for origin in ('http://evil.test', 'null', 'http://gift.example.test'):
            self.assert_status(403, '/api/rows', method='PUT', data={'revision': 0, 'rows': [self.row()]},
                               headers={'X-CSRF-Token': token, 'Origin': origin})
        self.assertEqual(self.store.read()['revision'], 0)

    def test_two_devices_share_revision_and_duplicate_starts_reuse_same_task(self):
        token = self.config()['token']
        headers = {'X-CSRF-Token': token, 'Origin': 'http://' + self.host}
        with self.request('/api/rows', method='PUT', data={'revision': 0, 'rows': [self.row()]}, headers=headers):
            pass
        self.assert_status(409, '/api/rows', method='PUT', data={'revision': 0, 'rows': []}, headers=headers)
        with self.request('/api/rows') as response:
            self.assertEqual(json.load(response)['rows'], [self.row()])
        with self.request('/api/registrations/start', method='POST', data={'revision': 1}, headers=headers) as response:
            first = json.load(response)
        with self.request('/api/registrations/start', method='POST', data={'revision': 1}, headers=headers) as response:
            second = json.load(response)
        self.assertEqual(first['task_id'], second['task_id'])
        self.assertEqual(len(list(self.jobs.directory.glob('*.json'))), 1)

    def test_remote_platform_login_cannot_be_enabled_by_forwarded_headers(self):
        self.server.RequestHandlerClass.is_local_client = lambda _: False
        config = self.config()
        self.assertFalse(config['platform_login_available'])
        with patch('gift_registry.frontend_server.subprocess.Popen') as spawn:
            self.assert_status(403, '/api/login', method='POST', headers={
                'X-CSRF-Token': config['token'], 'X-Forwarded-For': '127.0.0.1'})
        spawn.assert_not_called()

    def test_network_reminder_requires_real_local_access_and_is_read_only(self):
        self.assert_status(403, '/api/network/status')
        local = {'Host': f'127.0.0.1:{self.server.server_port}'}
        with self.request('/api/network/status', auth=False, headers=local) as response:
            self.assertEqual(json.load(response)['state'], 'checking')
        self.server.RequestHandlerClass.is_local_client = lambda _: False
        self.assert_status(403, '/api/network/status', headers={**local, 'X-Forwarded-For': '127.0.0.1'})
        self.assertEqual(self.store.read()['revision'], 0)
        self.assertEqual(list(self.jobs.directory.glob('*.json')), [])

    def test_network_ack_requires_local_connection_and_csrf(self):
        token = self.config()['token']
        local = {'Host': f'127.0.0.1:{self.server.server_port}', 'X-CSRF-Token': token}
        self.assert_status(403, '/api/network/ack', method='POST', data={'change_id': 'test'},
                           headers={'X-CSRF-Token': token})
        self.assert_status(403, '/api/network/ack', method='POST', data={'change_id': 'test'},
                           auth=False, headers={'Host': local['Host']})
        with patch.object(self.monitor, 'acknowledge', return_value={'state': 'ok'}) as ack:
            with self.request('/api/network/ack', method='POST', data={'change_id': 'test'},
                              auth=False, headers=local) as response:
                self.assertEqual(json.load(response)['state'], 'ok')
            ack.assert_called_once_with('test')

    def test_six_digit_password_authenticates_remote_and_rejects_previous_password(self):
        # 同样的真实 Handler 由独立临时服务验证，不更改生产密码。
        other_jobs = RegistrationQueue(Path(self.directory.name) / 'short-jobs',
                                       operations=type('Unconfigured', (), {'configured': False})())
        other = make_server(0, store=self.store, queue=other_jobs,
                            catalog=GiftCatalogStore(Path(self.directory.name) / 'gifts.json'),
                            network=SETTINGS, access_password='000042')
        thread = threading.Thread(target=other.serve_forever, daemon=True)
        thread.start()
        try:
            url = f'http://127.0.0.1:{other.server_port}/api/config'
            headers = {'Host': f'gift.example.test:{other.server_port}', 'Authorization': authorization('000042')}
            with self.opener.open(Request(url, headers=headers), timeout=3) as response:
                self.assertEqual(response.status, 200)
            headers['Authorization'] = authorization()
            with self.assertRaises(HTTPError) as error:
                self.opener.open(Request(url, headers=headers), timeout=3)
            self.assertEqual(error.exception.code, 401)
        finally:
            other.shutdown()
            other.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
