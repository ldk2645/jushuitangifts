"""提交后遗留表单恢复；仅使用合成表单与临时任务目录。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from selenium.common.exceptions import TimeoutException

from gift_registry.banniu_page import BANNIU_REGISTRATION_URL
from gift_registry.banniu_receipt import BanniuReceiptReader
from gift_registry.platform_operations import SeleniumOperations
from gift_registry.registration import RegistrationQueue
from gift_registry.workflow import OrderSnapshot, PlatformOperationError, RegistrationRequest, SubmissionReceipt
from fakes import FakeOperations
import test_banniu_form_cleanup as cleanup_cases
import test_page_reuse as reuse_cases
from test_banniu_form_cleanup import REQUEST
from test_banniu_page import SKU, NAME, gift


class SubmittedFormRecoveryTests(unittest.TestCase):
    def form_page(self):
        page, state, close = cleanup_cases.OwnedFormCleanupTests().form_page()
        page.driver.current_url = BANNIU_REGISTRATION_URL
        page._submitted_form_token = page._owned_form[2]
        page._attempted.add((REQUEST.order_no, REQUEST.gift_ids))
        state['selected'] = [gift()]
        page.driver.refresh.side_effect = lambda: state.update(open=False)
        return page, state, close

    def test_own_submitted_unchanged_form_refreshes_once_without_resetting_attempts(self):
        page, state, close = self.form_page()
        page.authenticate = Mock(return_value=False)
        self.assertTrue(page._refresh_submitted_form())
        self.assertFalse(state['open'])
        page.driver.refresh.assert_called_once()
        close.click.assert_not_called()
        self.assertIsNone(page._owned_form)
        self.assertFalse(page._refresh_submitted_form())
        with self.assertRaisesRegex(PlatformOperationError, '禁止再次提交'):
            page.submit(REQUEST, OrderSnapshot(REQUEST.order_no, True))

    def test_unowned_unsubmitted_changed_or_manually_touched_forms_are_preserved(self):
        for mode in ('unowned', 'not_submitted', 'root', 'order', 'gift', 'manual', 'route'):
            with self.subTest(mode=mode):
                page, state, close = self.form_page()
                if mode == 'unowned': page._owned_form = None
                elif mode == 'not_submitted': page._submitted_form_token = None
                elif mode == 'root': state['root'] = Mock()
                elif mode == 'order': state['order'] = 'other-order'
                elif mode == 'gift': state['selected'] = [gift(数量='2')]
                elif mode == 'manual': page.driver.execute_script.return_value = False
                elif mode == 'route': page.driver.current_url = 'about:blank'
                self.assertFalse(page._refresh_submitted_form())
                page.driver.refresh.assert_not_called()
                close.click.assert_not_called()

    def test_timed_out_refresh_consumes_the_only_recovery_attempt(self):
        for failure in ('transport', 'form_remains'):
            with self.subTest(failure=failure):
                page, _, _ = self.form_page()
                page.driver.refresh.side_effect = TimeoutException('synthetic') if failure == 'transport' else None
                with self.assertRaises((PlatformOperationError, TimeoutException)):
                    page._refresh_submitted_form()
                with self.assertRaisesRegex(PlatformOperationError, '已刷新过一次'):
                    page._refresh_submitted_form()
                page.driver.refresh.assert_called_once()

    def test_recovery_is_followed_by_fresh_order_query_and_existing_task_still_blocks_retry(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                reader, field, button, state = reuse_cases.BanniuReuseTests().make_reader()
                visible = reader.page._visible.side_effect
                state['form'] = True
                reader.page._visible.side_effect = lambda root, selector: (
                    [Mock()] if state['form'] and selector == '.w-edit-task-dialog' else visible(root, selector))
                reader.page._refresh_submitted_form.side_effect = lambda: (state.update(form=False) or True)
                if existing:
                    execute = reader.driver.execute_script.side_effect
                    reader.driver.execute_script.side_effect = lambda script: (
                        {'pager': '共 1 条', 'rows': [{'task_id': '001', 'order_no': '00001', 'created_at': '2026-01-01 00:00:00'}]}
                        if 'gift-banniu-list' in script else execute(script))
                operations = SeleniumOperations(browsers=Mock())
                operations._banniu = Mock(return_value=reader.page)
                request = RegistrationRequest('00001', (SKU,), (NAME,))
                with patch('gift_registry.banniu_receipt.BanniuReceiptReader', return_value=reader), \
                     patch('gift_registry.banniu_page._open_registration_list') as open_list:
                    if existing:
                        with self.assertRaisesRegex(PlatformOperationError, '旧任务'):
                            operations.check_retry_safety(request, OrderSnapshot('00001', True), {'before_record_ids': []})
                    else:
                        self.assertTrue(operations.check_retry_safety(request, OrderSnapshot('00001', True), {'before_record_ids': []}))
                reader.page._refresh_submitted_form.assert_called_once()
                open_list.assert_not_called()  # 成功恢复后不能再刷新一次。
                field.send_keys.assert_called_once_with('00001')
                button.click.assert_called_once()  # 只读筛选。
                reader.page.submit.assert_not_called()

    def test_uncertain_previous_order_stays_failed_while_next_order_can_register(self):
        page, state, _ = self.form_page()
        page._owned_form = None
        state['open'] = False
        page._attempted.clear()
        page._validate_prepared = Mock()
        page._dialog = Mock(return_value=state['root'])
        submit = Mock()
        submit.is_enabled.return_value = True
        page._one = Mock(return_value=submit)

        def prepare(request, before):
            state.update(open=True, order=request.order_no)
            page._track_form(state['root'], request)

        page.prepare = Mock(side_effect=prepare)
        submit.click.side_effect = lambda: state.update(open=state['order'] == REQUEST.order_no)
        reader = Mock()

        def query(order):
            if state['open']:
                self.assertTrue(page._refresh_submitted_form())
            return []

        reader.query_tasks.side_effect = query
        reader.wait_for_receipt.return_value = SubmissionReceipt(True, created_record_ids=('new-record',))

        class Operations(FakeOperations):
            def submit_registration(self, request, before):
                page.submit(request, before)
                return super().submit_registration(request, before)

        def row(order, identifier):
            return {'id': identifier, 'order_no': order, 'gift': NAME, 'gift_ids': [SKU],
                    'change_address': False, 'recipient_name': '', 'phone': '', 'address': ''}

        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder, \
             patch('gift_registry.banniu_receipt.BanniuReceiptReader', return_value=reader):
            with RegistrationQueue(Path(folder), operations=Operations()) as queue:
                queue.start([row(REQUEST.order_no, 'first'), row('0010', 'second')], [{'id': SKU, 'name': NAME}])
                queue.pending.join()
                self.assertEqual(queue.states()['first']['state'], 'failed')
                self.assertEqual(queue.states()['second']['state'], 'success')
                first = next(r for r in queue._latest_attempts() if r['row']['order_no'] == REQUEST.order_no)
                self.assertEqual(first['item']['submission_status'], 'uncertain')
            with RegistrationQueue(Path(folder), operations=Operations()) as queue:
                queue.start([row(REQUEST.order_no, 'copy')], [{'id': SKU, 'name': NAME}])
                queue.pending.join()
                self.assertEqual(queue.states()['copy']['state'], 'failed')
        self.assertEqual(submit.click.call_count, 2)  # 两个不同订单各点击一次。
        self.assertEqual(page.prepare.call_count, 2)
        page.driver.refresh.assert_called_once()
