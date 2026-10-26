"""未提交表单清理：归属、人工操作、提交不确定及跨订单防重回归。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gift_registry.banniu_page import BanniuPage, gifts_match
from gift_registry.registration import RegistrationQueue
from gift_registry.workflow import OrderSnapshot, PlatformOperationError, RegistrationNotSubmittedError, RegistrationRequest, SubmissionReceipt
from fakes import FakeOperations
from test_banniu_page import SKU, NAME, gift


REQUEST = RegistrationRequest('0009', (SKU,), (NAME,))


class OwnedFormCleanupTests(unittest.TestCase):
    def form_page(self):
        driver, root, close = Mock(), Mock(), Mock()
        driver.execute_script.return_value = True
        page = BanniuPage(driver, timeout=.01)
        state = {'open':True, 'root':root, 'order':REQUEST.order_no, 'selected':[]}
        page._track_form(root, REQUEST)
        page._visible = Mock(side_effect=lambda _, selector: (
            [state['root']] if state['open'] and selector == '.w-edit-task-dialog'
            else [Mock()] if state['open'] and selector == '#col_1915 .children-box-body > .el-table' else []))
        page._value = Mock(side_effect=lambda _: state['order'])
        page._selected_gifts = Mock(side_effect=lambda: state['selected'])
        page._one = Mock(return_value=close)
        close.click.side_effect = lambda: state.update(open=False)
        return page, state, close

    def test_exact_white_watch_name_alias_keeps_other_watch_codes_separate(self):
        actual = gift(商家编码='XS24A7A35002PMJ002', 货品名称='S11 Pro Max 智能手表')
        self.assertTrue(gifts_match([actual], ('XS24A7A35002PMJ002',), ('S11 Pro Max 智能手表（白色）',)))
        self.assertFalse(gifts_match([actual], ('XS24A7A35002PMJ001',), ('S11 Pro Max 智能手表（白色）',)))
        self.assertFalse(gifts_match([actual], ('XS24A7A23002J0Y001',), ('智能手表',)))

    def test_own_unchanged_form_closes_once_without_submit_and_releases_ownership(self):
        page, state, close = self.form_page()
        state['selected'] = [gift()]
        self.assertEqual(page._discard_unsubmitted_form(REQUEST), 'closed')
        self.assertFalse(state['open'])
        close.click.assert_called_once()
        self.assertIsNone(page._owned_form)
        self.assertEqual(page._discard_unsubmitted_form(REQUEST), 'none')
        close.click.assert_called_once()

    def test_observed_lenovo_name_mapping_is_limited_to_its_exact_code(self):
        record = gift(商家编码='N0800202036T000801', 货品名称='《赠品》联想蓝牙音响')
        self.assertTrue(gifts_match([record], ('N0800202036T000801',), ('赠品《联想》蓝牙音箱',)))
        self.assertFalse(gifts_match([record], ('N0800202036T000802',), ('赠品《联想》蓝牙音箱',)))
        self.assertFalse(gifts_match([{**record,'数量':'2'}], ('N0800202036T000801',), ('赠品《联想》蓝牙音箱',)))

    def test_foreign_request_or_replaced_form_is_preserved(self):
        for mode in ('request', 'root', 'unowned'):
            page, state, close = self.form_page()
            request = REQUEST
            if mode == 'request':
                request = RegistrationRequest('0010', (SKU,), (NAME,))
            elif mode == 'root':
                state['root'] = Mock()
            else:
                page._owned_form = None
            page._discard_unsubmitted_form(request)
            self.assertTrue(state['open'])
            close.click.assert_not_called()

    def test_changed_order_quantity_or_extra_gift_is_never_discarded(self):
        for mode in ('order', 'quantity', 'gift'):
            page, state, close = self.form_page()
            if mode == 'order':
                state['order'] = 'different-order'
            elif mode == 'quantity':
                state['selected'] = [gift(数量='2')]
            else:
                state['selected'] = [gift(商家编码='UNEXPECTED-SKU')]
            self.assertEqual(page._discard_unsubmitted_form(REQUEST), 'preserved')
            close.click.assert_not_called()

    def test_submit_interaction_or_manual_edit_marker_prevents_cleanup(self):
        page, state, close = self.form_page()
        page.driver.execute_script.side_effect = lambda script,*_: False if 'gift-owned-form-can-close' in script else True
        self.assertEqual(page._discard_unsubmitted_form(REQUEST), 'preserved')
        self.assertTrue(state['open'])
        close.click.assert_not_called()

    def test_close_failure_is_not_retried_and_does_not_navigate_away(self):
        page, _, close = self.form_page()
        close.click.side_effect = PlatformOperationError('synthetic close failure')
        self.assertEqual(page._discard_unsubmitted_form(REQUEST), 'preserved')
        close.click.assert_called_once()
        page.driver.get.assert_not_called()
        page.driver.refresh.assert_not_called()

    def test_failed_preparation_cleans_own_form_then_next_order_can_submit_once(self):
        page, state, close = self.form_page()
        state['open'] = False
        page._owned_form = None
        button = Mock()
        button.is_enabled.return_value = True
        button.click.side_effect = lambda: state.update(open=False)
        page._dialog = Mock(return_value=state['root'])
        page._validate_prepared = Mock()
        page._one.side_effect = lambda selector, **_: close if 'w-dialog-close' in selector else button

        def prepare(request, before):
            self.assertFalse(state['open'])
            state.update(open=True, order=request.order_no)
            page._track_form(state['root'], request)
            if request == REQUEST:
                raise PlatformOperationError('礼品搜索不符')

        page.prepare = Mock(side_effect=prepare)
        reader = Mock()
        reader.query_tasks.side_effect = lambda _: [] if not state['open'] else self.fail('Previous form leaked into next order')
        reader.wait_for_receipt.return_value = SubmissionReceipt(True, created_record_ids=('new-record',))
        with patch('gift_registry.banniu_receipt.BanniuReceiptReader', return_value=reader):
            with self.assertRaisesRegex(RegistrationNotSubmittedError, '已关闭本次未提交表单'):
                page.submit(REQUEST, OrderSnapshot(REQUEST.order_no, True))
            other = RegistrationRequest('0010', (SKU,), (NAME,))
            self.assertTrue(page.submit(other, OrderSnapshot(other.order_no, True)).accepted)
            with self.assertRaisesRegex(PlatformOperationError, '禁止再次提交'):
                page.submit(REQUEST, OrderSnapshot(REQUEST.order_no, True))
        close.click.assert_called_once()
        button.click.assert_called_once()
        self.assertEqual(page.prepare.call_count, 2)

    def test_submit_uncertainty_never_discards_form(self):
        page, state, close = self.form_page()
        page.prepare = Mock()
        page._validate_prepared = Mock()
        page._dialog = Mock(return_value=state['root'])
        submit = Mock()
        submit.is_enabled.return_value = True
        submit.click.side_effect = PlatformOperationError('提交结果不确定')
        page._one.return_value = submit
        page._discard_unsubmitted_form = Mock(wraps=page._discard_unsubmitted_form)
        reader = Mock()
        reader.query_tasks.return_value = []
        with patch('gift_registry.banniu_receipt.BanniuReceiptReader', return_value=reader):
            with self.assertRaisesRegex(PlatformOperationError, '结果不确定'):
                page.submit(REQUEST, OrderSnapshot(REQUEST.order_no, True))
        page._discard_unsubmitted_form.assert_not_called()
        submit.click.assert_called_once()
        close.click.assert_not_called()
        self.assertTrue(state['open'])


class PreparationFailureQueueTests(unittest.TestCase):
    def test_pre_submit_failure_stays_terminal_across_copy_and_restart_while_next_order_succeeds(self):
        attempts = []

        class Operations(FakeOperations):
            def submit_registration(self, request, before):
                attempts.append(request.order_no)
                if request.order_no == '0009':
                    raise RegistrationNotSubmittedError('搜索结果不符；已关闭本次未提交表单')
                return super().submit_registration(request, before)

        def row(order, identifier):
            return {'id':identifier, 'order_no':order, 'gift':NAME, 'gift_ids':[SKU],
                    'change_address':False,'recipient_name':'','phone':'','address':''}

        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            catalog = [{'id':SKU, 'name':NAME}]
            with RegistrationQueue(Path(directory), operations=Operations()) as queue:
                queue.start([row('0009','first'), row('0010','second')], catalog)
                queue.pending.join()
                states = queue.states()
                self.assertEqual(states['first']['state'], 'failed')
                self.assertIn('班牛登记未提交', states['first']['detail'])
                self.assertNotIn('结果可能不确定', states['first']['detail'])
                self.assertEqual(states['first']['attempts'], 1)
                self.assertEqual(states['second']['state'], 'success')
            with RegistrationQueue(Path(directory), operations=Operations()) as queue:
                queue.start([row('0009','copied')], catalog)
                queue.pending.join()
                self.assertEqual(queue.states()['copied']['state'], 'failed')
            self.assertEqual(attempts, ['0009','0010'])


if __name__ == '__main__':
    unittest.main()
