"""业务浏览器独占现有登录配置；会话恢复和加密保存沿用 SiteBrowser。"""

import os
from pathlib import Path
from threading import RLock

from selenium.webdriver.remote.webdriver import WebDriver

from .browser import SiteBrowser
from .config import DATA_DIR, SITES
from .sessions import SessionStore
from .workflow import PlatformOperationError


class ProfileLock:
    """与登录助手共用 app.lock 的第一个字节，不删除锁文件。"""

    def __init__(self, path, *, backend=None):
        self.path = Path(path)
        self.backend = backend
        self.file = None

    def acquire(self):
        if self.file is not None:
            return
        backend = self.backend
        if backend is None:
            if os.name != "nt":
                raise PlatformOperationError("业务浏览器及加密登录配置需要在 Windows 上运行")
            import msvcrt
            backend = msvcrt
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            backend.locking(handle.fileno(), backend.LK_NBLCK, 1)
        except OSError:
            handle.close()
            raise PlatformOperationError(
                "登录配置正在被登录助手或另一业务进程使用，请在登录助手点击“保存并关闭浏览器”，待窗口关闭后再点击开始登记") from None
        self.backend = backend
        self.file = handle

    def release(self):
        handle, self.file = self.file, None
        if handle is None:
            return
        try:
            handle.seek(0)
            self.backend.locking(handle.fileno(), self.backend.LK_UNLCK, 1)
        finally:
            handle.close()


class BusinessBrowsers:
    """只由串行业务工作线程使用；所有任务停止后由调用方 close()。

    不连接调试端口、不接管登录助手的正在使用的浏览器。依赖可注入，测试
    无需启动 Chrome、读取真实会话或写入用户 data 目录。
    """

    def __init__(self, *, data_dir=DATA_DIR, browser_factory=SiteBrowser,
                 store_factory=SessionStore, lock_factory=ProfileLock, sites=SITES):
        self.data_dir = Path(data_dir)
        # SiteBrowser 的默认 Chrome 配置位置固定在 DATA_DIR。自定义目录若
        # 仍使用默认工厂，会只锁测试目录却打开真实配置，必须直接拒绝。
        if self.data_dir.resolve() != DATA_DIR.resolve() and browser_factory is SiteBrowser:
            raise PlatformOperationError("自定义业务数据目录必须提供隔离的浏览器工厂")
        self.sites = {site.key: site for site in sites}
        self.browser_factory = browser_factory
        self.store = store_factory(self.data_dir / "sessions")
        self.lock = lock_factory(self.data_dir / "app.lock")
        self.browsers = {}
        self._pending_cleanup = {}
        self._owns_profiles = False
        self._start_reserved = False
        self._closed = False
        self._guard = RLock()

    def _acquire_profiles(self):
        try:
            if not self._owns_profiles:
                self.lock.acquire()
                self._owns_profiles = True
        except PlatformOperationError:
            raise
        except Exception:
            raise PlatformOperationError("无法取得登录配置使用权，请检查本机数据目录权限") from None

    def reserve_for_start(self):
        """入队前保留配置锁，不启动浏览器；同一所有者可反复调用。"""
        with self._guard:
            if self._closed:
                raise PlatformOperationError("业务浏览器已关闭，请重新启动工作台")
            if self._pending_cleanup:
                raise PlatformOperationError("业务浏览器关闭尚未完成，请先关闭本工具浏览器并重试关闭工作台")
            self._acquire_profiles()
            self._start_reserved = True

    def release_unused_reservation(self):
        """由队列在确认没有待执行或执行中任务后释放未使用的预留。

        队列调用此方法时必须阻止并发开始请求；浏览器一旦打开，仍保留其
        原有配置所有权，直到正常 close()，不能仅因一批任务结束就解锁。
        """
        with self._guard:
            if not self._start_reserved or self.browsers or self._pending_cleanup:
                return False
            self._release_profiles()
            return True

    def get(self, site_key):
        with self._guard:
            if self._closed:
                raise PlatformOperationError("业务浏览器已关闭，请重新启动工作台")
            if self._pending_cleanup:
                raise PlatformOperationError("业务浏览器关闭尚未完成，请先关闭本工具浏览器并重试关闭工作台")
            if site_key not in self.sites:
                raise PlatformOperationError("业务浏览器站点未配置")
            if site_key in self.browsers:
                return self.browsers[site_key]
            self._acquire_profiles()

            browser = None
            try:
                browser = self.browser_factory(self.sites[site_key], self.store)
                browser.open()
            except Exception:
                if browser is not None and not self._close_browser(browser):
                    self._pending_cleanup[site_key] = browser
                if not self.browsers and not self._pending_cleanup and not self._start_reserved:
                    self._release_profiles()
                if self._pending_cleanup:
                    raise PlatformOperationError(
                        "业务浏览器启动失败且关闭未完成，请先关闭本工具浏览器并重试关闭工作台") from None
                # 驱动异常可能含 URL、凭据或页面数据，不透传异常正文。
                raise PlatformOperationError(
                    "业务浏览器启动失败，请关闭占用该平台配置的 Chrome，再检查 Chrome 和登录状态") from None
            self.browsers[site_key] = browser
            return browser

    def driver(self, site_key):
        return self.get(site_key).driver

    @staticmethod
    def _close_browser(browser):
        try:
            result = browser.close()
        except Exception:
            # 真实 SiteBrowser 已有有界清理，失败必须保留对象和锁供明确重试。
            if isinstance(browser, SiteBrowser):
                return False
            # 兼容旧的注入式测试/工厂；不能让真实浏览器退回长等待的原生 quit。
            try:
                driver = browser.driver
                if isinstance(driver, WebDriver):
                    return False
                if driver is not None:
                    driver.quit()
                return True
            except Exception:
                return False
        if isinstance(browser, SiteBrowser):
            return isinstance(result, dict) and result.get("closed") is True
        return not isinstance(result, dict) or result.get("closed") is True

    def _release_profiles(self):
        self._start_reserved = False
        if self._owns_profiles:
            self._owns_profiles = False
            try:
                self.lock.release()
            except OSError:
                # ProfileLock 即使解锁报错也会关闭句柄，由 Windows 释放字节锁。
                pass

    def close(self):
        with self._guard:
            if self._closed:
                return
            closing = {**self.browsers, **self._pending_cleanup}
            self.browsers.clear()
            self._pending_cleanup.clear()
            for key, browser in closing.items():
                if not self._close_browser(browser):
                    self._pending_cleanup[key] = browser
            if self._pending_cleanup:
                raise PlatformOperationError(
                    "部分业务浏览器关闭未完成，登录配置仍保留，请关闭本工具浏览器并重试关闭工作台")
            self._closed = True
            self._release_profiles()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
