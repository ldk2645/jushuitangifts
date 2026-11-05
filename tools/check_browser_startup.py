"""只启动独立空白浏览器，检查运行环境；不读取账号、Cookie 或业务数据。

除浏览器启动本身外，还会报告当前 Python 进程的 Windows 完整性级别：
项目文件若继承了沙箱／权限工具留下的“低完整性级别”标签，Chrome / Edge
会在启动阶段直接退出（SessionNotCreatedException / tab crashed）。该检查
只读取标签，不修改任何权限。
"""
import argparse
import ctypes
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gift_registry.browser import create_driver
from gift_registry.config import Site
from gift_registry.driver_shutdown import quit_owned_driver

INTEGRITY_LEVELS = {
    'S-1-16-4096': 'low',
    'S-1-16-8192': 'medium',
    'S-1-16-12288': 'high',
    'S-1-16-16384': 'system',
}

LOW_INTEGRITY_HINT = (
    '当前 Python 以“低完整性级别”运行：项目文件继承了 Windows 沙箱工具留下的权限标签。'
    '这不是程序代码错误，低完整性进程启动的 Chrome / Edge 会在启动阶段退出。'
    '请双击项目根目录的 fix-browser-integrity.bat 修复，然后重新运行本检查。'
)


def process_integrity():
    """读取当前 Python 进程的 Windows 完整性级别，只读不修改。"""
    if os.name != 'nt':
        return {'supported': False}
    try:
        import ctypes.wintypes as wt

        advapi32 = ctypes.WinDLL('advapi32', use_last_error=True)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.GetCurrentProcess.restype = wt.HANDLE
        advapi32.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
        advapi32.OpenProcessToken.restype = wt.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)]
        advapi32.GetTokenInformation.restype = wt.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
        advapi32.ConvertSidToStringSidW.restype = wt.BOOL

        token = wt.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            return {'supported': False, 'error': ctypes.get_last_error()}
        try:
            # TokenIntegrityLevel = 25，返回 TOKEN_MANDATORY_LABEL{ SID_AND_ATTRIBUTES }。
            size = wt.DWORD(0)
            advapi32.GetTokenInformation(token, 25, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(token, 25, buffer, size.value, ctypes.byref(size)):
                return {'supported': False, 'error': ctypes.get_last_error()}
            sid_pointer = ctypes.c_void_p.from_address(ctypes.addressof(buffer)).value
            sid_text = ctypes.c_wchar_p()
            sid = ''
            if advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_text)):
                sid = sid_text.value
                kernel32.LocalFree(sid_text)
        finally:
            kernel32.CloseHandle(token)
    except Exception as exc:  # 诊断信息不能因权限查询失败而中断浏览器检查。
        return {'supported': False, 'error': type(exc).__name__}
    level = INTEGRITY_LEVELS.get(sid, 'unknown')
    return {'supported': True, 'sid': sid, 'level': level, 'low': level == 'low'}


def window_station_probe():
    """创建并立即关闭临时窗口站；不切换桌面，也不修改任何对象权限。"""
    if os.name != 'nt':
        return {'supported': False}
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.CreateWindowStationW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32,
                                         ctypes.c_uint32, ctypes.c_void_p]
    user.CreateWindowStationW.restype = ctypes.c_void_p
    user.CloseWindowStation.argtypes = [ctypes.c_void_p]
    user.CloseWindowStation.restype = ctypes.c_int
    handle = user.CreateWindowStationW(None, 0, 0x0002 | 0x0008, None)
    error = ctypes.get_last_error() if not handle else 0
    if handle:
        user.CloseWindowStation(handle)
    return {'supported': True, 'created': bool(handle), 'winerror': error}


def check(root, kind, headless):
    mode = 'headless' if headless else 'visible'
    profile = root / (kind + '-' + mode)
    driver = None
    result = {'browser': kind, 'mode': mode, 'status': 'starting'}
    started = time.monotonic()
    os.environ['GIFT_BROWSER'] = kind
    try:
        driver = create_driver(Site('startup-check', '空白页诊断', 'about:blank', ()),
                               headless=headless, profile=profile)
        driver.get('data:text/html,<title>gift-startup-check</title><p>ready</p>')
        if driver.title != 'gift-startup-check':
            raise RuntimeError('offline page title mismatch')
        result.update(status='passed', browser_version=driver.capabilities.get('browserVersion'))
    except Exception as exc:
        result.update(status='failed', error_type=type(exc).__name__,
                      error=str(exc).split('Stacktrace:')[0][:1400])
    finally:
        if driver is not None:
            try:
                result['shutdown'] = quit_owned_driver(driver)
            except Exception as exc:
                result.update(status='failed', shutdown_error=type(exc).__name__)
    result['seconds'] = round(time.monotonic() - started, 2)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', choices=('chrome', 'edge'), default='chrome')
    parser.add_argument('--label', default='manual')
    args = parser.parse_args()
    root = ROOT / '.tmp' / 'browser-startup' / str(time.time_ns())
    root.mkdir(parents=True)
    integrity = process_integrity()
    result = {'at': datetime.now().astimezone().isoformat(), 'label': args.label,
              'python': sys.version.split()[0], 'python_base': sys.base_prefix,
              'process_integrity': integrity,
              'window_station': window_station_probe(), 'orders_entered': False,
              'submitted': False, 'checks': []}
    if integrity.get('low'):
        result['hint'] = LOW_INTEGRITY_HINT
        print(LOW_INTEGRITY_HINT, flush=True)
    report = ROOT / '.tmp' / 'browser-startup-result.json'
    for headless in (True, False):
        result['checks'].append(check(root, args.browser, headless))
        if integrity.get('low'):
            result['checks'][-1]['hint'] = LOW_INTEGRITY_HINT
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result['checks'][-1], ensure_ascii=False), flush=True)
    (root / 'report.json').write_text(report.read_text(encoding='utf-8'), encoding='utf-8')
    print('Integrity: ' + (integrity.get('level', 'unknown')), flush=True)
    print('Report: ' + str(report), flush=True)
    return 0 if all(c['status'] == 'passed' for c in result['checks']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
