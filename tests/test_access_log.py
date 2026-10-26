"""最小访问日志；使用隔离目录与本地临时服务，不写生产日志。"""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from gift_registry.access_log import AccessLog, client_network, request_path
from gift_registry.frontend_server import RowStore, make_server
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.network import NetworkSettings
from gift_registry.network_monitor import NetworkMonitor
from gift_registry.registration import RegistrationQueue


class ClientNetworkTests(unittest.TestCase):
    def test_addresses_are_reduced_to_networks_and_loopback(self):
        self.assertEqual(client_network("10.12.90.26"), "10.12.90.0/24")
        self.assertEqual(client_network("192.168.1.91"), "192.168.1.0/24")
        self.assertEqual(client_network("127.0.0.1"), "loopback")
        self.assertEqual(client_network("::1"), "loopback")
        self.assertEqual(client_network("2001:db8:1:2:3:4:5:6"), "2001:db8:1:2::/64")
        for value in ("not-an-ip", "", None, 1234):
            with self.subTest(value=value):
                self.assertEqual(client_network(value), "unknown")


class RequestPathTests(unittest.TestCase):
    def test_query_fragment_and_unprintable_text_are_removed(self):
        self.assertEqual(request_path("/"), "/")
        self.assertEqual(request_path(""), "/")
        self.assertEqual(request_path("/api/rows?revision=3&token=secret"), "/api/rows")
        self.assertEqual(request_path("/api/rows#fragment"), "/api/rows")
        self.assertEqual(request_path("/a b\x01c"), "/a_b_c")
        # 换行和制表符由 urlsplit 先移除，不会进入日志行。
        self.assertEqual(request_path("/a b\r\nc"), "/a_bc")
        self.assertEqual(request_path("/" + "x" * 500), "/" + "x" * 119)
        self.assertEqual(request_path("//["), "-")


class AccessLogFileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)

    def test_line_contains_status_method_path_host_and_network_without_query(self):
        AccessLog(self.folder / "access.log").write(
            403, "get", "/api/config?token=secret", "ldkldk.win:8765", "10.12.90.26")
        text = (self.folder / "access.log").read_text(encoding="utf-8")
        self.assertRegex(
            text,
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2} 403 GET /api/config "
            r"ldkldk\.win:8765 10\.12\.90\.0/24\n$")
        self.assertNotIn("secret", text)

    def test_startup_note_keeps_spaces_and_has_no_access_password(self):
        AccessLog(self.folder / "access.log").note(
            "port=8765 lan=on ip=10.12.90.26 subnet=10.12.88.0/22 domain=ldkldk.win")
        text = (self.folder / "access.log").read_text(encoding="utf-8")
        self.assertRegex(
            text,
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2} START port=8765 lan=on "
            r"ip=10\.12\.90\.26 subnet=10\.12\.88\.0/22 domain=ldkldk\.win\n$")

    def test_large_log_rotates_to_exactly_one_backup(self):
        log = AccessLog(self.folder / "access.log", max_bytes=1024)
        for index in range(40):
            log.write(200, "GET", f"/api/rows?n={index}", "host:8765", "10.12.90.26")
        self.assertTrue((self.folder / "access.log").exists())
        self.assertTrue((self.folder / "access.log.1").exists())
        self.assertFalse((self.folder / "access.log.2").exists())
        self.assertLessEqual((self.folder / "access.log").stat().st_size, 1024)

    def test_unwritable_target_never_raises(self):
        blocker = self.folder / "blocked"
        blocker.write_text("x", encoding="utf-8")
        log = AccessLog(blocker / "access.log")
        log.write(200, "GET", "/", "host:8765", "127.0.0.1")
        log.note("port=8765")
        self.assertTrue(blocker.is_file())


class HandlerAccessLogTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.directory.cleanup)
        folder = Path(self.directory.name)
        catalog = folder / "gifts.json"
        catalog.write_text(json.dumps([{"id": "000SKU", "name": "合成礼品"}]), encoding="utf-8")
        self.log_path = folder / "access.log"
        self.jobs = RegistrationQueue(folder / "jobs", operations=type("Unconfigured", (), {"configured": False})())
        self.monitor = NetworkMonitor(NetworkSettings(), path=folder / "network-baseline.json",
                                      notify=None, probe=lambda: [])
        self.server = make_server(0, store=RowStore(folder / "rows.json"), queue=self.jobs,
                                  catalog=GiftCatalogStore(catalog), network=NetworkSettings(),
                                  network_monitor=self.monitor, access_log=AccessLog(self.log_path))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.host = f"127.0.0.1:{self.server.server_port}"
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def get(self, path, *, host=None, headers=None):
        values = {"Host": host or self.host, **(headers or {})}
        return self.opener.open(Request(self.url + path, headers=values), timeout=3)

    def lines(self):
        return self.log_path.read_text(encoding="utf-8").splitlines()

    def test_requests_are_logged_but_successful_status_polling_is_not(self):
        with self.get("/api/health") as response:
            self.assertEqual(response.status, 200)
        with self.assertRaises(HTTPError) as error:
            self.get("/api/config", host=f"evil.test:{self.server.server_port}")
        self.assertEqual(error.exception.code, 403)
        error.exception.read()
        with self.assertRaises(HTTPError) as error:
            self.get("/missing")
        self.assertEqual(error.exception.code, 404)
        error.exception.read()
        with self.get("/api/registrations/status") as response:
            self.assertEqual(response.status, 200)
        with self.get("/api/network/status") as response:
            self.assertEqual(response.status, 200)
        with self.get("/api/rows?token=secret") as response:
            self.assertEqual(response.status, 200)

        text = "\n".join(self.lines())
        self.assertIn(f" 200 GET /api/health {self.host} loopback", text)
        self.assertIn(f" 403 GET /api/config evil.test:{self.server.server_port} loopback", text)
        self.assertIn(f" 404 GET /missing {self.host} loopback", text)
        self.assertIn(f" 200 GET /api/rows {self.host} loopback", text)
        self.assertNotIn("/api/registrations/status", text)
        self.assertNotIn("/api/network/status", text)
        self.assertNotIn("secret", text)

    def test_authorization_header_is_never_written(self):
        token = "Basic dGVzdDpzZWNyZXQ="
        with self.get("/api/rows", headers={"Authorization": token}) as response:
            self.assertEqual(response.status, 200)
        text = "\n".join(self.lines())
        self.assertIn(" 200 GET /api/rows", text)
        self.assertNotIn("dGVzdDpzZWNyZXQ", text)
        self.assertNotIn("Authorization", text)


if __name__ == "__main__":
    unittest.main()
