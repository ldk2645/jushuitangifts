"""有界关闭本实例的 Selenium 驱动；不查找或终止其它 Chrome 进程。"""

import ctypes
import math
import os
import subprocess
from dataclasses import dataclass

from selenium.webdriver.remote.command import Command


TRANSPORT_TIMEOUT = 3
PROCESS_WAIT = 2
_STATE_ATTRIBUTE = "_gift_owned_browser_state"


class BrowserShutdownError(RuntimeError):
    """仅含固定可展示文案；失败时调用方仍须保留浏览器与配置所有权。"""


class _WindowsProcessHandle:
    """在浏览器创建后持有 HANDLE，防止退出时 PID 已被其他进程复用。"""

    def __init__(self, pid):
        from ctypes import wintypes

        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel.OpenProcess.restype = wintypes.HANDLE
        self._kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._kernel.WaitForSingleObject.restype = wintypes.DWORD
        self._kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._kernel.TerminateProcess.restype = wintypes.BOOL
        self._kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel.CloseHandle.restype = wintypes.BOOL
        # SYNCHRONIZE | PROCESS_TERMINATE；不请求读取浏览器内存的权限。
        self._handle = self._kernel.OpenProcess(0x00100000 | 0x0001, False, pid)
        if not self._handle:
            raise OSError("unable to retain owned process")

    def wait(self, timeout):
        if self._handle is None:
            raise OSError("owned process handle already closed")
        result = self._kernel.WaitForSingleObject(self._handle, int(timeout * 1000))
        if result == 0:
            return True
        if result == 258:
            return False
        raise OSError("unable to check owned process")

    def terminate(self):
        if not self._kernel.TerminateProcess(self._handle, 1) and not self.wait(0):
            raise OSError("unable to stop owned process")

    def close(self):
        if self._handle is not None:
            if not self._kernel.CloseHandle(self._handle):
                raise OSError("unable to release owned process handle")
            self._handle = None

    def __del__(self):
        # 一些开发/验收调用方直接执行原生 driver.quit()；GC 只回收 HANDLE，
        # 不终止 Chrome，也不根据 PID 执行任何操作。
        try:
            self.close()
        except Exception:
            pass


@dataclass
class _OwnedBrowserState:
    handle: object = None
    quit_confirmed: bool = False
    chrome_closed: bool = False
    forced: bool = False
    finished: bool = False


def _open_owned_process(pid):
    if os.name != "nt":
        return None
    return _WindowsProcessHandle(pid)


def configure_transport(driver, timeout):
    """只修改此连接的超时与重试设置，返回原超时；应由驱动唯一使用者调用。

    page_load/script 超时不约束 current_url、CDP 和 DELETE session。清空此
    连接的旧 urllib3 池后设置 retries=False，保证旧池也不继续自动重试。
    """
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 0 < timeout <= 120):
        raise ValueError("浏览器连接超时必须为 0 到 120 秒之间的有限数值")
    try:
        executor = driver.command_executor
        config = executor.client_config
        previous = config.timeout
        # 复制两层配置，避免注入的调用方与不同 driver 共享嵌套字典。
        arguments = dict(config.init_args_for_pool_manager or {})
        pool_arguments = dict(arguments.get("init_args_for_pool_manager", {}))
        pool_arguments["retries"] = False
        arguments["init_args_for_pool_manager"] = pool_arguments
        config.init_args_for_pool_manager = arguments
        config.timeout = timeout
        manager = getattr(executor, "_conn", None)
        if manager is not None:
            manager.clear()
            manager.connection_pool_kw["retries"] = False
        return previous
    except Exception:
        raise BrowserShutdownError("浏览器连接设置未完成，请保留此助手并检查本工具浏览器") from None


def track_owned_browser(driver):
    """仅在刚创建自有 driver 后调用；句柄取得失败不伪造所有权证明。"""
    existing = getattr(driver, _STATE_ATTRIBUTE, None)
    if isinstance(existing, _OwnedBrowserState):
        return
    state = _OwnedBrowserState()
    try:
        pid = driver.capabilities.get("goog:processID")
        if type(pid) is int and pid > 0:
            state.handle = _open_owned_process(pid)
    except Exception:
        # 正常 QUIT 仍可完成关闭；失败时不得改用晚获取的 PID 终止进程。
        pass
    setattr(driver, _STATE_ATTRIBUTE, state)


def _wait_owned(handle, timeout):
    try:
        return handle.wait(timeout) is True
    except Exception:
        return False


def _stop_service(driver):
    """仅处理 driver 持有的自有 Popen，避免 Service.stop 的多段长等待。"""
    try:
        service = getattr(driver, "service", None)
        process = getattr(service, "process", None)
        if process is None:
            return True
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=PROCESS_WAIT)
            except (subprocess.TimeoutExpired, OSError):
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=PROCESS_WAIT)
        return process.poll() is not None
    except Exception:
        return False


def _close_transport(driver):
    try:
        driver.command_executor.close()
    except Exception:
        # OS 进程已退出时，连接池清理异常不影响配置所有权是否可释放的事实。
        pass


def quit_owned_driver(driver):
    """关闭自有会话；不能确认关闭时抛异常，并保留 driver/句柄供人工重试。

    明确成功的 QUIT 加已退出的自有驱动服务可作为正常关闭证据。异常回退
    仅使用创建时保留的 Chrome HANDLE，不根据当前 PID 查找/终止进程。
    forced 表示曾强制终止自有 Chrome，不包括驱动服务的正常清理。
    """
    state = getattr(driver, _STATE_ATTRIBUTE, None)
    if not isinstance(state, _OwnedBrowserState):
        # 关闭时不再尝试打开 PID；该 PID 此时可能已经复用。
        state = _OwnedBrowserState()
        setattr(driver, _STATE_ATTRIBUTE, state)
    if state.finished:
        return {"closed": True, "forced": state.forced}

    if not state.chrome_closed:
        if state.handle is not None and _wait_owned(state.handle, 0):
            state.chrome_closed = True
        else:
            try:
                configure_transport(driver, TRANSPORT_TIMEOUT)
                driver.execute(Command.QUIT)
                state.quit_confirmed = True
            except Exception:
                # Transport 异常正文可能含敏感内容，不记录、不透传。
                pass
            if state.handle is not None:
                if not _wait_owned(state.handle, PROCESS_WAIT):
                    try:
                        state.handle.terminate()
                        state.forced = True
                    except Exception:
                        pass
                state.chrome_closed = _wait_owned(state.handle, PROCESS_WAIT)
            elif state.quit_confirmed:
                state.chrome_closed = True

    if not state.chrome_closed:
        # 未确认关闭时保留服务和连接，助手中的明确“重试关闭”仍可发出 QUIT。
        raise BrowserShutdownError("未能确认本工具的 Chrome 已关闭，登录配置仍保留，请关闭本工具浏览器后重试")

    service_closed = _stop_service(driver)
    _close_transport(driver)
    if state.chrome_closed and state.handle is not None:
        try:
            state.handle.close()
        except Exception:
            pass
        else:
            state.handle = None

    if not service_closed:
        # 保留 chrome_closed，避免下一次因 session 已删除而误判浏览器未退出。
        raise BrowserShutdownError("本工具浏览器已关闭，但驱动尚未退出，登录配置仍保留，请稍后重试关闭")
    if state.handle is not None:
        raise BrowserShutdownError("本工具浏览器已关闭，但关闭资源尚未清理完成，请保留助手并重试关闭")
    state.finished = True
    driver.session_id = None
    return {"closed": True, "forced": state.forced}
