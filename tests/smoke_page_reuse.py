"""真实 Chrome 的页面复用验收；只访问本机合成页面，绝不登记真实订单。"""

import sys
import tempfile
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gift_registry import banniu_page, jushuitan_page
from gift_registry.banniu_receipt import BanniuReceiptReader
from gift_registry.browser import create_driver
from gift_registry.config import ROOT, Site
from gift_registry.driver_shutdown import quit_owned_driver
from gift_registry.workflow import PlatformOperationError, RegistrationRequest, SubmissionReceipt, verify_new_records


JST_HTML = """
<iframe id="s_filter_frame" src="/filter"></iframe>
<div id="_jt_body" style="height:160px;overflow:auto"><div id="_jt_body_list"><div class="rowList"></div></div></div>
<div id="_jt_pagebar">总共 0 页 0 条记录</div>
<script>
window.queryCount=0; window.added=false; window.freeze=false;
window.renderOrder=function(order) {
  window.queryCount++;
  if(window.freeze) return;
  setTimeout(()=>{
    const row=(id,sku,index)=>`<div class="_jt_row" index="${index}">
      <span data-id="o_id"><span class="o_id">${id}</span></span>
      <span data-id="so_id">${order}</span>
      <span data-id="its"><div title="STYLE&#10;${sku}&#10;合成商品"><span class="sku_qty">1</span></div></span>
      <span data-id="skuCount"><span class="skuCount">1</span></span></div>`;
    document.querySelector('.rowList').innerHTML=row('10'+order,'ORIGINAL',0)+(window.added?row('20'+order,'SKU-A',1):'');
    document.querySelector('#_jt_pagebar').textContent=`总共 1 页 ${window.added?2:1} 条记录`;
  },70);
};
</script>
"""
FILTER_HTML = '<input id="so_id" onkeydown="if(event.key===\'Enter\') parent.renderOrder(this.value)">'
BN_HTML = """
<div class="title el-dropdown-selfdefine">聚水潭赠品补发-融合</div>
<button class="edit-task-button-btn" onclick="window.newTasks++">新建</button>
<div class="advancedFilter"><div id="list_1891" class="advancedFilter-top-item">
  <span class="advancedFilter-title" title="订单号">订单号</span>
  <span class="bnRule-down">等于</span><textarea></textarea>
</div></div>
<button class="el-button--primary el-button--mini" onclick="filterOrder()">筛选</button>
<div class="vxe-table"><table class="vxe-table--header"><tr>
  <th colid="order">订单号</th><th colid="created">创建时间</th>
</tr></table><table class="vxe-table--body"><tbody></tbody></table></div>
<div class="el-pagination__total">共 0 条</div>
<div class="el-loading-mask" style="position:fixed;inset:300px 0 0;opacity:0;pointer-events:none">合成淡出遮罩</div>
<script>
window.filterCount=0;window.newTasks=0;window.freeze=sessionStorage.getItem('freeze')==='true';
function filterOrder() {
  window.filterCount++;
  if(window.freeze) return;
  const order=document.querySelector('#list_1891 textarea').value;
  setTimeout(()=>{
    document.querySelector('.vxe-table--body tbody').innerHTML=`<tr rowid="10${order}">
      <td colid="order"><span class="worksheetColumn worksheetColumn-hover" onclick="openSyntheticDetail()">${order}</span></td>
      <td colid="created"><span class="worksheetColumn">2026-01-01 00:00:00</span></td></tr>`;
    document.querySelector('.el-pagination__total').textContent='共 1 条';
  },70);
}
function openSyntheticDetail() {
  window.openedDetails=(window.openedDetails||0)+1;
  const detail=document.createElement('div');detail.className='task-details';
  detail.style='position:fixed;inset:0;z-index:999;background:white';
  detail.textContent='合成详情';document.body.append(detail);
}
</script>
"""


def must_reject_stale(action):
    try:
        action()
    except PlatformOperationError:
        return
    raise AssertionError('Unrefreshed results must not be reused')


