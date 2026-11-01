"""结束工作台接口：只允许主机本地持有效 CSRF Token 的请求。"""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from gift_registry.frontend_server import RowStore, make_server
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.network import NetworkSettings
from gift_registry.registration import RegistrationQueue


class ShutdownEndpointTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.directory.cleanup)
        folder = Path(self.directory.name)
        catalog = folder / "gifts.json"
        catalog.write_text(json.dumps([{"id": "000SKU", "name": "合成礼品"}]), encoding="utf-8")
        self.jobs = RegistrationQueue(folder / "jobs", operations=type("Unconfigured", (), {"configured": False})())
        self.server = make_server(0, store=RowStore(folder / "rows.json"), queue=self.jobs,
                                  catalog=GiftCatalogStore(catalog), network=NetworkSettings())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.host = f"127.0.0.1:{self.server.server_port}"
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, *, method="GET", data=None, headers=None):
        values = {"Host": self.host}
        values.update(headers or {})
        payload = json.dumps(data).encode() if data is not None else None
        return self.opener.open(Request(self.base + path, data=payload, method=method, headers=values), timeout=3)

    def token(self):
        with self.request("/api/config") as response:
            return json.load(response)["token"]

    def test_missing_token_cannot_stop_the_workbench(self):
        with self.assertRaises(HTTPError) as error:
            self.request("/api/shutdown", method="POST", data={})
        self.assertEqual(error.exception.code, 403)
        self.assertTrue(self.thread.is_alive())

    def test_token_from_a_remote_client_cannot_stop_the_workbench(self):
        token = self.token()
        self.server.RequestHandlerClass.is_local_client = lambda _: False
        with self.assertRaises(HTTPError) as error:
            self.request("/api/shutdown", method="POST", data={}, headers={"X-CSRF-Token": token})
        self.assertEqual(error.exception.code, 403)
        self.assertTrue(self.thread.is_alive())

    def test_local_token_stops_the_server(self):
        token = self.token()
        with self.request("/api/shutdown", method="POST", data={}, headers={"X-CSRF-Token": token}) as response:
            self.assertEqual(response.status, 200)
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())

    def test_shutdown_keeps_read_only_page_available_until_accepted_tasks_finish(self):
        self.jobs.pending.put('synthetic-in-flight-task')
        try:
            token = self.token()
            headers = {'X-CSRF-Token': token}
            for _ in range(2):
                with self.request('/api/shutdown', method='POST', data={}, headers=headers) as response:
                    self.assertTrue(json.load(response)['stopping'])
            self.assertTrue(self.thread.is_alive())
            with self.request('/api/health') as response:
                self.assertTrue(json.load(response)['stopping'])
            with self.request('/') as response:
                self.assertEqual(response.status, 200)
            with self.assertRaises(HTTPError) as error:
                self.request('/api/rows', method='PUT', data={'revision': 0, 'rows': []}, headers=headers)
            self.assertEqual(error.exception.code, 409)
            self.assertEqual(json.loads(error.exception.read())['code'], 'server_stopping')
            self.assertTrue(self.jobs._stopping)
        finally:
            self.jobs.pending.get_nowait()
            self.jobs.pending.task_done()
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
