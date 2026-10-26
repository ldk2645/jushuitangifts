"""隔离 Chrome 验证表单归属与可信输入事件；只加载本地合成页面。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from selenium.webdriver.common.by import By
from gift_registry.banniu_page import BanniuPage
from gift_registry.browser import create_driver
from gift_registry.config import ROOT, Site
from gift_registry.driver_shutdown import quit_owned_driver
from gift_registry.workflow import PlatformOperationError, RegistrationRequest

HTML = '''<!doctype html><html><meta charset="utf-8"><style>
body{font:16px sans-serif}.w-edit-task-dialog{padding:20px;width:600px;border:1px solid #aaa}
.w-dialog-title{height:35px}.w-dialog-close{width:30px;height:30px;cursor:pointer;border:1px solid #aaa}
textarea,input{display:block;margin:10px 0}th{padding:5px}
</style><body><div class="w-edit-task-dialog">
<div class="w-dialog-title"><div class="w-dialog-close" onclick="window.closeCount++;this.closest('.w-edit-task-dialog').style.display='none'">×</div></div>
<div id="col_1891"><textarea></textarea></div><input id="manual-note" aria-label="合成人工备注">
<div id="col_1915"><div class="children-box-body"><div class="el-table">
<div class="el-table__header-wrapper"><table><tr><th>退换补类型</th><th>商家编码</th><th>货品名称</th><th>退换补商品金额</th><th>数量</th></tr></table></div>
<div class="el-table__body-wrapper"><table><tbody></tbody></table></div>
</div></div></div><button id="synthetic-submit" type="button" onclick="window.submitCount++">提交</button>
</div><script>window.closeCount=0;window.submitCount=0;</script></body></html>'''


def main():
    folder_root = ROOT / '.tmp'
    folder_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='owned-form-', dir=folder_root) as directory:
        folder = Path(directory)
        fixture = folder / 'form.html'
        fixture.write_text(HTML, encoding='utf-8')
        site = Site('owned-form-test', '隔离表单清理验收', fixture.as_uri(), ())
        driver = create_driver(site, headless=True, profile=folder / 'profile')
        request = RegistrationRequest('00001', ('TEST-SKU',), ('合成礼品',))

        def fresh():
            driver.get(fixture.as_uri())
            page = BanniuPage(driver, timeout=1)
            root = page._dialog()
            page._track_form(root, request)
            page._set_text('#col_1891 textarea', request.order_no)
            return page, root

        try:
            page, root = fresh()
            assert not driver.execute_script('return arguments[0].__giftRegistrationOwner.manualEdited', root)
            assert page._discard_unsubmitted_form(request) == 'closed'
            assert driver.execute_script('return window.closeCount') == 1
            assert driver.execute_script('return window.submitCount') == 0
            # Element UI 可复用隐藏的表单节点；前次监听器与标记必须清理。
            driver.execute_script("arguments[0].style.display='block'", root)
            page._track_form(root, request)
            assert page._discard_unsubmitted_form(request) == 'closed'
            assert driver.execute_script('return window.closeCount') == 2

            page, root = fresh()
            driver.find_element(By.ID, 'manual-note').send_keys('合成人工编辑')
            assert driver.execute_script('return arguments[0].__giftRegistrationOwner.manualEdited', root)
            assert page._discard_unsubmitted_form(request) == 'preserved'
            try:
                page._set_text('#col_1891 textarea', '00002')
            except PlatformOperationError:
                pass
            else:
                raise AssertionError('Automation must stop after trusted manual input')
            assert page._value('#col_1891 textarea') == '00001'
            assert driver.execute_script('return window.closeCount') == 0

            page, root = fresh()
            # 即使提交由自动动作发出、未标为人工编辑，提交交互仍单独阻止清理。
            page._click_once(driver.find_element(By.ID, 'synthetic-submit'))
            assert driver.execute_script('return arguments[0].__giftRegistrationOwner.submitSeen', root)
            assert not driver.execute_script('return arguments[0].__giftRegistrationOwner.manualEdited', root)
            assert page._discard_unsubmitted_form(request) == 'preserved'
            assert driver.execute_script('return window.submitCount') == 1
            assert driver.execute_script('return window.closeCount') == 0

            page, root = fresh()
            driver.execute_script("arguments[0].querySelector('#col_1891 textarea').value='another-order'", root)
            assert page._discard_unsubmitted_form(request) == 'preserved'
            assert driver.execute_script('return window.closeCount') == 0

            page, root = fresh()
            driver.execute_script("arguments[0].querySelector('.el-table__body-wrapper tbody').innerHTML='<tr><td>补发</td><td>OTHER-SKU</td><td>错误商品</td><td>0</td><td>1</td></tr>'", root)
            assert page._discard_unsubmitted_form(request) == 'preserved'
            foreign = BanniuPage(driver, timeout=1)
            assert foreign._discard_unsubmitted_form(request) == 'none'
            assert root.is_displayed()
            assert driver.execute_script('return window.closeCount') == 0
            print('PASS: owned unsubmitted forms close once; reused DOM loses old markers; trusted manual edits stop automation and preserve content; submit interaction, changed order/SKU and foreign forms prevent cleanup. Local synthetic HTML only; no real platform registration.')
        finally:
            quit_owned_driver(driver)


if __name__ == '__main__':
    main()
