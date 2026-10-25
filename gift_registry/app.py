import json
import msvcrt
import queue
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from tkinter import messagebox, ttk

from selenium.common.exceptions import InvalidSessionIdException, NoSuchWindowException, WebDriverException

from .browser import SiteBrowser
from .config import DATA_DIR, SAVE_INTERVAL, SITES
from .driver_shutdown import configure_transport
from .sessions import SessionStore, atomic_write


def close_login_browsers(browsers, publish):
    """每站独立关闭；只返回仍需人工处理的浏览器，不能据超时假定关闭。"""
    pending = {}

    def close_one(key, browser):
        name = browser.site.name

        def progress(stage):
            publish(key, {"state": f"{name}：{stage}", "ready": False, "warning": ""})

        progress("准备保存会话并关闭浏览器")
        result = browser.close(progress=progress)
        if not isinstance(result, dict) or result.get("closed") is not True:
            raise RuntimeError("browser shutdown not confirmed")
        return result

    with ThreadPoolExecutor(max_workers=max(1, len(browsers)), thread_name_prefix="login-close") as executor:
        futures = {executor.submit(close_one, key, browser): key for key, browser in browsers.items()}
        for future in as_completed(futures):
            key = futures[future]
            name = browsers[key].site.name
            try:
                result = future.result()
            except Exception:
                pending[key] = browsers[key]
                publish(key, {"state": f"{name}：关闭未完成", "ready": False,
                              "warning": "请手动关闭该平台的 Chrome 窗口，再点击“重试关闭浏览器”；登录配置仍受保护。"})
            else:
                warning = "最后一次会话读取未完成，已保留之前的加密快照。" if result.get("warning") else ""
                if result.get("forced"):
                    warning = (warning + " 浏览器退出超时，已完成本工具自有浏览器清理。").strip()
                publish(key, {"state": f"{name}：浏览器已关闭，会话已保留", "ready": False,
                              "warning": warning})
    return pending


