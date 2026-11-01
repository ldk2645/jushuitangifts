"""隔离 Chrome 验收：公式栏输入与后台草稿同步并发时不丢失。"""
import json
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from gift_registry.browser import create_driver
from gift_registry.config import ROOT, Site
from gift_registry.frontend_server import RowStore, make_server
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.registration import RegistrationQueue
from fakes import FakeOperations
from test_host_management import CATALOG, row


def main():
    with tempfile.TemporaryDirectory(prefix='draft-sync-', dir=ROOT / '.tmp') as temp:
        folder = Path(temp)
        catalog = folder / 'gifts.json'
        catalog.write_text(json.dumps(CATALOG), encoding='utf-8')
        initial = [row('ORIGINAL-A','one'), row('ORIGINAL-B','two')]
        store = RowStore(folder / 'rows.json')
        store.write(0, initial)
        operations = FakeOperations()
        jobs = RegistrationQueue(folder / 'jobs', operations=operations)
        server = make_server(0, store=store, queue=jobs, catalog=GiftCatalogStore(catalog))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        driver = None
        try:
            site = Site('draft-sync-test','隔离同步验收',f'http://127.0.0.1:{server.server_port}/',('127.0.0.1',))
            driver = create_driver(site, headless=True, profile=folder / 'chrome')
            wait = WebDriverWait(driver, 12)
            def script(value):
                return driver.execute_script(value)
            def open_page():
                driver.get(site.url)
                wait.until(lambda d: script('return initialized'))
                driver.find_element(By.CSS_SELECTOR, '#grid-body input[data-field="order_no"]').click()
            def type_formula(value):
                field = driver.find_element(By.ID,'formula-input')
                field.click()
                field.send_keys(Keys.CONTROL,'a')
                field.send_keys(value)
                return field
            def discard_isolated_edit():
                # 仅重置本测试的临时输入，允许载入下一场景。
                script("document.querySelector('#formula-input').blur(); version=savedVersion; conflict=false;")
            open_page()
            field = type_formula('PENDING-USER-INPUT')
            assert script("const e=new Event('beforeunload',{cancelable:true}); window.dispatchEvent(e); return e.defaultPrevented")
            store.write(1,[initial[0],{**initial[1],'order_no':'REMOTE-B'}])
            wait.until(lambda d: script('return conflict'))
            assert field.get_property('value') == 'PENDING-USER-INPUT'
            assert script('return revision') == 1
            field.send_keys(Keys.ENTER)
            assert script('return rows[0].order_no') == 'PENDING-USER-INPUT'
            assert store.read()['rows'][0]['order_no'] == 'ORIGINAL-A'
            discard_isolated_edit()

            open_page()
            # 草稿 GET 已发出后才开始输入，响应返回时仍必须重新检查编辑状态。
            script("""
                const original=window.fetch.bind(window); window.holdDraft=true; window.draftHeld=false;
                window.fetch=async(...args)=>{
                    const response=await original(...args);
                    if(args[0]==='/api/rows' && window.holdDraft) {
                        window.draftHeld=true;
                        await new Promise(resolve=>window.releaseDraft=resolve);
                    }
                    return response;
                };
            """)
            store.write(2,[initial[0],{**initial[1],'order_no':'REMOTE-C'}])
            wait.until(lambda d: script('return window.draftHeld'))
            field = type_formula('INPUT-DURING-FETCH')
            script('window.holdDraft=false; window.releaseDraft()')
            wait.until(lambda d: script('return conflict'))
            assert field.get_property('value') == 'INPUT-DURING-FETCH'
            assert script('return revision') == 2
            assert store.read()['rows'][0]['order_no'] == 'ORIGINAL-A'
            discard_isolated_edit()

            open_page()
            field = type_formula('SAVE-BY-CTRL-S')
            field.send_keys(Keys.CONTROL,'s')
            wait.until(lambda d: script('return version===savedVersion && !saving'))
            assert store.read()['rows'][0]['order_no'] == 'SAVE-BY-CTRL-S'
            assert not operations.submissions and not operations.trace
            assert not [entry for entry in driver.get_log('browser') if entry['level']=='SEVERE']
            print('PASS: pending formula input preserved before/during background fetch, conflict blocks overwrite, unload protection and Ctrl+S commits input; no platform calls')
        finally:
            if driver is not None:
                driver.quit()
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    main()
