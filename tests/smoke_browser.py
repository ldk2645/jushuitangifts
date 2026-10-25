"""真实 Chrome 验证：全新 Profile 中恢复会话 Cookie 和两种 Web Storage。"""

import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gift_registry.browser import SiteBrowser, create_driver
from gift_registry.config import DATA_DIR, Site
from gift_registry.sessions import SessionStore


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"<title>Local session test</title><p>Session test</p>")

    def log_message(self, *_):
        pass


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    DATA_DIR.mkdir(exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="smoke-", dir=DATA_DIR) as folder:
            root = Path(folder)
            site = Site("localtest", "本地测试", f"http://127.0.0.1:{server.server_port}/", ("127.0.0.1",))
            store = SessionStore(root / "sessions")
            first = SiteBrowser(site, store)
            second = SiteBrowser(site, store)
            try:
                first.driver = create_driver(site, headless=True, profile=root / "profile-first")
                first.navigate(site.url)
                first.driver.add_cookie({"name": "http_only_session", "value": "local-test-value", "httpOnly": True})
                first.driver.execute_script("localStorage.setItem('local_token','local-value'); sessionStorage.setItem('session_token','session-value');")
                first.capture()
                first.close()
                second.driver = create_driver(site, headless=True, profile=root / "profile-new")
                second.restore(store.load(site.key))
                second.navigate(site.url)
                assert second.driver.get_cookie("http_only_session")["value"] == "local-test-value"
                assert second.driver.get_cookie("http_only_session")["httpOnly"] is True
                assert second.driver.execute_script("return localStorage.getItem('local_token')") == "local-value"
                assert second.driver.execute_script("return sessionStorage.getItem('session_token')") == "session-value"
                # 页面后续修改存储，刷新不能再次写回旧值。
                second.driver.execute_script("sessionStorage.setItem('session_token','updated');")
                second.driver.refresh()
                assert second.driver.execute_script("return sessionStorage.getItem('session_token')") == "updated"
                print("PASS: session cookie + HttpOnly + localStorage + sessionStorage restored in a new Chrome profile.")
            finally:
                first.close()
                second.close()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
