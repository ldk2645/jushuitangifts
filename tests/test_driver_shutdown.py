"""有界驱动关闭测试；全部为 fake 句柄、连接池与自有进程。"""

import copy
import gc
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from selenium.webdriver.remote.command import Command
from urllib3.exceptions import ReadTimeoutError

from gift_registry.driver_shutdown import (BrowserShutdownError, configure_transport,
                                           quit_owned_driver, track_owned_browser,
                                           _WindowsProcessHandle)


class FakePool:
    def __init__(self):
        self.connection_pool_kw = {"timeout": 120}
        self.clears = 0

    def clear(self):
        self.clears += 1


class FakeExecutor:
    def __init__(self, arguments=None):
        self.client_config = SimpleNamespace(timeout=120, init_args_for_pool_manager=arguments or {})
        self._conn = FakePool()
        self.closes = 0

    def close(self):
        self.closes += 1


class FakeHandle:
    def __init__(self, *, exited=False, refuse=False):
        self.exited = exited
        self.refuse = refuse
        self.waits = []
        self.terminations = 0
        self.closes = 0

    def wait(self, timeout):
        self.waits.append(timeout)
        return self.exited

    def terminate(self):
        self.terminations += 1
        if not self.refuse:
            self.exited = True

    def close(self):
        self.closes += 1


class FakeProcess:
    def __init__(self, *, hung=False, refuse_kill=False):
        self.code = None
        self.hung = hung
        self.refuse_kill = refuse_kill
        self.terminations = 0
        self.kills = 0
        self.waits = []

    def poll(self):
        return self.code

    def terminate(self):
        self.terminations += 1
        if not self.hung:
            self.code = 0

    def wait(self, timeout):
        self.waits.append(timeout)
        if self.code is None:
            raise subprocess.TimeoutExpired("fake-owned-driver", timeout)
        return self.code

    def kill(self):
        self.kills += 1
        if not self.refuse_kill:
            self.code = 1


class FakeDriver:
    def __init__(self, *, error=False, process=None, on_quit=None, arguments=None):
        self.command_executor = FakeExecutor(arguments)
        self.service = SimpleNamespace(process=process or FakeProcess())
        self.capabilities = {"goog:processID": 123}
        self.session_id = "synthetic-session"
        self.error = error
        self.on_quit = on_quit
        self.commands = []

    def execute(self, command):
        self.commands.append(command)
        if self.error:
            raise ReadTimeoutError(None, "http://127.0.0.1/", "private-secret-must-not-escape")
        if self.on_quit:
            self.on_quit()
        return {"value": None}


