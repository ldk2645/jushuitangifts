import tempfile
import unittest
from pathlib import Path

from gift_registry.config import SITES
from gift_registry.sessions import MAGIC, SessionStore, cookie_for_restore


class SessionTests(unittest.TestCase):
    def test_encrypted_roundtrip_and_no_plaintext_token(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            store = SessionStore(Path(directory))
            snapshot = {"version": 1, "site": "banniu", "cookies": [{"value": "secret-token-123"}]}
            store.save("banniu", snapshot)
            data = store.path("banniu").read_bytes()
            self.assertTrue(data.startswith(MAGIC))
            self.assertNotIn(b"secret-token-123", data)
            self.assertEqual(store.load("banniu"), snapshot)

    def test_expired_and_foreign_cookies_are_not_restored(self):
        cookie = {"name": "sid", "value": "test", "domain": ".bytenew.com", "expires": 100}
        self.assertIsNone(cookie_for_restore(cookie, SITES[1], now=101))
        self.assertIsNone(cookie_for_restore({**cookie, "domain": "evilbytenew.com"}, SITES[1], now=99))
        self.assertIsNone(cookie_for_restore({**cookie, "domain": "bytenew.com.evil.test"}, SITES[1], now=99))

    def test_session_cookie_and_httponly_are_preserved(self):
        cookie = {"name": "sid", "value": "test", "domain": "work.bytenew.com", "expires": -1,
                  "httpOnly": True, "secure": True, "sameSite": "Lax", "size": 12}
        result = cookie_for_restore(cookie, SITES[1])
        self.assertNotIn("expires", result)
        self.assertNotIn("size", result)
        self.assertTrue(result["httpOnly"])
        self.assertEqual(result["sameSite"], "Lax")

    def test_truncated_snapshot_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            store = SessionStore(Path(directory))
            store.path("banniu").write_bytes(b"broken")
            with self.assertRaises(ValueError):
                store.load("banniu")


if __name__ == "__main__":
    unittest.main()
