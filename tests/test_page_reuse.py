"""页面复用与多订单串行回归；全部使用隔离目录和显式平台替身。"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

from gift_registry.banniu_page import BANNIU_REGISTRATION_URL, BanniuPage
from gift_registry.banniu_receipt import BanniuReceiptReader
from gift_registry.business_browser import BusinessBrowsers
from gift_registry.jushuitan_page import ORDER_URL, JushuitanPage
from gift_registry.platform_operations import SeleniumOperations
from gift_registry.registration import RegistrationQueue
from gift_registry.workflow import OrderSnapshot, PlatformOperationError, RegistrationRequest
from fakes import FakeOperations
from test_business_browser import FakeBrowser, FakeOwnership
from test_jushuitan_page import FakeDriver


class JushuitanReuseTests(unittest.TestCase):
    def test_multiple_orders_and_verifications_reuse_page_but_query_each_time(self):
        driver = FakeDriver()
        page = JushuitanPage(driver, timeout=.01)
        orders = ['00001', '00001', '00002', '00002', '00003', '00003']
        results = [page.query_order(order) for order in orders]
        self.assertEqual(driver.urls, [ORDER_URL])
        self.assertEqual([result.order_no for result in results], orders)
        self.assertEqual(sum(event == ('observer', 'install') for event in driver.events), 6)
        self.assertEqual(driver.elements['so_id'].keys[::2], orders)

    def test_reused_same_order_still_requires_a_new_refresh(self):
        driver = FakeDriver()
        page = JushuitanPage(driver, timeout=.01)
        page.query_order('00001')
        driver.refreshed = False
        with self.assertRaisesRegex(PlatformOperationError, '未确认刷新'):
            page.query_order('00001')
        self.assertEqual(driver.urls, [ORDER_URL])
        self.assertEqual(len(driver.elements['so_id'].keys), 4)

    def test_changed_route_or_missing_markers_navigates_to_known_entry(self):
        for changed_route in (True, False):
            with self.subTest(changed_route=changed_route):
                driver = FakeDriver()
                page = JushuitanPage(driver, timeout=.01)
                page.query_order('00001')
                if changed_route:
                    driver.current_url = 'https://dopre.jushuitan.com/login.aspx'
                else:
                    execute = driver.execute_script
                    driver.execute_script = lambda script, *args: (
                        False if 'gift-order-page-ready' in script else execute(script, *args))
                page.query_order('00002')
                self.assertEqual(driver.urls, [ORDER_URL, ORDER_URL])

    def test_reuse_keeps_authentication_check_on_every_query(self):
        guard = Mock(return_value=False)
        page = JushuitanPage(FakeDriver(), authenticate=guard, timeout=.01)
        page.query_order('00001')
        page.query_order('00002')
        self.assertEqual(guard.call_args_list, [call(target_url=ORDER_URL)] * 2)


class BanniuReuseTests(unittest.TestCase):
    def make_reader(self):
        state = {'order': '', 'ready': True, 'fresh': True}
        page = Mock()
        driver = page.driver
        driver.current_url = 'about:blank'
        field, button = Mock(), Mock()
        field.clear.side_effect = lambda: state.update(order='')
        field.send_keys.side_effect = lambda value: state.update(order=value)
        field.get_attribute.side_effect = lambda name: state['order'] if name == 'value' else None
        page._visible.side_effect = lambda _, selector: (
            [field] if selector == '.advancedFilter' else [])
        page._one.return_value = button

        def navigate(url):
            driver.current_url = url
            state['ready'] = True

        def execute(script):
            if 'gift-registration-list-ready' in script:
                return state['ready']
            if 'gift-banniu-fresh' in script:
                return state['fresh']
            if 'gift-banniu-list' in script:
                return {'pager': '共 0 条', 'rows': []}

        def wait(predicate, detail):
            for _ in range(3):
                result = predicate(driver)
                if result:
                    return result
            raise PlatformOperationError(detail)

        driver.get.side_effect = navigate
        driver.refresh.side_effect = lambda: state.update(ready=True)
        driver.execute_script.side_effect = execute
        page._wait.side_effect = wait
        reader = BanniuReceiptReader(page)
        reader._order_filter = Mock(return_value=field)
        return reader, field, button, state

    def test_baselines_and_receipt_queries_reuse_list_and_filter_every_order(self):
        reader, field, button, _ = self.make_reader()
        orders = ['00001', '00001', '00002', '00002', '00003', '00003']
        for order in orders:
            self.assertEqual(reader.query_tasks(order), [])
        reader.driver.get.assert_called_once_with(BANNIU_REGISTRATION_URL)
        self.assertEqual(field.clear.call_count, 6)
        self.assertEqual(field.send_keys.call_args_list, [call(order) for order in orders])
        self.assertEqual(reader._order_filter.call_count, 6)
        self.assertEqual(button.click.call_count, 6)

    def test_reused_empty_list_is_not_returned_without_new_filter_refresh(self):
        reader, _, button, state = self.make_reader()
        reader.query_tasks('00001')
        state['fresh'] = False
        with self.assertRaisesRegex(PlatformOperationError, '未完整刷新'):
            reader.query_tasks('00001')
        self.assertEqual(reader.driver.get.call_count, 1)
        reader.driver.refresh.assert_called_once()
        self.assertEqual(button.click.call_count, 3)

    def test_details_or_unready_list_and_different_routes_reload_once(self):
        for route_changed in (True, False):
            with self.subTest(route_changed=route_changed):
                reader, _, button, state = self.make_reader()
                reader.query_tasks('00001')
                if route_changed:
                    reader.driver.current_url = 'https://work.bytenew.com/app.html'
                else:
                    state['ready'] = False
                reader.query_tasks('00002')
                reader.query_tasks('00003')
                self.assertEqual(reader.driver.get.call_count, 2 if route_changed else 1)
                self.assertEqual(reader.driver.refresh.call_count, 0 if route_changed else 1)
                self.assertEqual(button.click.call_count, 3)

    def test_persistent_detail_after_one_refresh_stops_before_filter_or_form(self):
        reader, field, button, state = self.make_reader()
        reader.driver.current_url = BANNIU_REGISTRATION_URL
        state['ready'] = False
        reader.driver.refresh.side_effect = None
        with self.assertRaisesRegex(PlatformOperationError, '遮罩覆盖'):
            reader.query_tasks('00001')
        reader.driver.refresh.assert_called_once()
        reader.driver.get.assert_not_called()
        reader._order_filter.assert_not_called()
        field.send_keys.assert_not_called()
        button.click.assert_not_called()

    def test_existing_form_is_not_discarded_during_reuse(self):
        reader, field, button, _ = self.make_reader()
        reader.page._visible.side_effect = lambda _, selector: (
            [Mock()] if selector == '.w-edit-task-dialog' else [])
        with self.assertRaisesRegex(PlatformOperationError, '未关闭的任务表单'):
            reader.query_tasks('00001')
        reader.driver.get.assert_not_called()
        reader.driver.refresh.assert_not_called()
        field.send_keys.assert_not_called()
        button.click.assert_not_called()

    def test_preparing_each_new_order_reuses_ready_list_and_checks_authentication(self):
        driver, guard = Mock(), Mock(return_value=False)
        driver.current_url = BANNIU_REGISTRATION_URL
        driver.execute_script.return_value = True
        page = BanniuPage(driver, authenticate=guard)
        page._visible = Mock(return_value=[])
        page._fill_prepared = Mock()
        for order in ('00001', '00002'):
            request = RegistrationRequest(order, ('SKU-A',), ('合成礼品',))
            page.prepare(request, OrderSnapshot(order, True))
        driver.get.assert_not_called()
        self.assertEqual(page._fill_prepared.call_count, 2)
        self.assertEqual(guard.call_args_list, [call(target_url=BANNIU_REGISTRATION_URL)] * 2)


class BatchBrowserReuseTests(unittest.TestCase):
    def test_multiple_batches_and_timeout_keep_two_browsers_and_never_resubmit(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            created = []

            def factory(site, store):
                browser = FakeBrowser(site, store)
                created.append(browser)
                return browser

            browsers = BusinessBrowsers(data_dir=Path(directory) / 'browser-data', browser_factory=factory,
                                        store_factory=lambda path: path, lock_factory=FakeOwnership)
            fake = FakeOperations({'00002': 'submit_timeout'})
            operations = SeleniumOperations(browsers=browsers,
                query_factory=lambda _: SimpleNamespace(query_order=fake.query_order),
                form_factory=lambda _: SimpleNamespace(submit=fake.submit_registration),
                verification_interval=0)
            catalog = [{'id': 'SKU-A', 'name': '合成礼品'}]

            def row(order, identifier=None):
                return {'id': identifier or order, 'order_no': order, 'gift': '合成礼品',
                        'gift_ids': ['SKU-A'], 'change_address': False,
                        'recipient_name': '', 'phone': '', 'address': ''}

            try:
                with RegistrationQueue(Path(directory) / 'jobs', operations=operations) as jobs:
                    jobs.start([row('00001'), row('00002'), row('00003')], catalog)
                    jobs.pending.join()
                    states = jobs.states()
                    self.assertEqual([states[order]['state'] for order in ('00001', '00002', '00003')],
                                     ['success', 'failed', 'success'])
                    self.assertEqual([request.order_no for request in fake.submissions],
                                     ['00001', '00002', '00003'])
                    jobs.start([row('00002', 'copied'), row('00004')], catalog)
                    jobs.pending.join()
                    self.assertEqual([request.order_no for request in fake.submissions],
                                     ['00001', '00002', '00003', '00004'])
                    self.assertEqual([browser.site.key for browser in created], ['jushuitan', 'banniu'])
                    self.assertEqual([browser.opens for browser in created], [1, 1])
                    self.assertEqual([browser.closes for browser in created], [0, 0])
                    self.assertEqual(browsers.lock.releases, 0)
            finally:
                operations.close()
            self.assertEqual([browser.closes for browser in created], [1, 1])
            self.assertEqual(browsers.lock.releases, 1)


if __name__ == '__main__':
    unittest.main()
