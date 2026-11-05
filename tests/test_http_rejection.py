"""HTTP 提前拒绝响应与未读请求体的连接稳定性回归测试。

背景：`POST /api/network/ack` 等提前拒绝分支（Host/局域网/CSRF/本机权限）在
读取请求体之前就发出 403 并关闭连接。客户端把请求头和请求体分成两次发送时，
服务端可能先关闭；随后到达的请求体触发 RST，客户端读响应时偶发
`ConnectionAbortedError / WinError 10053`。

这里只用回环地址、合成礼品目录和临时任务目录，通过原始套接字精确控制请求体
的分段与延迟。任何平台错误都会让用例报错，不做重试、不吞异常。
"""

import base64
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from gift_registry.frontend_server import RowStore, make_server
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.network import NetworkSettings
from gift_registry.network_monitor import NetworkMonitor
from gift_registry.registration import RegistrationQueue

SETTINGS = NetworkSettings(True, '192.168.1.91', '192.168.1.0/24', 'gift.example.test')
PASSWORD = 'isolated-LAN-test-password-only'
BODY = b'{"change_id":"synthetic"}'


def authorization(password=PASSWORD):
    return 'Basic ' + base64.b64encode(('gift:' + password).encode()).decode()


class RejectionHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.directory.cleanup)
        folder = Path(self.directory.name)
        self.store = RowStore(folder / 'rows.json')
        catalog_path = folder / 'gifts.json'
        catalog_path.write_text(json.dumps([{'id': '000SKU', 'name': '合成礼品'}]), encoding='utf-8')
        self.jobs = RegistrationQueue(
            folder / 'jobs', operations=type('Unconfigured', (), {'configured': False})())
        self.monitor = NetworkMonitor(SETTINGS, path=folder / 'network-baseline.json', notify=None,
                                      probe=lambda: [])
        self.server = make_server(0, store=self.store, queue=self.jobs,
                                  catalog=GiftCatalogStore(catalog_path), network=SETTINGS,
                                  access_password=PASSWORD, network_monitor=self.monitor)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        self.local_host = f'127.0.0.1:{self.port}'
        self.domain_host = f'gift.example.test:{self.port}'
        self.token = self._read_token()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _read_token(self):
        response = self.raw('GET', '/api/config',
                            headers={'Host': self.domain_host, 'Authorization': authorization()})
        self.assertEqual(response.status, 200)
        return json.loads(response.body)['token']

    # ---- raw HTTP client helpers -------------------------------------

    def raw(self, method, path, *, headers=None, body=b'', declared_length='auto',
            send_body=True, delay=0.0, split=0, connection='close',
            shutdown_write=False, timeout=6):
        """发送一条原始请求并读取完整响应。

        `headers` 的值可以是列表，用来构造重复头（如两个 Content-Length）；
        `delay` 在请求头之后、请求体之前等待；`split` 先发前 N 字节请求体，
        间隔 10ms 再发剩余部分。任何 socket/平台异常都向上抛出，由用例失败。
        """
        values = dict(headers or {})
        host = values.pop('Host', self.domain_host)
        head = [f'{method} {path} HTTP/1.1', f'Host: {host}']
        for name, value in values.items():
            items = value if isinstance(value, (list, tuple)) else [value]
            head.extend(f'{name}: {item}' for item in items)
        if connection:
            head.append(f'Connection: {connection}')
        if declared_length == 'auto':
            head.append(f'Content-Length: {len(body)}')
        elif declared_length is not None:
            head.append(f'Content-Length: {declared_length}')
        request = ('\r\n'.join(head) + '\r\n\r\n').encode('iso-8859-1')
        sock = socket.create_connection(('127.0.0.1', self.port), timeout=timeout)
        try:
            sock.settimeout(timeout)
            sock.sendall(request)
            if send_body:
                if delay:
                    time.sleep(delay)
                if split:
                    sock.sendall(body[:split])
                    time.sleep(0.01)
                    sock.sendall(body[split:])
                else:
                    sock.sendall(body)
            if shutdown_write:
                sock.shutdown(socket.SHUT_WR)
            return self._read_response(sock)
        finally:
            sock.close()

    @staticmethod
    def _read_response(sock):
        data = b''
        while b'\r\n\r\n' not in data:
            chunk = sock.recv(65536)
            if not chunk:
                raise AssertionError('连接在收到响应头之前被关闭')
            data += chunk
        head, body = data.split(b'\r\n\r\n', 1)
        lines = head.decode('iso-8859-1').split('\r\n')
        status = int(lines[0].split(' ')[1])
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(':')
            headers[name.strip().lower()] = value.strip()
        length = int(headers.get('content-length', '0'))
        while len(body) < length:
            chunk = sock.recv(65536)
            if not chunk:
                raise AssertionError('响应体不完整')
            body += chunk
        extra = body[length:]
        body = body[:length]
        sock.settimeout(3)
        closed = False
        try:
            tail = sock.recv(64)
        except socket.timeout:
            tail = b''
        else:
            closed = tail == b'' and not extra
        if extra:
            raise AssertionError(f'响应体之后收到多余数据：{extra!r}')
        return SimpleNamespace(status=status, headers=headers, body=body, closed=closed)

    def assert_rejected(self, response, status=403):
        self.assertEqual(response.status, status, response.body)
        self.assertTrue(response.closed, '拒绝响应后连接应干净关闭（不能因未读请求体触发 RST）')
        self.assertEqual(response.headers.get('connection'), 'close')
        return json.loads(response.body) if response.body else None

    # ---- cases -------------------------------------------------------

    def test_small_body_early_rejections_return_403_and_close_cleanly(self):
        # 非本机 Host 的 ack：权限检查在读取请求体之前拒绝。
        error = self.assert_rejected(self.raw(
            'POST', '/api/network/ack',
            headers={'Host': self.domain_host, 'Authorization': authorization(),
                     'X-CSRF-Token': self.token},
            body=BODY))
        self.assertIn('网络提醒只能在主机本地核对', error['error'])
        # 缺 CSRF：同样在读取请求体之前拒绝。
        error = self.assert_rejected(self.raw(
            'POST', '/api/network/ack',
            headers={'Host': self.local_host, 'Authorization': authorization()},
            body=BODY))
        self.assertEqual(error['code'], 'csrf_expired')
        # 错误 CSRF：只读更新凭据的语义不变，拒绝码不变。
        error = self.assert_rejected(self.raw(
            'POST', '/api/rows',
            headers={'Host': self.local_host, 'X-CSRF-Token': 'stale-token'},
            body=json.dumps({'revision': 0, 'rows': []}).encode()))
        self.assertEqual(error['code'], 'csrf_expired')
        self.assertEqual(self.store.read()['revision'], 0)

    def test_wrong_host_with_body_is_rejected_without_abort(self):
        error = self.assert_rejected(self.raw(
            'POST', '/api/network/ack',
            headers={'Host': f'evil.test:{self.port}', 'X-CSRF-Token': self.token},
            body=BODY))
        self.assertIn('访问地址或客户端不在允许的局域网范围内', error['error'])

    def test_remote_peer_ack_with_body_is_rejected_without_abort(self):
        self.server.RequestHandlerClass.is_local_client = lambda _: False
        error = self.assert_rejected(self.raw(
            'POST', '/api/network/ack',
            headers={'Host': self.domain_host, 'Authorization': authorization(),
                     'X-CSRF-Token': self.token},
            body=BODY,
            # 远程客户端会在认证后失败于“仅允许主机本地”，请求体同样不能留在连接里。
            declared_length=len(BODY)))
        self.assertIn('网络提醒只能在主机本地核对', error['error'])

    def test_two_segment_body_matches_real_client_send_pattern(self):
        # http.client/urllib 把请求头和请求体分成两次 send；服务端提前拒绝时
        # 第二次 send 发生在响应/关闭附近，正是 10053 的触发时序。
        for _ in range(50):
            sock = socket.create_connection(('127.0.0.1', self.port), timeout=6)
            try:
                sock.settimeout(6)
                head = (
                    f'POST /api/network/ack HTTP/1.1\r\nHost: {self.domain_host}\r\n'
                    f'Authorization: {authorization()}\r\nX-CSRF-Token: {self.token}\r\n'
                    f'Content-Type: application/json\r\nContent-Length: {len(BODY)}\r\n'
                    f'Connection: close\r\n\r\n'
                ).encode()
                sock.sendall(head)
                sock.sendall(BODY)
                response = self._read_response(sock)
                self.assertEqual(response.status, 403, response.body)
                self.assertTrue(response.closed)
            finally:
                sock.close()

    def test_delayed_segmented_body_still_returns_403(self):
        # 服务端先写出拒绝响应，再在有限时间内排空已声明的请求体；
        # 请求体在响应之后才到达也必须拿到 403，而不是连接中止。
        for delay in (0.005, 0.05, 0.2):
            response = self.raw(
                'POST', '/api/network/ack',
                headers={'Host': self.domain_host, 'Authorization': authorization(),
                         'X-CSRF-Token': self.token},
                body=BODY, delay=delay, split=6)
            error = self.assert_rejected(response)
            self.assertIn('网络提醒只能在主机本地核对', error['error'])
            # 延迟后仍能连上并得到正常响应，证明服务端没有卡死。
            health = self.raw('GET', '/api/health', headers={'Host': self.domain_host})
            self.assertEqual(health.status, 200)

    def test_rejection_responds_before_reading_missing_body(self):
        # 客户端只发送请求头、不发送请求体：拒绝响应必须立即返回，
        # 不能被排空逻辑阻塞；随后客户端关闭写方向，服务端正常收尾。
        started = time.monotonic()
        response = self.raw(
            'POST', '/api/network/ack',
            headers={'Host': self.domain_host, 'Authorization': authorization(),
                     'X-CSRF-Token': self.token},
            body=b'', declared_length=4096, send_body=False, shutdown_write=True)
        elapsed = time.monotonic() - started
        self.assertEqual(response.status, 403)
        self.assertLess(elapsed, 2.5, '拒绝响应不应等待未发送的请求体')
        self.assertTrue(response.closed)

    def test_invalid_content_length_is_rejected_without_hanging(self):
        for value in ('abc', '-1', '1.5', ''):
            with self.subTest(value=value):
                response = self.raw('POST', '/api/network/ack',
                                    headers={'Host': self.local_host,
                                             'X-CSRF-Token': self.token},
                                    body=b'', declared_length=value)
                self.assertEqual(response.status, 400, response.body)
                self.assertTrue(response.closed)
        # 重复且可能互相矛盾的 Content-Length 不能按第一个值盲读。
        response = self.raw('POST', '/api/network/ack',
                            headers={'Host': self.local_host, 'X-CSRF-Token': self.token,
                                     'Content-Length': ['5', '5']},
                            body=b'', declared_length=None)
        self.assertEqual(response.status, 400, response.body)
        self.assertTrue(response.closed)

    def test_oversized_content_length_is_rejected_without_body_transfer(self):
        for value in (5_000_001, 50_000_000):
            with self.subTest(value=value):
                started = time.monotonic()
                response = self.raw('POST', '/api/network/ack',
                                    headers={'Host': self.local_host,
                                             'X-CSRF-Token': self.token},
                                    body=b'', declared_length=value, send_body=False,
                                    shutdown_write=True)
                elapsed = time.monotonic() - started
                self.assertEqual(response.status, 400, response.body)
                self.assertLess(elapsed, 2.0, '超大 Content-Length 不能先读取')
                self.assertTrue(response.closed)

    def test_missing_content_length_and_chunked_body_are_rejected(self):
        response = self.raw('POST', '/api/network/ack',
                            headers={'Host': self.local_host, 'X-CSRF-Token': self.token},
                            body=b'', declared_length=None)
        self.assertEqual(response.status, 400, response.body)
        self.assertTrue(response.closed)
        response = self.raw('POST', '/api/network/ack',
                            headers={'Host': self.local_host, 'X-CSRF-Token': self.token,
                                     'Transfer-Encoding': 'chunked'},
                            body=b'', declared_length=None)
        self.assertEqual(response.status, 400, response.body)
        self.assertTrue(response.closed)

    def test_keep_alive_request_is_explicitly_closed_on_rejection(self):
        response = self.raw(
            'POST', '/api/rows',
            headers={'Host': self.local_host, 'X-CSRF-Token': 'stale'},
            body=json.dumps({'revision': 0, 'rows': []}).encode(),
            connection='keep-alive')
        self.assert_rejected(response)
        self.assertEqual(self.store.read()['revision'], 0)
        # 被关闭的只是这条连接；新连接仍可正常访问。
        health = self.raw('GET', '/api/health', headers={'Host': self.domain_host})
        self.assertEqual(health.status, 200)
        self.assertTrue(health.closed)

    def test_connection_close_rejection_is_clean(self):
        response = self.raw(
            'POST', '/api/network/ack',
            headers={'Host': self.domain_host, 'Authorization': authorization(),
                     'X-CSRF-Token': self.token},
            body=BODY, connection='close')
        self.assert_rejected(response)

    def test_continuous_mixed_rejections_never_abort_or_hang(self):
        rejected = 0
        for index in range(40):
            kind = index % 4
            if kind == 0:
                response = self.raw('POST', '/api/network/ack',
                                    headers={'Host': self.domain_host,
                                             'Authorization': authorization(),
                                             'X-CSRF-Token': self.token},
                                    body=BODY, delay=0.01 if index % 8 else 0)
                self.assertEqual(response.status, 403, response.body)
            elif kind == 1:
                response = self.raw('POST', '/api/network/ack',
                                    headers={'Host': self.local_host},
                                    body=BODY, split=5)
                self.assertEqual(response.status, 403, response.body)
            elif kind == 2:
                response = self.raw('POST', '/api/network/ack',
                                    headers={'Host': f'evil.test:{self.port}'},
                                    body=BODY)
                self.assertEqual(response.status, 403, response.body)
            else:
                response = self.raw('PUT', '/api/rows',
                                    headers={'Host': self.local_host,
                                             'X-CSRF-Token': 'stale'},
                                    body=b'{"revision":0,"rows":[]}')
                self.assertEqual(response.status, 403, response.body)
            self.assertTrue(response.closed)
            rejected += 1
        self.assertEqual(rejected, 40)
        self.assertEqual(self.store.read()['revision'], 0)
        self.assertEqual(list(self.jobs.directory.glob('*.json')), [])


if __name__ == '__main__':
    unittest.main()
