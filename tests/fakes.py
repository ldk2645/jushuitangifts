"""仅供隔离测试使用的模拟平台，生产入口不导入此模块。"""

from gift_registry.workflow import GiftRecord, OrderSnapshot, Recipient, SubmissionReceipt


class FakeOperations:
    configured = True

    def __init__(self, modes=None, on_submit=None):
        self.modes = modes or {}
        self.on_submit = on_submit
        self.queries = {}
        self.requests = {}
        self.submissions = []
        self.trace = []

    def query_order(self, order_no):
        self.trace.append(("query", order_no))
        self.queries[order_no] = self.queries.get(order_no, 0) + 1
        mode = self.modes.get(order_no, "success")
        if mode == "query_error" or (mode == "verification_error" and self.queries[order_no] > 1):
            raise TimeoutError("test only")
        if mode == "missing":
            return OrderSnapshot(order_no, False)
        old = GiftRecord("old-" + order_no, ("N01B0L1LE91VZS8S01", "N01B0J1JDK1V007H08"))
        request = self.requests.get(order_no)
        if request is None or mode == "no_new":
            return OrderSnapshot(order_no, True, (old,))
        submitted = [entry for entry in self.submissions if entry.order_no == order_no]
        added = []
        for index, entry in enumerate(submitted or [request]):
            gifts = ("N00A0X070C0DXM7B01",) if mode == "wrong_gift" else entry.gift_ids
            recipient = Recipient("不同姓名", "不同电话", "不同地址") if mode == "wrong_address" else entry.recipient
            suffix = '' if index == 0 else f'-{index + 1}'
            added.append(GiftRecord("new-" + order_no + suffix, gifts, recipient, "ref-" + order_no + suffix))
        return OrderSnapshot(order_no + "-wrong" if mode == "wrong_order" else order_no, True, (old, *added))

    def submit_registration(self, request, before, *, on_submitted=None):
        self.trace.append(("submit", request.order_no))
        self.submissions.append(request)
        if self.on_submit:
            self.on_submit(request)
        self.requests[request.order_no] = request
        if on_submitted is not None:
            on_submitted()
        mode = self.modes.get(request.order_no, "success")
        if mode == "submit_timeout":
            raise TimeoutError("test only")
        count = sum(entry.order_no == request.order_no for entry in self.submissions)
        suffix = '' if count == 1 else f'-{count}'
        return SubmissionReceipt(mode != "rejected", reference="ref-" + request.order_no + suffix)
