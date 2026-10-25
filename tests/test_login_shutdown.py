"""登录助手关闭与进度的隔离测试；不用 Tk、浏览器、会话或真实 data。"""

import json
import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from urllib3.exceptions import ReadTimeoutError

from gift_registry.app import LoginApp, close_login_browsers


SITES = (SimpleNamespace(key="first", name="第一站"), SimpleNamespace(key="second", name="第二站"))


class FakeBrowser:
    def __init__(self, site, store=None):
        self.site = site
        self.driver = object()
        self.opens = 0
        self.saves = []
        self.closes = 0
        self.fail_open = False
        self.fail_save = False
        self.fail_close = False
        self.close_hook = None
        self.save_hook = None
        self.close_result = {"closed": True, "forced": False, "warning": ""}

    def open(self):
        self.opens += 1
        if self.fail_open:
            raise RuntimeError("private-login-token must not appear")

    def save(self, confirm=False):
        self.saves.append(confirm)
        if self.save_hook:
            self.save_hook()
        if self.fail_save:
            raise ReadTimeoutError(None, "private-login-token", "must not appear")
        return {"state": "测试会话已保存", "ready": True, "last_saved": "10:00:00", "cookie_count": 2}

    def close(self, *, progress=None):
        self.closes += 1
        if progress:
            progress("正在保存会话")
        if self.close_hook:
            self.close_hook()
        if self.fail_close:
            raise ReadTimeoutError(None, "private-login-token", "must not appear")
        if progress:
            progress("正在关闭浏览器")
        self.driver = None
        return self.close_result


def make_app():
    app = LoginApp.__new__(LoginApp)
    app.stopping = False
    app.shutdown_failed = False
    app.commands = queue.Queue()
    app.events = queue.Queue()
    return app


def collect(events):
    result = []
    while True:
        try:
            result.append(events.get_nowait())
        except queue.Empty:
            return result


def worker_args(browsers, writes):
    return {"browser_factory": lambda site, _: browsers[site.key], "store_factory": lambda _: None,
            "sites": SITES, "status_path": "unused-test-status", "status_writer": lambda _, data: writes.append(json.loads(data))}


class CloseLoginBrowsersTests(unittest.TestCase):
    def test_first_transport_error_does_not_skip_second_and_never_leaks_exception(self):
        first, second = (FakeBrowser(site) for site in SITES)
        first.fail_close = True
        events = []
        pending = close_login_browsers({"first": first, "second": second}, lambda key, value: events.append((key, value)))
        self.assertEqual(pending, {"first": first})
        self.assertEqual((first.closes, second.closes), (1, 1))
        self.assertIsNone(second.driver)
        self.assertIsNotNone(first.driver)
        self.assertNotIn("private-login-token", json.dumps(events))
        self.assertTrue(any(key == "first" and "关闭未完成" in state["state"] for key, state in events))

    def test_platforms_close_independently_in_parallel(self):
        entered = [threading.Event(), threading.Event()]
        release = threading.Event()
        browsers = {site.key: FakeBrowser(site) for site in SITES}
        for index, browser in enumerate(browsers.values()):
            def wait_for_release(index=index):
                entered[index].set()
                if not release.wait(5):
                    raise TimeoutError("test shutdown gate")
            browser.close_hook = wait_for_release
        results = []
        thread = threading.Thread(target=lambda: results.append(close_login_browsers(browsers, lambda *_: None)))
        thread.start()
        try:
            self.assertTrue(entered[0].wait(2))
            self.assertTrue(entered[1].wait(2))
            self.assertTrue(thread.is_alive())
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [{}])

    def test_missing_confirmation_is_not_treated_as_shutdown_success(self):
        browser = FakeBrowser(SITES[0])
        browser.close_result = {"closed": False}
        pending = close_login_browsers({"first": browser}, lambda *_: None)
        self.assertEqual(pending, {"first": browser})

    def test_progress_contains_both_phases_and_safe_warning(self):
        browser = FakeBrowser(SITES[0])
        browser.close_result = {"closed": True, "forced": True, "warning": "private-login-token"}
        events = []
        self.assertEqual(close_login_browsers({"first": browser}, lambda key, value: events.append(value)), {})
        self.assertTrue(any("正在保存会话" in value["state"] for value in events))
        self.assertTrue(any("正在关闭浏览器" in value["state"] for value in events))
        self.assertIn("之前的加密快照", events[-1]["warning"])
        self.assertNotIn("private-login-token", json.dumps(events))


