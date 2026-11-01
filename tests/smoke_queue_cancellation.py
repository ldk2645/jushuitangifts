"""真实 Chrome + 临时 HTTP/任务/草稿验证取消入口；平台操作全部为合成替身。"""
import json
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import Select, WebDriverWait
from gift_registry.browser import create_driver
from gift_registry.config import ROOT, Site
from gift_registry.frontend_server import RowStore, make_server
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.network import NetworkSettings
from gift_registry.registration import RegistrationQueue
from test_host_management import CATALOG, RetryOperations, row


def main():
    output = ROOT / '.tmp' / 'ui-checks'
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cancel-smoke-', dir=ROOT / '.tmp') as directory:
        folder = Path(directory)
        records = [row('SYNTHETIC-' + key.upper(), key) for key in 'abcd']
        store = RowStore(folder / 'rows.json')
        store.write(0, records)
        catalog = folder / 'gifts.json'
        catalog.write_text(json.dumps(CATALOG), encoding='utf-8')
        operations = RetryOperations()
        entered, release = threading.Event(), threading.Event()
        original_query = operations.query_order
        def query(order):
            if order == 'SYNTHETIC-A':
                entered.set()
                if not release.wait(60):
                    raise TimeoutError('synthetic gate')
            return original_query(order)
        operations.query_order = query
        jobs = RegistrationQueue(folder / 'jobs', operations=operations)
        server = make_server(0, store=store, queue=jobs, catalog=GiftCatalogStore(catalog), network=NetworkSettings())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        driver = None
        try:
            jobs.start(records, CATALOG)
            assert entered.wait(5)
            site = Site('cancel-smoke', '合成排队取消验收', f'http://127.0.0.1:{server.server_port}/', ('127.0.0.1',))
            driver = create_driver(site, headless=True, profile=folder / 'browser')
            driver.set_window_size(1440, 1000)
            driver.get(site.url)
            wait = WebDriverWait(driver, 15)
            find = lambda css: driver.find_element(By.CSS_SELECTOR, css)
            wait.until(lambda d: d.execute_script('return initialized'))
            assert find('#cancel-queued').is_displayed()
            assert not driver.find_elements(By.CSS_SELECTOR, 'tr[data-id="a"] .row-cancel')
            with patch.object(server.RequestHandlerClass, 'is_local_access', return_value=False):
                driver.refresh()
                wait.until(lambda d: d.execute_script('return initialized'))
                assert not find('#cancel-queued').is_displayed()
                assert not driver.find_elements(By.CSS_SELECTOR, '.row-cancel')
            driver.refresh()
            wait.until(lambda d: d.execute_script('return initialized'))
            wait.until(lambda d: d.execute_script('''const b=document.querySelector('tr[data-id="b"] .row-cancel');
                return b.getBoundingClientRect().right <= b.closest('td').getBoundingClientRect().right;'''))
            find('tr[data-id="b"] .row-cancel').click()
            assert find('#cancel-dialog').get_property('open')
            find('#cancel-apply').click()
            wait.until(lambda d: find('tr[data-id="b"] .status-badge').text == '已取消')
            Select(find('#status-filter')).select_by_value('cancelled')
            assert [node.get_attribute('data-id') for node in driver.find_elements(By.CSS_SELECTOR, '#grid-body tr[data-id]')] == ['b']
            Select(find('#status-filter')).select_by_value('')
            find('tr[data-id="c"] th button').click()
            ActionChains(driver).key_down(Keys.SHIFT).click(find('tr[data-id="d"] th button')).key_up(Keys.SHIFT).perform()
            find('#cancel-queued').click()
            assert '2 行正在等待执行' in find('#cancel-selection').text
            driver.save_screenshot(str(output / 'cancel-queued-confirm.png'))
            server.request_shutdown()
            wait.until(lambda d: find('#shutdown-notice').is_displayed())
            find('#cancel-apply').click()
            wait.until(lambda d: find('tr[data-id="d"] .status-badge').text == '已取消')
            assert jobs.states()['a']['state'] == 'querying'
            assert all(jobs.states()[key]['state'] == 'cancelled' for key in 'bcd')
            driver.save_screenshot(str(output / 'cancel-queued-shutdown.png'))
            release.set()
            jobs.pending.join()
            thread.join(5)
            assert not thread.is_alive()
            assert [request.order_no for request in operations.submissions] == ['SYNTHETIC-A']
            print('PASS: isolated Chrome row/batch cancel, cancelled filter, LAN hidden controls, shutdown banner and cancellation; only active synthetic order completed.')
        finally:
            release.set()
            if driver is not None:
                driver.quit()
            jobs.pending.join()
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    main()
