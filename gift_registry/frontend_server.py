"""仅监听本机的表格前端；与 Selenium 业务流程分离。"""

import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

from .config import DATA_DIR, ROOT
from .gift_catalog import GiftCatalogConflict, GiftCatalogStore
from .sessions import atomic_write
from .registration import RegistrationQueue, RegistrationStartError

WEB_DIR = ROOT / "frontend"
FIELDS = {"order_no": 200, "gift": 500, "recipient_name": 100, "phone": 60, "address": 2000}
MAX_ROWS = 10000


class RevisionConflict(ValueError):
    pass


class LocalHTTPServer(ThreadingHTTPServer):
    # Windows 的 SO_REUSEADDR 会允许重复进程争用端口，导致请求落到旧版服务。
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def server_close(self):
        jobs = getattr(self, "registration_queue", None)
        if jobs is not None:
            # 先冻结还未进入队列的 HTTP 请求，避免 join 返回后又出现新任务。
            jobs.stop_accepting()
        try:
            super().server_close()
        finally:
            if jobs is not None:
                try:
                    jobs.pending.join()
                    # 浏览器和登录配置仍在关闭期间，不能让新进程取得任务目录。
                    close_operations = getattr(jobs.operations, "close", None)
                    if callable(close_operations):
                        close_operations()
                finally:
                    jobs.close()
                    self.registration_queue = None


def validate_rows(rows):
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise ValueError("表格最多支持 10000 行")
    result, identifiers = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("行数据格式不正确")
        identifier = row.get("id")
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 80 or identifier in identifiers:
            raise ValueError("行标识不正确或重复")
        identifiers.add(identifier)
        if type(row.get("change_address")) is not bool:
            raise ValueError("是否改地址必须为是或否")
        record = {"id": identifier, "change_address": row["change_address"]}
        gift_ids = row.get("gift_ids", [])
        if not isinstance(gift_ids, list) or len(gift_ids) > 100 or any(not isinstance(item, str) or len(item) > 80 for item in gift_ids):
            raise ValueError("礼品选项格式不正确")
        record["gift_ids"] = list(dict.fromkeys(gift_ids))
        for key, limit in FIELDS.items():
            value = row.get(key, "")
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError(f"{key} 字段格式或长度不正确")
            record[key] = value
        # 不接受客户端写入业务执行结果，结果仅由后端复查产生。
        result.append(record)
    return result


class RowStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.document = {"revision": 0, "rows": []}
        if self.path.exists():
            document = json.loads(self.path.read_text(encoding="utf-8"))
            if type(document.get("revision")) is not int:
                raise ValueError("表格存档版本不正确，请先备份并检查存档")
            self.document = {"revision": document["revision"], "rows": validate_rows(document["rows"])}

    def read(self):
        with self.lock:
            return {"revision": self.document["revision"], "rows": [dict(row) for row in self.document["rows"]]}

    def write(self, revision, rows):
        records = validate_rows(rows)
        with self.lock:
            if type(revision) is not int or revision != self.document["revision"]:
                raise RevisionConflict("表格已在其他窗口更新，请先复制当前编辑内容，再刷新页面")
            document = {"revision": revision + 1, "rows": records}
            atomic_write(self.path, json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8"))
            self.document = document
            return {"revision": document["revision"]}


def make_server(port=8765, *, store=None, queue=None, catalog=None):
    row_store = store or RowStore(DATA_DIR / "registrations.json")
    catalog_store = catalog if catalog is not None else GiftCatalogStore()
    catalog_store.read()
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, code, content, content_type="application/json; charset=utf-8"):
            if isinstance(content, dict):
                content = json.dumps(content, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(content)

        def allowed_host(self):
            return self.headers.get("Host") in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

        def do_GET(self):
            if not self.allowed_host():
                self.send(403, {"error": "仅允许本机访问"})
                return
            path = urlsplit(self.path).path
            if path == "/api/config":
                self.send(200, {"app": "gift_registry", "frontend_version": 4, "token": token, "gifts": catalog_store.read(),
                                "workflow_configured": jobs.configured, "automatic_registration_retry": False,
                                "workflow_configuration_detail": getattr(jobs.operations, "configuration_detail", "")})
            elif path == "/api/gifts":
                self.send(200, {"gifts": catalog_store.read()})
            elif path == "/api/rows":
                self.send(200, {**row_store.read(), "row_states": jobs.states()})
            elif path == "/api/registrations/status":
                self.send(200, {"row_states": jobs.states(), "workflow_configured": jobs.configured})
            elif path in ("/", "/app.js", "/styles.css", "/favicon.svg"):
                name, mime = {
                    "/": ("index.html", "text/html; charset=utf-8"),
                    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
                    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
                }[path]
                self.send(200, (WEB_DIR / name).read_bytes(), mime)
            else:
                self.send(404, {"error": "页面不存在"})

        def mutate(self):
            if not self.allowed_host() or not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), token):
                self.send(403, {"error": "请求校验失败，请刷新页面"})
                return
            path = urlsplit(self.path).path
            if self.command == "POST" and path == "/api/login":
                pythonw = Path(sys.executable).with_name("pythonw.exe")
                subprocess.Popen([str(pythonw if pythonw.exists() else sys.executable), str(ROOT / "main.py"), "--login"],
                                 cwd=ROOT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.send(200, {"ok": True})
                return
            starting = self.command == "POST" and path == "/api/registrations/start"
            adding_gift = self.command == "POST" and path == "/api/gifts"
            if not starting and not adding_gift and (self.command != "PUT" or path != "/api/rows"):
                self.send(404, {"error": "接口不存在"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 5_000_000:
                    raise ValueError("保存数据过大，请减少行数")
                document = json.loads(self.rfile.read(length))
                if not isinstance(document, dict):
                    raise ValueError("请求数据格式不正确")
                if adding_gift:
                    self.send(201, catalog_store.add(document))
                elif starting:
                    with row_store.lock:
                        current = row_store.document
                        if type(document.get("revision")) is not int or document["revision"] != current["revision"]:
                            raise RevisionConflict("草稿已更新，请刷新页面后重新开始登记")
                        self.send(200, jobs.start(current["rows"], catalog_store.read()))
                else:
                    self.send(200, row_store.write(document.get("revision"), document.get("rows")))
            except GiftCatalogConflict as exc:
                self.send(409, {"error": str(exc), "code": "gift_catalog_conflict"})
            except RegistrationStartError as exc:
                self.send(409, {"error": str(exc), "code": "registration_not_started"})
            except RevisionConflict as exc:
                self.send(409, {"error": str(exc)})
            except (ValueError, UnicodeDecodeError) as exc:
                self.send(400, {"error": str(exc)})
            except OSError:
                self.send(500, {"error": "写入存档失败，请保留页面并重试"})

        do_PUT = mutate
        do_POST = mutate

    server = LocalHTTPServer(("127.0.0.1", port), Handler)
    try:
        # 必须先取得端口独占，再恢复任务；重复启动不能修改运行中任务的状态。
        jobs = queue if queue is not None else RegistrationQueue(DATA_DIR / "registration_jobs")
        server.registration_queue = jobs
    except Exception:
        server.server_close()
        raise
    server.daemon_threads = True
    return server


def run(port=8765, open_browser=True):
    url = f"http://127.0.0.1:{port}/"
    try:
        server = make_server(port)
    except OSError:
        # 重复启动时复用已运行的同一项目，而非打开更多服务进程。
        try:
            with urlopen(url + "api/config", timeout=2) as response:
                if json.load(response).get("app") != "gift_registry":
                    raise RuntimeError(f"端口 {port} 已被其他程序占用，请使用 --port 指定其他端口")
        except (URLError, ValueError) as exc:
            raise RuntimeError(f"无法启动本地前端，端口 {port} 不可用") from exc
        if open_browser:
            webbrowser.open(url)
        return
    if sys.stdout:
        print(f"Local: {url}", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
