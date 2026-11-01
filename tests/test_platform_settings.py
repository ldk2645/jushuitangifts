import json
import unittest
from unittest.mock import Mock, patch

from tests import test_credentials
from gift_registry.business_browser import BusinessBrowsers
from gift_registry.browser import SiteBrowser
from gift_registry.config import SITES
from gift_registry.credentials import CredentialStoreError


class SettingsStoreTests(unittest.TestCase):
    setUp = test_credentials.CredentialStoreTests.setUp
    def values(self):
        return {'jushuitan': {'username':'001','password':'synthetic-one','headless':True},
                'banniu': {'username':'002','password':'synthetic-two','headless':False}}

    def test_options_roundtrip_without_plaintext_and_blank_password_keeps_cipher(self):
        self.store.save_settings(self.values())
        before = json.loads(self.path.read_text(encoding='utf-8'))['accounts']
        self.assertNotIn('synthetic-one', self.path.read_text(encoding='utf-8'))
        values = self.values()
        for entry in values.values():
            entry['password'] = ''
        values['banniu']['headless'] = True
        self.store.save_settings(values)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['accounts'], before)
        self.assertTrue(self.store.settings()['banniu']['headless'])
        self.assertEqual(self.store.load('jushuitan').password,'synthetic-one')
        self.store.save('banniu','003','replacement')
        self.assertTrue(self.store.settings()['banniu']['headless'])

    def test_mode_only_save_preserves_accounts_without_decryption(self):
        self.store.save_settings(self.values())
        before = json.loads(self.path.read_text(encoding='utf-8'))['accounts']
        self.store._unprotect = Mock(side_effect=AssertionError('must not decrypt'))
        self.store._protect = Mock(side_effect=AssertionError('must not encrypt'))
        self.store.save_browser_modes({'banniu': True, 'jushuitan': False})
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['accounts'], before)
        self.assertTrue(self.store.settings()['banniu']['headless'])
        with self.assertRaises(CredentialStoreError):
            self.store.save_browser_modes({'banniu': 'false', 'jushuitan': False})
        self.assertTrue(self.store.settings()['banniu']['headless'])

    def test_invalid_flags_and_missing_new_password_do_not_partially_save(self):
        self.store.save_settings(self.values())
        before = self.path.read_bytes()
        values = self.values()
        values['jushuitan']['password'] = 'changed-but-must-not-save'
        values['banniu'].update(username='changed-user',password='')
        with self.assertRaises(CredentialStoreError):
            self.store.save_settings(values)
        self.assertEqual(self.path.read_bytes(),before)
        doc = json.loads(before)
        doc['browser_headless']['banniu'] = 'false'
        self.path.write_text(json.dumps(doc),encoding='utf-8')
        with self.assertRaises(CredentialStoreError):
            self.store.settings()

    def test_old_accounts_default_to_visible_and_options_do_not_require_decryption(self):
        self.store.save('banniu','002','synthetic-two')
        self.store._unprotect = Mock(side_effect=ValueError())
        self.assertFalse(self.store.settings()['banniu']['headless'])
        self.store._unprotect.assert_not_called()


class BusinessDisplayTests(unittest.TestCase):
    def test_business_uses_per_site_flag_but_login_browser_stays_visible(self):
        flags={'jushuitan':{'headless':True},'banniu':{'headless':False}}
        with patch('gift_registry.credentials.CredentialStore') as store, \
                patch.object(SiteBrowser,'open'):
            store.return_value.settings.return_value=flags
            manager=BusinessBrowsers(lock_factory=Mock(),store_factory=Mock())
            self.assertTrue(manager.get('jushuitan').headless)
            self.assertFalse(manager.get('banniu').headless)
            self.assertFalse(SiteBrowser(SITES[0],Mock(),authenticator=False).headless)
            manager._release_profiles()
