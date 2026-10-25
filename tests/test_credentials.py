import base64
import json
import os
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

from gift_registry.credentials import CredentialStore, CredentialStoreError, Credentials


class FakeCipher:
    """仅用于隔离测试的内存替身，不作为生产加密实现。"""

    def __init__(self):
        self.payloads = {}

    def protect(self, payload):
        token = f"opaque-test-cipher-{len(self.payloads)}".encode("ascii")
        self.payloads[token] = payload
        return token

    def unprotect(self, token):
        return self.payloads[token]


class CredentialStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config" / "accounts.local.json"
        self.cipher = FakeCipher()
        self.store = CredentialStore(self.path, protect=self.cipher.protect, unprotect=self.cipher.unprotect)

    def test_missing_account_is_none_and_read_creates_no_files(self):
        self.assertIsNone(self.store.load("jushuitan"))
        self.assertIsNone(self.store.load("banniu"))
        self.assertFalse(self.path.exists())

    def test_two_platforms_remain_independent_and_password_roundtrips_exactly(self):
        self.store.save("jushuitan", "00001", " isolated-测试-password \n")
        self.store.save("banniu", "00002", "another-test-password")
        first = self.store.load("jushuitan")
        second = self.store.load("banniu")
        self.assertEqual((first.username, first.password), ("00001", " isolated-测试-password \n"))
        self.assertEqual((second.username, second.password), ("00002", "another-test-password"))
        self.store.save("jushuitan", "00003", "changed-test-password")
        self.assertEqual(self.store.load("banniu"), second)
        self.assertEqual(self.store.load("jushuitan").username, "00003")

    def test_disk_and_repr_never_contain_plaintext_password(self):
        password = "synthetic-password-not-for-production"
        self.store.save("banniu", "00002", password)
        data = self.path.read_text(encoding="utf-8")
        self.assertNotIn(password, data)
        self.assertNotIn(password, repr(self.store.load("banniu")))
        self.assertNotIn(password, str(self.store.load("banniu")))
        document = json.loads(data)
        account = document["accounts"]["banniu"]
        self.assertEqual(account["username"], "00002")
        self.assertEqual(set(account), {"username", "password_dpapi"})
        self.assertEqual(base64.b64decode(account["password_dpapi"]), b"opaque-test-cipher-0")
        self.assertFalse(self.path.with_suffix(".json.tmp").exists())

    def test_unknown_site_and_invalid_input_are_rejected_without_writing(self):
        for key in ("other", "../banniu", "jushuitan.com", 123, None):
            with self.subTest(site=key):
                with self.assertRaises(CredentialStoreError):
                    self.store.save(key, "username", "password")
                with self.assertRaises(CredentialStoreError):
                    self.store.load(key)
        for username, password in ((123, "test"), ("", "test"), ("  ", "test"), ("test", ""), ("test", 123)):
            with self.subTest(username=username, password=password), self.assertRaises(CredentialStoreError):
                self.store.save("banniu", username, password)
        self.assertFalse(self.path.exists())

    def test_ciphertext_cannot_be_swapped_between_sites_or_usernames(self):
        self.store.save("jushuitan", "00001", "test-one")
        self.store.save("banniu", "00002", "test-two")
        original = json.loads(self.path.read_text(encoding="utf-8"))
        original["accounts"]["banniu"]["password_dpapi"] = original["accounts"]["jushuitan"]["password_dpapi"]
        self.path.write_text(json.dumps(original), encoding="utf-8")
        with self.assertRaises(CredentialStoreError):
            self.store.load("banniu")
        original["accounts"]["jushuitan"]["username"] = "changed-username"
        self.path.write_text(json.dumps(original), encoding="utf-8")
        with self.assertRaises(CredentialStoreError):
            self.store.load("jushuitan")

    def test_malformed_or_plaintext_file_is_rejected_and_never_overwritten(self):
        documents = ["not-json", '{"version": 2, "accounts": {}}',
                     json.dumps({"version": 1, "accounts": {"banniu": {"username": "name", "password": "synthetic-secret"}}}),
                     json.dumps({"version": 1, "accounts": {"banniu": {"username": "name", "password_dpapi": "not-base64!"}}})]
        self.path.parent.mkdir()
        for document in documents:
            self.path.write_text(document, encoding="utf-8")
            with self.subTest(document=document):
                with self.assertRaises(CredentialStoreError):
                    self.store.load("banniu")
                with self.assertRaises(CredentialStoreError):
                    self.store.save("jushuitan", "name", "new-test-password")
                self.assertEqual(self.path.read_text(encoding="utf-8"), document)

    def test_encryption_and_write_errors_are_sanitized_and_keep_previous_config(self):
        secret = "synthetic-secret-from-error"
        self.store.save("banniu", "00002", "old-test-password")
        original = self.path.read_bytes()

        def fail(*_):
            raise RuntimeError(secret)

        failing = CredentialStore(self.path, protect=fail, unprotect=fail)
        actions = (lambda: failing.save("jushuitan", "00001", secret), lambda: failing.load("banniu"))
        for action in actions:
            try:
                action()
                self.fail("必须拒绝底层加解密错误")
            except CredentialStoreError as exc:
                self.assertNotIn(secret, str(exc))
                self.assertNotIn(secret, "".join(traceback.format_exception(exc)))
        with patch("gift_registry.credentials.atomic_write", side_effect=OSError(secret)):
            with self.assertRaises(CredentialStoreError) as raised:
                self.store.save("jushuitan", "00001", secret)
            self.assertNotIn(secret, str(raised.exception))
        self.assertEqual(self.path.read_bytes(), original)

    @unittest.skipUnless(os.name == "nt", "真实 DPAPI 仅适用于 Windows")
    def test_real_dpapi_roundtrip_uses_only_isolated_synthetic_credentials(self):
        store = CredentialStore(self.path)
        password = "synthetic-DPAPI-password-测试-123"
        store.save("banniu", "000-test-user", password)
        self.assertEqual(store.load("banniu"), Credentials("000-test-user", password))
        self.assertNotIn(password.encode("utf-8"), self.path.read_bytes())


if __name__ == "__main__":
    unittest.main()
