import tempfile
import unittest
from unittest.mock import Mock, PropertyMock, patch
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.common.exceptions import InvalidSessionIdException, WebDriverException
from pathlib import Path

from gift_registry.browser import SiteBrowser
from gift_registry.business_browser import BusinessBrowsers, ProfileLock
from gift_registry.workflow import PlatformOperationError


class FakeLockBackend:
    LK_NBLCK = 1
    LK_UNLCK = 2

    def __init__(self, *, deny=False):
        self.deny = deny
        self.calls = []

    def locking(self, fileno, operation, length):
        self.calls.append((operation, length))
        if self.deny and operation == self.LK_NBLCK:
            raise OSError("private driver/session data must not escape")


class ExclusiveLockBackend(FakeLockBackend):
    """通过文件句柄模拟同一配置字节锁的互斥，不使用真实账号目录。"""

    def __init__(self):
        super().__init__()
        self.owner = None

    def locking(self, fileno, operation, length):
        super().locking(fileno, operation, length)
        if operation == self.LK_NBLCK:
            if self.owner is not None:
                raise OSError("private lock details must not escape")
            self.owner = fileno
        elif operation == self.LK_UNLCK:
            if self.owner != fileno:
                raise OSError("wrong lock owner")
            self.owner = None


class FakeOwnership:
    def __init__(self, path):
        self.path = path
        self.acquires = 0
        self.releases = 0
        self.deny = False

    def acquire(self):
        self.acquires += 1
        if self.deny:
            raise PlatformOperationError("请先保存并关闭登录助手")

    def release(self):
        self.releases += 1


class FakeDriver:
    def __init__(self):
        self.quits = 0

    def quit(self):
        self.quits += 1


class FakeBrowser:
    def __init__(self, site, store):
        self.site = site
        self.store = store
        self.driver = FakeDriver()
        self.opens = 0
        self.closes = 0
        self.fail_open = False
        self.fail_close = False

    def open(self):
        self.opens += 1
        if self.fail_open:
            raise RuntimeError("private driver/session data must not escape")

    def close(self):
        self.closes += 1
        if self.fail_close:
            raise ValueError("private driver/session data must not escape")
        self.driver.quit()


class ControlledSiteBrowser(SiteBrowser):
    """走真实 SiteBrowser 的关闭契约，但不启动或读取任何真实浏览器。"""

    def __init__(self, site, store):
        super().__init__(site, store)
        self.driver = FakeDriver()
        self.original_driver = self.driver
        self.opens = self.closes = 0
        self.fail_open = self.fail_close = self.return_unclosed = False

    def open(self):
        self.opens += 1
        if self.fail_open:
            raise RuntimeError("private synthetic startup failure")

    def close(self):
        self.closes += 1
        if self.fail_close:
            raise RuntimeError("private synthetic close failure")
        if self.return_unclosed:
            return {"closed": False}
        self.driver = None
        return {"closed": True}


