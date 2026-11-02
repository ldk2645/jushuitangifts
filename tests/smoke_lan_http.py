"""普通 HTTP 域名的隔离 Selenium 验收，临时目录与合成数据，不访问业务平台。"""
import base64
import json
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from gift_registry.browser import create_driver
from gift_registry.config import ROOT, Site
from gift_registry.driver_shutdown import quit_owned_driver
from gift_registry.frontend_server import RowStore, make_server
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.network import NetworkSettings
from gift_registry.registration import RegistrationQueue


def main():
    temporary = ROOT / '.tmp'
    temporary.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='lan-http-', dir=temporary) as directory:
        folder = Path(directory)
        catalog = folder / 'gifts.json'
        catalog.write_text(json.dumps([{'id': 'SYNTHETIC-SKU', 'name': '合成礼品'}]), encoding='utf-8')
        store = RowStore(folder / 'rows.json')
        jobs = RegistrationQueue(folder / 'jobs', operations=type('Unconfigured', (), {'configured': False})())
        network = NetworkSettings(True, '192.168.1.91', '192.168.1.0/24', 'lan-check.example.test')
        server = make_server(0, store=store, queue=jobs, catalog=GiftCatalogStore(catalog),
                             network=network, access_password='000042')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        driver = None
        try:
            # 域名映射只在此临时 Chrome 中使用；不添加安全源例外、不修改系统 DNS/hosts。
            options = webdriver.ChromeOptions()
            options.add_argument('--host-resolver-rules=MAP lan-check.example.test 127.0.0.1')
            options.add_argument('--no-proxy-server')
            site = Site('lan-http', '隔离 HTTP 客户端',
                        f'http://{network.domain}:{server.server_port}/', (network.domain,))
            with patch('gift_registry.browser.webdriver.ChromeOptions', return_value=options):
                driver = create_driver(site, headless=True, profile=folder / 'browser')
            driver.execute_cdp_cmd('Network.enable', {})
            driver.execute_cdp_cmd('Network.setExtraHTTPHeaders', {'headers': {
                'Authorization': 'Basic ' + base64.b64encode(b'gift:000042').decode()}})
            driver.get(site.url)
            wait = WebDriverWait(driver, 12)
            js = driver.execute_script
            assert js('return isSecureContext') is False
            assert js('return typeof crypto.randomUUID') == 'undefined'
            wait.until(lambda _: js('return initialized && rows.length === 18'))
            driver.find_element(By.ID, 'add-bottom').click()
            wait.until(lambda _: len(store.read()['rows']) == 19)
            js("focusCell(rows[18].id, 'order_no'); pasteText('SYNTHETIC-1\\t合成礼品\\nSYNTHETIC-2\\t合成礼品');")
            wait.until(lambda _: len(store.read()['rows']) == 20)
            identifiers = [item['id'] for item in store.read()['rows']]
            assert len(set(identifiers)) == 20 and all(len(value) == 36 for value in identifiers)
            print('PASS: insecure HTTP empty-sheet loading, new rows and expanding paste; unique row IDs.')
            assert not driver.find_element(By.ID, 'login-button').is_enabled()
            # 模拟旧页面保留了上个进程的 CSRF；首次写入必须被拒绝，编辑仍保留。
            revision_before = store.read()['revision']
            js("token='old-process-token'; rows[0].order_no='SYNTHETIC-UNSAVED'; version++; clearTimeout(saveTimer);")
            assert driver.execute_async_script('const done=arguments[0];saveNow().then(done);') is False
            assert store.read()['revision'] == revision_before
            assert js("return rows[0].order_no === 'SYNTHETIC-UNSAVED' && token !== 'old-process-token'")
            assert driver.execute_async_script('const done=arguments[0];saveNow().then(done);') is True
            assert store.read()['rows'][0]['order_no'] == 'SYNTHETIC-UNSAVED'
            # 起登记的旧请求也只能发送一次，自动更新凭据不能创建任务。
            js("""
                window.originalFetch=window.fetch; window.startRequests=0;
                window.fetch=(path,options)=>{if(path==='/api/registrations/start')startRequests++;return originalFetch(path,options);};
                token='another-stale-token';
            """)
            message = driver.execute_async_script("""
                const done=arguments[0];api('/api/registrations/start',{method:'POST',body:JSON.stringify({revision})})
                    .then(()=>done('unexpected success')).catch(error=>done(error.message));
            """)
            assert '本次操作未执行' in message
            assert js('return startRequests') == 1
            assert not list(jobs.directory.glob('*.json'))
            print('PASS: expired connection credentials renew read-only; edits retained; no automatic save or start replay.')
            # 模拟连接一直没有响应，将单次超时缩短，检验请求释放和不重发。
            js("""
                window.originalTimeout=window.setTimeout;
                window.setTimeout=(callback,delay,...args)=>originalTimeout(callback,delay===60000?20:delay,...args);
                window.fetch=(path,options)=>path==='/api/registrations/start'
                    ? (startRequests++,new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new DOMException('timeout','AbortError')))))
                    : originalFetch(path,options);
            """)
            message = driver.execute_async_script("""
                const done=arguments[0];api('/api/registrations/start',{method:'POST',body:JSON.stringify({revision})})
                    .then(()=>done('unexpected success')).catch(error=>done(error.message));
            """)
            assert '尚未确认' in message and js('return startRequests') == 2
            assert not list(jobs.directory.glob('*.json'))
            js('window.fetch=originalFetch; window.setTimeout=originalTimeout;')
            driver.execute_cdp_cmd('Network.emulateNetworkConditions', {
                'offline': True, 'latency': 0, 'downloadThroughput': -1, 'uploadThroughput': -1})
            driver.execute_async_script('const done=arguments[0];refreshExecutionStates().then(done);')
            assert driver.find_element(By.ID, 'connection-notice').is_displayed()
            driver.execute_cdp_cmd('Network.emulateNetworkConditions', {
                'offline': False, 'latency': 0, 'downloadThroughput': -1, 'uploadThroughput': -1})
            driver.execute_async_script('const done=arguments[0];refreshExecutionStates().then(done);')
            assert not driver.find_element(By.ID, 'connection-notice').is_displayed()
            assert store.read()['rows'][0]['order_no'] == 'SYNTHETIC-UNSAVED'
            assert not list(jobs.directory.glob('*.json'))
            print('PASS: bounded timeout, offline notice and read-only recovery; no business submission occurred.')
        finally:
            if driver is not None:
                quit_owned_driver(driver)
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    main()
