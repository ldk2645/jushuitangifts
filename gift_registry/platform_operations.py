"""真实 Selenium 页面适配入口；查询、单次登记和新增核验已接入。"""

import time

from .business_browser import BusinessBrowsers
from .edition import BANNIU_VERIFIED
from .jushuitan_page import JushuitanPage
from .workflow import (PlatformOperationError, RegistrationNotSubmittedError, request_quantities,
                       validate_snapshot, verify_new_records)


class SeleniumOperations:
    configured = BANNIU_VERIFIED
    configuration_detail = ("荣耀班牛页面字段已核对，使用 Selenium 单次登记及新增核验" if BANNIU_VERIFIED else
                            "荣耀班牛工作表字段尚待实际页面核对，当前可整理草稿，登记暂未启用。请运行 verify-honor.bat 完成只读检查。")

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
        self._retry_authorized_task = None

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
    def _task_identity(request):
        """重登授权标识：订单 + 整组编码及数量，数量不同视为不同登记请求。"""
        return (request.order_no, tuple(sorted(zip(request.gift_ids, request_quantities(request)))))

    @staticmethod
    def _validate_baseline(request, before):
        validate_snapshot(before, request.order_no)
        if not before.found:
            raise PlatformOperationError("聚水潭未找到该订单，不能进入班牛登记")
        if request.recipient is not None:
            raise PlatformOperationError("本版本暂不支持改地址；未进行班牛登记")

    def submit_registration_tracked(self, request, before, on_submitted):
        return self.submit_registration(request, before, on_submitted=on_submitted)

    def submit_registration(self, request, before, *, on_submitted=None):
        # 未启用时在打开浏览器、填写或点击之前停止；绝不模拟已提交。
        if self.configured is not True:
            raise PlatformOperationError("自动登记未启用；尚未提交")
        self._validate_baseline(request, before)
        retry_authorized = self._retry_authorized_task == self._task_identity(request)
        self._retry_authorized_task = None
        options = {"require_distinct_gifts": True} if retry_authorized else {}
        if on_submitted is not None:
            options["on_submitted"] = on_submitted
        try:
            page = self._banniu()
        except Exception as exc:
            # 适配器尚未取得，当前请求不可能已调用班牛 submit；不重开/重试该请求。
            reason = str(exc) if isinstance(exc, PlatformOperationError) else type(exc).__name__
            raise RegistrationNotSubmittedError("获取班牛业务浏览器失败，未打开本次登记表单：" + reason) from None
        return page.submit(request, before, **options)

    def check_retry_safety(self, request, before, evidence):
        """仅由主机明确创建的新重登任务调用；所有检查均为 Selenium 只读操作。"""
        from .banniu_receipt import BanniuReceiptReader
        self._validate_baseline(request, before)
        baseline = evidence.get("before_record_ids")
        if baseline is not None and set(baseline) != {record.record_id for record in before.records}:
            raise PlatformOperationError("聚水潭记录与失败任务的基线已不同，停止重登，请人工核查")
        reader = BanniuReceiptReader(self._banniu())
        rows = reader.query_tasks(request.order_no)
        if rows and reader.check_distinct_gifts(request, rows) is not True:
            raise PlatformOperationError("班牛旧任务礼品组合无法核对，停止重登")
        # 新的持久化任务拥有新的页面调用保护；不重开浏览器，也不清除旧任务记录。
        self._form_page = None
        self._retry_authorized_task = self._task_identity(request)
        return True

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
