"""Headless local UI only, disposable profiles and synthetic business data."""
import json
import sys
import tempfile
import threading
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from gift_registry.browser import create_driver
from gift_registry.config import Site
from gift_registry.driver_shutdown import quit_owned_driver
from gift_registry.frontend_server import RowStore,make_server
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.registration import RegistrationQueue
from test_task_day_export import fixture,Offline
from test_verification_lane import CATALOG

def main():
    with tempfile.TemporaryDirectory(prefix='prompt02-ui-') as folder:
        root=Path(folder);jobs=RegistrationQueue(root/'jobs',Offline())
        path=fixture(jobs,'synthetic-1000',1000,'2026-11-09T20:00:00+08:00','2026-11-09T23:00:00+08:00',600)
        rows=RowStore(root/'rows.json');rows.write(0,jobs._load(path)['rows'])
        catalog=GiftCatalogStore(root/'gifts.json');catalog.path.write_text(json.dumps(CATALOG))
        server=make_server(0,store=rows,queue=jobs,catalog=catalog);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        driver=None
        try:
            url=f'http://127.0.0.1:{server.server_port}'
            driver=create_driver(Site('prompt02','Synthetic UI',url,('127.0.0.1',)),headless=True,profile=root/'profile')
            driver.set_window_size(1500,1000);driver.get(url);wait=WebDriverWait(driver,20)
            wait.until(lambda d:d.execute_script('return initialized'))
            driver.execute_script("focusCell(rows[0].id,'status')")
            assert driver.find_element(By.ID,'edit-status').is_enabled()
            driver.find_element(By.ID,'edit-status').click()
            driver.execute_script("document.querySelector('#manual-status').value='pending'")
            driver.find_element(By.ID,'status-apply').click()
            wait.until(lambda d:d.execute_script("return rowStates[rows[0].id].manual_status==='pending'"))
            assert driver.execute_script('return rowStates[rows[0].id].state')=='success'
            driver.execute_script("focusCell(rows[800].id,'status')")
            assert not driver.find_element(By.ID,'edit-status').is_enabled()
            driver.find_element(By.ID,'overview-toggle').click()
            driver.find_element(By.ID,'export-today').click()
            wait.until(lambda d:d.find_element(By.ID,'task-log-dialog').get_attribute('open'))
            assert '600/1000' in driver.find_element(By.ID,'task-log-progress').text
            assert driver.find_element(By.ID,'task-log-day').get_attribute('value')=='2026-11-09'
            driver.execute_cdp_cmd('Browser.setDownloadBehavior',{'behavior':'allow','downloadPath':str(root/'downloads')})
            driver.find_element(By.ID,'task-log-partial').click()
            driver.find_element(By.ID,'task-log-download').click()
            wait.until(lambda d:(root/'downloads/gift-registration-2026-11-09-partial.xlsx').exists())
            wait.until(lambda d:'版本 1' in d.find_element(By.ID,'task-log-progress').text)
            assert '01:00 自动导出上一日' not in driver.find_element(By.ID,'maintenance-summary').text
            output=ROOT/'.tmp/prompt02-task-logs.png';driver.save_screenshot(str(output))
            print(json.dumps(dict(result='passed',browser=driver.capabilities['browserVersion'],
                                  original_rows=1000,done=600,partial_download=True,manual_edit_only_terminal=True)))
        finally:
            if driver is not None:quit_owned_driver(driver)
            server.shutdown();server.server_close();thread.join()

if __name__=='__main__':main()
