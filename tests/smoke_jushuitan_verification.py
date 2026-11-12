"""本地合成 DOM 验收：只读核验内部订单号，使用隔离 Chrome profile。"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gift_registry.browser import create_driver
from gift_registry.config import ROOT, Site
from gift_registry.driver_shutdown import quit_owned_driver
from gift_registry.jushuitan_page import JushuitanPage, _GRID_STATE
from gift_registry.workflow import RegistrationRequest, SubmissionReceipt, verify_new_records


HTML = """<!doctype html><meta charset="utf-8">
<div id="_jt_body"><div id="_jt_body_list"><div class="rowList">
<div class="_jt_row" index="0"><span data-id="o_id"><span class="o_id">000048</span></span>
<span data-id="so_id">00001</span></div>
<div class="_jt_row" index="1"><span data-id="o_id"><span class="o_id">000049</span></span>
<span data-id="so_id">00001</span></div>
<div class="_jt_row" index="2"><span data-id="o_id"><span class="o_id">000050</span></span>
<span data-id="so_id">00001</span></div>
</div></div></div><div id="_jt_pagebar">总共 1 页 3 条记录</div>"""


def main():
    folder = ROOT / ".tmp"
    folder.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="verify-ids-", dir=folder) as directory:
        path = Path(directory)
        page_file = path / "orders.html"
        page_file.write_text(HTML, encoding="utf-8")
        driver = create_driver(Site("verify-ids", "本地核验", page_file.as_uri(), ()),
                               headless=True, profile=path / "profile")
        try:
            driver.get(page_file.as_uri())
            page = JushuitanPage(driver, timeout=2)
            # 单独检查真实 DOM 采集；查询输入及刷新边界由单元测试覆盖。
            page._query = lambda order, **kwargs: driver.execute_script(_GRID_STATE, "order_ids")
            after = page.query_order("00001", identity_only=True)
            assert [record.record_id for record in after.records] == ["000048", "000049", "000050"]
            assert all(record.gift_ids == () for record in after.records)
            from gift_registry.workflow import OrderSnapshot
            before = OrderSnapshot("00001", True, (after.records[0],))
            request = RegistrationRequest("00001", ("SKU",), ("合成商品",))
            receipt = SubmissionReceipt(True, created_record_ids=("000049",))
            success, _, identifiers = verify_new_records(request, before, after, receipt)
            assert success and identifiers == ["000049"]
            wrong = SubmissionReceipt(True, created_record_ids=("49",))
            assert not verify_new_records(request, before, after, wrong)[0]
            print("PASS: real DOM with no product details; exact new internal ID match; unrelated order excluded; leading zeros preserved. No remote platform or submissions.")
        finally:
            quit_owned_driver(driver)


if __name__ == "__main__":
    main()
