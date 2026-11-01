"""复现班牛横向虚拟列缺失：本地合成页面，无真实订单或提交。"""
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gift_registry import banniu_page
from gift_registry.banniu_receipt import BanniuReceiptReader, _LIST
from gift_registry.browser import create_driver
from gift_registry.config import ROOT, Site
from gift_registry.workflow import PlatformOperationError
from smoke_page_reuse import BN_HTML

VIRTUAL = """
<style>.vxe-table--body-wrapper{width:600px;height:150px;overflow:auto}.space{width:5000px;height:1px}</style>
<script>
const table=document.querySelector('.vxe-table'); table.classList.add('is--virtual-x');
table.innerHTML='<div class="vxe-table--main-wrapper"><div class="vxe-table--body-wrapper"><div class="space"></div><table class="vxe-table--header"></table><table class="vxe-table--body"></table></div></div>';
const scrollBox=table.querySelector('.vxe-table--body-wrapper');
window.currentOrder='OLD';window.datasetVersion=0;window.virtualScrolls=0;
window.renderVirtual=function(){
 const includeOrder=scrollBox.scrollLeft>=600;
 const includeTime=scrollBox.scrollLeft<2400;
 const header=(includeOrder?'<th colid="order">订单号</th>':'')+(includeTime?'<th colid="created">创建时间</th>':'');
 table.querySelector('.vxe-table--header').style.marginLeft=includeOrder?'900px':'0';
 table.querySelector('.vxe-table--body').style.marginLeft=includeOrder?'900px':'0';
 table.querySelector('.vxe-table--header').innerHTML='<tr>'+header+'</tr>';
 const order=window.wrongOrder?'OTHER':window.currentOrder;
 const count=window.currentOrder==='EMPTY'?0:1;
 document.querySelector('.el-pagination__total').textContent='共 '+(window.extraPage?2:count)+' 条';
 table.querySelector('.vxe-table--body').innerHTML=count?'<tr rowid="001"><td colid="order">'+(includeOrder?'<span class="worksheetColumn worksheetColumn-hover" onclick="openSyntheticDetail()">'+order+'</span>':'')+'</td><td colid="created">'+(includeTime?'<span class="worksheetColumn">2026-01-01 00:00:00</span>':'')+'</td></tr>':'';
};
scrollBox.addEventListener('scroll',()=>{window.virtualScrolls++;setTimeout(renderVirtual,30)});
window.filterOrder=function(){window.filterCount++;if(window.freeze)return;setTimeout(()=>{window.currentOrder=document.querySelector('#list_1891 textarea').value;window.datasetVersion++;renderVirtual()},70)};
renderVirtual();
</script>
"""


def main():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            body=('<meta charset="utf-8">'+BN_HTML+VIRTUAL).encode()
            self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    origin=f'http://127.0.0.1:{server.server_port}/'
    try:
        with tempfile.TemporaryDirectory(prefix='virtual-columns-',dir=ROOT/'.tmp') as folder:
            driver=create_driver(Site('virtual','virtual',origin,('127.0.0.1',)),headless=True,profile=Path(folder)/'profile')
            try:
                with patch.object(banniu_page,'BANNIU_REGISTRATION_URL',origin):
                    page=banniu_page.BanniuPage(driver,timeout=5)
                    reader=BanniuReceiptReader(page)
                    driver.get(origin)
                    assert driver.execute_script(_LIST) is None  # 原实现在筛选前卡住。
                    assert driver.execute_script(banniu_page._REGISTRATION_LIST_READY) is True
                    result=reader.query_tasks('0000123')
                    assert result==[{'task_id':'001','order_no':'0000123','created_at':'2026-01-01 00:00:00'}]
                    assert driver.execute_script('return window.virtualScrolls')>0
                    assert driver.execute_script('return window.filterCount')==1
                    reader.open_task('0000123','001')
                    assert driver.execute_script('return window.openedDetails')==1
                    assert reader.query_tasks('EMPTY')==[]
                    assert driver.execute_script('return window.newTasks')==0
                    # 停在表尾、创建时间列已卸载，下一轮仍能从头滚回读取。
                    driver.execute_script("document.querySelector('.vxe-table--body-wrapper').scrollLeft=4000;renderVirtual()")
                    assert reader.query_tasks('0000456')[0]['order_no']=='0000456'
                    for flag in ('wrongOrder','extraPage','freeze'):
                        driver.get(origin)
                        driver.execute_script('window[arguments[0]]=true',flag)
                        # 单轮测试，避免合成页面刷新清除故障。
                        try:reader._query_tasks_once('0000789')
                        except PlatformOperationError:pass
                        else:raise AssertionError('unsafe list accepted: '+flag)
                        assert driver.execute_script('return window.newTasks')==0
                        if flag=='freeze':assert driver.execute_script('return window.virtualScrolls')==0
                    print('PASS: missing offscreen columns reproduced; filter before scroll; stable same-snapshot order/time; empty/nonempty/reuse/detail; wrong order, incomplete pagination and unrefreshed results rejected; zero submits.')
            finally:driver.quit()
    finally:server.shutdown();server.server_close()


if __name__=='__main__':main()
