"""所有浏览器操作均经 Python Selenium WebDriver 执行。"""

import json
import os
import time
from copy import deepcopy
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException

from .authentication import AuthenticationRequired, ENTRY_URLS, SessionAuthenticator
from .config import DATA_DIR, Site
from .driver_shutdown import BrowserShutdownError, configure_transport, quit_owned_driver, track_owned_browser
from .sessions import SessionStore, cookie_for_restore


def create_driver(site: Site, *, headless=False, profile=None):
    os.environ.setdefault("SE_CACHE_PATH", str(DATA_DIR / "selenium"))
    os.environ.setdefault("SE_AVOID_STATS", "true")
    options = webdriver.ChromeOptions()
    options.page_load_strategy = "eager"
    profile = profile or DATA_DIR / "browser" / site.key
    profile.mkdir(parents=True, exist_ok=True)
    options.add_argument(f"--user-data-dir={profile.resolve()}")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-notifications")
    options.add_argument("--window-size=1250,850")
    options.add_experimental_option("prefs", {
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
    })
    if headless:
        options.add_argument("--headless=new")
    # 不修改浏览器指纹，不绕过验证码；Selenium Manager 自动匹配驱动。
    driver = webdriver.Chrome(options=options)
    track_owned_browser(driver)
    configure_transport(driver, 50)
    driver.set_page_load_timeout(40)
    driver.set_script_timeout(10)
    return driver