class LoginApp:
    def __init__(self, root):
        self.root = root
        self.commands = queue.Queue()
        self.events = queue.Queue()
        self.stopping = False
        self.shutdown_failed = False
        self.rows = {}
        root.title("礼赠自动登记 · 登录助手")
        root.geometry("720x530")
        root.minsize(700, 510)
        style = ttk.Style()
        style.configure("TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", font=("Microsoft YaHei UI", 10))
        container = ttk.Frame(root, padding=22)
        container.pack(fill="both", expand=True)
        ttk.Label(container, text="聚水潭 / 班牛 · 初始登录", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        ttk.Label(container, text="会话过期时尝试已配置账号自动登录；验证码或二次验证需你完成。\n每 5 秒保存会话；完成后点击“保存并关闭浏览器”，再回工作台登记。", padding=(0, 12)).pack(anchor="w")
        for site in SITES:
            frame = ttk.LabelFrame(container, text=site.name, padding=12)
            frame.pack(fill="x", pady=7)
            state = tk.StringVar(value="正在启动浏览器，首次可能需要下载驱动……")
            detail = tk.StringVar(value=site.url)
            ttk.Label(frame, textvariable=state, wraplength=650).pack(anchor="w")
            ttk.Label(frame, textvariable=detail, foreground="#666666", wraplength=650).pack(anchor="w", pady=(3, 8))
            button = ttk.Button(frame, text="我已登录，确认并保存", state="disabled",
                                command=lambda key=site.key: self.commands.put(("confirm", key)))
            button.pack(anchor="e")
            self.rows[site.key] = (state, detail, button)
        footer = ttk.Frame(container)
        footer.pack(fill="x", pady=(12, 0))
        ttk.Label(footer, text="会话保存在本机，使用当前 Windows 用户加密。", foreground="#666666").pack(side="left")
        self.exit_button = ttk.Button(footer, text="保存并关闭浏览器", command=self.stop)
        self.exit_button.pack(side="right")
        root.protocol("WM_DELETE_WINDOW", self.stop)
        threading.Thread(target=self.worker, daemon=True).start()
        root.after(200, self.receive)

    def stop(self):
        if not self.stopping:
            self.stopping = True
            self.exit_button.configure(state="disabled", text="正在准备关闭浏览器……")
            for _, _, button in self.rows.values():
                button.configure(state="disabled")
            self.commands.put(("stop", None))
        elif self.shutdown_failed:
            self.shutdown_failed = False
            self.exit_button.configure(state="disabled", text="正在重试关闭浏览器……")
            self.commands.put(("retry_close", None))

    def receive(self):
        try:
            while True:
                key, result = self.events.get_nowait()
                if key == "stopped":
                    self.root.destroy()
                    return
                if key == "shutdown_failed":
                    self.shutdown_failed = True
                    self.exit_button.configure(state="normal", text="重试关闭浏览器")
                    continue
                if key == "shutdown_retrying":
                    self.shutdown_failed = False
                    self.exit_button.configure(state="disabled", text="正在关闭浏览器……")
                    continue
                state, detail, button = self.rows[key]
                state.set(result["state"])
                info = f"Cookie：{result.get('cookie_count', 0)} 个　最近保存：{result.get('last_saved', '—')}"
                detail.set(result.get("warning") or info)
                if not self.stopping:
                    button.configure(state="normal" if result.get("ready") else "disabled")
        except queue.Empty:
            pass
        self.root.after(200, self.receive)

    def worker(self, *, browser_factory=SiteBrowser, store_factory=SessionStore, sites=SITES,
               status_path=None, status_writer=atomic_write):
        browsers = {}
        states = {}
        state_guard = threading.RLock()
        store = store_factory(DATA_DIR / "sessions")
        status_path = status_path if status_path is not None else DATA_DIR / "status.json"

        def write_status(running):
            # 状态写入失败不能中断关闭或阻止 stopped 事件；异常正文可能含私有数据。
            try:
                status_writer(status_path, json.dumps(
                    {"running": running, "closing": self.stopping and running, "sites": states},
                    ensure_ascii=False, indent=2).encode("utf-8"))
                return True
            except Exception:
                return False

        def publish(key, state):
            with state_guard:
                states[key] = {**states.get(key, {}), **state}
                result = dict(states[key])
                if not write_status(True):
                    result["warning"] = (result.get("warning", "") + " 运行状态文件暂时无法更新；关闭流程仍会继续。").strip()
                self.events.put((key, result))

        def save(key, confirm=False):
            if self.stopping:
                return
            try:
                configure_transport(browsers[key].driver, 5)
                publish(key, browsers[key].save(confirm))
            except (InvalidSessionIdException, NoSuchWindowException):
                publish(key, {"state": "浏览器已关闭；已保留上次快照，请重新启动工具", "ready": False})
            except WebDriverException:
                publish(key, {"state": "页面暂不可读取，稍后自动重试", "ready": True})
            except Exception:
                publish(key, {"state": "本次会话读取或保存未完成，已保留之前的加密快照", "ready": True})

        try:
            for site in sites:
                if self.stopping:
                    break
                try:
                    browser = browser_factory(site, store)
                    browsers[site.key] = browser
                    if isinstance(browser, SiteBrowser):
                        browser.authentication_cancelled = lambda: self.stopping
                    browser.open()
                    save(site.key)
                except Exception:
                    # 不输出异常正文，防止驱动错误包含会话详情。
                    # 统一交由关闭阶段清理；这里异常不能跳过下一站或丢失自有驱动。
                    publish(site.key, {"state": "浏览器启动未完成，请关闭助手后重新启动", "ready": False})
            next_save = time.monotonic() + SAVE_INTERVAL
            while not self.stopping:
                try:
                    command, key = self.commands.get(timeout=0.5)
                    if command == "stop":
                        self.stopping = True
                        break
                    if not self.stopping and command == "confirm" and browsers.get(key) and browsers[key].driver:
                        save(key, confirm=True)
                except queue.Empty:
                    pass
                if time.monotonic() >= next_save:
                    for key, browser in browsers.items():
                        if self.stopping:
                            break
                        if browser.driver:
                            save(key)
                    next_save = time.monotonic() + SAVE_INTERVAL
        except Exception:
            for key in browsers:
                publish(key, {"state": "登录助手运行异常，正在安全关闭自有浏览器", "ready": False})
        finally:
            self.stopping = True
            pending = dict(browsers)
            while pending:
                try:
                    pending = close_login_browsers(pending, publish)
                except Exception:
                    # 清理调度异常也必须保留对象和配置锁，不能把仍运行的 Chrome 当作已退出。
                    for key in pending:
                        publish(key, {"state": "浏览器关闭未完成，请手动关闭窗口后重试", "ready": False})
                if pending:
                    self.events.put(("shutdown_failed", {}))
                    while True:
                        command, _ = self.commands.get()
                        if command in ("retry_close", "stop"):
                            self.events.put(("shutdown_retrying", {}))
                            break
            with state_guard:
                write_status(False)
            self.events.put(("stopped", {}))


def run():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Windows 字节锁会在进程退出时自动释放，避免两个助手争用同一 Profile。
    lock = (DATA_DIR / "app.lock").open("a+b")
    if lock.tell() == 0:
        lock.write(b"0")
        lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("登录配置正在使用", "登录助手或业务浏览器正在使用此配置。\n如登录助手已打开，请使用原窗口；如业务浏览器正在运行，请先完成任务并关闭对应工作台进程。")
        root.destroy()
        lock.close()
        return
    try:
        root = tk.Tk()
        LoginApp(root)
        root.mainloop()
    finally:
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        lock.close()
