import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from gift_registry.frontend_server import RowStore, make_server
from gift_registry.gift_catalog import GiftCatalogConflict, GiftCatalogStore
from gift_registry.registration import RegistrationQueue


class UnconfiguredOperations:
    configured = False


def seed(path):
    catalog = [{"id": "0001", "name": "原有礼赠", "aliases": ["原有简称"]}]
    path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    return GiftCatalogStore(path)


class GiftCatalogTests(unittest.TestCase):
    def test_add_preserves_original_and_survives_new_store(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gifts.json"
            store = seed(path)
            original = store.read()[0]
            result = store.add({"id": " 000023 ", "name": " 新礼赠Pro+套装 ", "aliases": ["新礼简称", "新礼简称", "新礼赠Pro+套装"]})
            self.assertEqual(result["gift"], {"id": "000023", "name": "新礼赠Pro+套装", "aliases": ["新礼简称"]})
            self.assertEqual(GiftCatalogStore(path).read(), [original, result["gift"]])

    def test_duplicate_codes_names_and_cross_aliases_never_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gifts.json"
            store = seed(path)
            before = path.read_bytes()
            for gift in ({"id": "0001", "name": "另一礼赠"}, {"id": "0002", "name": "原有礼赠"},
                         {"id": "0002", "name": "新礼赠", "aliases": ["原有简称"]},
                         {"id": "0002", "name": "原有简称"}, {"id": "000-1", "name": "新礼赠"}):
                with self.subTest(gift=gift), self.assertRaises(GiftCatalogConflict):
                    store.add(gift)
                self.assertEqual(path.read_bytes(), before)

    def test_invalid_values_do_not_change_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gifts.json"
            store = seed(path)
            before = path.read_bytes()
            for gift in ({"id": "", "name": "新礼赠"}, {"id": 123, "name": "新礼赠"},
                         {"id": "2", "name": " "}, {"id": "2", "name": "换\n行"},
                         {"id": "2", "name": "新礼赠", "aliases": "别名"},
                         {"id": "2", "name": "新礼赠", "aliases": [""]}):
                with self.subTest(gift=gift), self.assertRaises(ValueError):
                    store.add(gift)
                self.assertEqual(path.read_bytes(), before)

    def test_simultaneous_additions_are_both_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            store = seed(Path(directory) / "gifts.json")
            barrier = threading.Barrier(2)

            def add(number):
                barrier.wait()
                return store.add({"id": f"gift-{number}", "name": f"新礼赠{number}"})

            with ThreadPoolExecutor(max_workers=2) as executor:
                list(executor.map(add, (2, 3)))
            self.assertEqual({gift["id"] for gift in store.read()}, {"0001", "gift-2", "gift-3"})

    def test_new_gift_is_immediately_available_to_existing_http_server_and_job(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            store = seed(folder / "gifts.json")
            server = make_server(0, store=RowStore(folder / "rows.json"),
                                 queue=RegistrationQueue(folder / "jobs", operations=UnconfiguredOperations()), catalog=store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(url + "/api/config") as response:
                    token = json.load(response)["token"]
                gift = {"id": "000099", "name": "新增礼赠", "aliases": ["新增简称"]}

                def post(path, data, authorized=True, method="POST"):
                    headers = {"X-CSRF-Token": token} if authorized else {}
                    return urlopen(Request(url + path, data=json.dumps(data).encode(), headers=headers, method=method))

                with self.assertRaises(HTTPError) as error:
                    post("/api/gifts", gift, authorized=False)
                self.assertEqual(error.exception.code, 403)
                self.assertEqual(len(store.read()), 1)
                with post("/api/gifts", gift) as response:
                    self.assertEqual(response.status, 201)
                    self.assertEqual(json.load(response)["gift"], gift)
                with self.assertRaises(HTTPError) as error:
                    post("/api/gifts", gift)
                self.assertEqual(error.exception.code, 409)
                self.assertEqual(json.load(error.exception)["code"], "gift_catalog_conflict")
                for path in ("/api/config", "/api/gifts"):
                    with urlopen(url + path) as response:
                        self.assertEqual(json.load(response)["gifts"][-1], gift)
                row = {"id": "new-row", "order_no": "00012345678901234567890", "gift": gift["name"], "gift_ids": [gift["id"]],
                       "change_address": False, "recipient_name": "", "phone": "", "address": ""}
                with post("/api/rows", {"revision": 0, "rows": [row]}, method="PUT") as response:
                    self.assertEqual(json.load(response)["revision"], 1)
                with post("/api/registrations/start", {"revision": 1}) as response:
                    self.assertEqual(json.load(response)["state"], "waiting_for_workflow")
                job = json.loads(next((folder / "jobs").glob("*.json")).read_text(encoding="utf-8"))
                self.assertIn(gift, job["gift_catalog"])
                self.assertEqual(job["rows"][0]["gift_ids"], [gift["id"]])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
