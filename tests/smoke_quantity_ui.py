r"""隔离真实 Chrome 验收：复制/填充/结构化粘贴的数量传递（第 5 节 P1 回归）。

使用临时目录、临时礼品清单与 127.0.0.1 临时前端服务，绝不连接真实平台，也不改动
用户 config/data/队列。文件名 smoke_*，不会被 unittest 自动发现；需要本机 Chrome。

运行：.\.venv\Scripts\python.exe -X utf8 tests\smoke_quantity_ui.py
"""

import json
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from fakes import FakeOperations
from gift_registry.browser import create_driver
from gift_registry.config import Site
from gift_registry.driver_shutdown import quit_owned_driver
from gift_registry.frontend_server import RowStore, make_server
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.registration import RegistrationQueue

OUT = ROOT / ".tmp" / "ui-checks"
CATALOG = [
    {"id": "SYN-A", "name": "合成收纳包", "aliases": []},
    {"id": "SYN-B", "name": "合成自拍杆", "aliases": []},
    {"id": "SYN-C", "name": "合成保温杯", "aliases": []},
    # 短名称用于公式栏 100 次重复仍不超过单元格 500 字符上限，从而触达 >99 数量超限分支。
    {"id": "SYN-D", "name": "徽章", "aliases": []},
]


class UnconfiguredOperations(FakeOperations):
    configured = False


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    (ROOT / ".tmp").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="quantity-ui-", dir=ROOT / ".tmp") as directory:
        folder = Path(directory)
        catalog_path = folder / "gifts.json"
        catalog_path.write_text(json.dumps(CATALOG, ensure_ascii=False), encoding="utf-8")
        store = RowStore(folder / "rows.json")
        catalog_store = GiftCatalogStore(catalog_path)
        jobs = RegistrationQueue(folder / "jobs", operations=UnconfiguredOperations())
        server = make_server(0, store=store, queue=jobs, catalog=catalog_store)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        driver = None
        try:
            url = f"http://127.0.0.1:{server.server_port}/"
            site = Site("quantity-ui", "多件礼品验收", url, ("127.0.0.1",))
            driver = create_driver(site, headless=True, profile=folder / "profile")
            driver.set_window_size(1500, 1000)
            driver.get(url)
            wait = WebDriverWait(driver, 20)
            wait.until(lambda d: d.execute_script("return initialized"))

            js = driver.execute_script
            toast = lambda: driver.find_element(By.ID, "toast").text
            wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "#grid-body tr[data-id]")) >= 1)

            def saved():
                wait.until(lambda d: d.execute_script("return version === savedVersion && !saving"))

            def cell_text(index, field="gift"):
                return driver.find_elements(By.CSS_SELECTOR, "#grid-body tr[data-id]")[index] \
                    .find_element(By.CSS_SELECTOR, f'td[data-field="{field}"]').text

            def stored(order_no):
                return next((row for row in store.read()["rows"] if row["order_no"] == order_no), None)

            def seed(script):
                js(script + ";changed();render();")
                saved()

            def drag_fill(from_index, to_index):
                handle = driver.find_element(By.CSS_SELECTOR, ".fill-handle")
                target = js("return fieldElement(rows[arguments[0]].id,'gift')", to_index)
                ActionChains(driver).move_to_element(handle).click_and_hold().move_to_element(target).pause(.3).release().perform()
                saved()

            # 0. 合成草稿：来源默认 1 件、目标旧 5 件（不写入真实订单/队列）。
            seed("""
                rows = [
                  {...newRow(), order_no:'SYN-FILL-SRC'},
                  {...newRow(), order_no:'SYN-FILL-DST'},
                  {...newRow(), order_no:'SYN-PASTE-DST'},
                  {...newRow(), order_no:'SYN-UNDO-DST'}
                ];
                assignGifts(rows[0], ['SYN-A'], [], {});
                assignGifts(rows[1], ['SYN-A'], [], {'SYN-A':5});
                assignGifts(rows[2], ['SYN-B'], [], {'SYN-B':5});
                assignGifts(rows[3], ['SYN-A'], [], {'SYN-A':5});
                selectRange({id:rows[0].id, field:'gift'});
            """)
            assert stored("SYN-FILL-DST")["gift_quantities"] == {"SYN-A": 5}
            assert "×5" in cell_text(1)
            assert "同一礼品多件仅在工作台内复制/粘贴时保留数量" in driver.find_element(By.ID, "grid-help").text
            assert "Excel" in driver.find_element(By.ID, "copy-range").get_attribute("title")
            print("PASS: 隔离页面使用合成草稿；来源默认 1 件、目标旧 5 件；界面写明纯文本数量限制。", flush=True)

            # 1. 填充柄拖动：来源 1 件覆盖目标 5 件。
            drag_fill(0, 1)
            assert stored("SYN-FILL-DST").get("gift_quantities") is None, stored("SYN-FILL-DST")
            assert "×" not in cell_text(1)
            assert js("return giftQuantity(rows[1], 'SYN-A')") == 1
            print("PASS: 填充柄拖动把来源默认 1 件覆盖到目标旧 5 件，存档数量为默认 1 件。", flush=True)

            # 2. Ctrl+D：同样使用来源数量覆盖目标旧数量。
            seed("""
                assignGifts(rows[1], ['SYN-A'], [], {'SYN-A':5});
                selectRange({id:rows[0].id, field:'gift'}, {id:rows[1].id, field:'gift'});
            """)
            assert stored("SYN-FILL-DST")["gift_quantities"] == {"SYN-A": 5}
            js("focusCell(rows[0].id, 'gift', true)")
            driver.switch_to.active_element.send_keys(Keys.CONTROL, "d")
            saved()
            assert stored("SYN-FILL-DST").get("gift_quantities") is None, stored("SYN-FILL-DST")
            print("PASS: Ctrl+D 向下填充把来源默认 1 件覆盖到目标旧 5 件。", flush=True)

            # 3. 撤销恢复整行数量（并再次写回存档）。
            js("undo()")
            saved()
            assert js("return giftQuantity(rows[1], 'SYN-A')") == 5
            assert stored("SYN-FILL-DST")["gift_quantities"] == {"SYN-A": 5}
            assert "×5" in cell_text(1)
            print("PASS: Ctrl+Z 撤销恢复填充前的 5 件数量，并同步保存。", flush=True)

            # 4. 双击填充柄：扩展到相邻 order_no 连续行，目标旧数量全部被覆盖。
            js("selectRange({id:rows[0].id, field:'gift'})")
            wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, ".fill-handle"))
            ActionChains(driver).double_click(driver.find_element(By.CSS_SELECTOR, ".fill-handle")).perform()
            saved()
            for order_no in ("SYN-FILL-DST", "SYN-PASTE-DST", "SYN-UNDO-DST"):
                assert stored(order_no).get("gift_quantities") is None, stored(order_no)
            assert js("return rows.slice(1).every(row => row.gift_ids.length === 1 && row.gift_ids[0] === 'SYN-A')")
            print("PASS: 双击填充柄覆盖多行目标旧数量为来源默认 1 件。", flush=True)

            # 5. 结构化复制/粘贴：来源 A×3、B×2 覆盖目标旧 A×9、B×5；默认 1 件也覆盖。
            seed("""
                assignGifts(rows[1], ['SYN-A','SYN-B'], [], {'SYN-A':3,'SYN-B':2});
                assignGifts(rows[2], ['SYN-A','SYN-B'], [], {'SYN-A':9,'SYN-B':5});
            """)
            js("selectRange({id:rows[1].id, field:'gift'}); window.__copied = clipboardContent(); selectRange({id:rows[2].id, field:'gift'});")
            js("""
                const data = new DataTransfer();
                data.setData('text/plain', window.__copied.text);
                data.setData('text/html', window.__copied.html);
                document.querySelector('#registry-grid').dispatchEvent(new ClipboardEvent('paste', {clipboardData:data, bubbles:true, cancelable:true}));
            """)
            saved()
            pasted = stored("SYN-PASTE-DST")
            assert pasted["gift_ids"] == ["SYN-A", "SYN-B"], pasted
            assert pasted["gift_quantities"] == {"SYN-A": 3, "SYN-B": 2}, pasted
            assert "×3" in cell_text(2) and "×2" in cell_text(2)
            js("""
                assignGifts(rows[0], ['SYN-C'], [], {});
                selectRange({id:rows[0].id, field:'gift'}); window.__copied = clipboardContent();
                selectRange({id:rows[2].id, field:'gift'});
                const data = new DataTransfer();
                data.setData('text/plain', window.__copied.text);
                data.setData('text/html', window.__copied.html);
                document.querySelector('#grid-body tr[data-id] td[data-field="gift"] .gift-trigger')
                  .dispatchEvent(new ClipboardEvent('paste', {clipboardData:data, bubbles:true, cancelable:true}));
            """)
            saved()
            assert stored("SYN-PASTE-DST")["gift_ids"] == ["SYN-C"], stored("SYN-PASTE-DST")
            assert stored("SYN-PASTE-DST").get("gift_quantities") is None
            print("PASS: 工作台内结构化复制/粘贴完整保留 A×3、B×2，默认 1 件覆盖目标旧数量。", flush=True)

            # 6. 选择器勾选/取消保留其它礼品数量；真实加减按钮生效。
            driver.find_elements(By.CSS_SELECTOR, "#grid-body tr[data-id]")[1] \
                .find_element(By.CSS_SELECTOR, 'td[data-field="gift"] .gift-trigger').click()
            wait.until(lambda d: d.find_element(By.ID, "gift-picker").is_displayed())
            driver.find_element(By.CSS_SELECTOR, '#gift-options input[value="SYN-A"]').click()
            assert js("return JSON.stringify(rows[1].gift_quantities)") == '{"SYN-B":2}', js("return JSON.stringify(rows[1])")
            driver.find_element(By.CSS_SELECTOR, '#gift-options input[value="SYN-A"]').click()
            assert js("return JSON.stringify(rows[1].gift_ids)") == '["SYN-B","SYN-A"]'
            assert js("return JSON.stringify(rows[1].gift_quantities)") == '{"SYN-B":2}'
            plus = ('//*[@id="gift-options"]//input[@value="SYN-A"]/ancestor::label[1]'
                    '//span[contains(@class,"gift-quantity")]/button[last()]')
            driver.find_element(By.XPATH, plus).click()
            wait.until(lambda d: '共 4 件' in d.find_element(By.ID, "gift-count").text)
            driver.find_element(By.ID, "gift-done").click()
            saved()
            assert stored("SYN-FILL-DST")["gift_quantities"] == {"SYN-A": 2, "SYN-B": 2}, stored("SYN-FILL-DST")
            print("PASS: 选择器取消/重选 SYN-A 时保留 SYN-B×2，再加 1 件后存档正确。", flush=True)

            # 7. 纯文本“×2”不当作名称/编码，也不无条件当作数量：按 1 件并提示手动匹配。
            js("selectRange({id:rows[2].id, field:'gift'})")
            js("""
                const data = new DataTransfer();
                data.setData('text/plain', '合成收纳包 ×2');
                document.querySelector('#registry-grid').dispatchEvent(new ClipboardEvent('paste', {clipboardData:data, bubbles:true, cancelable:true}));
            """)
            saved()
            assert stored("SYN-PASTE-DST")["gift_ids"] == ["SYN-A"], stored("SYN-PASTE-DST")
            assert stored("SYN-PASTE-DST").get("gift_quantities") is None
            assert "未匹配：×2" in cell_text(2)
            wait.until(lambda d: "需手动匹配" in toast())
            print("PASS: 纯文本 ×2 未进入名称/编码，也未无条件当成 2 件；按 1 件并提示需手动匹配。", flush=True)

            # 8. >99 超限粘贴：提示并整体阻止，不静默截断为 99。
            js("selectRange({id:rows[2].id, field:'gift'}); window.__before = JSON.stringify(rows);")
            over = "、".join(["合成收纳包"] * 100)
            js("""
                const data = new DataTransfer();
                data.setData('text/plain', arguments[0]);
                document.querySelector('#registry-grid').dispatchEvent(new ClipboardEvent('paste', {clipboardData:data, bubbles:true, cancelable:true}));
            """, over)
            wait.until(lambda d: "99 件" in toast() and "未应用" in toast())
            assert js("return JSON.stringify(rows)") == js("return window.__before")
            assert stored("SYN-PASTE-DST")["gift_ids"] == ["SYN-A"], stored("SYN-PASTE-DST")
            print("PASS: 纯文本 100 次重复被明确阻止（提示 99 件上限），未静默截断、未改动表格/存档。", flush=True)

            # 9. 公式编辑同样阻止 >99，不写入行（短名称让 100 次重复仍不超过 500 字符上限）。
            formula_over = "、".join(["徽章"] * 100)
            js("""
                activate(rows[2].id, 'gift');
                const input = document.querySelector('#formula-input');
                input.focus(); input.value = arguments[0]; input.dispatchEvent(new Event('change'));
            """, formula_over)
            wait.until(lambda d: "99 件" in toast() and "未应用" in toast())
            assert stored("SYN-PASTE-DST")["gift_ids"] == ["SYN-A"], stored("SYN-PASTE-DST")
            assert js("return JSON.stringify(rows[2].gift_quantities)") == "{}"
            print("PASS: 公式栏输入 100 次重复同样提示并阻止应用。", flush=True)

            # 10. 清理为合法草稿后刷新：数量与新规则身份往返保存。
            js("""
                assignGifts(rows[2], ['SYN-A'], [], {'SYN-A':4});
                changed(); render();
            """)
            saved()
            driver.refresh()
            wait.until(lambda d: d.execute_script("return initialized"))
            wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "#grid-body tr[data-id]")) >= 4)
            refreshed = stored("SYN-PASTE-DST")
            assert refreshed["gift_quantities"] == {"SYN-A": 4}, refreshed
            assert "×4" in cell_text(2)
            print("PASS: 刷新后 A×4 数量保留，后端存档与界面展示一致。", flush=True)

            severe = [entry for entry in driver.get_log("browser") if entry["level"] == "SEVERE"]
            assert not severe, severe
            driver.save_screenshot(str(OUT / "quantity-ui-regression.png"))
            print("PASS: 无严重浏览器错误；截图 .tmp/ui-checks/quantity-ui-regression.png", flush=True)
        finally:
            if driver:
                quit_owned_driver(driver)
            server.shutdown()
            server.server_close()
            jobs.close()


if __name__ == "__main__":
    main()
