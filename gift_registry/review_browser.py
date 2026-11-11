"""A second profile and a query-only capability, owned by one worker thread.

SessionStore is read once. No credential login, session capture, or save method
is reachable through this capability. Existing restore filtering is reused.
"""
import os
import threading
import time
from pathlib import Path

from .authentication import ENTRY_URLS, AuthenticationRequired, SessionAuthenticator
from .browser import SiteBrowser, create_driver
from .business_browser import ProfileLock
from .config import DATA_DIR, SITES
from .driver_shutdown import quit_owned_driver
from .jushuitan_page import JushuitanPage
from .sessions import SessionStore
from .workflow import PlatformOperationError, verify_new_records


class ReadOnlyVerification:
    def __init__(self, *, data_dir=DATA_DIR, driver_factory=create_driver,
                 store_factory=SessionStore, lock_factory=ProfileLock,
                 query_factory=JushuitanPage, authenticated=None, sleep=time.sleep):
        self.site = next(s for s in SITES if s.key == 'jushuitan')
        kind = os.environ.get('GIFT_BROWSER', 'chrome').lower()
        self.profile = Path(data_dir) / ('browser_review_edge' if kind == 'edge' else 'browser_review') / self.site.key
        self.store = store_factory(Path(data_dir) / 'sessions')
        self.lock = lock_factory(self.profile.parent / 'review.lock')
        self.driver_factory, self.query_factory = driver_factory, query_factory
        self.authenticated = authenticated
        self.sleep, self.driver, self.page = sleep, None, None
        self.owner = None
        self.login_state = 'not_opened'

    def _owned(self):
        current = threading.get_ident()
        if self.owner is None:
            self.owner = current
        if self.owner != current:
            raise RuntimeError('核验驱动仅允许其工作线程使用')

    def _authenticate(self, **_):
        checker = self.authenticated or SessionAuthenticator(self.site).is_authenticated
        if not checker(self.driver):
            self.login_state = 'blocked'
            raise AuthenticationRequired('独立核验会话不可用，请在主机核对登录；未重新提交')
        self.login_state = 'authenticated'
        return False

    def query_order(self, order_no):
        self._owned()
        if self.driver is None:
            self.lock.acquire()
            try:
                snapshot = self.store.load('jushuitan')
                if not snapshot or snapshot.get('site') != 'jushuitan':
                    raise PlatformOperationError('核验会话缺失或站点不一致')
                self.driver = self.driver_factory(self.site, profile=self.profile)
                # Only the restoration method is borrowed; open/capture/login are
                # never used and this browser has no writable store capability.
                restorer = SiteBrowser(self.site, None, authenticator=False)
                restorer.driver = self.driver
                restorer.restore(snapshot)
                self.driver.get(ENTRY_URLS.get(self.site.key, self.site.url))
                self._authenticate()
                self.page = (self.query_factory(self.driver, authenticate=self._authenticate)
                             if self.query_factory is JushuitanPage else self.query_factory(self.driver))
            except Exception:
                self.login_state = 'blocked'
                self.close()
                raise
        self._authenticate()
        return self.page.query_order(order_no)

    def wait_for_verification(self, request, before, receipt):
        for attempt in range(4):
            after = self.query_order(request.order_no)
            if (verify_new_records(request, before, after, receipt)[0]
                    or {r.record_id for r in after.records} - {r.record_id for r in before.records}
                    or not after.found):
                return after
            if attempt < 3:
                self.sleep(2)
        return after

    def close(self):
        self._owned()
        if self.driver is not None:
            result = quit_owned_driver(self.driver)
            if not result.get('closed'):
                raise PlatformOperationError('独立核验浏览器未关闭，保留配置锁')
            self.driver = self.page = None
        self.lock.release()
