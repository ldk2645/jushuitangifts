"""默认本机、可选受限局域网的工作台；与 Selenium 业务流程分离。"""

import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import webbrowser
from socketserver import TCPServer
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit, parse_qs, urlencode
from urllib.request import ProxyHandler, build_opener

from .access_log import AccessLog, request_path
from .config import DATA_DIR, ROOT
from .gift_catalog import GiftCatalogConflict, GiftCatalogStore
from .gift_groups import GiftGroupStore, GroupConflict
from .maintenance import MaintenanceService
from .sessions import atomic_write
from .registration import RegistrationQueue, RegistrationStartError
from .network import NetworkSettings, authorized, valid_access_password
from .selection import SelectionPolicy, SelectionConflict, catalog_version, migrate_legacy_selections

WEB_DIR = ROOT / "frontend"
FIELDS = {"order_no": 200, "gift": 500}
MAX_ROWS = 10000


class RevisionConflict(ValueError):
    pass


class LocalHTTPServer(ThreadingHTTPServer):
    # Windows 的 SO_REUSEADDR 会允许重复进程争用端口，导致请求落到旧版服务。
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # HTTPServer.server_bind 会对绑定地址做反向 DNS；局域网离线或 DNS 慢时
        # 仅为未使用的 server_name 就阻塞整个启动。直接绑定数字地址即可。
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def request_shutdown(self):
        """排空已接收任务期间继续提供只读页面，不能先关端口造成假死。"""
        with self.shutdown_lock:
            if self.stopping:
                return
            self.stopping = True
            self.registration_queue.stop_accepting()

        def finish():
            self.maintenance.close()
            self.registration_queue.pending.join()
            self.shutdown()

        threading.Thread(target=finish, name='shutdown-request', daemon=True).start()

    def server_close(self):
        monitor = getattr(self, 'network_monitor', None)
        if monitor is not None:
            monitor.close()
            self.network_monitor = None
        jobs = getattr(self, "registration_queue", None)
        if jobs is not None:
            # 先冻结还未进入队列的 HTTP 请求，避免 join 返回后又出现新任务。
            jobs.stop_accepting()
        maintenance = getattr(self, "maintenance", None)
        if maintenance is not None:
            maintenance.close()
            self.maintenance = None
        try:
            super().server_close()
        finally:
            if jobs is not None:
                try:
                    jobs.pending.join()
                    if jobs.dual_verification:
                        jobs.verification.join()
                    if maintenance is not None and maintenance.task_exports is not None:
                        try:
                            maintenance.task_exports.tick()
                        except (OSError, ValueError) as exc:
                            from .diagnostics import diagnose
                            diagnose(jobs.directory, 'task_export', 'shutdown_reconcile', exc)
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
        if type(row.get("change_address", False)) is not bool:
            raise ValueError("是否改地址必须为是或否")
        record = {"id": identifier, "change_address": row.get("change_address", False)}
        gift_ids = row.get("gift_ids", [])
        if not isinstance(gift_ids, list) or len(gift_ids) > 100 or any(not isinstance(item, str) or len(item) > 80 for item in gift_ids):
            raise ValueError("礼品选项格式不正确")
        record["gift_ids"] = list(dict.fromkeys(gift_ids))
        unmatched = row.get('gift_unmatched', [])
        if not isinstance(unmatched, list) or len(unmatched) > 100 or any(not isinstance(v, str) or len(v) > 500 for v in unmatched):
            raise ValueError('未匹配礼品格式不正确')
        if 'gift_unmatched' in row:
            record['gift_unmatched'] = unmatched
        for key in ('selection_scope', 'gift_selection'):
            if key in row:
                if not isinstance(row[key], dict) or len(json.dumps(row[key])) > 50000:
                    raise ValueError('礼品选择依据格式不正确')
                record[key] = row[key]
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
        self.lock = threading.RLock()
        self.selection_policy = None
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
            if self.selection_policy is not None:
                records = self.selection_policy.validate(records, self.document['rows'])
            document = {"revision": revision + 1, "rows": records}
            atomic_write(self.path, json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8"))
            self.document = document
            return {"revision": document["revision"]}


def make_server(port=8765, *, store=None, queue=None, catalog=None, network=None, access_password=None,
                network_monitor=None, access_log=None, groups=None):
    network = network if network is not None else NetworkSettings()
    if network.enabled and not valid_access_password(access_password):
        raise ValueError('局域网模式必须先配置独立访问密码')
    row_store = store or RowStore(DATA_DIR / "registrations.json")
    catalog_store = catalog if catalog is not None else GiftCatalogStore()
    catalog_store.read()
    group_store = groups if groups is not None else GiftGroupStore(catalog_store.path.with_name("gift_groups.local.json"), catalog_store)
    row_store.selection_policy = SelectionPolicy(catalog_store, group_store)
    token = secrets.token_urlsafe(32)

    def sheet_snapshot():
        with row_store.lock, jobs.lock:
            document = row_store.read()
            identifiers = {row['id'] for row in document['rows']}
            states = management.states(identifiers) if identifiers else {}
            return {**document, 'row_states': {key: value for key, value in states.items() if key in identifiers}}


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

        def send(self, code, content, content_type="application/json; charset=utf-8", *, challenge=False, filename=None):
            if isinstance(content, dict):
                content = json.dumps(content, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            if challenge:
                self.send_header('WWW-Authenticate', 'Basic realm="Gift Registry LAN", charset="UTF-8"')
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            try:
                self.end_headers()
                self.wfile.write(content)
            except OSError as exc:
                # The mutation may already be committed. A lost response is not
                # a storage failure and must not produce a second 500/retry reply.
                self.diagnose_http('response_write', exc)
                self.close_connection = True

        def diagnose_http(self, phase, exc):
            from .diagnostics import diagnose
            endpoint = request_path(self.path)
            if endpoint.startswith('/api/registrations/batches/'):
                endpoint = '/api/registrations/batches/:id'
            diagnose(jobs.directory, 'http', phase + ':' + endpoint, exc)

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
            try:
                self.read_request()
            except (ValueError, OSError) as exc:
                self.diagnose_http('read', exc)
                self.send(503, {'error': '配置暂不可用，请主机检查目录维护事务或文件；现有任务历史保留'})

        def read_request(self):
            if not self.permitted(authenticate=False):
                return
            path = urlsplit(self.path).path
            if path == '/api/health':
                self.send(200, {'app': 'gift_registry', 'lan_enabled': network.enabled,
                                'domain': network.domain, 'lan_ip': network.lan_ip, 'subnet': network.subnet,
                                'stopping': self.server.stopping})
                return
            if not self.permitted():
                return
            if path == "/api/config":
                from . import __version__
                gifts, group_config, generation = row_store.selection_policy.context()
                self.send(200, {"app": "gift_registry", "frontend_version": 12, "version": __version__, "token": token, "gifts": gifts,
                                "catalog_version": generation,
                                "gift_groups": group_config,
                                "host_actions_available": self.is_local_access(),
                                "workflow_configured": jobs.configured, "automatic_registration_retry": False,
                                "workflow_configuration_detail": getattr(jobs.operations, "configuration_detail", ""),
                                'network_settings_available': network.enabled and self.is_local_access() and network_monitor is not None,
                                'platform_login_available': self.is_local_access()})
            elif path == '/api/network/status':
                if not self.is_local_access() or network_monitor is None:
                    self.send(403, {'error': '网络提醒仅在主机本地工作台查看'})
                    return
                self.send(200, network_monitor.snapshot())
            elif path == "/api/gifts":
                gifts, group_config, generation = row_store.selection_policy.context()
                self.send(200, {"gifts": gifts, "catalog_version": generation, "gift_groups": group_config})
            elif path == "/api/gift-groups":
                self.send(200, group_store.read())
            elif path == "/api/maintenance/status":
                if not self.is_local_access():
                    self.send(403, {"error": "后台归档详情仅允许主机查看"})
                    return
                self.send(200, {**self.server.maintenance.status(), 'queue': jobs.health()})
            elif path == "/api/rows":
                self.send(200, sheet_snapshot())
            elif path.startswith('/api/registrations/batches/'):
                if not self.is_local_access():
                    self.send(403, {'error': '批次详情仅允许主机查看'})
                    return
                self.send(200, jobs.retry_batch_status(path.rsplit('/', 1)[-1]))
            elif path == "/api/registrations/status":
                snapshot = sheet_snapshot()
                self.send(200, {"row_states": snapshot['row_states'], "workflow_configured": jobs.configured,
                                "draft_revision": snapshot['revision'], "server_stopping": self.server.stopping})
            elif path in ('/api/registrations/task-logs','/api/registrations/task-logs/download'):
                if not self.is_local_access():
                    self.send(403, {'error':'任务日志仅允许主机本地访问'}); return
                service=self.server.maintenance.task_exports
                if path.endswith('/download'):
                    query=parse_qs(urlsplit(self.path).query)
                    day=query.get('day',[''])[0]; service.manifest_path(day)
                    batch=query.get('batch_id',[''])[0]
                    import hashlib
                    suffix=('-batch-'+hashlib.sha256(batch.encode()).hexdigest()[:12] if batch else '')
                    suffix+='-partial' if query.get('partial',['0'])[0]=='1' else ''
                    name=f'gift-registration-{day}{suffix}.xlsx'
                    if not batch:
                        inventory=next((d for d in service.inventory()['days'] if d['business_day']==day),None)
                        if inventory is None or inventory['export_state'] not in {'final','partial'}:
                            self.send(409,{'error':'任务记录已变化，请重新生成报告'}); return
                        manifest=service.read_manifest(day)
                        if manifest.get('filename')!=name:
                            self.send(409,{'error':'报告版本已更新，请重新生成'}); return
                    payload=(service.exports/name).read_bytes()
                    if not batch and hashlib.sha256(payload).hexdigest()!=manifest.get('sha256'):
                        self.send(409,{'error':'报告校验失败，请重新生成'}); return
                    self.send(200,payload,'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',filename=name)
                else:
                    self.send(200,service.inventory())
            elif path in ("/api/registrations/failures", "/api/registrations/export-today"):
                if not self.is_local_access():
                    self.send(403, {"error": "失败重登与日志导出仅允许主机本地访问"})
                    return
                if path.endswith("/failures"):
                    self.send(200, management.retry_candidates())
                else:
                    from .daily_export import export_daily
                    day, records = jobs.daily_records()
                    self.send(200, export_daily(day, records),
                              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                              filename=f"gift-registration-{day}.xlsx")
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
                    or self.headers.get('Sec-Fetch-Site') == 'cross-site'):
                self.send(403, {"error": "请求校验失败，请刷新页面"})
                return
            if not secrets.compare_digest(self.headers.get("X-CSRF-Token", "").encode('utf-8'), token.encode('ascii')):
                # 在任何写入之前拒绝；前端可只读取得新凭据，但不能自动重放此请求。
                self.send(403, {"error": "页面连接凭据已失效，本次操作未执行", "code": "csrf_expired"})
                return
            path = urlsplit(self.path).path
            if self.command == "POST" and path == "/api/shutdown":
                # 只允许主机回环访问，且必须带当前页面的 CSRF Token。
                if not self.is_local_access():
                    self.send(403, {"error": "结束工作台仅允许主机本地操作"})
                    return
                self.server.request_shutdown()
                self.send(200, {"stopping": True})
                return
            cancelling = self.command == "POST" and path == "/api/registrations/cancel"
            if self.server.stopping and not cancelling:
                self.send(409, {"error": "工作台正在关闭，正在完成已接收任务；当前仅可查看，请等待完成后重新打开",
                                "code": "server_stopping"})
                return
            if self.command == "POST" and path == "/api/login":
                if not self.is_local_access():
                    self.send(403, {'error': '平台登录和验证码请在主机上维护；其他设备可填写草稿及开始登记'})
                    return
                pythonw = Path(sys.executable).with_name("pythonw.exe")
                subprocess.Popen([str(pythonw if pythonw.exists() else sys.executable), str(ROOT / "main.py"), "--login"],
                                 cwd=ROOT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.send(200, {"ok": True})
                return
            starting = self.command == "POST" and path == "/api/registrations/start"
            retrying = self.command == "POST" and path == "/api/registrations/retry"
            retrying_all = self.command == "POST" and path == "/api/registrations/retry-all"
            exporting = self.command == 'POST' and path == '/api/registrations/task-logs/export'
            managing = self.command == "POST" and path in {"/api/rows/status", "/api/rows/delete"}
            if (retrying or retrying_all or managing or cancelling or exporting) and not self.is_local_access():
                self.send(403, {"error": "主机管理仅允许本机回环地址操作"})
                return
            adding_gift = self.command == "POST" and path == "/api/gifts"
            updating_groups = self.command == "PUT" and path == "/api/gift-groups"
            if updating_groups and not self.is_local_access():
                self.send(403, {"error": "礼品分组只能在主机上管理"})
                return
            acknowledging_network = self.command == 'POST' and path == '/api/network/ack'
            if acknowledging_network and (not self.is_local_access() or network_monitor is None):
                self.send(403, {'error': '网络提醒只能在主机本地核对'})
                return
            if not any((starting, retrying, retrying_all, managing, cancelling, exporting, adding_gift, updating_groups, acknowledging_network)) and (self.command != "PUT" or path != "/api/rows"):
                self.send(404, {"error": "接口不存在"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 5_000_000:
                    raise ValueError("保存数据过大，请减少行数")
                document = json.loads(self.rfile.read(length))
                if not isinstance(document, dict):
                    raise ValueError("请求数据格式不正确")
                if exporting:
                    if type(document.get('partial',False)) is not bool or not isinstance(document.get('batch_id',''),str):
                        raise ValueError('导出范围格式不正确')
                    day=document.get('day',''); batch=document.get('batch_id','')
                    file=self.server.maintenance.task_exports.publish(day,partial=document.get('partial',False),batch_id=batch)
                    if file is None:
                        self.send(409,{'error':'仍有未完成订单，可选择导出 partial 非最终版'}); return
                    self.send(200,{'download_url':'/api/registrations/task-logs/download?'+urlencode(
                        dict(day=day,batch_id=batch,partial=int('-partial' in file.stem))), 'filename':file.name})
                elif acknowledging_network:
                    self.send(200, network_monitor.acknowledge(document.get('change_id')))
                elif adding_gift:
                    result = catalog_store.add(document)
                    result['catalog_version'] = catalog_version(result['gifts'])
                    self.send(201, result)
                elif updating_groups:
                    self.send(200, group_store.update(document))
                elif managing or cancelling:
                    with row_store.lock, jobs.lock:
                        if type(document.get("revision")) is not int or document["revision"] != row_store.document["revision"]:
                            raise RevisionConflict("草稿已更新，请刷新后重新进行主机管理操作")
                        if cancelling:
                            result = management.cancel(document.get("targets"))
                        elif path.endswith("/status"):
                            from .host_management import StatusConflict
                            if 'targets' not in document or document.get('draft_revision') != document.get('revision'):
                                raise RevisionConflict('页面缺少逐单执行版本，请刷新工作台后重试')
                            try:
                                result = management.mark(document.get("row_ids"), document.get("status"),
                                                         targets=document['targets'], draft_revision=document['draft_revision'])
                            except StatusConflict as exc:
                                raise RevisionConflict(str(exc)) from None
                        else:
                            result = management.delete(document.get("row_ids"), document.get("all", False))
                    self.send(200, result)
                elif retrying_all:
                    self.send(200, management.retry(document.get("failure_ids"), batch=True))
                elif retrying:
                    self.send(200, management.retry([document.get("failure_id")]))
                elif starting:
                    with row_store.lock:
                        current = row_store.document
                        if type(document.get("revision")) is not int or document["revision"] != current["revision"]:
                            raise RevisionConflict("草稿已更新，请刷新页面后重新开始登记")
                        result = jobs.start(current["rows"], catalog_store.read())
                        result["row_states"] = management.states({row['id'] for row in current['rows']})
                    self.send(200, result)
                else:
                    self.send(200, row_store.write(document.get("revision"), document.get("rows")))
            except GiftCatalogConflict as exc:
                self.send(409, {"error": str(exc), "code": "gift_catalog_conflict"})
            except SelectionConflict as exc:
                self.send(409, {"error": str(exc), "code": "selection_conflict"})
            except GroupConflict as exc:
                self.send(409, {"error": str(exc), "code": "gift_group_conflict"})
            except RegistrationStartError as exc:
                self.send(409, {"error": str(exc), "code": "registration_not_started"})
            except RevisionConflict as exc:
                self.send(409, {"error": str(exc)})
            except (ValueError, UnicodeDecodeError) as exc:
                self.send(400, {"error": str(exc)})
            except OSError as exc:
                self.diagnose_http('mutation', exc)
                self.send(500, {"error": "存档操作未完成或结果未确认，请保留页面并先查询状态"})

        do_PUT = mutate
        do_POST = mutate

    server = LocalHTTPServer((network.bind_host, port), Handler)
    server.stopping = False
    server.shutdown_lock = threading.Lock()
    server.network_monitor = network_monitor
    try:
        # 必须先取得端口独占，再恢复任务；重复启动不能修改运行中任务的状态。
        jobs = queue if queue is not None else RegistrationQueue(DATA_DIR / "registration_jobs")
        migrate_legacy_selections(row_store.document['rows'], catalog_store.read(), jobs)
        from .host_management import HostManagement
        management = HostManagement(row_store, jobs)
        server.registration_queue = jobs
        server.maintenance = MaintenanceService(jobs, row_store, row_store.path.parent)
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
    try:
        server = make_server(port, network=network, access_password=access_password,
                             network_monitor=monitor, access_log=access_log)
    except OSError:
        # 重复启动时复用已运行的同一项目，而非打开更多服务进程。
        try:
            with build_opener(ProxyHandler({})).open(url + "api/health", timeout=2) as response:
                health = json.load(response)
                if health.get("app") != "gift_registry":
                    raise RuntimeError(f"端口 {port} 已被其他程序占用，请使用 --port 指定其他端口")
                if not network.matches_health(health):
                    raise RuntimeError('工作台已在运行，但共享配置不同；请正常关闭旧工作台后重新启动')
        except (URLError, ValueError) as exc:
            raise RuntimeError(f"无法启动本地前端，端口 {port} 不可用") from exc
        if open_browser:
            webbrowser.open(url)
        return
    access_log.note(f"port={port} lan={'on' if network.enabled else 'off'} ip={network.lan_ip or '-'} "
                    f"subnet={network.subnet or '-'} domain={network.domain or '-'}")
    if sys.stdout:
        print(f"Local: {url}", flush=True)
        if network.enabled:
            print(f'LAN: http://{network.lan_ip}:{port}/' +
                  (f'  Domain: http://{network.domain}:{port}/' if network.domain else ''), flush=True)
    if open_browser:
        webbrowser.open(url)
    # 结束脚本用 PID 文件等待进程退出；进程崩溃时文件可能残留，脚本会校验。
    pid_path = DATA_DIR / "server.pid"
    try:
        if monitor is not None:
            monitor.start()
        server.maintenance.start()
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            pid_path.write_text(str(os.getpid()), encoding="ascii")
        except OSError:
            pass
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server.server_close()
        finally:
            try:
                pid_path.unlink()
            except OSError:
                pass
