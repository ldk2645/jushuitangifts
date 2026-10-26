"""按实际登录页结构恢复会话；仅登录，不调用或重试礼赠提交。"""

import math
import re
import time
from urllib.parse import urlsplit

from selenium.common.exceptions import (NoSuchElementException, StaleElementReferenceException,
                                        InvalidSessionIdException, NoSuchWindowException)
from selenium.webdriver.common.by import By

from .credentials import CredentialStore
from .workflow import PlatformOperationError


ENTRY_URLS = {
    "jushuitan": "https://dopre.jushuitan.com/app/order/order/list.aspx",
    "banniu": "https://work.bytenew.com/app.html#/applet/appNew/projectNew/54119/1883",
}
_HOSTS = {"jushuitan": "dopre.jushuitan.com", "banniu": "work.bytenew.com"}
_LOGIN_PATHS = {"jushuitan": "/login.aspx", "banniu": "/app.html"}
_NAMES = {"jushuitan": "聚水潭", "banniu": "班牛"}
_PASSWORD_AGE_NOTICE = "注意：您的密码已经超过3个月未修改(304)，请您尽快修改密码！"
_CHALLENGE = re.compile(r"captcha|geetest|verification|(?:^|[-_])verify(?:$|[-_])|验证码|验证", re.I)
_ERROR_VISIBLE = """
// gift-auth-errors: return only a boolean; never return page text or input values.
const visible=e=>e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
return [...document.querySelectorAll('body *')].some(e=>!e.children.length && visible(e) &&
 /密码错误|账号或密码|帐号或密码|用户名或密码|账户或密码|登录失败|账号不存在|账号已锁定|账户已锁定/.test(e.textContent));
"""


class AuthenticationRequired(PlatformOperationError):
    """固定安全的认证提示；不含账号、密码、令牌或底层异常正文。"""


