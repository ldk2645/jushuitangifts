"""礼赠业务编排。平台实现由适配器提供；每单最多调用一次登记提交。"""

import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Protocol


class PlatformOperationError(RuntimeError):
    """可直接展示的固定业务错误；禁止传入页面原文、凭据或 Selenium 异常原文。"""


@dataclass(frozen=True)
class Recipient:
    name: str
    phone: str
    address: str


@dataclass(frozen=True)
class RegistrationRequest:
    order_no: str
    gift_ids: tuple[str, ...]
    gift_names: tuple[str, ...]
    recipient: Recipient | None = None


@dataclass(frozen=True)
class GiftRecord:
    record_id: str
    gift_ids: tuple[str, ...]
    recipient: Recipient | None = None
    reference: str = ""
    gift_quantities: tuple[int, ...] = ()


@dataclass(frozen=True)
class OrderSnapshot:
    order_no: str
    found: bool
    records: tuple[GiftRecord, ...] = ()
    recipient: Recipient | None = None


@dataclass(frozen=True)
class SubmissionReceipt:
    accepted: bool
    reference: str = ""
    created_record_ids: tuple[str, ...] = ()


class PlatformOperations(Protocol):
    configured: bool

    def query_order(self, order_no: str) -> OrderSnapshot:
        """只读查询聚水潭，返回可稳定识别的登记记录。"""
        ...

    def submit_registration(self, request: RegistrationRequest, before: OrderSnapshot) -> SubmissionReceipt:
        """在班牛填写并提交一次；实现中不得自动重试提交。"""
        ...


def request_from_row(row, catalog):
    names = {item["id"]: item["name"] for item in catalog}
    recipient = Recipient(row["recipient_name"].strip(), row["phone"].strip(), row["address"].strip()) if row["change_address"] else None
    return RegistrationRequest(row["order_no"].strip(), tuple(row["gift_ids"]),
                               tuple(names[identifier] for identifier in row["gift_ids"]), recipient)


def _normalize(value):
    return "".join(unicodedata.normalize("NFKC", value).split())


def _same_recipient(expected, actual):
    if actual is None:
        return False
    return (_normalize(expected.name) == _normalize(actual.name)
            and _normalize(expected.phone).replace("-", "") == _normalize(actual.phone).replace("-", "")
            and _normalize(expected.address) == _normalize(actual.address))


def _nonempty_text(value):
    return isinstance(value, str) and bool(value.strip())


def _validate_recipient(recipient):
    if recipient is not None and (not isinstance(recipient, Recipient)
                                 or not all(_nonempty_text(value) for value in
                                            (recipient.name, recipient.phone, recipient.address))):
        raise ValueError("聚水潭收件信息缺失或不是文本")


def validate_snapshot(snapshot, order_no):
    if (not isinstance(snapshot, OrderSnapshot) or not _nonempty_text(order_no)
            or not _nonempty_text(snapshot.order_no) or snapshot.order_no != order_no
            or type(snapshot.found) is not bool):
        raise ValueError("聚水潭返回的订单标识不一致")
    if not isinstance(snapshot.records, (tuple, list)) or any(not isinstance(record, GiftRecord) for record in snapshot.records):
        raise ValueError("聚水潭记录格式不正确")
    identifiers = [record.record_id for record in snapshot.records]
    if any(not _nonempty_text(identifier) for identifier in identifiers) or len(set(identifiers)) != len(identifiers):
        raise ValueError("聚水潭记录缺少稳定且唯一的标识")
    _validate_recipient(snapshot.recipient)
    for record in snapshot.records:
        if (not isinstance(record.gift_ids, (tuple, list)) or not record.gift_ids
                or any(not _nonempty_text(identifier) for identifier in record.gift_ids)
                or len(set(record.gift_ids)) != len(record.gift_ids)):
            raise ValueError("聚水潭商品编码缺失、重复或不是文本")
        if not isinstance(record.reference, str):
            raise ValueError("聚水潭记录关联凭证不是文本")
        if (not isinstance(record.gift_quantities, (tuple, list))
                or (record.gift_quantities and len(record.gift_quantities) != len(record.gift_ids))
                or any(type(quantity) is not int or quantity <= 0 for quantity in record.gift_quantities)):
            raise ValueError("聚水潭商品数量缺失、无效或与商品编码不对应")
        _validate_recipient(record.recipient)


