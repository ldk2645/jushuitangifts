"""两台模拟客户端的隔离 Chrome 验收；业务仅使用 FakeOperations。"""

import base64
import json
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from gift_registry.browser import create_driver
from gift_registry.config import ROOT, Site
from gift_registry.driver_shutdown import quit_owned_driver
from gift_registry.frontend_server import RowStore, make_server
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.network import NetworkSettings
from gift_registry.network_monitor import NetworkMonitor, normalize_connections
from gift_registry.registration import RegistrationQueue
from fakes import FakeOperations


def main():
    temporary = ROOT / '.tmp'
    temporary.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='lan-ui-', dir=temporary) as directory:
        folder = Path(directory)
        path = folder / 'gifts.json'
        path.write_text(json.dumps([{'id': 'TEST-SKU', 'name': '合成礼品', 'aliases': []}]), encoding='utf-8')
        store = RowStore(folder / 'rows.json')
        operations = FakeOperations()
        jobs = RegistrationQueue(folder / 'jobs', operations=operations)
        network = NetworkSettings(True, '192.168.1.91', '192.168.1.0/24', 'gift.example.test')
        password = '000042'
        connections = normalize_connections([{'adapter': 'synthetic-wifi', 'name': 'test-one',
                     'ip': network.lan_ip, 'prefix': 24, 'gateway': '192.168.1.1'}])
        monitor = NetworkMonitor(network, path=folder / 'network.json', probe=lambda: connections, notify=None)
        monitor.check()
        server = make_server(0, store=store, queue=jobs, catalog=GiftCatalogStore(path),
                             network=network, access_password=password, network_monitor=monitor)
        # 在回环测试地址模拟远端客户端，禁止任何平台登录助手被启动。
        original_local_check = server.RequestHandlerClass.is_local_client
        server.RequestHandlerClass.is_local_client = lambda _: False
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}/'
        drivers = []
        try:
            for number in (1, 2):
                site = Site(f'lan-ui-{number}', '隔离局域网客户端', url, ('127.0.0.1',))
                driver = create_driver(site, headless=True, profile=folder / f'profile-{number}')
                drivers.append(driver)
                driver.set_window_size(1440, 980)
                driver.execute_cdp_cmd('Network.enable', {})
                driver.execute_cdp_cmd('Network.setExtraHTTPHeaders', {'headers': {
                    'Authorization': 'Basic ' + base64.b64encode(('gift:' + password).encode()).decode()}})
            first, second = drivers

            def loaded(driver):
                WebDriverWait(driver, 10).until(lambda d: d.execute_script('return initialized'))
                login = driver.find_element(By.ID, 'login-button')
                assert not login.is_enabled() and login.text == '平台登录由主机维护'

            def order_field(driver):
                return driver.find_element(By.CSS_SELECTOR, '#grid-body tr[data-id] [data-field="order_no"] input')

            def saved(driver):
                WebDriverWait(driver, 10).until(lambda d: d.find_element(By.ID, 'save-label').text.startswith('已保存'))

            def edit(driver, value):
                field = order_field(driver)
                ActionChains(driver).double_click(field).perform()
                field.clear()
                field.send_keys(value)
                driver.find_element(By.ID, 'save').click()

            first.get(url)
            loaded(first)
            assert not first.find_element(By.ID, 'network-notice').is_displayed()
            assert first.execute_async_script("const done=arguments[0];fetch('/api/network/status').then(r=>done(r.status));") == 403
            order_field(first).click()
            first.execute_script("""
                const data=new DataTransfer();data.setData('text/plain','00000123\t合成礼品');
                document.activeElement.dispatchEvent(new ClipboardEvent('paste',{bubbles:true,cancelable:true,clipboardData:data}));
            """)
            saved(first)
            second.get(url)
            loaded(second)
            assert order_field(second).get_attribute('value') == '00000123'
            edit(second, '00000124')
            saved(second)
            edit(first, '00000125')
            WebDriverWait(first, 10).until(lambda d: d.execute_script('return conflict'))
            assert store.read()['rows'][0]['order_no'] == '00000124'
            assert order_field(first).get_attribute('value') == '00000125'
            first.refresh()
            loaded(first)
            assert order_field(first).get_attribute('value') == '00000124'
            first.find_element(By.ID, 'start-registration').click()
            WebDriverWait(first, 10).until(lambda _: len(operations.submissions) == 1)
            jobs.pending.join()
            second.refresh()
            loaded(second)
            second.find_element(By.ID, 'start-registration').click()
            WebDriverWait(second, 10).until(lambda d: not d.find_element(By.ID, 'start-registration').get_property('disabled'))
            jobs.pending.join()
            assert len(operations.submissions) == 1
            assert len(list(jobs.directory.glob('*.json'))) == 1
            assert store.read()['rows'][0]['order_no'] == '00000124'
            # 第三个全新浏览器没有 Authorization 头或认证缓存，验证主机访问免登录。
            server.RequestHandlerClass.is_local_client = original_local_check
            site = Site('lan-local-ui', '隔离主机免登录验收', url, ('127.0.0.1',))
            local = create_driver(site, headless=True, profile=folder / 'profile-local-no-auth')
            drivers.append(local)
            local.get(url)
            WebDriverWait(local, 10).until(lambda d: d.execute_script('return initialized'))
            assert local.find_element(By.ID, 'login-button').is_enabled()
            assert order_field(local).get_attribute('value') == '00000124'
            assert len(operations.submissions) == 1
            # 同一合成主机切换网络后，通过实际状态 API 和页面轮询显示提醒。
            connections[0]['ip'] = '192.168.1.92'
            monitor.check()
            local.execute_script('refreshNetworkNotice()')
            WebDriverWait(local, 5).until(lambda d: d.find_element(By.ID, 'network-notice').is_displayed())
            assert 'Cloudflare' in local.find_element(By.ID, 'network-notice-text').text
            assert '192.168.1.92' in local.find_element(By.ID, 'network-notice-text').text
            assert not local.find_element(By.ID, 'network-ack').is_displayed()
            assert local.find_element(By.CSS_SELECTOR, '#network-notice a').get_attribute('href') == 'https://dash.cloudflare.com/'
            local.save_screenshot(str(temporary / 'network-change-reminder.png'))
            connections[0].update(ip=network.lan_ip, name='test-two')
            monitor.check()
            local.execute_script('refreshNetworkNotice()')
            WebDriverWait(local, 5).until(lambda d: d.find_element(By.ID, 'network-ack').is_displayed())
            local.find_element(By.ID, 'network-ack').click()
            WebDriverWait(local, 5).until(lambda d: not d.find_element(By.ID, 'network-notice').is_displayed())
            assert monitor.snapshot()['state'] == 'ok'
            assert len(operations.submissions) == 1
            print('PASS: six-digit LAN authentication, shared draft conflicts and deduplication, fresh local Chrome without credentials, and network/IP reminder plus local acknowledgement. All business operations were simulated; no real platform or user data was used.')
        finally:
            for driver in drivers:
                quit_owned_driver(driver)
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    main()
