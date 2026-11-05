"""诊断“平台登录”只打开一个平台浏览器的问题；只读，不提交任何业务。

依次检查：便携/浏览器模式、运行完整性级别、登录配置锁、会话与账号能否解密、
浏览器配置目录残留锁文件、该平台浏览器配置是否仍被另一个本工具浏览器占用、
两个平台浏览器能否启动并进入登录/业务页面。
报告写入 .tmp/login-diagnostic.json；不读取、不输出账号密码明文。
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

from gift_registry.authentication import ENTRY_URLS, AuthenticationRequired
from gift_registry.browser import SiteBrowser, create_driver
from gift_registry.business_browser import ProfileLock
from gift_registry.config import DATA_DIR, SITES
from gift_registry.credentials import CredentialStore
from gift_registry.driver_shutdown import quit_owned_driver
from gift_registry.sessions import SessionStore
from gift_registry.workflow import PlatformOperationError

OUT = ROOT / '.tmp' / 'login-diagnostic.json'


def _integrity():
    try:
        from check_browser_startup import process_integrity, window_station_probe
        return process_integrity(), window_station_probe()
    except Exception as exc:
        return {'supported': False, 'error': type(exc).__name__}, {'supported': False}


def _portable_config(result):
    manifest_path = ROOT / 'portable.json'
    if not manifest_path.exists():
        result['package'] = 'checkout'
        result['browser_kind'] = os.environ.get('GIFT_BROWSER', 'chrome')
        return
    result['package'] = 'portable'
    try:
        import portable
        manifest = portable.configure()
        result['portable'] = manifest
        result['browser_kind'] = os.environ.get('GIFT_BROWSER', manifest.get('browser', 'chrome'))
    except Exception as exc:
        result['browser_kind'] = os.environ.get('GIFT_BROWSER', 'unknown')
        result['portable_error_type'] = type(exc).__name__
        result['portable_error'] = str(exc)[:400]


def _devtools_alive(profile):
    """配置目录里的 DevToolsActivePort 仍能连接，说明该浏览器配置正被另一个窗口使用。"""
    port_file = profile / 'DevToolsActivePort'
    try:
        port = int(port_file.read_text(encoding='utf-8', errors='replace').splitlines()[0])
    except (OSError, ValueError, IndexError):
        return None
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/version', timeout=1) as response:
            data = json.loads(response.read(4096))
        return {'port': port, 'browser': str(data.get('Browser', ''))[:80]}
    except Exception:
        return None


def _profile_state(site):
    kind = 'browser_edge' if os.environ.get('GIFT_BROWSER') == 'edge' else 'browser'
    profile = DATA_DIR / kind / site.key
    state = {'path': str(profile), 'exists': profile.is_dir()}
    if not profile.is_dir():
        return state
    leftovers = []
    for item in profile.iterdir():
        name = item.name
        if name in {'SingletonLock', 'SingletonCookie', 'SingletonSocket', 'DevToolsActivePort',
                    'lockfile', 'chrome_debug.log', 'Last Browser'} or name.startswith('Singleton'):
            leftovers.append(name)
    state['lock_like_files'] = sorted(leftovers)
    state['file_count'] = sum(1 for _ in profile.iterdir())
    alive = _devtools_alive(profile)
    state['profile_in_use'] = alive is not None
    if alive is not None:
        state['devtools'] = alive
    return state


def _credential_state(site):
    state = {}
    try:
        store = CredentialStore()
        settings = store.settings()
        state['username_present'] = bool(settings.get(site.key, {}).get('username'))
        state['headless'] = settings.get(site.key, {}).get('headless')
        if state['username_present']:
            try:
                credentials = store.load(site.key)
                state['dpapi_decrypt_ok'] = bool(credentials.password)
            except Exception as exc:
                state['dpapi_decrypt_ok'] = False
                state['dpapi_error_type'] = type(exc).__name__
    except Exception as exc:
        state['config_error_type'] = type(exc).__name__
    return state


def _session_state(site):
    state = {}
    try:
        store = SessionStore(DATA_DIR / 'sessions')
        path = store.path(site.key)
        state['file_exists'] = path.exists()
        if path.exists():
            try:
                snapshot = store.load(site.key)
                state['dpapi_decrypt_ok'] = True
                state['saved_at'] = snapshot.get('saved_at', '')
                state['cookie_count'] = len(snapshot.get('cookies', []))
            except Exception as exc:
                state['dpapi_decrypt_ok'] = False
                state['dpapi_error_type'] = type(exc).__name__
    except Exception as exc:
        state['error_type'] = type(exc).__name__
    return state


def _safe_error(exc):
    """只保留可用于定位的异常类与首行；不输出账号、密码或页面正文。"""
    line = str(exc).split('Stacktrace:')[0].strip().splitlines()[0] if str(exc).strip() else ''
    return {'error_type': type(exc).__name__, 'error_line': line[:400]}


def diagnose_site(site, timeout=45):
    result = {'site': site.key, 'name': site.name, 'stages': []}
    result['profile'] = _profile_state(site)
    result['session'] = _session_state(site)
    result['credentials'] = _credential_state(site)
    store = SessionStore(DATA_DIR / 'sessions')
    browser = SiteBrowser(site, store)
    driver = None
    try:
        result['stages'].append('load_session')
        try:
            browser.snapshot = store.load(site.key)
        except Exception as exc:
            result['session']['load_at_start_error_type'] = type(exc).__name__
            browser.snapshot = None
        result['stages'].append('create_driver')
        driver = create_driver(site, headless=False)
        browser.driver = driver
        result['browser_version'] = driver.capabilities.get('browserVersion')
        if browser.snapshot:
            result['stages'].append('restore_session')
            browser.restore(browser.snapshot)
        initial_url = (site.url if site.key == 'banniu' and not browser.snapshot
                       else ENTRY_URLS.get(site.key, site.url))
        result['stages'].append('navigate')
        browser.navigate(initial_url)
        result['stages'].append('current_page')
        result['url_after_open'] = driver.current_url
        result['title_after_open'] = driver.title
        result['stages'].append('authenticate')
        started = time.monotonic()
        try:
            recovered = browser.ensure_authenticated()
            result['auth_recovered'] = recovered
        except AuthenticationRequired as exc:
            result['auth_requires_manual'] = True
            result['auth_warning'] = str(exc)[:300]
        except Exception as exc:
            result['auth_error'] = _safe_error(exc)
        result['auth_seconds'] = round(time.monotonic() - started, 1)
        result['url_after_auth'] = driver.current_url
        result['title_after_auth'] = driver.title
        try:
            result['looks_like_login'] = bool(browser.looks_like_login())
        except Exception as exc:
            result['login_probe_error_type'] = type(exc).__name__
        result['ok'] = True
        result['verdict'] = '浏览器可启动并已进入页面'
    except Exception as exc:
        result['ok'] = False
        result['stage'] = result['stages'][-1] if result['stages'] else 'init'
        result.update(_safe_error(exc))
        result['profile_in_use'] = bool(result.get('profile', {}).get('profile_in_use'))
        if result['stage'] == 'create_driver':
            result['verdict'] = '浏览器启动失败（与账号无关）；查看 error_line 和 browser-startup-result.json'
            if result['profile_in_use']:
                result['verdict'] += ('；该平台浏览器配置仍被另一个本工具浏览器占用，'
                                      '请在任务管理器结束对应 Chrome/Edge 进程后重试')
        elif result['stage'] == 'navigate':
            result['verdict'] = '浏览器已启动但打不开登录页；检查目标电脑能否访问该平台'
        else:
            result['verdict'] = '浏览器可启动，问题在登录会话/账号或页面状态'
    finally:
        if driver is not None:
            try:
                result['shutdown'] = quit_owned_driver(driver)
            except Exception:
                result['shutdown'] = {'closed': False}
        result['stages'].append('done')
    return result


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', choices=[site.key for site in SITES], action='append',
                        help='只诊断指定平台，可重复；默认两个平台')
    args = parser.parse_args(argv)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    report = {'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'root': str(ROOT), 'submitted': False}
    _portable_config(report)
    report['integrity'], report['window_station'] = _integrity()
    try:
        status_path = DATA_DIR / 'status.json'
        report['status_json'] = json.loads(status_path.read_text(encoding='utf-8')) if status_path.exists() else None
    except Exception as exc:
        report['status_json_error_type'] = type(exc).__name__
    lock = ProfileLock(DATA_DIR / 'app.lock')
    try:
        lock.acquire()
        report['profile_lock'] = 'acquired'
    except PlatformOperationError as exc:
        report['profile_lock'] = 'busy'
        report['profile_lock_detail'] = str(exc)
        report['sites'] = []
        OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('登录配置正被占用：请先关闭登录助手 / 本工具浏览器后再运行诊断。')
        print('报告：' + str(OUT))
        return 2
    try:
        report['sites'] = [diagnose_site(site) for site in SITES
                           if not args.site or site.key in args.site]
    finally:
        lock.release()
        OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    for site in report['sites']:
        print('=' * 60)
        print(site['name'] + '：' + site.get('verdict', '?'))
        for key in ('stage', 'error_type', 'error_line', 'url_after_open', 'title_after_open',
                    'auth_warning', 'url_after_auth', 'title_after_auth', 'looks_like_login'):
            if site.get(key) is not None:
                print(f'  {key}: {site[key]}')
        leftovers = site.get('profile', {}).get('lock_like_files')
        if leftovers:
            print('  浏览器配置目录残留文件：' + ', '.join(leftovers))
        if site.get('profile', {}).get('profile_in_use'):
            devtools = site['profile'].get('devtools', {})
            print('  该平台浏览器配置正在被另一个本工具浏览器使用'
                  + (f"（{devtools.get('browser', '')}，调试端口 {devtools.get('port')}）" if devtools else ''))
        if site.get('session', {}).get('dpapi_decrypt_ok') is False:
            print('  会话快照无法解密（通常是从其它电脑/用户复制来，或 Windows 账户不同）')
        if site.get('credentials', {}).get('dpapi_decrypt_ok') is False:
            print('  账号密码密文无法解密（通常是从其它电脑/用户复制来）')
    integrity = report['integrity']
    if integrity.get('low'):
        print('注意：当前 Python 以低完整性级别运行，可能导致浏览器无法启动；请运行 fix-browser-integrity.bat。')
    print('报告：' + str(OUT))
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
