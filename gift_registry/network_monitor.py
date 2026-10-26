"""只读检测物理局域网变化，提醒人工核对 Cloudflare；不修改网络或 DNS。"""

import ctypes
import hashlib
import ipaddress
import json
import os
import subprocess
import threading
from pathlib import Path

from .config import DATA_DIR
from .sessions import atomic_write

BASELINE_PATH = DATA_DIR / 'lan_network_baseline.json'
_PROBE = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$result = @(Get-NetIPConfiguration -ErrorAction Stop | Where-Object {
    $_.NetAdapter.Status -eq 'Up' -and $_.NetAdapter.HardwareInterface -and $_.IPv4DefaultGateway
} | ForEach-Object {
    $configuration = $_
    foreach ($address in $configuration.IPv4Address) {
        [PSCustomObject]@{
            adapter = [string]$configuration.NetAdapter.InterfaceGuid
            name = [string]$configuration.NetProfile.Name
            ip = [string]$address.IPAddress
            prefix = [int]$address.PrefixLength
            gateway = [string]$configuration.IPv4DefaultGateway[0].NextHop
        }
    }
})
ConvertTo-Json -InputObject $result -Compress
"""


def probe_network():
    """忽略 VPN、代理及虚拟网卡；执行时间有界，输出不包含账号和访问密码。"""
    if os.name != 'nt':
        raise OSError('Windows network probe required')
    result = subprocess.run(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', _PROBE],
        capture_output=True, encoding='utf-8', errors='strict', timeout=12, check=True,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return normalize_connections(json.loads(result.stdout.lstrip('\ufeff')))


def normalize_connections(connections):
    if not isinstance(connections, list):
        raise ValueError('Invalid network probe')
    normalized = []
    for item in connections:
        address = ipaddress.IPv4Address(item['ip'])
        prefix = item['prefix']
        if type(prefix) is not int or not 1 <= prefix <= 30:
            raise ValueError('Invalid network prefix')
        subnet = ipaddress.IPv4Network(f'{address}/{prefix}', strict=False)
        # 与共享配置使用相同的 RFC1918 地址范围，不选择自动分配或公网地址。
        if not any(subnet.subnet_of(ipaddress.IPv4Network(private)) for private in
                   ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')):
            continue
        adapter, name = item['adapter'], item['name']
        if not all(isinstance(value, str) and len(value) <= 256 for value in (adapter, name)):
            raise ValueError('Invalid network identity')
        normalized.append({'adapter': adapter.lower(), 'name': name, 'ip': str(address),
                           'subnet': str(subnet), 'gateway': str(ipaddress.IPv4Address(item['gateway']))})
    return sorted(normalized, key=lambda entry: (entry['adapter'], entry['ip']))


def desktop_notice(message):
    if os.name == 'nt':
        ctypes.windll.user32.MessageBoxW(None, message, '礼赠工作台 · 网络变化提醒', 0x40 | 0x10000)


class NetworkMonitor:
    def __init__(self, settings, *, path=BASELINE_PATH, probe=probe_network, notify=desktop_notice, interval=30):
        self.settings, self.path = settings, Path(path)
        self.probe, self.notify, self.interval = probe, notify, interval
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.thread = None
        self.notice_lock = threading.Lock()
        self.notified = set()
        self.current = None
        self.status = self._status('checking', '')

    def _status(self, state, message, connections=()):
        change_id = hashlib.sha256(json.dumps(list(connections), sort_keys=True).encode()).hexdigest()
        return {'enabled': self.settings.enabled, 'state': state, 'message': message,
                'domain': self.settings.domain, 'configured_ip': self.settings.lan_ip,
                'current_addresses': [{'ip': entry['ip'], 'subnet': entry['subnet']} for entry in connections],
                'change_id': change_id}

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.status))

    def _configuration(self):
        return {'lan_ip': self.settings.lan_ip, 'subnet': self.settings.subnet, 'domain': self.settings.domain}

    def _save(self, connection):
        atomic_write(self.path, json.dumps({'version': 1, 'configuration': self._configuration(),
                                          'connection': connection}, ensure_ascii=False).encode('utf-8'))

    def check(self):
        if not self.settings.enabled:
            return
        try:
            connections = self.probe()
            matches = [entry for entry in connections if entry['ip'] == self.settings.lan_ip
                       and entry['subnet'] == self.settings.subnet]
            with self.lock:
                self.current = matches[0] if len(matches) == 1 else None
                if not connections:
                    status = self._status('offline', '未检测到已连接的物理局域网。恢复连接后将继续检查 IP；请先使用本机地址访问。')
                elif self.current is None:
                    addresses = '、'.join(entry['ip'] for entry in connections)
                    message = (f'网络或 IP 已变化：原配置 {self.settings.lan_ip}，当前 {addresses}。'
                               f'请到 Cloudflare 核对 {self.settings.domain} 的 A 记录，改为用于共享的当前内网 IP，保持“仅 DNS”。'
                               '同时更新本机共享 IP/子网、防火墙及已有 hosts 映射；更新前请使用 http://127.0.0.1:8765/。')
                    status = self._status('changed', message, connections)
                else:
                    baseline = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else None
                    if baseline is not None and (not isinstance(baseline, dict) or baseline.get('version') != 1
                                                 or not isinstance(baseline.get('connection'), dict)
                                                 or not isinstance(baseline.get('configuration'), dict)):
                        raise ValueError('Invalid network baseline')
                    if baseline is None or baseline['configuration'] != self._configuration():
                        self._save(self.current)
                        status = self._status('ok', '', connections)
                    elif baseline['connection'] != self.current:
                        status = self._status('changed',
                            f'网络已切换，当前 IP 为 {self.current["ip"]}。请到 Cloudflare 核对 {self.settings.domain} 的 A 记录。'
                            'IP 未变化时无需改值；确认仍指向当前主机并保持“仅 DNS”后，可点击“已核对”。', connections)
                    else:
                        status = self._status('ok', '', connections)
                self.status = status
        except Exception:
            with self.lock:
                self.current = None
                # 探测失败不能清除已经发现的变化，也不能覆盖基线。
                if self.status['state'] != 'changed':
                    self.status = self._status('unavailable', '暂时无法读取网络状态，请手动核对主机 IP 和 Cloudflare DNS；稍后自动检查。')
                status = self.status.copy()
        if status['state'] == 'changed':
            self._notify(status)

    def _notify(self, status):
        key = status['change_id']
        if self.notify is None or key in self.notified or not self.notice_lock.acquire(blocking=False):
            return
        self.notified.add(key)

        def show():
            try:
                self.notify(status['message'])
            finally:
                self.notice_lock.release()
        threading.Thread(target=show, daemon=True, name='lan-change-notice').start()

    def acknowledge(self, change_id):
        with self.lock:
            if self.status['state'] != 'changed' or self.status['change_id'] != change_id:
                raise ValueError('网络状态已更新，请刷新后重新核对')
            if self.current is None:
                raise ValueError('请先更新本机共享 IP/子网并重启工作台，不能忽略地址不一致的提醒')
            self._save(self.current)
            self.status = {**self.status, 'state': 'ok', 'message': ''}
            return self.snapshot()

    def start(self):
        def watch():
            while not self.stopped.is_set():
                self.check()
                self.stopped.wait(self.interval)
        if self.thread is None:
            self.thread = threading.Thread(target=watch, daemon=True, name='lan-network-monitor')
            self.thread.start()

    def close(self):
        self.stopped.set()
        if self.thread is not None:
            self.thread.join(timeout=13)