class LoginWorkerShutdownTests(unittest.TestCase):
    def test_status_write_error_cannot_hide_stopped_event_or_skip_cleanup(self):
        app = make_app()
        app.commands.put(("stop", None))
        browsers = {site.key: FakeBrowser(site) for site in SITES}
        args = worker_args(browsers, [])
        args["status_writer"] = Mock(side_effect=OSError("private-login-token"))
        with patch("gift_registry.app.configure_transport") as configure:
            app.worker(**args)
        events = collect(app.events)
        self.assertEqual([key for key, _ in events].count("stopped"), 1)
        self.assertTrue(all(browser.closes == 1 for browser in browsers.values()))
        self.assertEqual(configure.call_count, 2)
        self.assertNotIn("private-login-token", json.dumps(events))

    def test_close_failure_keeps_worker_alive_until_explicit_retry(self):
        app = make_app()
        app.commands.put(("stop", None))
        browsers = {site.key: FakeBrowser(site) for site in SITES}
        browsers["first"].fail_close = True
        events = []
        with patch("gift_registry.app.configure_transport"):
            thread = threading.Thread(target=app.worker, kwargs=worker_args(browsers, []), daemon=True)
            thread.start()
            try:
                while True:
                    event = app.events.get(timeout=3)
                    events.append(event)
                    if event[0] == "shutdown_failed":
                        break
                self.assertTrue(thread.is_alive())
                self.assertFalse(any(key == "stopped" for key, _ in events))
                self.assertEqual(browsers["second"].closes, 1)
                browsers["first"].fail_close = False
            finally:
                browsers["first"].fail_close = False
                app.commands.put(("retry_close", None))
                thread.join(5)
        self.assertFalse(thread.is_alive())
        events.extend(collect(app.events))
        self.assertEqual([key for key, _ in events].count("stopped"), 1)
        self.assertEqual((browsers["first"].closes, browsers["second"].closes), (2, 1))

    def test_transport_save_error_preserves_worker_and_other_platform(self):
        app = make_app()
        app.commands.put(("stop", None))
        browsers = {site.key: FakeBrowser(site) for site in SITES}
        browsers["first"].fail_save = True
        with patch("gift_registry.app.configure_transport"):
            app.worker(**worker_args(browsers, []))
        events = collect(app.events)
        self.assertTrue(any(key == "stopped" for key, _ in events))
        self.assertEqual(browsers["second"].opens, 1)
        self.assertTrue(all(browser.closes == 1 for browser in browsers.values()))
        self.assertNotIn("private-login-token", json.dumps(events))

    def test_stop_during_save_prevents_next_platform_open_and_queued_confirmation(self):
        app = make_app()
        app.commands.put(("confirm", "first"))
        browsers = {site.key: FakeBrowser(site) for site in SITES}
        browsers["first"].save_hook = lambda: setattr(app, "stopping", True)
        with patch("gift_registry.app.configure_transport"):
            app.worker(**worker_args(browsers, []))
        self.assertEqual(browsers["first"].saves, [False])
        self.assertEqual(browsers["second"].opens, 0)
        self.assertEqual(browsers["first"].closes, 1)
        self.assertTrue(any(key == "stopped" for key, _ in collect(app.events)))

    def test_open_failure_still_cleans_own_driver_and_allows_other_site(self):
        app = make_app()
        app.commands.put(("stop", None))
        browsers = {site.key: FakeBrowser(site) for site in SITES}
        browsers["first"].fail_open = True
        with patch("gift_registry.app.configure_transport"):
            app.worker(**worker_args(browsers, []))
        self.assertEqual(browsers["second"].opens, 1)
        self.assertTrue(all(browser.closes == 1 for browser in browsers.values()))
        self.assertTrue(any(key == "stopped" for key, _ in collect(app.events)))

    def test_final_status_is_stopped_and_keeps_last_save_details(self):
        app = make_app()
        app.commands.put(("stop", None))
        browsers = {site.key: FakeBrowser(site) for site in SITES}
        writes = []
        with patch("gift_registry.app.configure_transport"):
            app.worker(**worker_args(browsers, writes))
        self.assertFalse(writes[-1]["running"])
        self.assertFalse(writes[-1]["closing"])
        self.assertEqual(writes[-1]["sites"]["first"]["last_saved"], "10:00:00")
        self.assertEqual(writes[-1]["sites"]["second"]["cookie_count"], 2)


class LoginShutdownUITests(unittest.TestCase):
    def setup_ui(self):
        app = make_app()
        app.root = Mock()
        app.exit_button = Mock()
        app.rows = {"first": (Mock(), Mock(), Mock())}
        return app

    def test_stop_disables_confirmation_and_only_user_can_retry_failed_close(self):
        app = self.setup_ui()
        app.stop()
        app.stop()
        self.assertEqual(collect(app.commands), [("stop", None)])
        self.assertTrue(app.stopping)
        app.rows["first"][2].configure.assert_called_with(state="disabled")
        app.shutdown_failed = True
        app.stop()
        self.assertEqual(collect(app.commands), [("retry_close", None)])
        self.assertFalse(app.shutdown_failed)

    def test_failed_event_does_not_destroy_window_and_stopped_event_does(self):
        app = self.setup_ui()
        app.stopping = True
        app.events.put(("shutdown_failed", {}))
        app.receive()
        app.root.destroy.assert_not_called()
        app.exit_button.configure.assert_called_with(state="normal", text="重试关闭浏览器")
        app.events.put(("stopped", {}))
        app.receive()
        app.root.destroy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
