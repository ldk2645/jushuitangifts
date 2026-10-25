"""自动登录边界；显式 fake 页面、合成凭据和模拟时间，不读取真实配置。"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from selenium.common.exceptions import StaleElementReferenceException

from gift_registry.authentication import AuthenticationRequired, ENTRY_URLS, SessionAuthenticator
from gift_registry.config import SITES


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.on_tick = None

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        if self.on_tick:
            self.on_tick()


class FakeElement:
    def __init__(self, *, attrs=None, text="", enabled=True, displayed=True, selected=False,
                 on_click=None, on_send=None):
        self.attrs = attrs or {}
        self.text = text
        self.enabled = enabled
        self.displayed = displayed
        self.selected = selected
        self.on_click = on_click
        self.on_send = on_send
        self.clicks = 0
        self.clears = 0
        self.sent = []

    def is_displayed(self):
        return self.displayed

    def is_enabled(self):
        return self.enabled

    def is_selected(self):
        return self.selected

    def get_attribute(self, key):
        if key == "value":
            raise AssertionError("自动登录不得回读密码或账号输入值")
        return self.attrs.get(key)

    def clear(self):
        self.clears += 1

    def send_keys(self, value):
        self.sent.append(value)
        if self.on_send:
            self.on_send()

    def click(self):
        self.clicks += 1
        if self.on_click:
            self.on_click()


class FakeCredentials:
    def __init__(self, account=None):
        self.account = account if account is not None else SimpleNamespace(
            username="000123456789", password=" isolated-test-password \n")
        self.calls = []
        self.error = None

    def load(self, key):
        self.calls.append(key)
        if self.error:
            raise self.error
        return self.account


class FakeDriver:
    def __init__(self, key="banniu", mode="login"):
        self.key = key
        self.mode = mode
        self.challenges = []
        self.error = False
        self.title_text = "聚水潭赠品补发-融合"
        self.missing_iframe = False
        self.hidden_login_overlay = False
        self.duplicate_user = False
        self.navigation = []
        self.on_get = None
        self.stale_once = False
        self.notice_visible = False
        self.notice_text = "注意：您的密码已经超过3个月未修改(304)，请您尽快修改密码！"
        self.user = FakeElement(attrs={"placeholder": "邮箱地址/手机号码" if key == "jushuitan" else "请输入手机号码或邮箱"})
        self.password = FakeElement(attrs={"type": "password", "placeholder": "密码"})
        self.agreement = FakeElement(attrs={"type": "checkbox"})
        self.agreement.on_click = lambda: setattr(self.agreement, "selected", True)
        self.button = FakeElement(text="立即登录" if key == "jushuitan" else "登录", on_click=self.ready)
        self.notice_button = FakeElement(text="确认", on_click=self.home)
        self.new_button = FakeElement(text="新建")
        self.current_url = ""
        if mode == "login":
            self.login()
        else:
            self.current_url = ENTRY_URLS[key]

    def ready(self):
        self.mode = "ready"
        self.current_url = ENTRY_URLS[self.key]
        self.title_text = "聚水潭赠品补发-融合"
        self.notice_visible = False

    def home(self):
        self.notice_visible = False
        self.mode = "home" if self.key == "jushuitan" else "shell"
        self.current_url = ("https://dopre.jushuitan.com/epaas" if self.key == "jushuitan" else
                            "https://work.bytenew.com/app.html#/applet/appNew/projectNew/56881/35019")
        if self.key == "banniu":
            self.title_text = "综合业务外呼工作表"

    def login(self):
        self.mode = "login"
        self.current_url = ("https://dopre.jushuitan.com/login.aspx" if self.key == "jushuitan"
                            else ENTRY_URLS[self.key])

    def find_elements(self, by, selector):
        if self.stale_once:
            self.stale_once = False
            raise StaleElementReferenceException("synthetic stale DOM")
        login_visible = self.mode in ("login", "overlay") or self.hidden_login_overlay
        if selector == "input,iframe":
            return list(self.challenges) + ([self.user, self.password] if login_visible else [])
        if selector in ('#login_id,#password', 'input[placeholder="请输入手机号码或邮箱"],input[type="password"][placeholder="密码"]'):
            return [self.user, self.password] if login_visible else []
        if selector in ("#login_id", 'input[placeholder="请输入手机号码或邮箱"]'):
            if not login_visible:
                return []
            return [self.user, self.user] if self.duplicate_user else [self.user]
        if selector in ("#password", 'input[type="password"][placeholder="密码"]'):
            return [self.password] if login_visible else []
        if selector in ("div#login-sub", "button.login-main-body-button-btn"):
            return [self.button] if login_visible else []
        if selector == "input#agreementCheck":
            return [self.agreement] if login_visible and self.key == "jushuitan" else []
        if selector == ".tip-box":
            return [FakeElement()] if self.notice_visible else []
        if selector == ".tip-box .tip-body":
            return [FakeElement(text=self.notice_text)] if self.notice_visible else []
        if selector == ".tip-box button.btn.btn-primary":
            return [self.notice_button] if self.notice_visible else []
        if selector == "#_jt_body":
            return [FakeElement()] if self.mode in ("ready", "overlay") else []
        if selector == "iframe#s_filter_frame":
            return [FakeElement()] if self.mode in ("ready", "overlay") and not self.missing_iframe else []
        if selector == "div.title.el-dropdown-selfdefine":
            return [FakeElement(text=self.title_text)] if self.mode in ("ready", "overlay", "shell") else []
        if selector == "button.edit-task-button-btn":
            return [self.new_button] if self.mode in ("ready", "overlay", "shell") else []
        raise AssertionError("未定义的合成页面读取：" + selector)

    def execute_script(self, script):
        if "gift-auth-errors" not in script:
            raise AssertionError("测试仅允许安全布尔错误观察")
        return self.error

    def get(self, url):
        self.navigation.append(url)
        self.current_url = url
        if self.on_get:
            self.on_get()
        else:
            self.ready()


class AuthenticationTests(unittest.TestCase):
    def make(self, key="banniu", mode="login", *, credentials=None):
        driver = FakeDriver(key, mode)
        store = credentials if credentials is not None else FakeCredentials()
        clock = FakeClock()
        site = next(site for site in SITES if site.key == key)
        auth = SessionAuthenticator(site, credentials=store, timeout=2, clock=clock, sleep=clock.sleep)
        return auth, driver, store, clock

    def test_ready_business_pages_do_not_read_credentials_or_login(self):
        for key in ENTRY_URLS:
            with self.subTest(key=key):
                auth, driver, store, _ = self.make(key, "ready")
                self.assertFalse(auth.ensure_authenticated(driver))
                self.assertTrue(auth.is_authenticated(driver))
                self.assertEqual(store.calls, [])
                self.assertEqual(driver.button.clicks, 0)
                self.assertEqual(driver.navigation, [])

    def test_each_real_form_fills_once_and_confirms_business_marker(self):
        for key in ENTRY_URLS:
            with self.subTest(key=key):
                auth, driver, store, _ = self.make(key)
                self.assertTrue(auth.ensure_authenticated(driver))
                self.assertEqual(store.calls, [key])
                self.assertEqual(driver.user.sent, [store.account.username])
                self.assertEqual(driver.password.sent, [store.account.password])
                self.assertEqual((driver.user.clears, driver.password.clears), (1, 1))
                self.assertEqual(driver.button.clicks, 1)
                self.assertEqual(driver.agreement.clicks, int(key == "jushuitan"))
                self.assertFalse(auth.manual_required)
                self.assertFalse(auth.ensure_authenticated(driver))
                self.assertEqual(driver.button.clicks, 1)

    def test_already_checked_agreement_is_not_clicked_again(self):
        auth, driver, _, _ = self.make("jushuitan")
        driver.agreement.selected = True
        self.assertTrue(auth.ensure_authenticated(driver))
        self.assertEqual(driver.agreement.clicks, 0)

    def test_unchecked_agreement_that_does_not_apply_stops_before_login_click(self):
        auth, driver, store, _ = self.make("jushuitan")
        driver.agreement.on_click = lambda: None
        with self.assertRaises(AuthenticationRequired):
            auth.ensure_authenticated(driver)
        with self.assertRaises(AuthenticationRequired):
            auth.ensure_authenticated(driver)
        self.assertEqual(driver.agreement.clicks, 1)
        self.assertEqual(driver.button.clicks, 0)
        self.assertEqual(store.calls, ["jushuitan"])

    def test_missing_encrypted_account_is_latched_without_filling(self):
        auth, driver, store, _ = self.make()
        store.account = None
        for _ in range(3):
            with self.assertRaises(AuthenticationRequired):
                auth.ensure_authenticated(driver)
        self.assertEqual(store.calls, ["banniu"])
        self.assertEqual(driver.user.sent, [])
        self.assertEqual(driver.password.sent, [])
        self.assertEqual(driver.button.clicks, 0)

    def test_credential_exception_never_exposes_its_text(self):
        auth, driver, store, _ = self.make()
        store.error = RuntimeError("synthetic-private-password")
        with self.assertRaises(AuthenticationRequired) as error:
            auth.ensure_authenticated(driver)
        self.assertNotIn("synthetic-private-password", str(error.exception))
        self.assertEqual(driver.button.clicks, 0)

    def test_invalid_account_types_never_fill_or_click(self):
        for account in (SimpleNamespace(username=123, password="fake"),
                        SimpleNamespace(username="0001", password=123),
                        SimpleNamespace(username="", password="fake")):
            with self.subTest(account=account):
                auth, driver, _, _ = self.make(credentials=FakeCredentials(account))
                with self.assertRaises(AuthenticationRequired):
                    auth.ensure_authenticated(driver)
                self.assertEqual(driver.user.sent, [])
                self.assertEqual(driver.password.sent, [])
                self.assertEqual(driver.button.clicks, 0)

    def test_visible_captcha_stops_before_loading_credentials(self):
        for attributes in ({"placeholder": "请输入验证码"}, {"name": "captcha"},
                           {"src": "https://challenge.test/verification"}):
            with self.subTest(attributes=attributes):
                auth, driver, store, _ = self.make()
                driver.challenges = [FakeElement(attrs=attributes)]
                with self.assertRaisesRegex(AuthenticationRequired, "验证码"):
                    auth.ensure_authenticated(driver)
                driver.challenges.clear()
                with self.assertRaises(AuthenticationRequired):
                    auth.ensure_authenticated(driver)
                self.assertEqual(store.calls, [])
                self.assertEqual(driver.button.clicks, 0)

    def test_hidden_captcha_is_not_solved_or_clicked(self):
        auth, driver, _, _ = self.make()
        hidden = FakeElement(attrs={"id": "captcha"}, displayed=False)
        driver.challenges = [hidden]
        self.assertTrue(auth.ensure_authenticated(driver))
        self.assertEqual(hidden.clicks, 0)
        self.assertEqual(hidden.sent, [])

    def test_captcha_appearing_after_login_latches_the_single_attempt(self):
        auth, driver, store, _ = self.make()
        driver.button.on_click = lambda: driver.challenges.append(FakeElement(attrs={"placeholder": "验证码"}))
        for _ in range(3):
            with self.assertRaisesRegex(AuthenticationRequired, "验证码"):
                auth.ensure_authenticated(driver)
        self.assertEqual(driver.button.clicks, 1)
        self.assertEqual(store.calls, ["banniu"])
        self.assertEqual(len(driver.password.sent), 1)

    def test_password_error_stops_immediately_without_exposing_page_text(self):
        auth, driver, store, clock = self.make()
        driver.button.on_click = lambda: setattr(driver, "error", True)
        with self.assertRaisesRegex(AuthenticationRequired, "登录未通过"):
            auth.ensure_authenticated(driver)
        self.assertEqual(clock.now, 0)
        with self.assertRaises(AuthenticationRequired):
            auth.ensure_authenticated(driver)
        self.assertEqual(driver.button.clicks, 1)
        self.assertEqual(store.calls, ["banniu"])

    def test_uncertain_login_click_is_never_retried(self):
        auth, driver, store, _ = self.make()
        def timeout():
            raise TimeoutError("synthetic-private-token")
        driver.button.on_click = timeout
        for _ in range(3):
            with self.assertRaises(AuthenticationRequired) as error:
                auth.ensure_authenticated(driver)
            self.assertNotIn("synthetic-private-token", str(error.exception))
        self.assertEqual(driver.button.clicks, 1)
        self.assertEqual(store.calls, ["banniu"])
        self.assertEqual(len(driver.password.sent), 1)

    def test_password_box_disappearance_is_insufficient_without_business_marker(self):
        auth, driver, _, clock = self.make()
        driver.button.on_click = lambda: setattr(driver, "mode", "blank")
        with self.assertRaisesRegex(AuthenticationRequired, "未确认成功"):
            auth.ensure_authenticated(driver)
        self.assertEqual(driver.button.clicks, 1)
        self.assertGreaterEqual(clock.now, 2)
        self.assertEqual(driver.navigation, [])

    def test_login_landing_home_can_navigate_only_to_known_entry_once(self):
        auth, driver, _, _ = self.make("jushuitan")
        def landing():
            driver.mode = "home"
            driver.current_url = "https://dopre.jushuitan.com/epaas"
        driver.button.on_click = landing
        self.assertTrue(auth.ensure_authenticated(driver))
        self.assertEqual(driver.navigation, [ENTRY_URLS["jushuitan"]])
        self.assertEqual(driver.button.clicks, 1)

    def test_entry_redirecting_back_to_login_does_not_trigger_another_click(self):
        auth, driver, _, _ = self.make("jushuitan")
        def landing():
            driver.mode = "home"
            driver.current_url = "https://dopre.jushuitan.com/epaas"
        driver.button.on_click = landing
        driver.on_get = driver.login
        with self.assertRaisesRegex(AuthenticationRequired, "会话未恢复"):
            auth.ensure_authenticated(driver)
        self.assertEqual(driver.navigation, [ENTRY_URLS["jushuitan"]])
        self.assertEqual(driver.button.clicks, 1)

    def test_untrusted_origin_http_userinfo_and_port_never_load_passwords(self):
        for url in ("http://work.bytenew.com/app.html", "https://work.bytenew.com.evil.test/app.html",
                    "https://user:synthetic@work.bytenew.com/app.html", "https://work.bytenew.com:444/app.html",
                    "https://other.bytenew.com/app.html"):
            with self.subTest(url=url):
                auth, driver, store, _ = self.make()
                driver.current_url = url
                with self.assertRaises(AuthenticationRequired):
                    auth.ensure_authenticated(driver)
                self.assertFalse(auth.is_authenticated(driver))
                self.assertEqual(store.calls, [])
                self.assertEqual(driver.button.clicks, 0)

    def test_unknown_path_never_loads_credentials_even_with_similar_controls(self):
        auth, driver, store, _ = self.make()
        driver.current_url = "https://work.bytenew.com/not-observed.html"
        with self.assertRaises(AuthenticationRequired):
            auth.ensure_authenticated(driver)
        self.assertEqual(store.calls, [])
        self.assertEqual(driver.button.clicks, 0)

    def test_unknown_or_ambiguous_login_form_never_loads_credentials(self):
        for field in ("button_text", "duplicate_user", "user_placeholder", "password_type"):
            with self.subTest(field=field):
                auth, driver, store, _ = self.make("jushuitan")
                if field == "button_text":
                    driver.button.text = "未观察的操作"
                elif field == "duplicate_user":
                    driver.duplicate_user = True
                elif field == "user_placeholder":
                    driver.user.attrs["placeholder"] = "changed"
                else:
                    driver.password.attrs["type"] = "text"
                with self.assertRaises(AuthenticationRequired):
                    auth.ensure_authenticated(driver)
                self.assertEqual(store.calls, [])
                self.assertEqual(driver.button.clicks, 0)

    def test_cancellation_before_input_never_reads_credentials(self):
        auth, driver, store, _ = self.make()
        with self.assertRaisesRegex(AuthenticationRequired, "已停止"):
            auth.ensure_authenticated(driver, cancelled=lambda: True)
        self.assertEqual(store.calls, [])
        self.assertEqual(driver.button.clicks, 0)

    def test_cancellation_between_username_and_password_stops_without_click(self):
        auth, driver, _, _ = self.make()
        stopped = [False]
        driver.user.on_send = lambda: stopped.__setitem__(0, True)
        with self.assertRaisesRegex(AuthenticationRequired, "已停止"):
            auth.ensure_authenticated(driver, cancelled=lambda: stopped[0])
        self.assertEqual(len(driver.user.sent), 1)
        self.assertEqual(driver.password.sent, [])
        self.assertEqual(driver.button.clicks, 0)

    def test_cancellation_is_checked_during_page_wait(self):
        auth, driver, store, clock = self.make(mode="blank")
        stopped = [False]
        clock.on_tick = lambda: stopped.__setitem__(0, True)
        with self.assertRaisesRegex(AuthenticationRequired, "已停止"):
            auth.ensure_authenticated(driver, cancelled=lambda: stopped[0])
        self.assertEqual(store.calls, [])
        self.assertLess(clock.now, 2)

    def test_only_positive_marker_clears_latch_for_a_later_expiry(self):
        auth, driver, store, _ = self.make()
        driver.button.on_click = lambda: setattr(driver, "error", True)
        with self.assertRaises(AuthenticationRequired):
            auth.ensure_authenticated(driver)
        driver.error = False
        driver.ready()
        self.assertFalse(auth.ensure_authenticated(driver))
        self.assertFalse(auth.manual_required)
        driver.login()
        driver.button.on_click = driver.ready
        self.assertTrue(auth.ensure_authenticated(driver))
        self.assertEqual(driver.button.clicks, 2)
        self.assertEqual(store.calls, ["banniu", "banniu"])

    def test_read_only_authenticated_probe_does_not_clear_latch(self):
        auth, driver, store, _ = self.make()
        store.account = None
        with self.assertRaises(AuthenticationRequired):
            auth.ensure_authenticated(driver)
        driver.ready()
        self.assertTrue(auth.is_authenticated(driver))
        self.assertTrue(auth.manual_required)
        self.assertEqual(store.calls, ["banniu"])
        self.assertEqual(driver.navigation, [])
        self.assertFalse(auth.ensure_authenticated(driver))
        self.assertFalse(auth.manual_required)

    def test_login_overlay_and_disabled_button_override_background_ready_marker(self):
        for disabled in (False, True):
            with self.subTest(disabled=disabled):
                auth, driver, store, _ = self.make(mode="overlay")
                driver.button.enabled = not disabled
                self.assertFalse(auth.is_authenticated(driver))
                if disabled:
                    with self.assertRaises(AuthenticationRequired):
                        auth.ensure_authenticated(driver)
                    self.assertEqual(store.calls, [])
                else:
                    self.assertTrue(auth.ensure_authenticated(driver))
                    self.assertEqual(driver.button.clicks, 1)

    def test_captcha_overlay_does_not_accept_background_business_marker(self):
        auth, driver, store, _ = self.make(mode="ready")
        driver.challenges = [FakeElement(attrs={"name": "captcha"})]
        self.assertFalse(auth.is_authenticated(driver))
        with self.assertRaisesRegex(AuthenticationRequired, "验证码"):
            auth.ensure_authenticated(driver)
        self.assertEqual(store.calls, [])

    def test_incomplete_business_markers_are_not_authenticated(self):
        for key in ENTRY_URLS:
            with self.subTest(key=key):
                auth, driver, store, _ = self.make(key, "ready")
                driver.title_text = "其他工作表"
                driver.missing_iframe = True
                driver.on_get = lambda: None
                self.assertFalse(auth.is_authenticated(driver))
                with self.assertRaises(AuthenticationRequired):
                    auth.ensure_authenticated(driver)
                self.assertEqual(store.calls, [])

    def test_no_default_credential_store_is_created_until_a_known_form_is_seen(self):
        site = next(site for site in SITES if site.key == "banniu")
        auth = SessionAuthenticator(site, timeout=.1)
        driver = FakeDriver(mode="ready")
        with patch("gift_registry.authentication.CredentialStore", side_effect=AssertionError("not allowed")) as constructor:
            self.assertFalse(auth.ensure_authenticated(driver))
            constructor.assert_not_called()

    def test_initial_page_loading_and_stale_dom_use_bounded_read_wait(self):
        auth, driver, store, clock = self.make(mode="blank")
        driver.stale_once = True
        clock.on_tick = driver.login
        self.assertTrue(auth.ensure_authenticated(driver))
        self.assertEqual(store.calls, ["banniu"])
        self.assertEqual(driver.button.clicks, 1)

    def test_unsupported_restore_target_never_navigates_or_reads_credentials(self):
        auth, driver, store, _ = self.make()
        with self.assertRaises(AuthenticationRequired):
            auth.ensure_authenticated(driver, target_url="https://work.bytenew.com/app.html#/unexpected")
        self.assertEqual(store.calls, [])
        self.assertEqual(driver.navigation, [])

    def test_custom_wait_can_be_injected_without_real_sleep(self):
        auth, driver, _, _ = self.make()
        calls = []
        auth.wait = lambda predicate, timeout: calls.append(timeout) or predicate()
        self.assertTrue(auth.ensure_authenticated(driver))
        self.assertEqual(calls, [2, 2])

    def test_invalid_timeout_is_rejected(self):
        for timeout in (0, -1, True, 61, float("inf"), float("nan"), "2"):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                SessionAuthenticator(SITES[0], timeout=timeout)

    def test_exact_jst_password_age_notice_is_confirmed_once_before_entry_verification(self):
        auth, driver, store, _ = self.make("jushuitan")
        driver.button.on_click = lambda: setattr(driver, "notice_visible", True)
        self.assertTrue(auth.ensure_authenticated(driver))
        self.assertEqual(driver.button.clicks, 1)
        self.assertEqual(driver.notice_button.clicks, 1)
        self.assertEqual(driver.navigation, [ENTRY_URLS["jushuitan"]])
        self.assertEqual(store.calls, ["jushuitan"])
        self.assertTrue(auth.is_authenticated(driver))

    def test_other_password_notices_and_change_password_buttons_are_never_clicked(self):
        for mutation in ("text", "button"):
            with self.subTest(mutation=mutation):
                auth, driver, _, _ = self.make("jushuitan")
                driver.button.on_click = lambda: setattr(driver, "notice_visible", True)
                if mutation == "text":
                    driver.notice_text = "请立即修改密码"
                else:
                    driver.notice_button.text = "修改密码"
                with self.assertRaises(AuthenticationRequired):
                    auth.ensure_authenticated(driver)
                self.assertEqual(driver.notice_button.clicks, 0)
                self.assertEqual(driver.button.clicks, 1)
                self.assertEqual(driver.navigation, [])

    def test_password_notice_confirmation_timeout_is_not_clicked_again(self):
        auth, driver, store, _ = self.make("jushuitan")
        driver.button.on_click = lambda: setattr(driver, "notice_visible", True)
        def timeout():
            raise TimeoutError("synthetic-private-notice-error")
        driver.notice_button.on_click = timeout
        for _ in range(3):
            with self.assertRaises(AuthenticationRequired) as error:
                auth.ensure_authenticated(driver)
            self.assertNotIn("synthetic-private-notice-error", str(error.exception))
        self.assertEqual(driver.button.clicks, 1)
        self.assertEqual(driver.notice_button.clicks, 1)
        self.assertEqual(store.calls, ["jushuitan"])

    def test_bn_disabled_login_fields_do_not_trigger_premature_entry_navigation(self):
        auth, driver, store, clock = self.make()
        navigation_times = []
        def login_pending():
            driver.mode = "overlay"
            driver.current_url = "https://work.bytenew.com/app.html#/pending-login"
            driver.title_text = "综合业务外呼工作表"
            driver.button.enabled = False
        driver.button.on_click = login_pending
        def finish_pending():
            if clock.now >= .8 and driver.mode == "overlay":
                driver.home()
        clock.on_tick = finish_pending
        def entry():
            navigation_times.append(clock.now)
            driver.ready()
        driver.on_get = entry
        self.assertTrue(auth.ensure_authenticated(driver))
        self.assertEqual(driver.navigation, [ENTRY_URLS["banniu"]])
        self.assertEqual(len(navigation_times), 1)
        self.assertGreaterEqual(navigation_times[0], 1.3)
        self.assertEqual(driver.button.clicks, 1)
        self.assertEqual(store.calls, ["banniu"])

    def test_bn_waits_for_logged_in_worksheet_shell_before_navigation(self):
        auth, driver, _, clock = self.make()
        driver.button.on_click = lambda: (setattr(driver, "mode", "blank"),
                                          setattr(driver, "current_url", "https://work.bytenew.com/app.html#/loading"))
        observed_navigation = []
        def page_load():
            if clock.now >= .8 and driver.mode == "blank":
                driver.home()
        clock.on_tick = page_load
        driver.on_get = lambda: (observed_navigation.append(clock.now), driver.ready())
        self.assertTrue(auth.ensure_authenticated(driver))
        self.assertEqual(driver.navigation, [ENTRY_URLS["banniu"]])
        self.assertGreaterEqual(observed_navigation[0], 1.3)

    def test_manual_login_landing_can_recover_entry_without_reloading_credentials(self):
        for key in ENTRY_URLS:
            with self.subTest(key=key):
                auth, driver, store, _ = self.make(key)
                driver.button.on_click = lambda: setattr(driver, "error", True)
                with self.assertRaises(AuthenticationRequired):
                    auth.ensure_authenticated(driver)
                self.assertTrue(auth.manual_required)
                driver.error = False
                driver.home()
                self.assertTrue(auth.ensure_authenticated(driver))
                self.assertEqual(store.calls, [key])
                self.assertEqual(driver.button.clicks, 1)
                self.assertEqual(driver.navigation, [ENTRY_URLS[key]])
                self.assertFalse(auth.manual_required)

    def test_manual_entry_recovery_stays_single_navigation_when_entry_never_becomes_ready(self):
        auth, driver, store, _ = self.make()
        driver.button.on_click = lambda: setattr(driver, "error", True)
        with self.assertRaises(AuthenticationRequired):
            auth.ensure_authenticated(driver)
        driver.error = False
        driver.home()
        driver.on_get = driver.home
        for _ in range(3):
            with self.assertRaises(AuthenticationRequired):
                auth.ensure_authenticated(driver)
        self.assertEqual(driver.navigation, [ENTRY_URLS["banniu"]])
        self.assertEqual(driver.button.clicks, 1)
        self.assertEqual(store.calls, ["banniu"])

    def test_existing_logged_in_landing_can_restore_entry_without_credentials(self):
        for key in ENTRY_URLS:
            with self.subTest(key=key):
                auth, driver, store, _ = self.make(key)
                driver.home()
                self.assertTrue(auth.ensure_authenticated(driver))
                self.assertEqual(store.calls, [])
                self.assertEqual(driver.button.clicks, 0)
                self.assertEqual(driver.navigation, [ENTRY_URLS[key]])


if __name__ == "__main__":
    unittest.main()
