"""Selenium 隔离验收：同名新增、整组防重、精确重登、多分组及合成班牛详情。"""
import json
import sys
import tempfile
import threading
from pathlib import Path
from html import escape

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait
from gift_registry.banniu_page import BanniuPage, GIFT_COLUMNS
from gift_registry.banniu_receipt import BanniuReceiptReader
from gift_registry.browser import create_driver
from gift_registry.config import ROOT, Site
from gift_registry.frontend_server import RowStore, make_server
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.registration import RegistrationQueue
from gift_registry.workflow import PlatformOperationError, RegistrationRequest
from test_task_identity import CATALOG, row
from test_host_management import RetryOperations


def main():
    output = ROOT / '.tmp' / 'ui-checks'
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='gift-identity-', dir=ROOT / '.tmp') as directory:
        folder = Path(directory)
        records = [row('a'), row('b', ('001-B',)), row('a-copy')]
        store = RowStore(folder / 'rows.json')
        store.write(0, records)
        catalog = folder / 'gifts.json'
        catalog.write_text(json.dumps(CATALOG[:2]), encoding='utf-8')
        operations = RetryOperations({'SYNTHETIC-ORDER': 'query_error'})
        jobs = RegistrationQueue(folder / 'jobs', operations=operations)
        jobs.start(records, CATALOG[:2])
        jobs.pending.join()
        server = make_server(0, store=store, queue=jobs, catalog=GiftCatalogStore(catalog))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        driver = None
        try:
            site = Site('gift-identity', '任务身份隔离验收', f'http://127.0.0.1:{server.server_port}/', ('127.0.0.1',))
            driver = create_driver(site, headless=True, profile=folder / 'browser')
            driver.set_window_size(1440, 1000)
            driver.get(site.url)
            wait = WebDriverWait(driver, 15)
            find = lambda selector: driver.find_element(By.CSS_SELECTOR, selector)
            js = driver.execute_script
            wait.until(lambda d: js('return initialized'))
            assert '1 组' in find('#duplicate-summary').text
            assert 'duplicate-order' not in find('tr[data-id="b"] td[data-field="order_no"]').get_attribute('class')
            for code in ('001-C', '001A'):
                find('#add-gift').click()
                find('#gift-create-code').send_keys(code)
                find('#gift-create-name').send_keys('合成同名礼品')
                find('#gift-create-submit').click()
                wait.until(lambda d: not find('#gift-create-dialog').get_property('open'))
            assert js('return matchGifts("001A").ids') == ['001A']
            assert js('return matchGifts("001-A").ids') == ['001-A']
            assert js('return matchGifts("合成同名礼品").ids') == []
            find('#add-gift').click()
            find('#gift-create-code').send_keys('001-A')
            find('#gift-create-name').send_keys('合成同名礼品')
            find('#gift-create-submit').click()
            wait.until(lambda d: find('#gift-create-error').is_displayed())
            assert '编码已存在' in find('#gift-create-error').text
            find('#gift-create-cancel').click()
            find('tr[data-id="a"] .gift-trigger').click()
            find('#picker-manage-groups').click()
            wait.until(lambda d: find('#groups-dialog').get_property('open'))
            find('#group-gift-list input[value="001-A"]').click()
            for revision, group in enumerate(('xiaomi-ecosystem', 'xiaomi-phone'), 1):
                find(f'#group-targets input[value="{group}"]').click()
                find('#assign-groups').click()
                wait.until(lambda d: js('return giftGroups.revision') == revision)
            assert js('return giftGroups.memberships["001-A"]') == ['xiaomi-ecosystem', 'xiaomi-phone']
            driver.save_screenshot(str(output / 'task-identity-groups.png'))
            find('#groups-close').click()
            find('tr[data-id="a"] .gift-trigger').click()
            for group in ('xiaomi-ecosystem', 'xiaomi-phone'):
                Select(find('#gift-group-filter')).select_by_value(group)
                assert find('#gift-options input[value="001-A"]').is_displayed()
            find('#gift-done').click()
            operations.modes.clear()
            find('tr[data-id="b"] .row-retry').click()
            wait.until(lambda d: find('tr[data-id="b"] .status-badge').text == '成功')
            assert [request.gift_ids for request in operations.submissions] == [('001-B',)]
            assert jobs.states()['a']['state'] == 'failed'
            find('#retry-failures').click()
            wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, '.retry-card')) == 1)
            assert '001-A' in find('#retry-list').text and '001-B' not in find('#retry-list').text
            driver.save_screenshot(str(output / 'task-identity-retry.png'))
            find('#retry-close').click()
            driver.refresh()
            wait.until(lambda d: js('return initialized'))
            assert len(js('return giftCatalog.filter(g=>g.name==="合成同名礼品")')) == 4
            assert js('return giftGroups.memberships["001-A"]') == ['xiaomi-ecosystem', 'xiaomi-phone']

            # 使用生产详情提取器读取合成 HTML，逐字段/编码核对，不访问业务平台。
            driver.get('about:blank')
            headers = ''.join('<th>' + escape(name) + '</th>' for name in GIFT_COLUMNS)
            def fixture(code):
                cells = ''.join('<td>' + escape(value) + '</td>' for value in ('补发', code, '合成同名礼品', '0', '1'))
                html = f'''<div class="vxe-table--body"><table><tbody><tr rowid="001"><td><button class="worksheetColumn-hover">SYNTHETIC-ORDER</button></td></tr></tbody></table></div>
                <div class="task-details" style="display:none">
                <div class="task-details-component-box"><span class="task-details-component-title" title="工单编号"></span><span class="show-value-content">001</span></div>
                <div class="task-details-component-box"><span class="task-details-component-title" title="订单号"></span><span class="show-value-content">SYNTHETIC-ORDER</span></div>
                <div id="col_1915"><div class="el-table"><div class="el-table__header-wrapper"><table><tr>{headers}</tr></table></div>
                <div class="el-table__body-wrapper"><table><tbody><tr>{cells}</tr></tbody></table></div></div></div></div>'''
                js('document.body.innerHTML=arguments[0];document.querySelector("button").addEventListener("click",()=>document.querySelector(".task-details").style.display="block");', html)
            reader = BanniuReceiptReader(BanniuPage(driver))
            request = RegistrationRequest('SYNTHETIC-ORDER', ('001-A',), ('合成同名礼品',))
            fixture('001-B')
            assert reader.check_distinct_gifts(request, [{'task_id': '001'}])
            fixture('001-A')
            try:
                reader.check_distinct_gifts(request, [{'task_id': '001'}])
            except PlatformOperationError as error:
                assert '已有该订单及礼品组合' in str(error)
            else:
                raise AssertionError('Same gift codes must block retry')
            print('PASS: isolated Chrome same-name add/exact-code match, duplicate task highlighting, gift-scoped row retry, multi-group persistence, production detail reader on synthetic HTML.')
        finally:
            if driver is not None:
                driver.quit()
            jobs.pending.join()
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    main()