class ProfileLockTests(unittest.TestCase):
    def test_lock_uses_login_helpers_first_byte_and_releases_once(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            backend = FakeLockBackend()
            path = Path(folder) / "data" / "app.lock"
            lock = ProfileLock(path, backend=backend)
            lock.acquire()
            lock.acquire()
            self.assertEqual(path.read_bytes(), b"0")
            lock.release()
            lock.release()
            self.assertEqual(backend.calls, [(backend.LK_NBLCK, 1), (backend.LK_UNLCK, 1)])
            self.assertTrue(path.exists())
            self.assertIsNone(lock.file)

    def test_contention_is_actionable_and_does_not_leak_raw_exception(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            backend = FakeLockBackend(deny=True)
            lock = ProfileLock(Path(folder) / "app.lock", backend=backend)
            with self.assertRaises(PlatformOperationError) as raised:
                lock.acquire()
            self.assertIn("登录助手", str(raised.exception))
            self.assertNotIn("private", str(raised.exception))
            self.assertIsNone(lock.file)
            lock.release()
            self.assertEqual(len(backend.calls), 1)


class BusinessBrowsersTests(unittest.TestCase):
    def test_closed_session_replaced_once_while_profile_lock_stays_owned(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            manager, created = self.make_controlled_manager(folder)
            original = manager.get('jushuitan')
            original.driver = Mock(spec=WebDriver)
            type(original.driver).window_handles = PropertyMock(side_effect=InvalidSessionIdException('private'))
            with patch('gift_registry.business_browser.configure_transport', return_value=None):
                replacement = manager.get('jushuitan')
            self.assertIsNot(original, replacement)
            self.assertEqual(original.closes, 1)
            self.assertEqual(len(created), 2)
            self.assertIs(manager.get('jushuitan'), replacement)
            self.assertEqual(manager.lock.releases, 0)
            manager.close()

    def test_uncertain_connection_does_not_replace_browser_or_release_lock(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            manager, created = self.make_controlled_manager(folder)
            original = manager.get('jushuitan')
            original.driver = Mock(spec=WebDriver)
            type(original.driver).window_handles = PropertyMock(side_effect=WebDriverException('private'))
            with patch('gift_registry.business_browser.configure_transport', return_value=None):
                with self.assertRaisesRegex(PlatformOperationError, '无法确认窗口状态'):
                    manager.get('jushuitan')
            self.assertEqual(len(created), 1)
            self.assertEqual(original.closes, 0)
            self.assertEqual(manager.lock.releases, 0)
            manager.close()

    def test_closed_session_cleanup_failure_prevents_reopening(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            manager, created = self.make_controlled_manager(folder, fail_close=True)
            original = manager.get('jushuitan')
            original.driver = Mock(spec=WebDriver, window_handles=[])
            with patch('gift_registry.business_browser.configure_transport', return_value=None):
                with self.assertRaisesRegex(PlatformOperationError, '清理未完成'):
                    manager.get('jushuitan')
            self.assertEqual(len(created), 1)
            self.assertIn('jushuitan', manager._pending_cleanup)
            self.assertEqual(manager.lock.releases, 0)
            original.fail_close = False
            manager.close()

    def make_controlled_manager(self, folder, *, fail_open=False, fail_close=False):
        created = []

        def factory(site, store):
            browser = ControlledSiteBrowser(site, store)
            if site.key == "jushuitan":
                browser.fail_open, browser.fail_close = fail_open, fail_close
            created.append(browser)
            return browser

        manager = BusinessBrowsers(data_dir=folder, browser_factory=factory,
                                   store_factory=lambda path: path, lock_factory=FakeOwnership)
        return manager, created

    def test_real_close_failure_still_closes_other_site_and_keeps_lock_until_retry(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            manager, created = self.make_controlled_manager(folder, fail_close=True)
            first, second = manager.get("jushuitan"), manager.get("banniu")
            with self.assertRaises(PlatformOperationError) as raised:
                manager.close()
            self.assertNotIn("private", str(raised.exception))
            self.assertEqual((first.closes, second.closes), (1, 1))
            self.assertEqual(first.original_driver.quits, 0)
            self.assertIsNone(second.driver)
            self.assertEqual(manager._pending_cleanup, {"jushuitan": first})
            self.assertEqual(manager.lock.releases, 0)
            self.assertFalse(manager._closed)
            with self.assertRaisesRegex(PlatformOperationError, "关闭尚未完成"):
                manager.get("banniu")
            with self.assertRaisesRegex(PlatformOperationError, "关闭尚未完成"):
                manager.reserve_for_start()
            first.fail_close = False
            manager.close()
            self.assertEqual((first.closes, second.closes), (2, 1))
            self.assertEqual(manager.lock.releases, 1)
            self.assertTrue(manager._closed)
            self.assertEqual(manager._pending_cleanup, {})

    def test_reserved_open_and_cleanup_failure_is_retained_and_not_released_as_unused(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            manager, created = self.make_controlled_manager(folder, fail_open=True, fail_close=True)
            manager.reserve_for_start()
            with self.assertRaisesRegex(PlatformOperationError, "关闭未完成") as raised:
                manager.get("jushuitan")
            self.assertNotIn("private", str(raised.exception))
            self.assertEqual(manager.browsers, {})
            self.assertEqual(manager._pending_cleanup, {"jushuitan": created[0]})
            self.assertFalse(manager.release_unused_reservation())
            self.assertEqual(manager.lock.releases, 0)
            for site in ("jushuitan", "banniu"):
                with self.assertRaisesRegex(PlatformOperationError, "关闭尚未完成"):
                    manager.get(site)
            self.assertEqual(len(created), 1)
            self.assertEqual(created[0].original_driver.quits, 0)
            created[0].fail_close = False
            manager.close()
            self.assertEqual(manager.lock.releases, 1)

    def test_unreserved_failed_open_also_keeps_failed_cleanup_owned(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            manager, created = self.make_controlled_manager(folder, fail_open=True, fail_close=True)
            with self.assertRaises(PlatformOperationError):
                manager.get("jushuitan")
            self.assertTrue(manager._owns_profiles)
            self.assertEqual(manager.lock.releases, 0)
            self.assertEqual(manager._pending_cleanup, {"jushuitan": created[0]})
            created[0].fail_close = False
            manager.close()
            self.assertEqual(manager.lock.releases, 1)

    def test_explicit_unclosed_result_does_not_trigger_native_quit_or_release(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            manager, created = self.make_controlled_manager(folder)
            first = manager.get("jushuitan")
            first.return_unclosed = True
            with self.assertRaises(PlatformOperationError):
                manager.close()
            self.assertEqual(first.original_driver.quits, 0)
            self.assertEqual(manager.lock.releases, 0)
            self.assertIs(manager._pending_cleanup["jushuitan"], first)
            first.return_unclosed = False
            manager.close()
            self.assertEqual(manager.lock.releases, 1)

    def test_start_reservation_contention_never_opens_a_browser(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            manager, created = self.make_manager(folder)
            manager.lock.deny = True
            with self.assertRaisesRegex(PlatformOperationError, "登录助手"):
                manager.reserve_for_start()
            self.assertEqual(created, [])
            self.assertFalse(manager.release_unused_reservation())
            manager.close()
            self.assertEqual(manager.lock.releases, 0)

    def test_reservation_keeps_external_owner_out_until_explicit_release_or_close(self):
        for close_manager in (False, True):
            with self.subTest(close=close_manager), tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
                backend = ExclusiveLockBackend()
                created = []

                def create_browser(site, store):
                    created.append(site.key)
                    return FakeBrowser(site, store)

                manager = BusinessBrowsers(data_dir=folder, browser_factory=create_browser,
                                           store_factory=lambda path: path,
                                           lock_factory=lambda path: ProfileLock(path, backend=backend))
                external = ProfileLock(Path(folder) / "app.lock", backend=backend)
                manager.reserve_for_start()
                manager.reserve_for_start()
                self.assertEqual(created, [])
                with self.assertRaises(PlatformOperationError):
                    external.acquire()
                if close_manager:
                    manager.close()
                else:
                    self.assertTrue(manager.release_unused_reservation())
                    self.assertFalse(manager.release_unused_reservation())
                external.acquire()
                external.release()
                manager.close()
                self.assertIsNone(backend.owner)

    def test_existing_browser_owner_can_reserve_idempotently_but_not_release_while_open(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            manager, created = self.make_manager(folder)
            browser = manager.get("jushuitan")
            manager.reserve_for_start()
            manager.reserve_for_start()
            self.assertEqual(manager.lock.acquires, 1)
            self.assertEqual(len(created), 1)
            self.assertFalse(manager.release_unused_reservation())
            self.assertEqual(manager.lock.releases, 0)
            self.assertIs(manager.get("jushuitan"), browser)
            manager.close()
            self.assertEqual(manager.lock.releases, 1)

    def test_reserved_first_open_failure_keeps_lock_until_queue_releases_unused_start(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            manager, created = self.make_manager(folder, fail_site="jushuitan")
            manager.reserve_for_start()
            with self.assertRaises(PlatformOperationError):
                manager.get("jushuitan")
            self.assertEqual(created[0].closes, 1)
            self.assertEqual(manager.lock.releases, 0)
            manager.reserve_for_start()
            self.assertEqual(manager.lock.acquires, 1)
            self.assertTrue(manager.release_unused_reservation())
            self.assertEqual(manager.lock.releases, 1)
            manager.reserve_for_start()
            self.assertEqual(manager.lock.acquires, 2)
            manager.close()
            self.assertEqual(manager.lock.releases, 2)

    def test_closed_manager_cannot_reserve_or_reacquire_profiles(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            manager, created = self.make_manager(folder)
            manager.reserve_for_start()
            manager.close()
            with self.assertRaisesRegex(PlatformOperationError, "已关闭"):
                manager.reserve_for_start()
            self.assertFalse(manager.release_unused_reservation())
            self.assertEqual(manager.lock.acquires, 1)
            self.assertEqual(manager.lock.releases, 1)
            self.assertEqual(created, [])

    def test_custom_directory_cannot_open_real_user_profiles_with_a_different_lock(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            with self.assertRaisesRegex(PlatformOperationError, "隔离的浏览器工厂"):
                BusinessBrowsers(data_dir=folder)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def make_manager(self, folder, *, fail_site=None):
        created = []

        def browser_factory(site, store):
            self.assertEqual(manager.lock.acquires, 1)
            browser = FakeBrowser(site, store)
            browser.fail_open = site.key == fail_site
            created.append(browser)
            return browser

        manager = BusinessBrowsers(data_dir=folder, browser_factory=browser_factory,
                                   store_factory=lambda path: path, lock_factory=FakeOwnership)
        return manager, created

    def test_browsers_open_lazily_reuse_one_owner_and_original_session_directory(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            manager, created = self.make_manager(folder)
            self.assertEqual(created, [])
            self.assertEqual(manager.lock.acquires, 0)
            first = manager.get("jushuitan")
            self.assertIs(manager.get("jushuitan"), first)
            self.assertIs(manager.driver("jushuitan"), first.driver)
            second = manager.get("banniu")
            self.assertEqual([browser.site.key for browser in created], ["jushuitan", "banniu"])
            self.assertTrue(all(browser.opens == 1 for browser in created))
            self.assertEqual(first.store, Path(folder) / "sessions")
            self.assertEqual(manager.lock.path, Path(folder) / "app.lock")
            manager.close()
            manager.close()
            self.assertEqual((first.closes, second.closes, manager.lock.releases), (1, 1, 1))
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_login_helper_contention_never_opens_a_browser(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            manager, created = self.make_manager(folder)
            manager.lock.deny = True
            with self.assertRaisesRegex(PlatformOperationError, "登录助手"):
                manager.get("jushuitan")
            manager.close()
            self.assertEqual(created, [])
            self.assertEqual(manager.lock.releases, 0)

    def test_failed_first_open_cleans_up_and_releases_ownership(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            manager, created = self.make_manager(folder, fail_site="jushuitan")
            with self.assertRaises(PlatformOperationError) as raised:
                manager.get("jushuitan")
            self.assertNotIn("private", str(raised.exception))
            self.assertEqual(created[0].closes, 1)
            self.assertEqual(manager.lock.releases, 1)
            self.assertEqual(manager.browsers, {})
            manager.close()
            self.assertEqual(manager.lock.releases, 1)

    def test_failed_second_open_keeps_first_profiles_owned_until_close(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            manager, created = self.make_manager(folder, fail_site="banniu")
            first = manager.get("jushuitan")
            with self.assertRaises(PlatformOperationError):
                manager.get("banniu")
            self.assertEqual(first.closes, 0)
            self.assertEqual(manager.lock.releases, 0)
            manager.close()
            self.assertEqual([browser.closes for browser in created], [1, 1])
            self.assertEqual(manager.lock.releases, 1)

    def test_failed_save_still_quits_owned_driver_and_closes_other_browser(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            manager, created = self.make_manager(folder)
            manager.get("jushuitan").fail_close = True
            manager.get("banniu")
            manager.close()
            self.assertEqual([browser.driver.quits for browser in created], [1, 1])
            self.assertEqual(manager.lock.releases, 1)

    def test_unknown_site_and_closed_manager_never_acquire_profiles(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            manager, created = self.make_manager(folder)
            with self.assertRaisesRegex(PlatformOperationError, "站点未配置"):
                manager.get("unknown")
            manager.close()
            with self.assertRaisesRegex(PlatformOperationError, "已关闭"):
                manager.get("jushuitan")
            self.assertEqual(created, [])
            self.assertEqual(manager.lock.acquires, 0)
            self.assertEqual(manager.lock.releases, 0)


if __name__ == "__main__":
    unittest.main()
