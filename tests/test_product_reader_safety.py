"""商品查询窗口人工内容保护回归：只使用 Mock/合成 DOM 状态，不连接真实平台。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gift_registry.authentication import AuthenticationRequired
from gift_registry.product_reader import (BanniuProductPage, ProductQueryBlocked, SeleniumProductReader,
                                         query_form_state_problem)


def clean_state(**updates):
    state = {'rootConnected': True, 'sameRoot': True, 'tokenMatch': True, 'submitSeen': False,
             'manualEdited': False, 'automation': 0, 'order': '', 'giftCount': 0}
    state.update(updates)
    return state


class QueryFormStateTests(unittest.TestCase):
    def test_clean_blank_form_is_allowed(self):
        self.assertIsNone(query_form_state_problem(clean_state()))

    def test_manual_content_submit_or_replaced_form_is_never_treated_as_blank(self):
        cases = {
            'manualEdited': ('人工修改', True),
            'submitSeen': ('提交类点击', True),
            'order': ('已有订单号', 'SYN-ORDER'),
            'giftCount': ('已有商品明细', 1),
            'tokenMatch': ('归属标记', False),
            'rootConnected': ('已被替换', False),
            'sameRoot': ('已被替换', False),
        }
        for key, (reason, value) in cases.items():
            with self.subTest(key=key):
                problem = query_form_state_problem(clean_state(**{key: value}))
                self.assertIsNotNone(problem)
                self.assertIn(reason, problem)

    def test_missing_or_foreign_state_fails_closed(self):
        for state in (None, {}, {'rootConnected': True}):
            with self.subTest(state=state):
                self.assertIsNotNone(query_form_state_problem(state))


class ProductFormRecoveryTests(unittest.TestCase):
    def page(self, state):
        page = object.__new__(BanniuProductPage)
        page.driver = Mock()
        page.page = Mock()
        page.root = Mock()
        page.token = 'synthetic-token'
        page.page._dialog.return_value = page.root
        page._dialog_visible = Mock(return_value=True)
        page._owner_state = Mock(return_value=state)
        return page

    def test_recover_never_refreshes_unknown_or_manual_form(self):
        for state in (clean_state(manualEdited=True), clean_state(order='SYN-ORDER'),
                      clean_state(giftCount=2), clean_state(tokenMatch=False), clean_state(submitSeen=True)):
            page = self.page(state)
            with self.subTest(state=state), self.assertRaises(ProductQueryBlocked):
                page.recover()
            page.driver.refresh.assert_not_called()
            page.page._click_once.assert_not_called()
            page.page._dialog.assert_called()  # 先核对窗口，再决定是否恢复。

    def test_recover_reopens_only_confirmed_blank_tool_form(self):
        page = self.page(clean_state())
        page.page._one = Mock(side_effect=[Mock(), Mock()])
        page.page._wait = Mock()
        page._track_query_form = Mock()
        page._blank = Mock()
        page.driver.execute_script.return_value = True
        with patch('gift_registry.product_reader._open_registration_list') as open_list:
            page.recover()
        self.assertEqual(page.page._click_once.call_count, 2)  # 先关闭本工具空白表单，再重新点新建。
        open_list.assert_called_once()
        page._track_query_form.assert_called_once()
        page._blank.assert_called_once()

    def test_ensure_ownership_blocks_query_after_manual_edit(self):
        page = object.__new__(BanniuProductPage)
        page._owner_state = Mock(return_value=clean_state(manualEdited=True))
        with self.assertRaises(ProductQueryBlocked):
            page._ensure_ownership()


class ReaderBlockedLoopTests(unittest.TestCase):
    def test_manual_content_aborts_platform_and_preserves_window(self):
        driver = Mock()
        browser = Mock(driver=driver)
        browser.navigate = Mock()
        browser.ensure_authenticated = Mock()
        fake_page = Mock()
        fake_page.query.side_effect = ProductQueryBlocked('SYNTHETIC: 检测到人工修改，已保留窗口')

        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            reader = SeleniumProductReader(headless=True, platforms=('banniu',))
            with patch('gift_registry.product_reader.SiteBrowser', return_value=browser), \
                 patch('gift_registry.product_reader.create_driver', return_value=driver), \
                 patch('gift_registry.product_reader.SessionStore') as store, \
                 patch('gift_registry.product_reader.BanniuProductPage', return_value=fake_page), \
                 patch('gift_registry.product_reader.quit_owned_driver') as quit_driver:
                store.return_value.load.return_value = None
                result = reader.query(['SYN-CODE-1', 'SYN-CODE-2'], Path(folder) / 'profile')
        self.assertEqual(len(result), 2)
        self.assertTrue(all(entry['status'] == 'blocked' for entry in result))
        self.assertTrue(all('人工' in entry['reason'] for entry in result))
        quit_driver.assert_not_called()  # 窗口保留交人工处理，不刷新不关闭。
        self.assertEqual(fake_page.query.call_count, 1)  # 检测到人工内容后不再输入后续编码。


class ReaderManualLoginTests(unittest.TestCase):
    def test_manual_login_wait_then_query_continues_and_saves_session(self):
        driver = Mock()
        browser = Mock(driver=driver)
        browser.ensure_authenticated.side_effect = [
            AuthenticationRequired('班牛：自动登录未确认成功'),
            AuthenticationRequired('班牛：仍在等待人工登录'),
            None,
        ]
        browser.authenticator.is_authenticated.return_value = True
        browser.snapshot = None
        page = Mock()
        page.query.return_value = {'platform': 'banniu', 'queried_code': 'SYN-1',
                                   'status': 'exact', 'matches': [{'code': 'SYN-1', 'name': '名称'}]}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            reader = SeleniumProductReader(headless=False, platforms=('banniu',), login_wait_seconds=10)
            with patch('gift_registry.product_reader.SiteBrowser', return_value=browser), \
                 patch('gift_registry.product_reader.create_driver', return_value=driver), \
                 patch('gift_registry.product_reader.SessionStore') as store, \
                 patch('gift_registry.product_reader.BanniuProductPage', return_value=page), \
                 patch('gift_registry.product_reader.quit_owned_driver'), \
                 patch('gift_registry.product_reader.time.sleep'):
                store.return_value.load.return_value = None
                result = reader.query(['SYN-1'], Path(folder) / 'profile')
        self.assertEqual(result[0]['status'], 'exact')
        self.assertEqual(page.query.call_count, 1)
        self.assertGreaterEqual(browser.ensure_authenticated.call_count, 3)
        browser.capture.assert_called_once()

    def test_manual_login_timeout_blocks_with_actionable_reason(self):
        driver = Mock()
        browser = Mock(driver=driver)
        browser.ensure_authenticated.side_effect = AuthenticationRequired('班牛：自动登录未确认成功')
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            reader = SeleniumProductReader(headless=False, platforms=('banniu',), login_wait_seconds=10)
            with patch('gift_registry.product_reader.SiteBrowser', return_value=browser), \
                 patch('gift_registry.product_reader.create_driver', return_value=driver), \
                 patch('gift_registry.product_reader.SessionStore') as store, \
                 patch('gift_registry.product_reader.quit_owned_driver'), \
                 patch('gift_registry.product_reader.time.sleep'), \
                 patch('gift_registry.product_reader.time.monotonic', side_effect=[0, 1000, 1000]):
                store.return_value.load.return_value = None
                result = reader.query(['SYN-1', 'SYN-2'], Path(folder) / 'profile')
        self.assertEqual(len(result), 2)
        self.assertTrue(all(entry['status'] == 'blocked' for entry in result))
        self.assertTrue(all('等待人工登录超时' in entry['reason'] for entry in result))

    def test_headless_or_zero_wait_never_waits_for_manual_login(self):
        driver = Mock()
        browser = Mock(driver=driver)
        browser.ensure_authenticated.side_effect = AuthenticationRequired('班牛：自动登录未确认成功')
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            reader = SeleniumProductReader(headless=True, platforms=('banniu',))
            with patch('gift_registry.product_reader.SiteBrowser', return_value=browser), \
                 patch('gift_registry.product_reader.create_driver', return_value=driver), \
                 patch('gift_registry.product_reader.SessionStore') as store, \
                 patch('gift_registry.product_reader.quit_owned_driver'), \
                 patch('gift_registry.product_reader.time.sleep') as sleep:
                store.return_value.load.return_value = None
                result = reader.query(['SYN-1'], Path(folder) / 'profile')
        self.assertEqual(result[0]['status'], 'blocked')
        self.assertIn('自动登录未确认', result[0]['reason'])
        sleep.assert_not_called()

    def test_login_wait_seconds_are_validated(self):
        for value in (-1, 601, 1.5, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SeleniumProductReader(login_wait_seconds=value)


if __name__ == '__main__':
    unittest.main()
