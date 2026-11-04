"""双击启动的可见等待与错误提示；不停止旧进程，不改任务记录。"""
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener
import webbrowser

from .config import ROOT
from .edition import DEFAULT_PORT, EDITION, APP_NAME
from .network import load_network


class StartupError(RuntimeError):
    """固定、安全的用户提示，不包含子进程异常原文。"""


def read_health(port):
    try:
        with build_opener(ProxyHandler({})).open(f'http://127.0.0.1:{port}/api/health', timeout=2) as response:
            result = json.loads(response.read(4096))
        if not isinstance(result, dict) or result.get('app') != 'gift_registry' or result.get('edition') != EDITION:
            raise StartupError(f'端口 {port} 被其他服务占用，请检查后再启动。')
        return result
    except (HTTPError, ValueError):
        raise StartupError(f'端口 {port} 的响应不是礼赠工作台，请检查端口占用。') from None
    except (URLError, TimeoutError, ConnectionError):
        return None


def queue_is_locked(directory):
    """只尝试现有锁文件的字节锁，不创建队列、不恢复或重写历史。"""
    try:
        stream = (Path(directory) / '.queue.lock').open('r+b')
    except FileNotFoundError:
        return False
    except OSError:
        raise StartupError('无法检查任务目录，请检查文件权限；未改动任务历史。') from None
    with stream:
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
    return False


def failure_message(raw):
    # 不展示 traceback、路径、账号配置或异常的任意参数。
    for marker, message in (
        ('任务目录已被其他进程占用', '旧工作台仍占用任务目录，请等待其正常退出。'),
        ('局域网访问凭据无法', '局域网访问凭据无法解密，请使用原 Windows 用户启动并检查本机配置。'),
        ('局域网配置无法读取', '局域网配置无法读取，请检查 network.local.json。'),
        ('共享配置不同', '工作台已运行，但共享配置已变化；请正常关闭旧工作台后重开。'),
        ('端口', '工作台端口不可用，请检查端口是否被其他程序占用。'),
        ('表格存档', '草稿文件无法读取，请保留文件并检查格式；不要删除草稿或任务历史。'),
        ('ModuleNotFoundError', 'Python 环境缺少依赖，请检查本项目的 .venv 环境。'),
    ):
        if marker in raw:
            return message
    kinds = re.findall(r'^([A-Za-z][A-Za-z0-9_]*(?:Error|Exception)):', raw, re.MULTILINE)
    kind = kinds[-1] if kinds else '启动进程异常退出'
    return f'工作台启动失败（{kind}）。请在项目目录运行 .venv\\Scripts\\python.exe main.py --no-open 查看原因。'


def start_child(port, root):
    return subprocess.Popen([sys.executable, str(root / 'main.py'), '--no-open', '--port', str(port)],
                            cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def supervise(port, notify, cancelled, *, root=ROOT, health=read_health,
              locked=queue_is_locked, spawn=start_child, settings=load_network):
    child = None
    previous = None
    try:
        while not cancelled.is_set():
            current = health(port)
            if current is not None and not current.get('stopping'):
                expected = settings()
                if not expected.matches_health(current):
                    raise StartupError('工作台已运行，但共享配置已变化；请正常关闭旧工作台后重开。')
                notify('ready', f'http://127.0.0.1:{port}/')
                return
            busy = locked(root / 'data' / 'registration_jobs')
            if child is not None and child.poll() is not None:
                raw = child.communicate()[1].decode('utf-8', errors='replace')
                child = None
                if not busy:
                    raise StartupError(failure_message(raw))
            if current is not None or busy:
                message = '上一工作台仍在完成已接收的订单或关闭浏览器。\n正常退出后会自动打开，无需重复双击，也不会重提订单。'
            else:
                if child is None:
                    child = spawn(port, root)
                message = '正在启动工作台，等待服务就绪……'
            if message != previous:
                notify('waiting', message)
                previous = message
            cancelled.wait(1)
    finally:
        if child is not None and child.stderr is not None:
            child.stderr.close()
        # 关闭等待窗口只取消等待，不终止任何工作台或业务浏览器。


def run(port=DEFAULT_PORT):
    import tkinter as tk
    from tkinter import ttk

    window = tk.Tk()
    window.title(APP_NAME + ' · 启动')
    window.geometry('570x245')
    window.resizable(False, False)
    frame = ttk.Frame(window, padding=24)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='正在打开' + APP_NAME, font=('Microsoft YaHei UI', 16, 'bold')).pack(anchor='w')
    message = tk.StringVar(value='正在检查运行状态……')
    ttk.Label(frame, textvariable=message, wraplength=520, justify='left',
              font=('Microsoft YaHei UI', 10)).pack(anchor='w', pady=18)
    progress = ttk.Progressbar(frame, mode='indeterminate')
    progress.pack(fill='x')
    progress.start()
    cancelled = threading.Event()
    events = queue.Queue()
    timer = None

    def close():
        cancelled.set()
        progress.stop()
        if timer is not None:
            window.after_cancel(timer)
        window.destroy()

    ttk.Button(frame, text='停止等待', command=close).pack(anchor='e', pady=12)
    window.protocol('WM_DELETE_WINDOW', close)

    def work():
        try:
            supervise(port, lambda kind, value: events.put((kind, value)), cancelled)
        except StartupError as exc:
            events.put(('error', str(exc)))
        except Exception as exc:
            events.put(('error', f'启动检查失败（{type(exc).__name__}），请检查本机环境后重试。'))

    def tick():
        nonlocal timer
        try:
            while True:
                kind, value = events.get_nowait()
                if kind == 'ready':
                    try:
                        opened = webbrowser.open(value)
                    except Exception:
                        opened = False
                    if opened:
                        close()
                        return
                    progress.stop()
                    message.set('服务已启动，请在浏览器打开：\n' + value)
                else:
                    message.set(value)
                    if kind == 'error':
                        progress.stop()
        except queue.Empty:
            pass
        timer = window.after(150, tick)

    threading.Thread(target=work, name='workbench-launch', daemon=True).start()
    timer = window.after(150, tick)
    window.mainloop()
