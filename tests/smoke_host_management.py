"""真实 Chrome 操作隔离工作台：主机重登、日志下载、LAN 隐藏入口；不访问业务平台。"""

import json
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from gift_registry.browser import create_driver
from gift_registry.config import ROOT, Site
from gift_registry.frontend_server import RowStore, make_server
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.registration import RegistrationQueue
from test_host_management import RetryOperations, CATALOG, row


def main():
    output = ROOT / ".tmp" / "ui-checks"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="host-management-", dir=ROOT / ".tmp") as directory:
        folder = Path(directory)
        store = RowStore(folder / "rows.json")
        store.write(0, [row()])
        catalog_path = folder / "gifts.json"
        catalog_path.write_text(json.dumps(CATALOG, ensure_ascii=False), encoding="utf-8")
        catalog = GiftCatalogStore(catalog_path)
        operations = RetryOperations({row()["order_no"]: "query_error"})
        jobs = RegistrationQueue(folder / "jobs", operations=operations)
        jobs.start([row()], catalog.read())
        jobs.pending.join()
        server = make_server(0, store=store, queue=jobs, catalog=catalog)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        driver = None
        try:
            site = Site("host-management-test", "主机管理验收", f"http://127.0.0.1:{server.server_port}/", ("127.0.0.1",))
            driver = create_driver(site, headless=True, profile=folder / "browser")
            driver.set_window_size(1440, 1050)
            driver.execute_cdp_cmd("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(folder)})
            driver.get(site.url)
            wait = WebDriverWait(driver, 15)
            wait.until(lambda d: d.execute_script("return initialized"))
            if not driver.find_element(By.ID, "workspace-overview").get_property("open"):
                driver.find_element(By.ID, "overview-toggle").click()
            wait.until(lambda d: d.find_element(By.ID, "host-actions").is_displayed())
            driver.find_element(By.ID, "retry-failures").click()
            wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, ".retry-card")) == 1)
            assert row()["order_no"] in driver.find_element(By.ID, "retry-list").text
            assert driver.find_element(By.CSS_SELECTOR, ".retry-card button").is_enabled()
            driver.save_screenshot(str(output / "host-management-retry.png"))
            operations.modes.clear()
            driver.find_element(By.CSS_SELECTOR, ".retry-card button").click()
            wait.until(lambda _: len(operations.submissions) == 1)
            jobs.pending.join()
            wait.until(lambda d: "没有需要处理" in d.find_element(By.ID, "retry-message").text)
            driver.find_element(By.ID, "retry-close").click()
            wait.until(lambda d: "成功" in d.find_element(By.CSS_SELECTOR, '#grid-body [data-field="status"]').text)
            driver.find_element(By.ID, "start-registration").click()
            wait.until(lambda d: not d.find_element(By.ID, "start-registration").get_attribute("disabled"))
            jobs.pending.join()
            assert len(operations.submissions) == 1
            driver.find_element(By.ID, "export-today").click()
            downloaded = wait.until(lambda _: next(folder.glob("gift-registration-*.xlsx"), None))
            with ZipFile(downloaded) as archive:
                for name in ("xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml"):
                    sheet = ET.fromstring(archive.read(name))
                    assert len(sheet.findall(".//{*}row")) == 2
            (output / "host-management-log.xlsx").write_bytes(downloaded.read_bytes())
            driver.save_screenshot(str(output / "host-management-success.png"))
            with patch.object(server.RequestHandlerClass, "is_local_access", return_value=False):
                driver.refresh()
                wait.until(lambda d: d.execute_script("return initialized"))
                assert not driver.find_element(By.ID, "host-actions").is_displayed()
            print("PASS: isolated Chrome host retry, one submit, ordinary-start skip, Excel download and LAN control visibility")
        finally:
            if driver is not None:
                driver.quit()
            jobs.pending.join()
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
