"""U 盘入口：独立 Python 运行环境，默认使用目标电脑安装的 Edge。"""

import json
import os
from pathlib import Path
import runpy
import socket
import subprocess
import sys
from urllib.request import ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parent


def configure():
    manifest = json.loads((ROOT / 'portable.json').read_text(encoding='utf-8'))
    kind = manifest.get('browser', 'chrome')  # 旧包仍兼容；新包明确指定 edge。
    if kind not in ('chrome', 'edge'):
        raise RuntimeError('portable.json 的 browser 必须为 edge 或 chrome')
    os.environ['GIFT_BROWSER'] = kind
    if kind == 'edge':
        for key in ('GIFT_CHROME_BINARY', 'GIFT_CHROMEDRIVER', 'GIFT_EDGEDRIVER', 'SE_EDGEDRIVER'):
            os.environ.pop(key, None)
        # 按目标电脑当前 Edge 版本匹配驱动，不复用 PATH 上的不匹配驱动。
        os.environ['SE_AVOID_BROWSER_DOWNLOAD'] = 'true'
        os.environ['SE_SKIP_DRIVER_IN_PATH'] = 'true'
        os.environ['SE_TIMEOUT'] = '60'
    else:
        for key, field in (('GIFT_CHROME_BINARY', 'chrome'), ('GIFT_CHROMEDRIVER', 'driver')):
            path = (ROOT / manifest[field]).resolve()
            if not path.is_relative_to(ROOT) or not path.is_file():
                raise RuntimeError(f'便携文件缺失：{field}；请保留完整目录结构。')
            os.environ[key] = str(path)
    os.environ['SE_CACHE_PATH'] = str(ROOT / 'data' / 'selenium')
    os.environ['SE_AVOID_STATS'] = 'true'
    os.environ['PYTHONNOUSERSITE'] = '1'
    os.chdir(ROOT)
    return manifest


def stop(port):
    """只关闭由这一副本记录的服务；不强制终止正在执行的登记。"""
    base = f'http://127.0.0.1:{port}'
    opener = build_opener(ProxyHandler({}))
    with opener.open(base + '/api/health', timeout=5) as response:
        health = json.load(response)
    pid_file = ROOT / 'data' / 'server.pid'
    if health.get('app') != 'gift_registry' or not pid_file.is_file():
        raise RuntimeError('未确认本副本的运行服务，未发送关闭请求。')
    expected_pid = str(int(pid_file.read_text(encoding='ascii').strip()))
    netstat = Path(os.environ['SystemRoot']) / 'System32' / 'netstat.exe'
    rows = subprocess.check_output([str(netstat), '-ano', '-p', 'tcp'],
                                   creationflags=subprocess.CREATE_NO_WINDOW).decode(errors='replace').splitlines()
    if not any(len(parts := line.split()) == 5 and parts[0] == 'TCP'
               and parts[1] in (f'127.0.0.1:{port}', f'0.0.0.0:{port}')
               and parts[3] == 'LISTENING' and parts[4] == expected_pid for line in rows):
        raise RuntimeError('监听端口与本副本进程不一致，未发送关闭请求。')
    with opener.open(base + '/api/config', timeout=5) as response:
        config = json.load(response)
    request = Request(base + '/api/shutdown', data=b'{}', headers={
        'Content-Type': 'application/json', 'Origin': base, 'X-CSRF-Token': config['token']})
    with opener.open(request, timeout=10) as response:
        if not json.load(response).get('stopping'):
            raise RuntimeError('服务没有确认关闭请求。')
    print('已请求正常退出；请等待登记结束、浏览器关闭后再安全弹出 U 盘。')


def main():
    manifest = configure()
    mode = sys.argv[1] if len(sys.argv) > 1 else 'start'
    if mode == 'settings':
        from gift_registry.platform_settings import run
        run()
        return
    if mode == 'stop':
        stop(manifest['port'])
        return
    if mode == 'lan-setup':
        from gift_registry.network import NetworkSettings, LanAccessStore, save_network, show_lan_info
        from gift_registry.network_monitor import probe_network
        connections = probe_network()
        if not connections:
            print('没有检测到带网关的物理内网连接，请先连接目标局域网。')
            return
        for index, item in enumerate(connections, 1):
            print(f"{index}. {item['name']}  IP {item['ip']}  子网 {item['subnet']}")
        choice = input('选择用于共享的网络编号：').strip()
        if not choice.isdecimal() or not 1 <= int(choice) <= len(connections):
            print('未选择有效网络，配置未更改。')
            return
        item = connections[int(choice) - 1]
        settings = NetworkSettings(True, item['ip'], item['subnet'])
        LanAccessStore().ensure()
        save_network(settings)
        print('已保存无域名共享配置。请关闭并重启工作台，再以管理员身份运行 allow-lan.bat。')
        show_lan_info(settings, manifest['port'])
        return
    if mode == 'accounts':
        from getpass import getpass
        from gift_registry.config import SITES
        from gift_registry.credentials import CredentialStore
        store = CredentialStore()
        for site in SITES:
            username = input(f'{site.name}账号（留空跳过）：').strip()
            if username:
                password = getpass('密码（输入不显示）：')
                if not password:
                    raise ValueError('密码不能为空')
                store.save(site.key, username, password)
        print('已使用当前 Windows 用户加密保存。')
        return
    modes = {'start': ['--port', str(manifest['port'])], 'login': ['--login'], 'version': ['--version'],
             'lan-info': ['--lan-info', '--port', str(manifest['port'])], 'lan-disable': ['--disable-lan']}
    if mode not in modes:
        raise ValueError('Unknown portable mode')
    if mode == 'start':
        with socket.socket() as probe:
            if probe.connect_ex(('127.0.0.1', manifest['port'])) == 0:
                print(f"端口 {manifest['port']} 已在使用，请先关闭该端口的旧工作台。")
                return
    # 项目里的登录助手子进程继承此环境；sys.executable 仍指向随包 Python。
    sys.argv = [str(ROOT / 'main.py'), *modes[mode]]
    runpy.run_path(str(ROOT / 'main.py'), run_name='__main__')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # 不打印可能带密码/令牌的第三方异常正文。
        print(f'便携版启动/操作失败（{type(exc).__name__}）。请检查文件完整性及端口占用。')
        raise SystemExit(1)
