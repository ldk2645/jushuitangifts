"""荣耀目录解析与跨版本隔离；只使用合成数据。"""
import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from gift_registry import launcher
from gift_registry.authentication import SessionAuthenticator
from gift_registry.config import SITES
from gift_registry.edition import BANNIU_URL, DEFAULT_PORT
from tools.import_honor_catalog import parse_catalog
from gift_registry.banniu_page import BanniuPage
from gift_registry.banniu_receipt import receipt_from_detail
from gift_registry.workflow import PlatformOperationError


class HonorTests(unittest.TestCase):
    def test_honor_form_rejects_other_brand_company_and_missing_platform(self):
        page = BanniuPage(Mock())
        values = {'#col_27777 input': '荣耀', '#col_27776 input': '抖音'}
        page._value = Mock(side_effect=values.__getitem__)
        page._radio_selected = Mock(return_value=True)
        page._validate_honor_context(require_platform=True)
        values['#col_27776 input'] = ''
        with self.assertRaisesRegex(PlatformOperationError, '平台尚未回填'):
            page._validate_honor_context(require_platform=True)
        page._validate_honor_context()  # 空白表单尚未输入订单时，平台可为空。
        values['#col_27777 input'] = '努比亚'
        with self.assertRaisesRegex(PlatformOperationError, '公司或品牌'):
            page._validate_honor_context()
        values['#col_27777 input'] = '荣耀'
        page._radio_selected.return_value = False
        with self.assertRaisesRegex(PlatformOperationError, '公司或品牌'):
            page._validate_honor_context()

    def test_observed_honor_failure_statuses_never_wait_as_success(self):
        from test_banniu_receipt import detail, REQUEST, BEFORE, TASK
        for state in ('平台失败', '班牛失败'):
            snapshot = detail()
            snapshot['fields']['同步状态'] = state
            with self.subTest(state=state), self.assertRaisesRegex(PlatformOperationError, '同步未成功'):
                receipt_from_detail(snapshot, REQUEST, BEFORE, TASK)

    def test_login_landing_board_is_only_a_navigation_signal(self):
        from gift_registry.edition import BANNIU_BOARD_FRAGMENT, BANNIU_BOARD_TITLE
        auth = SessionAuthenticator(SITES[1])
        driver = Mock()
        driver.current_url = 'https://work.bytenew.com/app.html#' + BANNIU_BOARD_FRAGMENT
        board = Mock(text=BANNIU_BOARD_TITLE)
        auth._one = Mock(side_effect=lambda _, selector: board if selector == 'button.board-top-item-title.el-dropdown-selfdefine' else None)
        auth._login_fields_visible = Mock(return_value=False)
        auth._challenge = Mock(return_value=False)
        parts = urlsplit(driver.current_url)
        self.assertTrue(auth._logged_in_shell(driver, parts))
        self.assertFalse(auth._ready(driver, parts))
        board.text = '其他看板'
        self.assertFalse(auth._logged_in_shell(driver, parts))

    def test_catalog_keeps_full_codes_and_duplicate_names_without_importing_orders(self):
        gifts = parse_catalog('gifts list:\n001-Ab/7\t同名礼品\n001-Ab.7\t同名礼品\n\nexample:\n00000001\tUNKNOWN-CODE\n')
        self.assertEqual([g['id'] for g in gifts], ['001-Ab/7', '001-Ab.7'])
        self.assertEqual([g['name'] for g in gifts], ['同名礼品', '同名礼品'])

    def test_ambiguous_or_empty_catalog_fails_before_write(self):
        for text in ['gifts list:\n', 'example:\n', 'gifts list:\nSKU-1\n',
                     'gifts list:\nSKU-1 名称甲\nSKU-1 名称乙']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_catalog(text)

    def test_wrong_worksheet_fragment_is_never_authenticated(self):
        auth = SessionAuthenticator(SITES[1])
        driver = Mock()
        driver.current_url = 'https://work.bytenew.com/app.html#/applet/appNew/projectNew/54119/1883'
        with patch.object(auth, '_login_fields_visible') as fields:
            self.assertFalse(auth._ready(driver, urlsplit(driver.current_url)))
        fields.assert_not_called()
        self.assertEqual(DEFAULT_PORT, 8767)
        self.assertEqual(urlsplit(BANNIU_URL).fragment, '/applet/appNew/projectNew/58231/27715')

    def test_launcher_refuses_original_edition_even_if_app_name_matches(self):
        for edition in (None, 'honor'):
            response = Mock()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=None)
            response.read.return_value = json.dumps({'app': 'gift_registry', 'edition': edition}).encode()
            opener = Mock()
            opener.open.return_value = response
            with patch.object(launcher, 'build_opener', return_value=opener):
                if edition is None:
                    with self.assertRaises(launcher.StartupError):
                        launcher.read_health(8767)
                else:
                    self.assertEqual(launcher.read_health(8767)['edition'], 'honor')
