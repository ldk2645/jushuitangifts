"""从当前已停用的项目制作 Windows x64 自带 Python/Tk/Selenium 的便携副本。"""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
from datetime import datetime


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from gift_registry import __version__


def copy_tree(source, target):
    if source.is_dir():
        shutil.copytree(source, target, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.lock'))


def write_lan_assets(destination, port=8766):
    for filename, mode in (('lan-setup.bat', 'lan-setup'), ('lan-info.bat', 'lan-info'), ('lan-disable.bat', 'lan-disable')):
        (destination / filename).write_text(
            '@echo off\ncd /d "%~dp0"\n"%~dp0runtime\\python.exe" "%~dp0portable.py" ' + mode + '\npause\n',
            encoding='ascii')
    shutil.copy2(ROOT / 'allow-lan.ps1', destination / 'allow-lan.ps1')
    (destination / 'allow-lan.bat').write_text(
        '@echo off\ncd /d "%~dp0"\npowershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0allow-lan.ps1" -Port ' + str(port) + '\npause\n',
        encoding='ascii')
    guide = (ROOT / 'docs' / 'portable-lan.md').read_text(encoding='utf-8')
    if port != 8766:
        guide = '隔离候选包，仅供离线验收，尚未批准上线。\n\n' + guide.replace('8766', str(port))
    (destination / '局域网使用说明.txt').write_text(guide, encoding='utf-8-sig')
    shutil.copy2(ROOT / 'docs' / 'portable-lan.md', destination / 'docs' / 'portable-lan.md')


def write_settings_assets(destination):
    (destination / 'settings.bat').write_text(
        '@echo off\ncd /d "%~dp0"\n"%~dp0runtime\\python.exe" "%~dp0portable.py" settings\npause\n',encoding='ascii')
    config = destination / 'config' / 'accounts.local.json'
    if not config.exists():
        config.write_text(json.dumps({'version':1,'accounts':{},'browser_headless':{
            'jushuitan':False,'banniu':False}},indent=2),encoding='utf-8')
    (destination / '账号与浏览器配置说明.txt').write_text(
        (ROOT/'docs/portable-settings.md').read_text(encoding='utf-8'),encoding='utf-8-sig')
    shutil.copy2(ROOT/'docs/portable-settings.md', destination/'docs/portable-settings.md')


def build(destination, chrome_relative=None, driver=None, *, candidate=False):
    destination = destination.resolve()
    port = 18766 if candidate else 8766
    for protected in (Path(r'D:\UserData\Desktop\NL'), Path(r'E:\gifts')):
        if candidate and (destination == protected or destination.is_relative_to(protected)):
            raise ValueError('候选包禁止写入生产目录或 U 盘')
    chrome = (destination / chrome_relative).resolve() if chrome_relative else None
    if chrome is not None and (not chrome.is_relative_to(destination) or not chrome.is_file() or driver is None):
        raise ValueError('指定便携 Chrome 不在目标目录内或不存在，或未指定配套驱动')
    # 只允许添加到准备好的 Chrome 目录旁，绝不覆盖已有项目或用户数据。
    names = ('runtime', 'drivers', 'gift_registry', 'frontend', 'config', 'data', 'tests',
             'docs', 'tools', '.gitignore', 'main.py', 'portable.py', 'portable.json', 'start.bat', 'login.bat',
             'stop.bat', 'accounts.bat', 'README.md', 'requirements.txt', 'example.md',
             '便携版使用说明.txt', 'portable-files.sha256.json', 'lan-setup.bat', 'lan-info.bat',
             'lan-disable.bat', 'allow-lan.bat', 'allow-lan.ps1', '局域网使用说明.txt',
             'settings.bat', 'browser-settings.bat', '账号与浏览器配置说明.txt',
             'diagnose-login.bat', 'diagnose-selenium.bat', 'fix-browser-integrity.bat',
             'verify-honor.bat', 'update-gifts.bat')
    if any((destination / name).exists() for name in names):
        raise ValueError('目标已有项目文件，未覆盖；请使用新的目录或人工备份后处理')
    if (ROOT / 'data' / 'server.pid').exists():
        raise ValueError('请先正常关闭源工作台，确保草稿和防重复记录是完整快照')
    print('Copying isolated Python and Selenium runtime...', flush=True)
    runtime = destination / 'runtime'
    runtime.mkdir()
    base = Path(sys.base_prefix)
    for source in base.iterdir():
        if source.is_file() and (source.suffix in ('.dll', '.exe') or source.name == 'LICENSE.txt'):
            shutil.copy2(source, runtime / source.name)
    for name in ('DLLs', 'tcl'):
        copy_tree(base / name, runtime / name)
    shutil.copytree(base / 'Lib', runtime / 'Lib', ignore=shutil.ignore_patterns(
        'site-packages', '__pycache__', '*.pyc'))
    copy_tree(Path(sys.prefix) / 'Lib' / 'site-packages', runtime / 'Lib' / 'site-packages')
    # 隔离系统 Python、PYTHONPATH 和用户 site-packages；.. 随盘符变化解析为项目根目录。
    (runtime / 'python312._pth').write_text('Lib\nDLLs\nLib/site-packages\n..\nimport site\n', encoding='ascii')
    print('Copying project and business history snapshot...', flush=True)
    (destination / 'drivers').mkdir()
    if chrome is not None:
        shutil.copy2(driver, destination / 'drivers' / 'chromedriver.exe')
    for name in ('gift_registry', 'frontend', 'tests'):
        copy_tree(ROOT / name, destination / name)
    for name in ('main.py', 'portable.py', 'README.md', 'requirements.txt', 'example.md'):
        shutil.copy2(ROOT / name, destination / name)
    shutil.copy2(ROOT / '.gitignore', destination / '.gitignore')
    (destination / 'tools').mkdir()
    for name in ('build_portable.py', 'catalog_harness.py', 'prepare_v120_rollback.py', 'check_dev_isolation.py',
                 'check_login_startup.py', 'check_browser_startup.py', 'fix_browser_integrity.ps1',
                 'inspect_honor_page.py', 'update_gifts.py'):
        shutil.copy2(ROOT / 'tools' / name, destination / 'tools' / name)
    (destination / 'docs').mkdir()
    for name in ('business-workflow.md', 'automatic-login.md', 'lan.md', 'catalog-name-review-2026-10-28.md',
                 'catalog-harness.md', 'catalog-harness-policy.json', 'deepseek-catalog-prompt.md'):
        shutil.copy2(ROOT / 'docs' / name, destination / 'docs' / name)
    copy_tree(ROOT / 'docs' / 'releases', destination / 'docs' / 'releases')
    write_lan_assets(destination, port)
    (destination / 'config').mkdir()
    write_settings_assets(destination)
    shutil.copy2(ROOT / 'browser-settings.bat', destination / 'browser-settings.bat')
    for name in ('diagnose-login.bat', 'diagnose-selenium.bat', 'fix-browser-integrity.bat',
                 'verify-honor.bat', 'update-gifts.bat'):
        shutil.copy2(ROOT / name, destination / name)
    for name in ('gifts.json', 'gift_groups.local.json'):
        source = ROOT / 'config' / name
        if source.exists():
            shutil.copy2(source, destination / 'config' / name)
    (destination / 'data').mkdir()
    for name in ('registrations.json', 'maintenance.json', 'manual_status.json'):
        source = ROOT / 'data' / name
        if source.exists():
            shutil.copy2(source, destination / 'data' / name)
    for name in ('registration_jobs', 'registration_archive', 'exports', 'draft_archive', 'backups'):
        copy_tree(ROOT / 'data' / name, destination / 'data' / name)
    manifest = {'version': __version__, 'built_at': datetime.now().astimezone().isoformat(),
                'browser': 'chrome' if chrome else 'edge', 'port': port, 'release_candidate': candidate,
                'python': sys.version.split()[0], 'platform': 'Windows x64'}
    if chrome:
        manifest.update(chrome=chrome.relative_to(destination).as_posix(), driver='drivers/chromedriver.exe')
    (destination / 'portable.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    for filename, mode in (('start.bat', 'start'), ('login.bat', 'login'), ('stop.bat', 'stop'), ('accounts.bat', 'accounts')):
        (destination / filename).write_text(
            '@echo off\ncd /d "%~dp0"\n"%~dp0runtime\\python.exe" "%~dp0portable.py" ' + mode + '\npause\n',
            encoding='ascii')
    (destination / '便携版使用说明.txt').write_text(f'''礼赠自动登记工具 v{__version__} U 盘便携版（Windows 10/11 x64）

{'隔离候选包：尚未批准上线，仅在独立目录验收，不得覆盖生产或 U 盘。' if candidate else ''}

1. 双击 start.bat，工作台地址 http://127.0.0.1:{port}/ 。启动窗口保持打开。
2. 首次在新电脑使用，点“平台登录”或双击 login.bat，手动完成两平台登录及验证，保存并关闭助手。
   若需账号密码自动登录，双击 accounts.bat 在目标电脑输入，密码不回显、由该 Windows 用户加密。
3. 已包含 Python 3.12、Tk、Selenium；不使用电脑上的 Python，不必运行 setup.ps1。
   本包浏览器：{'随包 Chrome 与配套驱动，不需在线下载浏览器' if chrome else '目标电脑安装的 Microsoft Edge；首次运行或更新后需联网匹配驱动'}。
   浏览器路径和驱动配置见 portable.json；平台登录及业务访问仍需要网络。
4. 双击 stop.bat 请求正常关闭，等服务窗口和业务浏览器完全退出，再安全弹出 U 盘。
5. 可移动整个 gifts 目录、改变盘符。请保持 runtime、data、config 与项目文件的相对位置。

已带入制作时的草稿、礼品分组和完整防重复任务历史。按订单号＋整组礼品编码防重，失败保护规则保持不变。
未复制原电脑的账号密码、Cookie、Chrome 用户配置及局域网设置，它们不能跨 Windows 用户解密。
便携版默认仅本机访问；局域网共享无需域名。运行 lan-setup.bat 选择目标电脑网络，再以管理员身份运行 allow-lan.bat，重启工作台。
详见“局域网使用说明.txt”；lan-info.bat 查看其他设备访问地址和六位密码。
源电脑与 U 盘此后各自保存数据，不会自动同步：同一业务请固定使用一份工作台，避免两份新增历史分叉。
备份/升级时保留整个 data 及 config；不要删除任务历史来重登订单。

等待调整：班牛拉单确认最多60秒；回执最多24轮、每轮间隔5秒（页面加载另计）。
无效/旧回传号只读等待同一任务更新；信息不符、明确同步失败仍失败，绝不再次点击提交。
''', encoding='utf-8-sig')
    print('Hashing portable files for transfer verification...', flush=True)
    hashes = {p.relative_to(destination).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for name in names if name != 'portable-files.sha256.json'
              for p in ((destination / name).rglob('*') if (destination / name).is_dir() else [destination / name])
              if p.is_file()}
    if chrome:
        hashes.update({p.relative_to(destination).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in chrome.parent.rglob('*') if p.is_file()})
    (destination / 'portable-files.sha256.json').write_text(json.dumps(hashes, indent=2), encoding='utf-8')
    print(json.dumps({'copied_files': len(hashes), 'destination': str(destination), 'port': port}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--chrome', help='可选：制作旧版 Chrome 包时的相对路径；默认使用系统 Edge')
    parser.add_argument('--driver', type=Path)
    parser.add_argument('--candidate', action='store_true', help='隔离验收候选包，固定使用 18766 端口，不部署生产')
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12) or sys.maxsize <= 2**32:
        raise SystemExit('请使用当前项目的 Windows x64 Python 3.12 环境制作')
    build(args.destination, args.chrome, args.driver, candidate=args.candidate)