def validate_receipt(receipt):
    if (not isinstance(receipt, SubmissionReceipt) or type(receipt.accepted) is not bool
            or not isinstance(receipt.reference, str)
            or not isinstance(receipt.created_record_ids, (tuple, list))
            or any(not _nonempty_text(identifier) for identifier in receipt.created_record_ids)
            or len(set(receipt.created_record_ids)) != len(receipt.created_record_ids)):
        raise ValueError("班牛提交凭证格式不正确")


def verify_new_records(request, before, after, receipt):
    """只接受提交前不存在、且订单 / 礼品 / 必要地址信息正确的新记录。"""
    validate_snapshot(before, request.order_no)
    validate_snapshot(after, request.order_no)
    validate_receipt(receipt)
    if not before.found or not receipt.accepted:
        return False, "原订单或班牛提交状态未确认", []
    if not after.found:
        return False, "复查未找到原订单", []
    previous_ids = {record.record_id for record in before.records}
    added = [record for record in after.records if record.record_id not in previous_ids]
    if receipt.created_record_ids:
        expected_ids = set(receipt.created_record_ids)
        if {record.record_id for record in added} != expected_ids:
            # 回执之外的新增记录同样属于本次复查范围，不能过滤掉后按其余记录判成功。
            return False, "聚水潭新增记录与本次提交回执不一致", []
    if receipt.reference:
        if any(record.reference != receipt.reference for record in added):
            return False, "聚水潭新增记录的关联凭证与本次提交不一致", []
    if not added:
        return False, "聚水潭未发现本次登记的新增信息", []
    actual_quantities = Counter()
    for record in added:
        quantities = record.gift_quantities or (1,) * len(record.gift_ids)
        actual_quantities.update(dict(zip(record.gift_ids, quantities)))
    # 前端每项礼品仅可选择一次；同编码在不同新增单据中出现也必须累计。
    expected_quantities = Counter({identifier: 1 for identifier in request.gift_ids})
    if actual_quantities != expected_quantities:
        return False, "聚水潭新增信息的礼品或数量与草稿不一致", []
    # 本版本不读取或核对原收件信息；保留显式收件请求的校验供历史记录核验。
    expected_recipient = request.recipient
    if expected_recipient is not None and any(not _same_recipient(expected_recipient, record.recipient) for record in added):
        return False, "聚水潭新增信息的姓名、电话或地址与草稿不一致", []
    return True, "聚水潭已确认本次新增礼赠信息正确", [record.record_id for record in added]


class Workflow:
    def __init__(self, operations: PlatformOperations):
        self.operations = operations

    def execute(self, row, catalog, report: Callable):
        stage = "querying"
        try:
            request = request_from_row(row, catalog)
            if request.recipient is not None:
                report("failed", detail="本版本不支持改地址登记；未查询或提交，请关闭改地址选项")
                return
            report("querying", detail="正在聚水潭查询订单")
            before = self.operations.query_order(request.order_no)
            validate_snapshot(before, request.order_no)
            if not before.found:
                report("failed", detail="聚水潭未找到该订单；未进行班牛登记")
                return
            stage = "submitting"
            # 必须先持久化已尝试标记，再调用有副作用的平台提交。
            report("submitting", detail="正在班牛登记并提交", submit_attempted=True, attempts=1,
                   evidence={"before_record_ids": [record.record_id for record in before.records]})
            receipt = self.operations.submit_registration(request, before)
            validate_receipt(receipt)
            if receipt.accepted is not True:
                report("failed", detail="班牛未确认提交成功，请人工核查；不会自动重试登记")
                return
            stage = "verifying"
            report("verifying", detail="班牛已提交，正在聚水潭复查新增信息", reference=receipt.reference)
            # 可选适配器只做有界的只读复查；不会再次调用班牛提交方法。
            wait_for_verification = getattr(self.operations, "wait_for_verification", None)
            after = (wait_for_verification(request, before, receipt) if callable(wait_for_verification)
                     else self.operations.query_order(request.order_no))
            success, detail, identifiers = verify_new_records(request, before, after, receipt)
            report("success" if success else "failed", detail=detail,
                   evidence={"verified_record_ids": identifiers})
        except Exception as exc:
            label = {"querying": "聚水潭首次查询失败，未进行班牛登记", "submitting": "班牛提交异常，结果可能不确定，请人工核查",
                     "verifying": "聚水潭复查失败，未能确认新增信息"}[stage]
            reason = str(exc) if isinstance(exc, PlatformOperationError) else type(exc).__name__
            report("failed", detail=f"{label}（{reason}）；不会自动重试登记")
