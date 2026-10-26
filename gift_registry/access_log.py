"""最小访问日志：只记录状态码、方法、无查询参数的路径、Host 与客户端网段。

用于排查局域网共享时“请求到底是没到、被地址/子网拒绝，还是缺密码”。
不记录 Cookie、Authorization、请求体或访问密码；成功轮询不写入，避免刷掉
真正有用的 401/403。写日志失败绝不打断请求。
"""

import ipaddress
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_MAX_BYTES = 512 * 1024
MAX_PATH = 120
MAX_HOST = 80


def _timestamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _printable(value, limit, fallback="-"):
    """只保留可打印 ASCII，防止换行或控制字符伪造日志行。"""
    if not isinstance(value, str) or not value:
        return fallback
    text = "".join(char if 0x21 <= ord(char) <= 0x7E else "_" for char in value)
    return text[:limit] or fallback


def client_network(address):
    """把客户端地址收敛为网段，日志里不保存完整设备地址。"""
    if not isinstance(address, str):
        return "unknown"
    try:
        client = ipaddress.ip_address(address)
    except ValueError:
        return "unknown"
    if client.is_loopback:
        return "loopback"
    prefix = 24 if client.version == 4 else 64
    return str(ipaddress.ip_network(f"{client}/{prefix}", strict=False))


def request_path(target):
    """丢弃查询参数与片段，避免把令牌或筛选内容写进日志。"""
    try:
        path = urlsplit(target or "").path or "/"
    except ValueError:
        return "-"
    return _printable(path, MAX_PATH, "/")


class AccessLog:
    """追加写入本机日志；超过上限轮转为一份备份，写失败不影响请求。"""

    def __init__(self, path, *, max_bytes=DEFAULT_MAX_BYTES):
        self.path = Path(path)
        self.max_bytes = max(1024, int(max_bytes))
        self.lock = threading.Lock()

    def line(self, status, command, target, host, address):
        method = (command.upper() if isinstance(command, str) and command.isalpha()
                  and len(command) <= 10 else "-")
        return (f"{_timestamp()} {int(status)} {method} {request_path(target)} "
                f"{_printable(host, MAX_HOST)} {client_network(address)}\n")

    def write(self, status, command, target, host, address):
        try:
            self._append(self.line(status, command, target, host, address))
        except (OSError, ValueError, TypeError):
            pass

    def note(self, message):
        """记录一行启动信息，便于确认运行中的进程加载了哪份共享配置。"""
        try:
            text = "".join(char if 0x20 <= ord(char) <= 0x7E else "_" for char in str(message))[:240]
            self._append(f"{_timestamp()} START {text}\n")
        except (OSError, ValueError):
            pass

    def _append(self, line):
        payload = line.encode("utf-8")
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                size = self.path.stat().st_size
            except FileNotFoundError:
                size = 0
            if size and size + len(payload) > self.max_bytes:
                try:
                    self.path.replace(self.path.with_name(self.path.name + ".1"))
                except OSError:
                    # 备份文件被占用时不要丢日志；宁可暂时超过上限，下次再轮转。
                    pass
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line)
