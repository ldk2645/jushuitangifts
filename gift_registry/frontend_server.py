"""默认本机、可选受限局域网的工作台；与 Selenium 业务流程分离。"""

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

from .access_log import AccessLog, request_path
from .config import DATA_DIR, ROOT
from .gift_catalog import GiftCatalogConflict, GiftCatalogStore
from .sessions import atomic_write
from .registration import RegistrationQueue, RegistrationStartError
from .network import NetworkSettings, authorized, valid_access_password

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
        monitor = getattr(self, 'network_monitor', None)
        if monitor is not None:
            monitor.close()
            self.network_monitor = None
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


def make_server(port=8765, *, store=None, queue=None, catalog=None, network=None, access_password=None,
                network_monitor=None, access_log=None):
    network = network if network is not None else NetworkSettings()
    if network.enabled and not valid_access_password(access_password):
        raise ValueError('局域网模式必须先配置独立访问密码')
    row_store = store or RowStore(DATA_DIR / "registrations.json")
    catalog_store = catalog if catalog is not None else GiftCatalogStore()
    catalog_store.read()
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(20)

        def log_message(self, *_):
            pass

        def log_request(self, code="-", size="-"):
            # 成功的状态轮询每 1.5~10 秒一次，写入会刷掉真正有用的 401/403。
            if access_log is None:
                return
            if code == 200 and request_path(self.path) in ("/api/registrations/status", "/api/network/status"):
                return
            access_log.write(code, self.command, self.path, self.headers.get("Host"), self.client_address[0])

        def send(self, code, content, content_type="application/json; charset=utf-8", *, challenge=False):
            if isinstance(content, dict):
                content = json.dumps(content, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            if challenge:
                self.send_header('WWW-Authenticate', 'Basic realm="Gift Registry LAN", charset="UTF-8"')
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(content)

        def allowed_host(self):
            values = self.headers.get_all('Host', [])
            return len(values) == 1 and network.allows_host(values[0], self.server.server_port)

        def permitted(self, *, authenticate=True):
            if not self.allowed_host() or not network.allows_client(self.client_address[0]):
                self.send(403, {'error': '访问地址或客户端不在允许的局域网范围内'})
                return False
            if (authenticate and network.enabled and not self.is_local_access()
                    and not authorized(self.headers.get('Authorization'), access_password)):
                self.send(401, {'error': '请输入工作台的独立访问账号和密码'}, challenge=True)
                return False
            return True

        def is_local_client(self):
            # 不信任代理转发头；局域网客户端不能远程弹出主机登录助手。
            return self.client_address[0] == '127.0.0.1'

        def is_local_access(self):
            # 同时要求真实连接来自回环地址、访问网址为本机；转发头不能豁免认证。
            if not self.is_local_client():
                return False
            port = self.server.server_port
            local_hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
            if port == 80:
                local_hosts.update(('127.0.0.1', 'localhost'))
            return self.headers.get('Host', '').lower() in local_hosts

        def do_GET(self):
            if not self.permitted(authenticate=False):
                return
            path = urlsplit(self.path).path
            if path == '/api/health':
                self.send(200, {'app': 'gift_registry', 'lan_enabled': network.enabled,
                                'domain': network.domain, 'lan_ip': network.lan_ip})
                return
            if not self.permitted():
                return
            if path == "/api/config":
                self.send(200, {"app": "gift_registry", "frontend_version": 4, "token": token, "gifts": catalog_store.read(),
                                "workflow_configured": jobs.configured, "automatic_registration_retry": False,
                                "workflow_configuration_detail": getattr(jobs.operations, "configuration_detail", ""),
                                'network_settings_available': network.enabled and self.is_local_access() and network_monitor is not None,
                                'platform_login_available': self.is_local_client()})
            elif path == '/api/network/status':
                if not self.is_local_access() or network_monitor is None:
                    self.send(403, {'error': '网络提醒仅在主机本地工作台查看'})
                    return
                self.send(200, network_monitor.snapshot())
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
            if not self.permitted():
                return
            origins = self.headers.get_all('Origin', [])
            if (len(origins) > 1 or (origins and origins[0] != 'http://' + self.headers.get('Host', ''))
                    or self.headers.get('Sec-Fetch-Site') == 'cross-site'
                    or not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), token)):
                self.send(403, {"error": "请求校验失败，请刷新页面"})
                return
            path = urlsplit(self.path).path
            if self.command == "POST" and path == "/api/login":
                if not self.is_local_client():
                    self.send(403, {'error': '平台登录和验证码请在主机上维护；其他设备可填写草稿及开始登记'})
                    return
                pythonw = Path(sys.executable).with_name("pythonw.exe")
                subprocess.Popen([str(pythonw if pythonw.exists() else sys.executable), str(ROOT / "main.py"), "--login"],
                                 cwd=ROOT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.send(200, {"ok": True})
                return
            starting = self.command == "POST" and path == "/api/registrations/start"
            adding_gift = self.command == "POST" and path == "/api/gifts"
            acknowledging_network = self.command == 'POST' and path == '/api/network/ack'
            if acknowledging_network and (not self.is_local_access() or network_monitor is None):
                self.send(403, {'error': '网络提醒只能在主机本地核对'})
                return
            if not starting and not adding_gift and not acknowledging_network and (self.command != "PUT" or path != "/api/rows"):
                self.send(404, {"error": "接口不存在"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 5_000_000:
                    raise ValueError("保存数据过大，请减少行数")
                document = json.loads(self.rfile.read(length))
                if not isinstance(document, dict):
                    raise ValueError("请求数据格式不正确")
                if acknowledging_network:
                    self.send(200, network_monitor.acknowledge(document.get('change_id')))
                elif adding_gift:
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

    server = LocalHTTPServer((network.bind_host, port), Handler)
    server.network_monitor = network_monitor
    try:
        # 必须先取得端口独占，再恢复任务；重复启动不能修改运行中任务的状态。
        jobs = queue if queue is not None else RegistrationQueue(DATA_DIR / "registration_jobs")
        server.registration_queue = jobs
    except Exception:
        server.server_close()
        raise
    server.daemon_threads = True
    return server


def run(port=8765, open_browser=True, *, network=None, access_password=None):
    network = network if network is not None else NetworkSettings()
    url = f"http://127.0.0.1:{port}/"
    from .network_monitor import NetworkMonitor
    monitor = NetworkMonitor(network) if network.enabled else None
    # 启动行让人能确认正在运行的进程加载了哪份共享配置；不含访问密码。
    access_log = AccessLog(DATA_DIR / "frontend_access.log")
    access_log.note(f"port={port} lan={'on' if network.enabled else 'off'} ip={network.lan_ip or '-'} "
                    f"subnet={network.subnet or '-'} domain={network.domain or '-'}")
    try:
        server = make_server(port, network=network, access_password=access_password,
                             network_monitor=monitor, access_log=access_log)
    except OSError:
        # 重复启动时复用已运行的同一项目，而非打开更多服务进程。
        try:
            with urlopen(url + "api/health", timeout=2) as response:
                health = json.load(response)
                if health.get("app") != "gift_registry":
                    raise RuntimeError(f"端口 {port} 已被其他程序占用，请使用 --port 指定其他端口")
                if (health.get('lan_enabled') != network.enabled or health.get('domain') != network.domain
                        or health.get('lan_ip') != network.lan_ip):
                    raise RuntimeError('工作台已在运行，但共享配置不同；请正常关闭旧工作台后重新启动')
        except (URLError, ValueError) as exc:
            raise RuntimeError(f"无法启动本地前端，端口 {port} 不可用") from exc
        if open_browser:
            webbrowser.open(url)
        return
    if sys.stdout:
        print(f"Local: {url}", flush=True)
        if network.enabled:
            print(f'LAN: http://{network.lan_ip}:{port}/  Domain: http://{network.domain}:{port}/', flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        if monitor is not None:
            monitor.start()
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
