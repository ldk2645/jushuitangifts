"""可选的局域网访问配置；独立访问密码仅以 Windows DPAPI 密文保存。"""

import base64
import ipaddress
import json
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from .config import DATA_DIR, ROOT
from .sessions import _crypt, atomic_write

NETWORK_PATH = ROOT / 'config' / 'network.local.json'
ACCESS_PATH = DATA_DIR / 'lan_access.dpapi'
ACCESS_USERNAME = 'gift'
_MAGIC = b'GIFT-LAN-ACCESS-1\n'
_PRIVATE = tuple(ipaddress.ip_network(value) for value in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


@dataclass(frozen=True)
class NetworkSettings:
    enabled: bool = False
    lan_ip: str = ''
    subnet: str = ''
    domain: str = ''

    def __post_init__(self):
        if type(self.enabled) is not bool or any(not isinstance(value, str) for value in
                                               (self.lan_ip, self.subnet, self.domain)):
            raise ValueError('局域网配置格式不正确')
        if not self.enabled:
            if self.lan_ip or self.subnet or self.domain:
                raise ValueError('本机模式不能包含局域网配置')
            return
        try:
            address = ipaddress.IPv4Address(self.lan_ip)
            network = ipaddress.IPv4Network(self.subnet, strict=True)
        except ValueError:
            raise ValueError('请输入有效的内网 IPv4 地址和子网') from None
        if (not any(network.subnet_of(private) for private in _PRIVATE)
                or address not in network or address in (network.network_address, network.broadcast_address)):
            raise ValueError('局域网服务只允许主机所在的私有 IPv4 子网')
        if (self.domain != self.domain.lower() or len(self.domain) > 253
                or len(self.domain.split('.')) < 2
                or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                       for label in self.domain.split('.'))):
            raise ValueError('域名应填写小写完整主机名，不包含协议、路径或端口')

    @property
    def bind_host(self):
        # 保留主机回环访问；请求端另行严格校验客户端子网与 Host。
        return '0.0.0.0' if self.enabled else '127.0.0.1'

    def allows_client(self, address):
        try:
            client = ipaddress.ip_address(address)
            return client.is_loopback or (self.enabled and client in ipaddress.ip_network(self.subnet))
        except ValueError:
            return False

    def allows_host(self, authority, port):
        if not isinstance(authority, str) or not authority or any(c.isspace() for c in authority):
            return False
        hosts = {'127.0.0.1', 'localhost'}
        if self.enabled:
            hosts.update((self.lan_ip, self.domain))
        authorities = {f'{host}:{port}' for host in hosts}
        if port == 80:
            authorities.update(hosts)
        return authority.lower() in authorities


def load_network(path=NETWORK_PATH):
    try:
        document = json.loads(Path(path).read_text(encoding='utf-8'))
        if (not isinstance(document, dict) or type(document.get('version')) is not int
                or document.pop('version', None) != 1):
            raise ValueError()
        return NetworkSettings(**document)
    except FileNotFoundError:
        return NetworkSettings()
    except (ValueError, TypeError, OSError):
        raise ValueError('局域网配置无法读取，请检查本机 network.local.json') from None


def save_network(settings, path=NETWORK_PATH):
    document = {'version': 1, 'enabled': settings.enabled, 'lan_ip': settings.lan_ip,
                'subnet': settings.subnet, 'domain': settings.domain}
    atomic_write(Path(path), json.dumps(document, ensure_ascii=False, indent=2).encode())


class LanAccessStore:
    def __init__(self, path=ACCESS_PATH, *, protect=_crypt, unprotect=None):
        self.path = Path(path)
        self.protect = protect
        self.unprotect = unprotect if unprotect is not None else lambda data: _crypt(data, decrypt=True)

    def ensure(self):
        try:
            if self.path.exists():
                data = self.path.read_bytes()
                if not data.startswith(_MAGIC):
                    raise ValueError()
                password = self.unprotect(data[len(_MAGIC):]).decode('ascii')
                if not (re.fullmatch(r'[0-9]{6}', password) or re.fullmatch(r'[A-Za-z0-9_-]{24}', password)):
                    raise ValueError()
                return password
            password = f'{secrets.randbelow(1_000_000):06d}'
            atomic_write(self.path, _MAGIC + self.protect(password.encode('ascii')))
            return password
        except Exception:
            raise ValueError('局域网访问凭据无法保存或解密，请使用原 Windows 用户检查本机配置') from None

    def simplify(self):
        """仅在明确要求时替换旧长密码；已有六位数字密码保持不变。"""
        password = self.ensure()
        if re.fullmatch(r'[0-9]{6}', password):
            return password
        password = f'{secrets.randbelow(1_000_000):06d}'
        try:
            atomic_write(self.path, _MAGIC + self.protect(password.encode('ascii')))
        except Exception:
            raise ValueError('局域网访问密码保存失败，原密码仍保留') from None
        return password


def valid_access_password(password):
    return (isinstance(password, str) and password.isascii()
            and (re.fullmatch(r'[0-9]{6}', password) is not None or 24 <= len(password) <= 128))


def authorized(header, password):
    if not isinstance(header, str) or len(header) > 2048:
        return False
    try:
        method, encoded = header.split(' ', 1)
        if method.lower() != 'basic':
            return False
        username, supplied = base64.b64decode(encoded, validate=True).split(b':', 1)
        return (secrets.compare_digest(username, ACCESS_USERNAME.encode())
                and secrets.compare_digest(supplied, password.encode('ascii')))
    except (ValueError, UnicodeError):
        return False


def show_lan_info(settings, port=8765):
    """仅在主机桌面显示访问方式；不向网页、日志或命令输出密码。"""
    import tkinter as tk
    from tkinter import ttk
    if not settings.enabled:
        raise ValueError('尚未启用局域网模式')
    password = LanAccessStore().ensure()
    window = tk.Tk()
    window.title('礼赠工作台 · 局域网访问')
    window.geometry('590x330')
    frame = ttk.Frame(window, padding=24)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='同一局域网中的设备可访问', font=('Microsoft YaHei UI', 15, 'bold')).pack(anchor='w')
    ttk.Label(frame, text=f'域名：http://{settings.domain}:{port}/\n地址：http://{settings.lan_ip}:{port}/\n用户名：{ACCESS_USERNAME}',
              font=('Microsoft YaHei UI', 11), justify='left').pack(anchor='w', pady=16)
    value = tk.StringVar(value=password)
    entry = ttk.Entry(frame, textvariable=value, show='●', state='readonly', width=38)
    entry.pack(anchor='w')
    actions = ttk.Frame(frame)
    actions.pack(anchor='w', pady=12)

    def copy():
        window.clipboard_clear()
        window.clipboard_append(password)
        status.set('访问密码已复制；请只分享给需要使用工作台的人。')

    ttk.Button(actions, text='显示 / 隐藏', command=lambda: entry.configure(show='' if entry.cget('show') else '●')).pack(side='left')
    ttk.Button(actions, text='复制访问密码', command=copy).pack(side='left', padx=10)
    status = tk.StringVar(value='访问密码与聚水潭、班牛密码独立，仅保存在这台主机。')
    ttk.Label(frame, textvariable=status, wraplength=530).pack(anchor='w')
    window.mainloop()
