"""Python Selenium v1.2.0 隔离验收：合成订单、本地 HTTP、临时浏览器。"""
import json
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from gift_registry.browser import create_driver, stabilize_banniu_viewport
from gift_registry.config import ROOT, Site
from gift_registry.frontend_server import RowStore, make_server
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.registration import RegistrationQueue
from test_host_management import RetryOperations


def main():
    output = ROOT / '.tmp' / 'ui-checks'
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='v120-ui-', dir=ROOT / '.tmp') as directory:
        folder = Path(directory)
        catalog = [{'id': 'SKU-W1', 'name': '同名测试手表', 'aliases': ['白色测试表']},
                   {'id': 'SKU-W2', 'name': '同名测试手表', 'aliases': ['黑色测试表']},
                   {'id': 'SKU-T', 'name': '测试礼品', 'aliases': []}]
        records = [{'id': f'r{i}', 'order_no': f'0000000000000000000{i}', 'gift': catalog[0]['name'],
                    'gift_ids': ['SKU-W1'], 'change_address': False, 'recipient_name': '', 'phone': '', 'address': ''}
                   for i in range(1, 7)]
        catalog_path = folder / 'gifts.json'
        catalog_path.write_text(json.dumps(catalog), encoding='utf-8')
        store = RowStore(folder / 'rows.json')
        store.write(0, records)
        operations = RetryOperations({records[0]['order_no']: 'query_error', records[1]['order_no']: 'query_error',
                                      records[2]['order_no']: 'submit_timeout'})
        jobs = RegistrationQueue(folder / 'jobs', operations=operations)
        jobs.start(records[:4], catalog)
        jobs.pending.join()
        server = make_server(0, store=store, queue=jobs, catalog=GiftCatalogStore(catalog_path))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        driver = None
        try:
            site = Site('v120-ui', 'v1.2.0 隔离验收', f'http://127.0.0.1:{server.server_port}', ('127.0.0.1',))
            driver = create_driver(site, headless=True, profile=folder / 'profile')
            driver.set_window_size(1440, 1000)
            driver.get(site.url)
            wait = WebDriverWait(driver, 15)
            find = lambda selector: driver.find_element(By.CSS_SELECTOR, selector)
            js = driver.execute_script
            wait.until(lambda d: js('return initialized'))
            def saved():
                wait.until(lambda d: js('return version === savedVersion && !saving && !hostManaging'))
            def select(top, bottom, left='order_no', right=None):
                js('selectRange({id:arguments[0],field:arguments[2]}, {id:arguments[1],field:arguments[3]}); activate(arguments[0],arguments[2],true);', top, bottom, left, right or left)
            def paste(text):
                js("const data=new DataTransfer();data.setData('text/plain',arguments[0]);document.querySelector('#registry-grid').dispatchEvent(new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true}));", text)
                saved()
            def undo():
                find('#undo').click()
                saved()
            def restore():
                saved()
                doc = store.read()
                store.write(doc['revision'], records)
                driver.refresh()
                wait.until(lambda d: js('return initialized'))

            # 同名多 SKU：实际勾选、取消、重载、按编码复制，不再按名称反推身份。
            find('tr[data-id="r1"] .gift-trigger').click()
            find('#gift-options input[value="SKU-W2"]').click()
            assert js('return rows[0].gift_ids') == ['SKU-W1', 'SKU-W2']
            assert not find('#gift-unmatched').is_displayed()
            find('#gift-options input[value="SKU-W2"]').click()
            assert js('return rows[0].gift_ids') == ['SKU-W1']
            assert not find('#gift-unmatched').is_displayed()
            find('#gift-options input[value="SKU-W2"]').click()
            find('#gift-done').click()
            saved()
            driver.refresh()
            wait.until(lambda d: js('return initialized'))
            assert js('return rows[0].gift_ids') == ['SKU-W1', 'SKU-W2']
            assert js('return unresolvedGiftLabels(rows[0])') == []
            select('r1', 'r1', 'gift')
            js('window.copiedRange=clipboardContent()')
            select('r2', 'r2', 'gift')
            js("const data=new DataTransfer();data.setData('text/plain',window.copiedRange.text);data.setData('text/html',window.copiedRange.html);document.querySelector('#registry-grid').dispatchEvent(new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true}));")
            saved()
            assert js('return rows[1].gift_ids') == ['SKU-W1', 'SKU-W2']
            select('r3', 'r3', 'gift')
            paste('同名测试手表')
            assert js('return rows[2].gift_ids') == []
            find('tr[data-id="r3"] .gift-trigger').click()
            find('#gift-options input[value="SKU-W2"]').click()
            assert not find('#gift-unmatched').is_displayed()
            find('#gift-done').click()
            saved()
            assert js('return rows[2].gift_ids') == ['SKU-W2']
            restore()
            # 旧 bug 保存的重复同名“未匹配”文本修复时保留已确认 SKU 和真正未知文本。
            doc = store.read()
            doc['rows'][0]['gift'] = '同名测试手表、同名测试手表、完全未知礼物'
            store.write(doc['revision'], doc['rows'])
            driver.refresh()
            wait.until(lambda d: js('return initialized'))
            saved()
            assert js('return rows[0].gift_ids') == ['SKU-W1']
            assert js('return unresolvedGiftLabels(rows[0])') == ['完全未知礼物']
            restore()

            # 非整倍数区域、单格扩展、二维粘贴、单行填充和状态筛选隔离。
            select('r1', 'r5')
            paste('000A\n000B')
            assert js('return rows.slice(0,5).map(r=>r.order_no)') == ['000A', '000B', '000A', '000B', '000A']
            undo()
            select('r1', 'r2')
            paste('000A\n000B\n000C')
            assert js('return rows.slice(0,3).map(r=>r.order_no)') == ['000A', '000B', '000C']
            undo()
            select('r1', 'r5', 'order_no', 'gift')
            paste('000A\tSKU-W1\n000B\tSKU-W2')
            assert js('return rows.slice(0,5).map(r=>r.gift_ids)') == [['SKU-W1'], ['SKU-W2'], ['SKU-W1'], ['SKU-W2'], ['SKU-W1']]
            undo()
            Select(find('#status-filter')).select_by_value('failed')
            select('r1', 'r3')
            paste('000F')
            assert js('return rows.slice(0,4).map(r=>r.order_no)') == ['000F', '000F', '000F', records[3]['order_no']]
            undo()
            Select(find('#status-filter')).select_by_value('')

            # 删除本次 IP 提醒后轮询/刷新不重新弹出；新变化仍显示。
            notice = {'state': 'changed', 'change_id': 'synthetic-a', 'configured_ip': '192.168.1.1', 'domain': '', 'message': '隔离网络变化', 'current_addresses': []}
            js('renderNetworkNotice(arguments[0])', notice)
            find('#network-dismiss').click()
            js('renderNetworkNotice(arguments[0])', notice)
            assert not find('#network-notice').is_displayed()
            saved()
            driver.refresh()
            wait.until(lambda d: js('return initialized'))
            js('renderNetworkNotice(arguments[0])', notice)
            assert not find('#network-notice').is_displayed()
            notice['change_id'] = 'synthetic-b'
            js('renderNetworkNotice(arguments[0])', notice)
            assert find('#network-notice').is_displayed()
            find('#network-dismiss').click()

            # 主机行内重登与工具栏位置，远程视图不提供入口。
            assert find('#auto-fit-columns').find_element(By.XPATH, 'following-sibling::*[1]').get_attribute('id') == 'retry-failures'
            assert find('#retry-failures').find_element(By.XPATH, 'following-sibling::*[1]').get_attribute('id') == 'edit-status'
            assert find('tr[data-id="r1"] .row-retry').is_displayed()
            js('hostActionsAvailable=false;render()')
            assert not driver.find_elements(By.CSS_SELECTOR, '.row-retry')
            js('hostActionsAvailable=true;render()')

            # 主机人工状态独立显示、独立筛选，后端仍为失败且保留重登资格。
            find('#overview-toggle').click()
            select('r1', 'r1')
            find('#edit-status').click()
            Select(find('#manual-status')).select_by_value('success')
            # 模拟人工保存前发出、保存后才返回的旧状态轮询。
            js("window.originalApi=api;window.delayedStatus=null;api=async function(path,options){if(path==='/api/registrations/status'&&!window.delayedStatus){const old=await window.originalApi(path,options);return new Promise(resolve=>{window.delayedStatus=()=>resolve(old);});}return window.originalApi(path,options);};refreshExecutionStates();")
            wait.until(lambda d: js('return typeof window.delayedStatus === "function"'))
            find('#status-apply').click()
            wait.until(lambda d: not find('#status-dialog').get_property('open'))
            js('api=window.originalApi;window.delayedStatus();')
            wait.until(lambda d: js('return !hostManaging'))
            assert find('tr[data-id="r1"] .status-badge').text == '人工标记成功'
            assert jobs.states()['r1']['state'] == 'failed'
            Select(find('#status-filter')).select_by_value('manual_success')
            assert js('return gridRows().map(r=>r.id)') == ['r1']
            Select(find('#status-filter')).select_by_value('')
            driver.save_screenshot(str(output / 'v120-management.png'))

            # 仅当前表格：人工成功排除，1 条可重登，1 条不确定仅展示。
            operations.modes.clear()
            find('#retry-failures').click()
            wait.until(lambda d: not find('#retry-all').get_property('disabled'))
            assert records[0]['order_no'] not in find('#retry-list').text
            assert len(driver.find_elements(By.CSS_SELECTOR, '.retry-card')) == 2
            assert find('#retry-summary').text == '当前失败 2 单 · 可核查重登 1 单 · 需人工处理 1 单'
            driver.save_screenshot(str(output / 'v120-retry-current-sheet.png'))
            find('#retry-all').click()
            wait.until(lambda d: find('#retry-batch-result').is_displayed())
            jobs.pending.join()
            assert len(operations.checks) == 1
            assert len(operations.submissions) == 3
            assert '拦截 0 条' in find('#retry-batch-result').text
            driver.save_screenshot(str(output / 'v120-batch.png'))
            find('#retry-close').click()
            saved()
            # 所选删除仅删所选；全部删除含隐藏行；撤销恢复草稿仍不清历史。
            select('r1', 'r2')
            history_files = {str(p): p.read_bytes() for p in jobs.directory.rglob('*.json')}
            find('#host-delete-selected').click()
            wait.until(lambda d: d.switch_to.alert).accept()
            wait.until(lambda d: len(store.read()['rows']) == 4)
            saved()
            assert js('return rows.map(r=>r.id)') == ['r3', 'r4', 'r5', 'r6']
            undo()
            assert len(store.read()['rows']) == 6
            Select(find('#status-filter')).select_by_value('failed')
            find('#host-delete-all').click()
            wait.until(lambda d: d.switch_to.alert).accept()
            wait.until(lambda d: store.read()['rows'] == [])
            saved()
            assert {str(p): p.read_bytes() for p in jobs.directory.rglob('*.json')} == history_files
            assert len(list((folder / 'draft_archive').glob('*.json'))) == 2
            undo()
            assert len(store.read()['rows']) == 6
            assert len(operations.submissions) == 3

            # 固定业务视口不受窗口大小影响，本地验证，无真实平台动作。
            driver.set_window_size(700, 600)
            stabilize_banniu_viewport(driver)
            assert js('return [innerWidth,innerHeight,visualViewport.scale]') == [1440, 900, 1]
            errors = [entry for entry in driver.get_log('browser') if entry['level'] == 'SEVERE']
            assert not errors, errors
            print('PASS: SKU identity, reload, clipboard, non-multiple range fill, filtered paste, dismiss notice, manual status, bulk safe retry, backed-up delete/undo, fixed viewport; synthetic data only.')
        finally:
            if driver is not None:
                driver.quit()
            jobs.pending.join()
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    main()
