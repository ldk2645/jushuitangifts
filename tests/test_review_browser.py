import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from gift_registry.review_browser import ReadOnlyVerification
from gift_registry.workflow import OrderSnapshot, GiftRecord, RegistrationRequest, SubmissionReceipt

class Driver:
    def __init__(self): self.calls=[]; self.driver_id=id(self)
    def execute_cdp_cmd(self, method, params):
        self.calls.append((method,params))
        if method=='Storage.getCookies': return {'cookies':[]}
        if method=='Page.addScriptToEvaluateOnNewDocument': return {'identifier':'restore'}
        return {'success':True}
    def get(self, url): self.url=url

class ReviewBrowserTests(unittest.TestCase):
    def test_internal_order_verification_uses_identity_only_in_independent_browser(self):
        with tempfile.TemporaryDirectory() as folder:
            ops = ReadOnlyVerification(data_dir=folder, authenticated=lambda _: True)
            ops.driver, ops.page = Driver(), Mock()
            unrelated = OrderSnapshot('SYN', True, (GiftRecord('other', ()),))
            matched = OrderSnapshot('SYN', True, unrelated.records + (GiftRecord('00049', ()),))
            ops.page.query_order.side_effect = [unrelated, matched]
            ops.sleep = Mock()
            result = ops.wait_for_verification(RegistrationRequest('SYN', ('SKU',), ('Gift',)),
                OrderSnapshot('SYN', True), SubmissionReceipt(True, created_record_ids=('00049',)))
            self.assertEqual(result, matched)
            self.assertEqual(ops.page.query_order.call_count, 2)
            ops.page.query_order.assert_called_with('SYN', identity_only=True)
            ops.sleep.assert_called_once_with(2)

    def test_isolated_profile_filtered_restore_read_only_session_and_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Mock(); store.load.return_value=dict(site='jushuitan', cookies=[
                dict(name='ok',value='synthetic',domain='.jushuitan.com',path='/',expires=-1),
                dict(name='expired',value='synthetic',domain='.jushuitan.com',path='/',expires=1),
                dict(name='foreign',value='synthetic',domain='evil.test',path='/',expires=-1)],origins={})
            lock=Mock(); driver=Driver(); factory=Mock(return_value=driver)
            page=Mock(); page.query_order.return_value=OrderSnapshot('SYNTHETIC',True)
            ops=ReadOnlyVerification(data_dir=Path(folder),driver_factory=factory,store_factory=lambda p:store,
                                     lock_factory=lambda p:lock,query_factory=lambda d:page,authenticated=lambda d:True)
            self.assertFalse(hasattr(ops,'submit_registration')); self.assertFalse(hasattr(ops,'check_retry_safety'))
            with patch('gift_registry.review_browser.quit_owned_driver',return_value={'closed':True}):
                ops.query_order('SYNTHETIC')
                self.assertIn('browser_review',str(factory.call_args.kwargs['profile']))
                self.assertEqual([p['name'] for m,p in driver.calls if m=='Network.setCookie'],['ok'])
                errors=[]
                def foreign():
                    try:ops.query_order('SYNTHETIC')
                    except RuntimeError:errors.append('owner')
                thread=threading.Thread(target=foreign); thread.start(); thread.join()
                self.assertEqual(errors,['owner']); ops.close()
            store.save.assert_not_called(); store.load.assert_called_once_with('jushuitan'); lock.release.assert_called_once()

    def test_expiry_captcha_kickout_and_driver_failure_do_not_authenticate_or_save(self):
        for mode in ('expiry','captcha','kickout','driver','wrong_site'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as folder:
                store=Mock(); store.load.return_value=dict(site='banniu' if mode=='wrong_site' else 'jushuitan',cookies=[],origins={})
                driver=Driver(); auth=Mock(return_value=False)
                factory=Mock(side_effect=RuntimeError('synthetic')) if mode=='driver' else Mock(return_value=driver)
                ops=ReadOnlyVerification(data_dir=Path(folder),driver_factory=factory,store_factory=lambda p:store,
                                         lock_factory=lambda p:Mock(),authenticated=auth)
                with patch('gift_registry.review_browser.quit_owned_driver',return_value={'closed':True}):
                    with self.assertRaises(Exception):ops.query_order('SYNTHETIC')
                self.assertEqual(ops.login_state,'blocked'); store.save.assert_not_called()

if __name__=='__main__':unittest.main()