def main():
    requests = Counter()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            pages = {'/jst': JST_HTML, '/filter': FILTER_HTML, '/banniu': BN_HTML}
            body = pages.get(self.path)
            if body is None:
                self.send_error(404)
                return
            requests[self.path] += 1
            content = ('<!doctype html><html><head><meta charset="utf-8">'
                       '<style>.vxe-table{min-height:24px}</style></head><body>' + body + '</body></html>').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    temporary_root = ROOT / '.tmp'
    temporary_root.mkdir(exist_ok=True)
    drivers = []
    try:
        with tempfile.TemporaryDirectory(prefix='page-reuse-', dir=temporary_root) as directory:
            origin = f'http://127.0.0.1:{server.server_port}'
            try:
                with patch.object(jushuitan_page, 'ORDER_URL', origin + '/jst'), \
                     patch.object(banniu_page, 'BANNIU_REGISTRATION_URL', origin + '/banniu#/worksheet'):
                    for key in ('jst', 'banniu'):
                        site = Site('reuse-' + key, '本地页面复用验收', origin + '/' + key, ('127.0.0.1',))
                        drivers.append(create_driver(site, headless=True, profile=Path(directory) / key))
                    jst, bn = drivers
                    page = jushuitan_page.JushuitanPage(jst, timeout=2)
                    for order in ('00001', '00002', '00003'):
                        if requests['/jst']:
                            jst.execute_script('window.added=false')
                        before = page.query_order(order)
                        jst.execute_script('window.added=true')
                        after = page.query_order(order)
                        request = RegistrationRequest(order, ('SKU-A',), ('合成商品',))
                        receipt = SubmissionReceipt(True, created_record_ids=('20' + order,))
                        assert verify_new_records(request, before, after, receipt)[0]
                    assert requests['/jst'] == 1, requests
                    assert jst.execute_script('return window.queryCount') == 6
                    jst.execute_script('window.freeze=true')
                    must_reject_stale(lambda: page.query_order('00003'))
                    assert requests['/jst'] == 1
                    assert jst.execute_script('return window.queryCount') == 7

                    reader = BanniuReceiptReader(banniu_page.BanniuPage(bn, timeout=2))
                    for order in ('00001', '00001', '00002', '00002', '00003', '00003'):
                        rows = reader.query_tasks(order)
                        assert len(rows) == 1 and rows[0]['order_no'] == order
                    assert requests['/banniu'] == 1, requests
                    assert bn.execute_script('return window.filterCount') == 6
                    assert bn.execute_script('return window.newTasks') == 0
                    bn.execute_script('window.freeze=true')
                    # 丢失的一次筛选响应可刷新一次只读重查，不进入新建。
                    assert reader.query_tasks('00003')[0]['order_no'] == '00003'
                    assert requests['/banniu'] == 2
                    assert bn.execute_script('return window.newTasks') == 0
                    bn.execute_script("window.freeze=true;sessionStorage.setItem('freeze','true')")
                    must_reject_stale(lambda: reader.query_tasks('00003'))
                    assert requests['/banniu'] == 3
                    assert bn.execute_script('return window.filterCount') == 1
                    bn.execute_script("sessionStorage.removeItem('freeze')")
                    bn.execute_script("const detail=document.createElement('div');detail.className='task-details';detail.style='position:fixed;inset:0;z-index:999;background:white';detail.textContent='合成详情';document.body.append(detail)")
                    # 带 hash 的同地址 get 会保留详情 DOM，不能把它当成关闭详情。
                    bn.get(origin + '/banniu#/worksheet')
                    assert len(bn.find_elements('css selector', '.task-details')) == 1
                    assert requests['/banniu'] == 3
                    assert reader.query_tasks('00004')[0]['order_no'] == '00004'
                    assert requests['/banniu'] == 4
                    assert bn.execute_script('return window.newTasks') == 0
                    bn.execute_script("const back=document.createElement('div');back.className='worksheet-main-details-back';back.style='position:fixed;inset:0;z-index:999';document.body.append(back)")
                    assert reader.query_tasks('00005')[0]['order_no'] == '00005'
                    assert requests['/banniu'] == 5
                    bn.execute_script("const back=document.createElement('div');back.className='synthetic-unknown-blocker';back.style='position:fixed;inset:0;z-index:999';document.body.append(back)")
                    assert reader.query_tasks('00006')[0]['order_no'] == '00006'
                    assert requests['/banniu'] == 6
                    assert bn.execute_script('return window.newTasks') == 0
                    # opacity:0 的遮罩仍可拦截指针；等待实际落点放行后才点击一次。
                    bn.execute_script("const mask=document.querySelector('.el-loading-mask');mask.style.inset='0';mask.style.pointerEvents='auto';setTimeout(()=>mask.style.pointerEvents='none',500)")
                    reader.open_task('00006', '1000006')
                    assert bn.execute_script('return window.openedDetails') == 1
                    assert reader.query_tasks('00007')[0]['order_no'] == '00007'
                    assert requests['/banniu'] == 7
                    print('PASS: reused lists still require fresh queries; hash navigation reproduced the retained detail bug; a single refresh removed detail/backdrop/unknown click blockers; transparent loading masks did not block readiness. Local synthetic pages only; zero registration submissions.')
            finally:
                for driver in drivers:
                    quit_owned_driver(driver)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == '__main__':
    main()
