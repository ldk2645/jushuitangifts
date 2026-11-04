"""本地 Selenium 验收：提交后表单不关闭，下一单刷新一次后继续；不访问真实平台。"""
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selenium.webdriver.common.by import By
from gift_registry import banniu_page
from gift_registry.banniu_receipt import BanniuReceiptReader
from gift_registry.browser import create_driver
from gift_registry.config import ROOT, Site
from gift_registry.driver_shutdown import quit_owned_driver
from gift_registry.platform_operations import SeleniumOperations
from gift_registry.workflow import OrderSnapshot, PlatformOperationError, RegistrationRequest, SubmissionReceipt
from smoke_banniu_form_cleanup import HTML as FORM_HTML
from smoke_page_reuse import BN_HTML

FORM_BODY = FORM_HTML.split('<body>', 1)[1].split('<script>', 1)[0]
EXTRA = r'''
<style>body{font:16px sans-serif}.vxe-table{min-height:30px}
.w-edit-task-dialog{position:fixed;inset:100px 20px;z-index:5;background:white;padding:20px}
textarea,input{display:block;margin:10px 0}th{padding:6px}</style>
<script>
sessionStorage.setItem('loads',String(Number(sessionStorage.getItem('loads')||0)+1));
window.filterOrder=function(){
 const order=document.querySelector('#list_27724 textarea').value;
 const filters=JSON.parse(sessionStorage.getItem('filters')||'[]');filters.push(order);
 sessionStorage.setItem('filters',JSON.stringify(filters));
 setTimeout(()=>{document.querySelector('.vxe-table--body tbody').innerHTML='<!--fresh-'+filters.length+'-->';
 document.querySelector('.el-pagination__total').textContent='共 0 条';},50);
};
window.syntheticSubmit=function(button){
 const form=button.closest('.w-edit-task-dialog'),order=form.querySelector('#col_27724 textarea').value;
 const counts=JSON.parse(sessionStorage.getItem('submits')||'{}');counts[order]=(counts[order]||0)+1;
 sessionStorage.setItem('submits',JSON.stringify(counts));
 if(!['00001','00003','00005'].includes(order))form.remove();
};
</script>
'''


def main():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            body=('<meta charset="utf-8">'+BN_HTML+EXTRA).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server=ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin=f'http://127.0.0.1:{server.server_port}/'
    try:
        with tempfile.TemporaryDirectory(prefix='stalled-form-', dir=ROOT/'.tmp') as folder:
            driver=create_driver(Site('stalled-test', '合成表单恢复验收', origin, ('127.0.0.1',)),
                                 headless=True, profile=Path(folder)/'profile')
            try:
                with patch.object(banniu_page, 'BANNIU_REGISTRATION_URL', origin), \
                     patch.object(BanniuReceiptReader, 'wait_for_receipt', return_value=SubmissionReceipt(True, created_record_ids=('00099',))):
                    driver.get(origin)
                    page=banniu_page.BanniuPage(driver, timeout=2)
                    page._validate_prepared=Mock()  # 本测试只覆盖恢复、筛选及单次提交，不替代完整业务字段验收。

                    def fill(request):
                        driver.execute_script('document.body.insertAdjacentHTML("beforeend",arguments[0])', FORM_BODY)
                        page._track_form(page._dialog(), request)
                        page._set_text('#col_27724 textarea', request.order_no)
                        driver.execute_script('''
                          document.querySelector('#col_27748 .el-table__body-wrapper tbody').innerHTML='<tr><td>补发</td><td>TEST-SKU</td><td>合成礼品</td><td>0</td><td>1</td></tr>';
                          const button=document.querySelector('#synthetic-submit');
                          button.className='el-button--primary';button.setAttribute('onclick','syntheticSubmit(this)');
                        ''')

                    page._fill_prepared=fill
                    request=lambda order: RegistrationRequest(order, ('TEST-SKU',), ('合成礼品',))

                    def expect_stalled(order):
                        try:
                            page.submit(request(order), OrderSnapshot(order, True))
                        except PlatformOperationError as exc:
                            assert '表单未关闭' in str(exc)
                        else:
                            raise AssertionError('Synthetic form must remain open after submit')

                    expect_stalled('00001')
                    assert page.submit(request('00002'), OrderSnapshot('00002', True)).accepted
                    assert driver.execute_script('return sessionStorage.getItem("loads")')=='2'
                    assert json.loads(driver.execute_script('return sessionStorage.getItem("submits")'))=={'00001':1,'00002':1}
                    try:
                        page.submit(request('00001'), OrderSnapshot('00001', True))
                    except PlatformOperationError as exc:
                        assert '禁止再次提交' in str(exc)
                    else:
                        raise AssertionError('Refreshing must not reset submission guard')

                    # 主机重登核查也使用同一恢复入口，随后仍执行空任务基线复查。
                    expect_stalled('00003')
                    operations=SeleniumOperations(browsers=Mock(), form_factory=lambda _:page)
                    operations._banniu=Mock(return_value=page)
                    assert operations.check_retry_safety(request('00004'),OrderSnapshot('00004',True),{'before_record_ids':[]})
                    assert operations.submit_registration(request('00004'),OrderSnapshot('00004',True)).accepted
                    assert driver.execute_script('return sessionStorage.getItem("loads")')=='3'
                    assert json.loads(driver.execute_script('return sessionStorage.getItem("filters")'))==['00001','00002','00003','00004','00004']

                    # 可信人工编辑过的表单不能被自动刷新抹掉。
                    expect_stalled('00005')
                    driver.find_element(By.ID,'manual-note').send_keys('合成人工内容')
                    for guarded_page in (page, banniu_page.BanniuPage(driver,timeout=2)):
                        try:
                            BanniuReceiptReader(guarded_page).query_tasks('00006')
                        except PlatformOperationError as exc:
                            assert '未关闭的任务表单' in str(exc)
                        else:
                            raise AssertionError('Manual/foreign form must be preserved')
                    assert driver.execute_script('return sessionStorage.getItem("loads")')=='3'
                    assert driver.find_element(By.ID,'manual-note').get_attribute('value')=='合成人工内容'
                    counts=json.loads(driver.execute_script('return sessionStorage.getItem("submits")'))
                    assert counts=={order:1 for order in ('00001','00002','00003','00004','00005')}
                    print('PASS: stalled submitted forms refresh once; next order and host retry requery before one submit; prior submission cannot replay; manual/foreign forms preserved. Local synthetic Selenium only.')
            finally:
                quit_owned_driver(driver)
    finally:
        server.shutdown()
        server.server_close()


if __name__=='__main__':
    main()