class TransportTests(unittest.TestCase):
    def test_timeout_and_retries_are_local_to_driver_and_existing_pools_are_cleared(self):
        arguments = {"init_args_for_pool_manager": {"retries": 3, "maxsize": 2}}
        original = copy.deepcopy(arguments)
        driver, other = FakeDriver(arguments=arguments), FakeDriver(arguments=arguments)
        self.assertEqual(configure_transport(driver, 3), 120)
        self.assertEqual(driver.command_executor.client_config.timeout, 3)
        self.assertIs(driver.command_executor._conn.connection_pool_kw["retries"], False)
        self.assertEqual(driver.command_executor._conn.clears, 1)
        self.assertIs(driver.command_executor.client_config.init_args_for_pool_manager[
            "init_args_for_pool_manager"]["retries"], False)
        self.assertEqual(arguments, original)
        self.assertEqual(other.command_executor.client_config.timeout, 120)
        self.assertNotIn("retries", other.command_executor._conn.connection_pool_kw)

    def test_invalid_or_unbounded_timeout_is_rejected(self):
        for value in (None, True, 0, -1, float("inf"), float("nan"), 121, "3"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                configure_transport(FakeDriver(), value)

    def test_connection_setup_error_does_not_leak_raw_exception(self):
        with self.assertRaises(BrowserShutdownError) as raised:
            configure_transport(object(), 3)
        self.assertNotIn("AttributeError", str(raised.exception))


class OwnershipTests(unittest.TestCase):
    def test_track_opens_once_and_never_touches_other_driver(self):
        handle = FakeHandle()
        driver, other = FakeDriver(), FakeDriver()
        with patch("gift_registry.driver_shutdown._open_owned_process", return_value=handle) as opened:
            track_owned_browser(driver)
            track_owned_browser(driver)
            opened.assert_called_once_with(123)
        self.assertFalse(hasattr(other, "_gift_owned_browser_state"))
        self.assertEqual(handle.terminations, 0)

    def test_missing_or_invalid_pid_does_not_create_ownership(self):
        for value in (None, "123", True, 0, -1):
            driver = FakeDriver()
            driver.capabilities["goog:processID"] = value
            with self.subTest(value=value), patch("gift_registry.driver_shutdown._open_owned_process") as opened:
                track_owned_browser(driver)
                opened.assert_not_called()
            self.assertIsNone(driver._gift_owned_browser_state.handle)

    def test_unavailable_handle_is_not_fabricated(self):
        driver = FakeDriver()
        with patch("gift_registry.driver_shutdown._open_owned_process", side_effect=OSError("private")):
            track_owned_browser(driver)
        self.assertIsNone(driver._gift_owned_browser_state.handle)

    def test_handle_garbage_collection_only_releases_handle_never_stops_browser(self):
        kernel = SimpleNamespace(CloseHandle=Mock(return_value=True), TerminateProcess=Mock())
        handle = object.__new__(_WindowsProcessHandle)
        handle._kernel, handle._handle = kernel, 123
        del handle
        gc.collect()
        kernel.CloseHandle.assert_called_once_with(123)
        kernel.TerminateProcess.assert_not_called()


class ShutdownTests(unittest.TestCase):
    def track(self, driver, handle):
        with patch("gift_registry.driver_shutdown._open_owned_process", return_value=handle):
            track_owned_browser(driver)

    def test_normal_quit_proves_browser_exit_and_is_idempotent(self):
        handle = FakeHandle()
        driver = FakeDriver(on_quit=lambda: setattr(handle, "exited", True))
        self.track(driver, handle)
        self.assertEqual(quit_owned_driver(driver), {"closed": True, "forced": False})
        self.assertEqual(driver.commands, [Command.QUIT])
        self.assertEqual(handle.terminations, 0)
        self.assertEqual(handle.closes, 1)
        self.assertIsNone(driver.session_id)
        self.assertEqual(quit_owned_driver(driver), {"closed": True, "forced": False})
        self.assertEqual(driver.commands, [Command.QUIT])

    def test_transport_timeout_falls_back_only_to_saved_owned_handle(self):
        handle, other = FakeHandle(), FakeHandle()
        driver = FakeDriver(error=True)
        self.track(driver, handle)
        self.assertEqual(quit_owned_driver(driver), {"closed": True, "forced": True})
        self.assertEqual(handle.terminations, 1)
        self.assertEqual(handle.closes, 1)
        self.assertEqual(other.terminations, 0)
        self.assertTrue(all(wait <= 2 for wait in handle.waits))
        self.assertEqual(driver.command_executor.client_config.timeout, 3)
        self.assertEqual(driver.command_executor.closes, 1)

    def test_normal_quit_without_handle_can_confirm_close(self):
        driver = FakeDriver()
        self.assertEqual(quit_owned_driver(driver), {"closed": True, "forced": False})
        self.assertEqual(driver.commands, [Command.QUIT])

    def test_quit_failure_without_saved_handle_never_opens_pid_or_claims_success(self):
        driver = FakeDriver(error=True)
        with patch("gift_registry.driver_shutdown._open_owned_process") as opened:
            with self.assertRaises(BrowserShutdownError) as raised:
                quit_owned_driver(driver)
            opened.assert_not_called()
        self.assertNotIn("private-secret", str(raised.exception))
        self.assertIsNotNone(driver.session_id)
        self.assertEqual(driver.command_executor.closes, 0)
        self.assertEqual(driver.service.process.terminations, 0)

    def test_failed_quit_without_handle_keeps_service_for_explicit_retry(self):
        driver = FakeDriver(error=True)
        with self.assertRaises(BrowserShutdownError):
            quit_owned_driver(driver)
        self.assertEqual(driver.service.process.terminations, 0)
        self.assertEqual(driver.service.process.kills, 0)
        self.assertEqual(driver.command_executor.closes, 0)
        driver.error = False
        self.assertEqual(quit_owned_driver(driver), {"closed": True, "forced": False})
        self.assertEqual(driver.commands, [Command.QUIT, Command.QUIT])
        self.assertEqual(driver.service.process.terminations, 1)
        self.assertEqual(driver.command_executor.closes, 1)

    def test_unconfirmed_owned_process_retains_handle_and_can_be_retried(self):
        handle = FakeHandle(refuse=True)
        driver = FakeDriver(error=True)
        self.track(driver, handle)
        with self.assertRaises(BrowserShutdownError):
            quit_owned_driver(driver)
        self.assertIs(driver._gift_owned_browser_state.handle, handle)
        self.assertEqual(handle.closes, 0)
        handle.exited = True
        self.assertEqual(quit_owned_driver(driver), {"closed": True, "forced": True})
        self.assertEqual(handle.closes, 1)

    def test_already_exited_owned_chrome_does_not_send_late_session_command(self):
        driver = FakeDriver(error=True)
        handle = FakeHandle(exited=True)
        self.track(driver, handle)
        self.assertEqual(quit_owned_driver(driver), {"closed": True, "forced": False})
        self.assertEqual(driver.commands, [])

    def test_hung_owned_service_uses_bounded_wait_and_kill(self):
        process = FakeProcess(hung=True)
        driver = FakeDriver(process=process)
        self.assertEqual(quit_owned_driver(driver), {"closed": True, "forced": False})
        self.assertEqual((process.terminations, process.kills), (1, 1))
        self.assertEqual(process.waits, [2, 2])

    def test_service_failure_preserves_proven_chrome_exit_for_next_cleanup(self):
        process = FakeProcess(hung=True, refuse_kill=True)
        driver = FakeDriver(process=process)
        with self.assertRaises(BrowserShutdownError):
            quit_owned_driver(driver)
        self.assertTrue(driver._gift_owned_browser_state.chrome_closed)
        self.assertEqual(driver.commands, [Command.QUIT])
        process.code = 0
        self.assertEqual(quit_owned_driver(driver), {"closed": True, "forced": False})
        self.assertEqual(driver.commands, [Command.QUIT])

    def test_handle_cleanup_failure_retains_object_until_retry(self):
        class FlakyHandle(FakeHandle):
            def close(self):
                self.closes += 1
                if self.closes == 1:
                    raise OSError("private-handle-error")

        handle = FlakyHandle(exited=True)
        driver = FakeDriver()
        self.track(driver, handle)
        with self.assertRaises(BrowserShutdownError) as raised:
            quit_owned_driver(driver)
        self.assertNotIn("private-handle", str(raised.exception))
        self.assertIs(driver._gift_owned_browser_state.handle, handle)
        self.assertEqual(quit_owned_driver(driver), {"closed": True, "forced": False})
        self.assertEqual(handle.closes, 2)


if __name__ == "__main__":
    unittest.main()