class SessionAuthenticator:
    """同一失效周期最多尝试一次登录，失败后只观察并等待人工处理。

    ensure_authenticated 返回 True 表示本次恢复了登录，False 表示已看到正向
    业务就绪标记。调用前应由页面导航到受保护入口；此类不执行业务动作。
    注入的 wait 接口为 wait(predicate, timeout)，应返回 predicate 的真值结果。
    """

    def __init__(self, site, credentials=None, timeout=20, *, wait=None,
                 clock=time.monotonic, sleep=time.sleep):
        if site.key not in ENTRY_URLS:
            raise ValueError("自动登录站点不受支持")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 60):
            raise ValueError("自动登录等待必须在 0 到 60 秒之间")
        self.site = site
        self.credentials = credentials
        self.timeout = timeout
        self.wait = wait
        self.clock = clock
        self.sleep = sleep
        self._blocked = None
        self._attempted = False
        self._notice_attempted = False
        self._manual_entry_attempted = False

    @property
    def manual_required(self):
        return self._blocked is not None

    def _message(self, message):
        return _NAMES[self.site.key] + "：" + message

    def _cancel(self, cancelled):
        if cancelled is not None and cancelled():
            raise AuthenticationRequired(self._message("自动登录已停止，请在当前窗口确认登录状态"))

    def _url(self, driver):
        parts = urlsplit(driver.current_url)
        if (parts.scheme != "https" or parts.hostname != _HOSTS[self.site.key]
                or parts.username is not None or parts.password is not None or parts.port not in (None, 443)):
            raise AuthenticationRequired(self._message("当前页面不是已核对的安全登录地址，未自动输入账号密码"))
        return parts

    @staticmethod
    def _visible(driver, selector):
        return [element for element in driver.find_elements(By.CSS_SELECTOR, selector) if element.is_displayed()]

    def _one(self, driver, selector):
        elements = self._visible(driver, selector)
        return elements[0] if len(elements) == 1 else None

    def _login_fields_visible(self, driver):
        login_fields = ('#login_id,#password' if self.site.key == "jushuitan" else
                        'input[placeholder="请输入手机号码或邮箱"],input[type="password"][placeholder="密码"]')
        return bool(self._visible(driver, login_fields))

    def _ready(self, driver, parts):
        entry = urlsplit(ENTRY_URLS[self.site.key])
        if parts.path != entry.path:
            return False
        # 过期登录弹层可能保留后台业务标题；可见登录/验证控件优先于旧标记。
        if self._login_fields_visible(driver) or self._challenge(driver):
            return False
        if self.site.key == "jushuitan":
            return (self._one(driver, "#_jt_body") is not None
                    and self._one(driver, "iframe#s_filter_frame") is not None)
        title = self._one(driver, "div.title.el-dropdown-selfdefine")
        return title is not None and title.text.strip() == "聚水潭赠品补发-融合"

    def _logged_in_shell(self, driver, parts):
        """仅允许已观察到的登录后外壳触发一次只读入口导航。"""
        if self._login_fields_visible(driver) or self._challenge(driver):
            return False
        if self.site.key == "jushuitan":
            return parts.path == "/epaas"
        if parts.path != "/app.html":
            return False
        title = self._one(driver, "div.title.el-dropdown-selfdefine")
        create = self._one(driver, "button.edit-task-button-btn")
        return (title is not None and bool(title.text.strip()) and create is not None
                and create.text.strip() == "新建")

    def _acknowledge_password_notice(self, driver, parts, cancelled):
        if self.site.key != "jushuitan" or parts.path != "/login.aspx" or self._notice_attempted:
            return False
        box = self._one(driver, ".tip-box")
        body = self._one(driver, ".tip-box .tip-body")
        button = self._one(driver, ".tip-box button.btn.btn-primary")
        if (box is None or body is None or body.text.strip() != _PASSWORD_AGE_NOTICE
                or button is None or button.text.strip() != "确认" or not button.is_enabled()):
            return False
        self._cancel(cancelled)
        self._notice_attempted = True  # 确认超时也不重复点击，不进入修改密码页面。
        button.click()
        return True

    def _verify_entry(self, driver, target, cancelled):
        """手动完成登录或已有会话落在默认工作表时，仅恢复只读目标入口。"""
        if self._manual_entry_attempted:
            raise AuthenticationRequired(self._message("目标业务页面尚未恢复，请手动打开礼赠业务页面确认登录"))
        self._cancel(cancelled)
        self._manual_entry_attempted = True
        driver.get(target)

        def ready():
            parts = self._url(driver)
            if self._ready(driver, parts):
                return True
            if self._challenge(driver):
                raise AuthenticationRequired(self._message("需要验证码或二次验证，请在当前平台窗口完成验证"))
            if self._login_fields_visible(driver):
                raise AuthenticationRequired(self._message("登录会话未恢复，请在当前平台窗口核对登录状态"))
            return None

        self._until(ready, cancelled, "目标业务页面尚未就绪，请手动确认登录状态")
        self._clear_latch()
        return True

    def _form(self, driver, parts):
        if parts.path != _LOGIN_PATHS[self.site.key]:
            return None
        if self.site.key == "jushuitan":
            user = self._one(driver, "#login_id")
            password = self._one(driver, "#password")
            button = self._one(driver, "div#login-sub")
            agreement = self._one(driver, "input#agreementCheck")
            if (user is None or password is None or button is None or agreement is None
                    or user.get_attribute("placeholder") != "邮箱地址/手机号码"
                    or password.get_attribute("type") != "password"
                    or agreement.get_attribute("type") != "checkbox"
                    or button.text.strip() != "立即登录"):
                return None
        else:
            user = self._one(driver, 'input[placeholder="请输入手机号码或邮箱"]')
            password = self._one(driver, 'input[type="password"][placeholder="密码"]')
            button = self._one(driver, "button.login-main-body-button-btn")
            agreement = None
            if user is None or password is None or button is None or button.text.strip() != "登录":
                return None
        if not all(element.is_enabled() for element in (user, password, button)):
            return None
        return {"user": user, "password": password, "button": button, "agreement": agreement}

    def _challenge(self, driver):
        for element in self._visible(driver, "input,iframe"):
            fields = ("id", "name", "placeholder", "title", "src")
            if any(_CHALLENGE.search(element.get_attribute(field) or "") for field in fields):
                return True
        return False

    def _until(self, predicate, cancelled, detail):
        def guarded():
            self._cancel(cancelled)
            try:
                return predicate()
            except (NoSuchElementException, StaleElementReferenceException):
                return None

        if self.wait is not None:
            result = self.wait(guarded, self.timeout)
            self._cancel(cancelled)
            if result:
                return result
            raise AuthenticationRequired(self._message(detail))
        deadline = self.clock() + self.timeout
        while True:
            result = guarded()
            if result:
                return result
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise AuthenticationRequired(self._message(detail))
            self.sleep(min(.2, remaining))

    def _clear_latch(self):
        self._blocked = None
        self._attempted = False
        self._notice_attempted = False
        self._manual_entry_attempted = False

    def is_authenticated(self, driver):
        """只读核对受保护业务标记，不登录、导航、读取密码或重置失败锁存。"""
        try:
            return self._ready(driver, self._url(driver)) is True
        except Exception:
            return False

    def ensure_authenticated(self, driver, target_url=None, cancelled=None):
        target = ENTRY_URLS[self.site.key] if target_url is None else target_url
        if target != ENTRY_URLS[self.site.key]:
            raise AuthenticationRequired(self._message("自动登录恢复地址不受支持，未自动输入账号密码"))
        try:
            self._cancel(cancelled)
            parts = self._url(driver)
            try:
                ready = self._ready(driver, parts)
            except (NoSuchElementException, StaleElementReferenceException):
                ready = False
            if ready:
                self._clear_latch()
                return False
            if self._blocked is not None:
                if self._login_fields_visible(driver):
                    # 人工登录仍在进行；不读密码、不重复点击。之后可确认新的人工结果。
                    self._manual_entry_attempted = False
                elif self._logged_in_shell(driver, parts):
                    return self._verify_entry(driver, target, cancelled)
                raise AuthenticationRequired(self._blocked)

            def initial():
                current = self._url(driver)
                if self._ready(driver, current):
                    return {"ready": True}
                if self._challenge(driver):
                    raise AuthenticationRequired(self._message("需要验证码或二次验证，请在当前平台窗口完成验证"))
                if self._logged_in_shell(driver, current):
                    return {"shell": True}
                form = self._form(driver, current)
                return {"form": form} if form else None

            observed = self._until(initial, cancelled, "登录页面尚未就绪或结构发生变化，请手动确认登录")
            if observed.get("ready"):
                self._clear_latch()
                return False
            if observed.get("shell"):
                return self._verify_entry(driver, target, cancelled)
            form = observed["form"]
            self._cancel(cancelled)
            # 最后再次验证来源；只有确认过的登录页可以读取本机密码配置。
            current = self._url(driver)
            if self._form(driver, current) is None:
                raise AuthenticationRequired(self._message("登录页面已变化，请手动确认登录"))
            provider = self.credentials if self.credentials is not None else CredentialStore()
            try:
                account = provider.load(self.site.key)
                if (account is None or not isinstance(account.username, str) or not account.username.strip()
                        or not isinstance(account.password, str) or not account.password):
                    raise ValueError("missing account")
            except Exception:
                raise AuthenticationRequired(self._message("没有可用的加密账号配置，请手动登录或更新本机账号配置")) from None
            # 登录过程任一异常都保留锁存；点击超时不允许再次发送同一登录。
            self._blocked = self._message("已尝试自动登录，尚未确认恢复，请在当前平台窗口完成验证或检查账号密码")
            try:
                self._cancel(cancelled)
                form["user"].clear()
                form["user"].send_keys(account.username)
                self._cancel(cancelled)
                form["password"].clear()
                form["password"].send_keys(account.password)
            finally:
                del account
            self._cancel(cancelled)
            current = self._url(driver)
            fresh_form = self._form(driver, current)
            if fresh_form is None:
                if self._ready(driver, current):
                    self._clear_latch()
                    return False
                raise AuthenticationRequired(self._message("登录页面已变化，请手动确认登录"))
            if self._challenge(driver):
                raise AuthenticationRequired(self._message("需要验证码或二次验证，请在当前平台窗口完成验证"))
            agreement = fresh_form["agreement"]
            if agreement is not None and not agreement.is_selected():
                self._cancel(cancelled)
                if not agreement.is_enabled():
                    raise AuthenticationRequired(self._message("登录协议选项尚不可用，请手动确认登录"))
                agreement.click()
                if not agreement.is_selected():
                    raise AuthenticationRequired(self._message("登录协议选项未生效，请手动确认登录"))
            self._cancel(cancelled)
            self._url(driver)
            self._attempted = True  # 必须在可能超时的登录点击之前记录。
            fresh_form["button"].click()
            redirected = False
            shell_ready_at = None

            def restored():
                nonlocal redirected, shell_ready_at
                current = self._url(driver)
                if self._ready(driver, current):
                    return True
                if self._challenge(driver):
                    raise AuthenticationRequired(self._message("需要验证码或二次验证，请在当前平台窗口完成验证"))
                if driver.execute_script(_ERROR_VISIBLE) is True:
                    raise AuthenticationRequired(self._message("账号密码登录未通过，请在当前平台窗口核对账号或完成验证"))
                if self._acknowledge_password_notice(driver, current, cancelled):
                    return None
                if self._login_fields_visible(driver):
                    shell_ready_at = None
                    if redirected:
                        raise AuthenticationRequired(self._message("登录会话未恢复，请在当前平台窗口核对登录状态"))
                    return None
                if not self._logged_in_shell(driver, current):
                    shell_ready_at = None
                    return None
                if shell_ready_at is None:
                    shell_ready_at = self.clock()
                if (not redirected and driver.current_url != target
                        and self.clock() - shell_ready_at >= .5):
                    self._cancel(cancelled)
                    redirected = True
                    driver.get(target)  # 只进入已核对的只读业务入口一次。
                return None

            self._until(restored, cancelled, "自动登录未确认成功，请在当前平台窗口完成验证或核对账号密码")
            self._clear_latch()
            return True
        except AuthenticationRequired as exc:
            self._blocked = str(exc)
            raise
        except (InvalidSessionIdException, NoSuchWindowException):
            self._blocked = self._message("浏览器窗口已关闭或会话已失效，本次操作已停止；未重复登录或登记")
            raise AuthenticationRequired(self._blocked) from None
        except Exception:
            self._blocked = self._message("自动登录未完成，请在当前平台窗口手动处理，未自动重复登录")
            raise AuthenticationRequired(self._blocked) from None
