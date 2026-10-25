"""隔离 Chrome 关闭验收；仅本地 HTML、合成会话和临时浏览器配置。"""

import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from urllib3.exceptions import ReadTimeoutError

from gift_registry.app import close_login_browsers
from gift_registry.browser import SiteBrowser, create_driver
from gift_registry.config import ROOT, Site
from gift_registry.driver_shutdown import quit_owned_driver
from gift_registry.sessions import MAGIC, SessionStore


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        content = b"<!doctype html><html><title>Local shutdown fixture</title><body>Fixture</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)


def main():
    temporary_root = ROOT / ".tmp"
    temporary_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="login-close-test-", dir=temporary_root) as directory:
        folder = Path(directory)
        server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        server.daemon_threads = True
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        store = SessionStore(folder / "sessions")
        browsers = {}
        events = []
        event_guard = threading.Lock()

        def publish(key, state):
            with event_guard:
                events.append((key, dict(state)))

        try:
            for key, label in (("close-normal", "正常关闭验收"), ("close-error", "保存异常验收")):
                site = Site(key, label, origin + "/", ("127.0.0.1",))
                browser = SiteBrowser(site, store)
                browsers[key] = browser
                browser.driver = create_driver(site, headless=True, profile=folder / key)
                browser.driver.get(site.url)
                browser.driver.execute_script(
                    "localStorage.setItem('fixture', 'synthetic-only');"
                    "sessionStorage.setItem('close_marker', 'before-close');")
                assert browser.capture() is not None
                assert store.path(key).read_bytes().startswith(MAGIC)

            preserved_error_bytes = store.path("close-error").read_bytes()
            browsers["close-normal"].driver.execute_script(
                "sessionStorage.setItem('close_marker', 'saved-during-close');")

            def failing_capture(**_):
                raise ReadTimeoutError(None, None, "synthetic-read-timeout")

            browsers["close-error"].capture = failing_capture
            started = time.monotonic()
            pending = close_login_browsers(browsers, publish)
            elapsed = time.monotonic() - started

            assert not pending, "A locally owned browser was not confirmed closed"
            assert all(browser.driver is None for browser in browsers.values())
            assert elapsed < 20, f"Local shutdown exceeded its test allowance: {elapsed:.2f}s"
            normal = store.load("close-normal")
            previous = store.load("close-error")
            assert normal["origins"][origin]["session"]["close_marker"] == "saved-during-close"
            assert previous["origins"][origin]["session"]["close_marker"] == "before-close"
            assert store.path("close-error").read_bytes() == preserved_error_bytes
            assert any(key == "close-error" and "保留" in state.get("warning", "")
                       for key, state in events)
            for key in browsers:
                stages = [state["state"] for event_key, state in events if event_key == key]
                assert any("正在保存会话" in stage for stage in stages)
                assert any("正在关闭浏览器" in stage for stage in stages)
                assert "浏览器已关闭" in stages[-1]
            print(f"PASS: two isolated Chrome profiles closed in {elapsed:.2f}s; normal save updated; "
                  "simulated capture timeout preserved the prior encrypted snapshot; both browsers "
                  "confirmed closed; only local HTML and synthetic session data were used.")
        finally:
            try:
                for browser in browsers.values():
                    if browser.driver is not None:
                        quit_owned_driver(browser.driver)
                        browser.driver = None
            finally:
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=2)


if __name__ == "__main__":
    main()
