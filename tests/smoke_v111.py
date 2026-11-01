"""Chrome 实测分组、状态筛选、跨客户端归档同步及延迟复核；合成订单，无真实平台访问。"""
import json
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait, Select
from gift_registry.browser import create_driver
from gift_registry.config import ROOT, Site
from gift_registry.frontend_server import RowStore, make_server
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.maintenance import MaintenanceService, CHINA
from gift_registry.registration import RegistrationQueue
from fakes import FakeOperations
from test_host_management import CATALOG, row


def main():
    output = ROOT / ".tmp/ui-checks"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="v111-ui-", dir=ROOT / ".tmp") as directory:
        folder = Path(directory)
        records = [row(f"0000{i}", f"row-{i}", int(i == 3)) for i in range(1, 5)]
        catalog_path = folder / "gifts.json"
        catalog_path.write_text(json.dumps(CATALOG), encoding="utf-8")
        catalog = GiftCatalogStore(catalog_path)
        store = RowStore(folder / "rows.json")
        store.write(0, records)
        operations = FakeOperations({"00002": "no_new", "00004": "no_new"})
        jobs = RegistrationQueue(folder / "jobs", operations=operations)
        clock = datetime(2026, 10, 27, 0, 59, tzinfo=CHINA)
        with patch("gift_registry.registration.now", return_value=(clock - timedelta(hours=3)).isoformat()):
            jobs.start(records, CATALOG)
            jobs.pending.join()
        server = make_server(0, store=store, queue=jobs, catalog=catalog)
        service = MaintenanceService(jobs, store, folder, clock=lambda: clock)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        driver = None
        try:
            site = Site("v111-test", "1.1.1 隔离验收", f"http://127.0.0.1:{server.server_port}/", ("127.0.0.1",))
            driver = create_driver(site, headless=True, profile=folder / "chrome")
            driver.set_window_size(1440, 1000)
            driver.get(site.url)
            wait = WebDriverWait(driver, 15)
            wait.until(lambda d: d.execute_script("return initialized"))
            driver.execute_script("renderNetworkNotice({state:'changed',domain:'',message:'IP changed',current_addresses:[]})")
            assert not driver.find_element(By.CSS_SELECTOR, '#network-notice .network-notice-actions a').is_displayed()
            driver.execute_script("renderNetworkNotice({state:'changed',domain:'gift.example.test',message:'DNS check',current_addresses:[]})")
            assert driver.find_element(By.CSS_SELECTOR, '#network-notice .network-notice-actions a').is_displayed()
            driver.execute_script("renderNetworkNotice({state:'ok',domain:'',current_addresses:[]})")
            def find(selector):
                return driver.find_element(By.CSS_SELECTOR, selector)
            def visible_ids():
                # 后台 revision 同步会替换表格节点；一次读取 DOM，避免轮询抓住旧节点。
                return driver.execute_script("return [...document.querySelectorAll('#grid-body tr[data-id]')].map(el=>el.dataset.id)")
            def save():
                wait.until(lambda d: d.execute_script("return version === savedVersion && !saving"))
            def undo():
                find("#undo").click()
                save()
            find('#grid-body tr[data-id="row-1"] .gift-trigger').click()
            Select(find("#gift-group-filter")).select_by_value("__ungrouped")
            assert len(driver.find_elements(By.CSS_SELECTOR, ".gift-option")) == 2
            find("#picker-manage-groups").click()
            wait.until(lambda d: find("#groups-dialog").get_property("open"))
            # 分组对话框内撤销输入不能误触底层表格的撤销。
            original_rows = driver.execute_script("return JSON.stringify(rows)")
            driver.execute_script("history.push([])")
            find("#new-group-name").send_keys("临时", Keys.CONTROL, "z")
            assert driver.execute_script("return JSON.stringify(rows)") == original_rows
            driver.execute_script("history.pop()")
            find("#new-group-name").clear()
            find('#group-gift-list input[value="SKU-A"]').click()
            find('#group-targets input[value="xiaomi-ecosystem"]').click()
            find("#assign-groups").click()
            wait.until(lambda d: d.execute_script("return giftGroups.revision") == 1)
            find('#group-gift-list input[value="SKU-A"]').click()
            find('#group-gift-list input[value="SKU-B"]').click()
            find('#group-targets input[value="xiaomi-phone"]').click()
            find("#assign-groups").click()
            wait.until(lambda d: d.execute_script("return giftGroups.revision") == 2)
            find("#new-group-name").send_keys("平板")
            find("#create-group").click()
            wait.until(lambda d: d.execute_script("return giftGroups.groups.length") == 3)
            driver.save_screenshot(str(output / "v111-groups.png"))
            find("#groups-close").click()
            find('#grid-body tr[data-id="row-1"] .gift-trigger').click()
            Select(find("#gift-group-filter")).select_by_value("xiaomi-phone")
            assert [el.get_attribute("value") for el in driver.find_elements(By.CSS_SELECTOR, '.gift-option input')] == ["SKU-B"]
            find("#gift-search").send_keys("完全没有对应商品的搜索词")
            assert not driver.find_elements(By.CSS_SELECTOR, ".gift-option")
            assert driver.execute_script("return matchGifts('礼品甲').ids") == ["SKU-A"]
            find("#gift-search").clear()
            Select(find("#gift-group-filter")).select_by_value("xiaomi-ecosystem")
            driver.save_screenshot(str(output / "v111-picker.png"))
            find("#gift-close").click()

            Select(find("#status-filter")).select_by_value("failed")
            assert visible_ids() == ["row-2", "row-4"]
            find('#grid-head .letters [data-field="order_no"]').click()
            assert driver.execute_script("return selectionMatrix()") == [["00002"], ["00004"]]
            find('#grid-body input[data-field="order_no"]').send_keys(Keys.DELETE)
            wait.until(lambda d: not visible_ids())
            assert driver.execute_script("return rows.filter(r=>['row-1','row-3'].includes(r.id)).map(r=>r.order_no)") == ["00001", "00003"]
            undo()
            assert visible_ids() == ["row-2", "row-4"]
            find('#grid-head .letters [data-field="order_no"]').click()
            find("#delete-row").click()
            assert driver.execute_script("return rows.map(r=>r.id)") == ["row-1", "row-3"]
            undo()
            find('#grid-head .letters [data-field="order_no"]').click()
            driver.execute_script("const data=new DataTransfer(); data.setData('text/plain','000X2\\n000X4'); document.querySelector('#grid-body input').dispatchEvent(new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true}));")
            save()
            assert driver.execute_script("return rows.map(r=>r.order_no)") == ["00001", "000X2", "00003", "000X4"]
            undo()
            find('#grid-body input[data-field="order_no"]').click()
            find('#grid-body input[data-field="order_no"]').send_keys(Keys.ARROW_DOWN)
            assert driver.execute_script("return active.id") == "row-4"
            assert "第 5 行" in find("#selection-summary").text
            driver.save_screenshot(str(output / "v111-status-filter.png"))
            Select(find("#status-filter")).select_by_value("")
            save()
            operations.configured = False
            service.tick()
            clock += timedelta(minutes=1)
            service.tick()
            wait.until(lambda d: visible_ids() == ["row-2", "row-4"])
            assert driver.execute_script("return history.length") == 0
            assert store.read()["rows"] == [records[1], records[3]]
            operations.configured = True
            operations.modes.clear()
            clock += timedelta(minutes=1)
            service.tick()
            jobs.pending.join()
            service.tick()
            wait.until(lambda d: all(el.text == "成功" for el in d.find_elements(By.CSS_SELECTOR, '#grid-body [data-field="status"]')))
            assert len(operations.submissions) == 4
            assert operations.queries == {"00001": 2, "00002": 3, "00003": 2, "00004": 3}
            with ZipFile(service.exports / "gift-registration-2026-10-26.xlsx") as archive:
                assert len(ET.fromstring(archive.read("xl/worksheets/sheet1.xml")).findall('.//{*}row')) == 5
                assert len(ET.fromstring(archive.read("xl/worksheets/sheet2.xml")).findall('.//{*}row')) == 1
            driver.refresh()
            wait.until(lambda d: d.execute_script("return initialized"))
            assert driver.execute_script("return giftGroups.memberships['SKU-A']") == ["xiaomi-ecosystem"]
            driver.set_window_size(430, 900)
            assert driver.execute_script("return document.documentElement.scrollWidth <= innerWidth")
            driver.save_screenshot(str(output / "v111-mobile.png"))
            errors = [entry for entry in driver.get_log("browser") if entry["level"] == "SEVERE"]
            assert not errors, errors
            print("PASS: Chrome gift groups, persistent bulk assignment, status filtering with hidden-row-safe copy/paste/delete, nightly cleanup sync, one delayed read and no resubmission")
        except Exception:
            if driver is not None:
                driver.save_screenshot(str(output / "v111-failure.png"))
            raise
        finally:
            if driver is not None:
                driver.quit()
            jobs.pending.join()
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
