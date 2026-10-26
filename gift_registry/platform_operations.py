"""真实 Selenium 页面适配入口；查询、单次登记和新增核验已接入。"""

import time

from .business_browser import BusinessBrowsers
from .jushuitan_page import JushuitanPage
from .workflow import PlatformOperationError, validate_snapshot, verify_new_records


class SeleniumOperations:
    configured = True
    configuration_detail = "Selenium 查询、单次登记及新增核验已接入"

    def __init__(self, *, browsers=None, query_factory=JushuitanPage,
                 form_factory=None, verification_queries=4, verification_interval=2,
                 sleep=time.sleep):
        if not 1 <= verification_queries <= 10 or not 0 <= verification_interval <= 10:
            raise ValueError("复查次数或间隔超出上限")
        self.browsers = browsers if browsers is not None else BusinessBrowsers()
        self.query_factory = query_factory
        self.form_factory = form_factory
        self.verification_queries = verification_queries
        self.verification_interval = verification_interval
        self.sleep = sleep
        self._query_page = None
        self._form_page = None
        self._query_driver = None
        self._form_driver = None

    def prepare_start(self):
        """任务落盘前预留共享登录配置；不查询平台、不打开浏览器。"""
        self.browsers.reserve_for_start()

    def release_unused_start(self):
        """队列完全空闲或未入队的存档失败时，回收未打开浏览器的预留。"""
        return self.browsers.release_unused_reservation()

    def _jushuitan(self):
        driver = self.browsers.driver("jushuitan")
        if self._query_page is None or self._query_driver is not driver:
            if self.query_factory is JushuitanPage:
                self._query_page = self.query_factory(
                    driver, authenticate=self.browsers.get("jushuitan").ensure_authenticated)
            else:
                self._query_page = self.query_factory(driver)
            self._query_driver = driver
        return self._query_page

    def _banniu(self):
        driver = self.browsers.driver("banniu")
        if self._form_page is None or self._form_driver is not driver:
            factory = self.form_factory
            if factory is None:
                from .banniu_page import BanniuPage
                factory = BanniuPage
            from .banniu_page import BanniuPage
            if factory is BanniuPage:
                self._form_page = factory(
                    driver, authenticate=self.browsers.get("banniu").ensure_authenticated)
            else:
                self._form_page = factory(driver)
            self._form_driver = driver
        return self._form_page

    def query_order(self, order_no):
        return self._jushuitan().query_order(order_no)

    def prepare_registration(self, request, before):
        """开发核对用：填写后停在提交前，绝不创建远端任务。"""
        self._validate_baseline(request, before)
        return self._banniu().prepare(request, before)

    @staticmethod
    def _validate_baseline(request, before):
        validate_snapshot(before, request.order_no)
        if not before.found:
            raise PlatformOperationError("聚水潭未找到该订单，不能进入班牛登记")
        if request.recipient is not None:
            raise PlatformOperationError("本版本暂不支持改地址；未进行班牛登记")

    def submit_registration(self, request, before):
        # 未启用时在打开浏览器、填写或点击之前停止；绝不模拟已提交。
        if self.configured is not True:
            raise PlatformOperationError("自动登记未启用；尚未提交")
        self._validate_baseline(request, before)
        return self._banniu().submit(request, before)

    def wait_for_verification(self, request, before, receipt):
        """有界只读复查；新增信息不一致时立即交给统一核验判失败。"""
        previous_ids = {record.record_id for record in before.records}
        for attempt in range(self.verification_queries):
            after = self.query_order(request.order_no)
            success, _, _ = verify_new_records(request, before, after, receipt)
            added = any(record.record_id not in previous_ids for record in after.records)
            if success or added or not after.found:
                return after
            if attempt + 1 < self.verification_queries:
                self.sleep(self.verification_interval)
        return after

    def close(self):
        self.browsers.close()