class SiteBrowser:
    def __init__(self, site: Site, store: SessionStore, *, authenticator=None):
        self.site = site
        self.store = store
        self.driver = None
        self.snapshot = None
        self.confirmed = False
        self.last_saved = ""
        self.restore_warning = ""
        self.restore_script = None
        self.authenticator = (None if authenticator is False else authenticator) if authenticator is not None else (
            SessionAuthenticator(site) if site.key in ENTRY_URLS else None)
        self.authentication_cancelled = None
        self.auth_warning = ""
        self.auto_logged_in = False

    def navigate(self, url):
        try:
            self.driver.get(url)
        except TimeoutException:
            # 页面可能仍可交互，留给用户继续登录，不中止另一个站点。
            pass

    def open(self):
        try:
            self.snapshot = self.store.load(self.site.key)
        except (OSError, ValueError):
            self.restore_warning = "旧快照无法读取，请重新登录；独立浏览器配置仍会保留。"
        self.driver = create_driver(self.site)
        if self.snapshot:
            self.restore(self.snapshot)
        self.navigate(ENTRY_URLS.get(self.site.key, self.site.url))
        try:
            self.ensure_authenticated()
        except AuthenticationRequired as exc:
            # 验证码或登录失败时保留可见窗口，不能作为浏览器启动失败关闭它。
            self.auth_warning = str(exc)

    def ensure_authenticated(self, *, target_url=None):
        if self.authenticator is None:
            return False
        previous_timeout = None
        if isinstance(self.driver, webdriver.Remote):
            previous_timeout = configure_transport(self.driver, 10)
        try:
            recovered = self.authenticator.ensure_authenticated(
                self.driver, target_url=target_url, cancelled=self.authentication_cancelled)
            self.auth_warning = ""
            if recovered:
                self.confirmed = True
                self.auto_logged_in = True
                if self.restore_script:
                    try:
                        self.driver.execute_cdp_cmd("Page.removeScriptToEvaluateOnNewDocument",
                                                    {"identifier": self.restore_script})
                        self.restore_script = None
                    except Exception:
                        self.restore_warning = "自动登录已恢复，旧会话恢复脚本未能移除，请确认本次登录状态。"
                try:
                    self.capture(deadline=time.monotonic() + 5)
                except Exception:
                    self.restore_warning = "自动登录已恢复，本次会话快照未能更新；已保留上次完整快照。"
            return recovered
        finally:
            if previous_timeout is not None:
                configure_transport(self.driver, previous_timeout)

    def restore(self, snapshot):
        # 保留浏览器内较新的 Cookie，只补回缺失的 Cookie（包括会话 Cookie）。
        existing = self.driver.execute_cdp_cmd("Storage.getCookies", {})["cookies"]
        identity = lambda item: (item["name"], item["domain"], item.get("path", "/"))
        keys = {identity(item) for item in existing}
        rejected = 0
        for cookie in snapshot.get("cookies", []):
            normalized = cookie_for_restore(cookie, self.site)
            if not normalized or identity(normalized) in keys:
                continue
            try:
                result = self.driver.execute_cdp_cmd("Network.setCookie", normalized)
                rejected += not result.get("success", True)
            except WebDriverException:
                rejected += 1
        # 首次页面脚本执行前恢复存储；每个 origin / tab 仅运行一次，随后移除。
        origins = {key: value for key, value in snapshot.get("origins", {}).items()
                   if self.site.allows_url(key)}
        script = """
        (() => {
          const states = %s;
          const state = states[location.origin];
          if (!state) return;
          const marker = '__gift_session_restored_v1';
          if (sessionStorage.getItem(marker)) return;
          for (const [k,v] of Object.entries(state.local || {})) {
            if (localStorage.getItem(k) === null) localStorage.setItem(k,v);
          }
          for (const [k,v] of Object.entries(state.session || {})) {
            sessionStorage.setItem(k,v);
          }
          sessionStorage.setItem(marker, '1');
        })();
        """ % json.dumps(origins, ensure_ascii=True)
        self.restore_script = self.driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument", {"source": script})["identifier"]
        if rejected:
            self.restore_warning = f"{rejected} 个 Cookie 未能恢复，请核对页面登录状态。"

    def capture(self, *, deadline=None):
        driver = self.driver

        def remaining():
            if deadline is not None:
                seconds = deadline - time.monotonic()
                if seconds <= 0:
                    raise TimeoutError("会话保存等待已达上限")
                configure_transport(driver, min(3, seconds))

        # 正常流程只操作一个标签页，不来回切换或抢焦点。
        remaining()
        url = driver.current_url
        if not self.site.allows_url(url):
            return None
        remaining()
        if self.authenticator is not None and not self.authenticator.is_authenticated(driver):
            return None  # 登录页或验证码阶段不能覆盖上次完整业务会话。
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        remaining()
        state = driver.execute_script("""
            return {local: Object.fromEntries(Object.entries(localStorage)),
                    session: Object.fromEntries(Object.entries(sessionStorage))};
        """)
        state["session"].pop("__gift_session_restored_v1", None)
        remaining()
        cookies = [item for item in driver.execute_cdp_cmd("Storage.getCookies", {})["cookies"]
                   if self.site.allows_host(item.get("domain", ""))]
        snapshot = deepcopy(self.snapshot) if self.snapshot else {"version": 1, "site": self.site.key, "origins": {}}
        snapshot["origins"][origin] = state
        snapshot.update(cookies=cookies, saved_at=datetime.now().astimezone().isoformat(), url=url)
        remaining()
        self.store.save(self.site.key, snapshot)
        self.snapshot = snapshot
        self.last_saved = datetime.now().strftime("%H:%M:%S")
        if self.auto_logged_in:
            self.restore_warning = ""  # 自动恢复后完整快照已成功保存，清除过时的恢复警告。
        return {"url": urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")),
                "cookie_count": len(cookies), "last_saved": self.last_saved}

    def looks_like_login(self):
        # 仅用于阻止在明确的登录页误点确认，不把“没有密码框”当成登录成功。
        url = self.driver.current_url.lower()
        if any(word in urlsplit(url).path + urlsplit(url).fragment
               for word in ("login", "signin", "sign-in", "passport")):
            return True
        return self.driver.execute_script("""
            return [...document.querySelectorAll('input[type="password"]')]
              .some(e => e.getClientRects().length && !e.disabled);
        """)

    def save(self, confirm=False):
        try:
            self.ensure_authenticated()
        except AuthenticationRequired as exc:
            self.confirmed = False
            self.auto_logged_in = False
            self.auth_warning = str(exc)
            return {"state": self.auth_warning, "warning": self.auth_warning, "ready": True,
                    "confirmed": False, "last_saved": self.last_saved,
                    "cookie_count": len((self.snapshot or {}).get("cookies", []))}
        details = self.capture()
        if details is None:
            return {"state": "当前页面不属于此站点，请切回对应页面", "ready": True}
        login_page = self.looks_like_login()
        if login_page:
            self.confirmed = False
            self.auto_logged_in = False
        if confirm and not login_page:
            self.confirmed = True
            self.auto_logged_in = False
            if self.restore_script:
                self.driver.execute_cdp_cmd("Page.removeScriptToEvaluateOnNewDocument",
                                            {"identifier": self.restore_script})
                self.restore_script = None
        if login_page:
            state = "等待你手动登录" if not confirm else "仍在登录页面，请先完成登录 / 验证"
        elif self.auto_logged_in:
            state = "登录已恢复，会话已保存"
        elif self.confirmed:
            state = "你已确认登录，会话已保存"
        else:
            state = "已保存当前会话；登录完成后请点击确认"
        return {"state": state, "confirmed": self.confirmed, "ready": True,
                "warning": self.restore_warning, **details}

    def close(self, *, progress=None):
        if self.driver is None:
            return {"closed": True, "forced": False, "warning": ""}

        def notify(message):
            if callable(progress):
                try:
                    progress(message)
                except Exception:
                    pass  # 界面或状态文件通知不能阻断浏览器清理。

        warning = ""
        notify("正在保存会话")
        try:
            if self.capture(deadline=time.monotonic() + 5) is None:
                warning = "本次未更新会话，已保留已有加密快照"
        except Exception:
            # 保留上一次完整快照；连接异常不一定属于 WebDriverException。
            warning = "未能更新会话，已保留上次加密快照"
        notify("正在关闭浏览器")
        try:
            result = quit_owned_driver(self.driver)
        except BrowserShutdownError:
            raise
        except Exception:
            raise BrowserShutdownError("浏览器关闭失败，请手动关闭此工具的平台窗口后重试") from None
        if not result.get("closed"):
            raise BrowserShutdownError("浏览器尚未关闭，请手动关闭此工具的平台窗口后重试")
        self.driver = None
        if result.get("forced"):
            warning = "；".join(filter(None, (warning, "浏览器退出超时，已关闭本工具创建的浏览器，下次请确认登录状态")))
        return {**result, "warning": warning}
