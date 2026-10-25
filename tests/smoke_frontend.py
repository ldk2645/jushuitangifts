"""隔离存档下的真实 Chrome 前端验收，不触碰用户登记数据和平台会话。"""

import json
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait

from gift_registry.browser import create_driver
from gift_registry.config import ROOT, Site
from gift_registry.frontend_server import RowStore, make_server
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.registration import RegistrationQueue
from gift_registry.workflow import PlatformOperationError
from fakes import FakeOperations


class UnconfiguredOperations(FakeOperations):
    configured = False


class LoginBusyOperations(FakeOperations):
    def __init__(self):
        super().__init__()
        self.busy = True
        self.prepare_calls = 0

    def prepare_start(self):
        self.prepare_calls += 1
        if self.busy:
            raise PlatformOperationError("登录助手正在使用浏览器配置，请先保存并关闭登录助手")


def main():
    output = ROOT / ".tmp" / "ui-checks"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="frontend-test-", dir=ROOT / ".tmp") as directory:
        folder = Path(directory)
        store = RowStore(folder / "rows.json")
        catalog_path = folder / "gifts.json"
        catalog_path.write_bytes((ROOT / "config" / "gifts.json").read_bytes())
        catalog_store = GiftCatalogStore(catalog_path)
        jobs = RegistrationQueue(folder / "jobs", operations=UnconfiguredOperations())
        server = make_server(0, store=store, queue=jobs, catalog=catalog_store)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        driver = None
        try:
            site = Site("frontend-test", "前端验收", f"http://127.0.0.1:{server.server_port}/", ("127.0.0.1",))
            driver = create_driver(site, headless=True, profile=folder / "profile")
            driver.set_window_size(1440, 980)
            driver.get(site.url)
            wait = WebDriverWait(driver, 12)
            wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "#grid-body tr[data-id]")) == 18)

            catalog = driver.execute_script("return giftCatalog")
            source_lines = (ROOT / "example.md").read_text(encoding="utf-8").split("example:")[0].splitlines()[1:]
            source = dict(line.split(None, 1) for line in source_lines if line.strip())
            assert len(catalog) == len(source) == 31
            assert {gift["id"]: gift["name"] for gift in catalog} == source
            for gift in catalog:
                for value in (gift["id"], gift["name"], *gift["aliases"]):
                    matched = driver.execute_script("return matchGifts(arguments[0])", value)
                    assert matched == {"ids": [gift["id"]], "unmatched": []}, (value, matched)

            def control(field, index=0):
                main_row = driver.find_elements(By.CSS_SELECTOR, "#grid-body tr[data-id]")[index]
                if field in ("recipient_name", "phone", "address"):
                    identifier = main_row.get_attribute("data-id")
                    return driver.find_element(By.CSS_SELECTOR, f'tr[data-for-row="{identifier}"] [data-field="{field}"] input')
                return main_row.find_element(By.CSS_SELECTOR, f'[data-field="{field}"] input,[data-field="{field}"] .gift-trigger,[data-field="{field}"] .address-toggle')

            def fill(field, value, index=0):
                element = control(field, index)
                if element.get_property("readOnly"):
                    ActionChains(driver).double_click(element).perform()
                    wait.until(lambda d: not control(field, index).get_property("readOnly"))
                    element = control(field, index)
                element.clear()
                element.send_keys(value)

            def labels():
                return [e.text for e in driver.find_elements(By.CSS_SELECTOR, ".field-names .column-label")]

            def saved():
                wait.until(lambda d: d.find_element(By.ID, "save-label").text.startswith("已保存"))

            def paste(text):
                driver.execute_script("""
                    const target = document.activeElement;
                    const data = new DataTransfer(); data.setData('text/plain', arguments[0]);
                    target.dispatchEvent(new ClipboardEvent('paste', {bubbles:true, cancelable:true, clipboardData:data}));
                """, text)

            def column_width(field):
                return driver.execute_script("return document.querySelector('#grid-columns .col-'+arguments[0]).getBoundingClientRect().width", field)

            def resize_handle(field):
                return driver.find_element(By.CSS_SELECTOR, f'.field-names .column-resizer[data-resize-field="{field}"]')

            assert labels() == ["订单号", "礼品", "是否改地址", "状态"], labels()
            assert control("change_address").get_attribute("aria-checked") == "false"
            assert not driver.find_elements(By.CSS_SELECTOR, ".address-details")
            assert driver.execute_script("return getComputedStyle(arguments[0]).textAlign", control("order_no")) == "center"
            assert driver.execute_script("return getComputedStyle(document.querySelector('.field-names th[data-field=order_no]')).textAlign") == "center"
            wait.until(lambda d: d.execute_script("const grid=document.querySelector('#grid-scroll');return grid.scrollWidth<=grid.clientWidth+1"))
            driver.save_screenshot(str(output / "default.png"))

            # 实际输入驱动自动列宽，真实鼠标拖动驱动手动列宽；布局不写入业务草稿。
            original_width = column_width("order_no")
            fill("order_no", "0" + "1234567890" * 9)
            wait.until(lambda d: column_width("order_no") > original_width + 30)
            fitted_width = column_width("order_no")
            ActionChains(driver).move_to_element(resize_handle("order_no")).click_and_hold().move_by_offset(100, 0).release().perform()
            wait.until(lambda d: abs(column_width("order_no") - fitted_width - 100) < 3)
            manual_width = column_width("order_no")
            fill("order_no", "0" + "1234567890" * 16)
            saved()
            assert abs(column_width("order_no") - manual_width) < 2
            driver.refresh()
            wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "#grid-body tr[data-id]")) == 18)
            wait.until(lambda d: abs(column_width("order_no") - manual_width) < 2)
            ActionChains(driver).double_click(resize_handle("order_no")).perform()
            wait.until(lambda d: "order_no" not in d.execute_script("return JSON.parse(localStorage.getItem('gift_registry.column_widths.v1'))"))
            wait.until(lambda d: column_width("order_no") > manual_width + 20)
            stored_widths = driver.execute_script("return JSON.parse(localStorage.getItem('gift_registry.column_widths.v1'))")
            assert "order_no" not in stored_widths
            # 方向键微调以及一键清除全部手动设置。
            gift_width = column_width("gift")
            resize_handle("gift").send_keys(Keys.ARROW_RIGHT)
            wait.until(lambda d: abs(column_width("gift") - gift_width - 10) < 3)
            driver.find_element(By.ID, "auto-fit-columns").click()
            assert driver.execute_script("return localStorage.getItem('gift_registry.column_widths.v1')") == "{}"

            fill("order_no", "00012345678901234567890")
            control("gift").click()
            option_ids = [element.get_attribute("value") for element in driver.find_elements(By.CSS_SELECTOR, "#gift-options input")]
            assert option_ids == [gift["id"] for gift in catalog]
            assert abs(option_ids.index("N00A0L1LE90DXM5001") - option_ids.index("N01B0J1JDK1V007H08")) >= 10
            driver.find_element(By.CSS_SELECTOR, '#gift-options input[value="N01B0L1LE91VZS8S01"]').click()
            driver.find_element(By.CSS_SELECTOR, '#gift-options input[value="N01B0J1JDK1V007H08"]').click()
            assert "已选 2 项" in driver.find_element(By.ID, "gift-count").text
            driver.save_screenshot(str(output / "gift-picker.png"))
            driver.find_element(By.ID, "gift-done").click()
            driver.execute_script("""
                const cell=document.querySelector('tr[data-id] td[data-field=gift]');
                const chips=cell.querySelector('.gift-chips');
                const a=cell.getBoundingClientRect(), b=chips.getBoundingClientRect();
                if(Math.abs((a.left+a.right-b.left-b.right)/2)>2) throw new Error('Gift chips are not centered');
            """)
            control("change_address").click()
            assert len(labels()) == 4
            assert len(driver.find_elements(By.CSS_SELECTOR, ".address-details")) == 1
            assert control("change_address").get_attribute("aria-checked") == "true"
            second_id = driver.find_elements(By.CSS_SELECTOR, "#grid-body tr[data-id]")[1].get_attribute("data-id")
            assert not driver.find_elements(By.CSS_SELECTOR, f'tr[data-for-row="{second_id}"]')
            fill("recipient_name", "测试收件人")
            fill("phone", "0013800000000")
            fill("address", "上海市 测试区 示例路 88 号")
            saved()
            assert store.read()["rows"][0]["phone"] == "0013800000000"
            assert "待登记" in driver.find_element(By.CSS_SELECTOR, '#grid-body tr [data-field="status"]').text
            driver.save_screenshot(str(output / "address.png"))
            driver.refresh()
            wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "#grid-body tr[data-id]")) == 18)
            assert control("order_no").get_attribute("value") == "00012345678901234567890"
            assert control("recipient_name").get_attribute("value") == "测试收件人"
            assert "米家保温杯口袋版" in control("gift").text and "小米小背包" in control("gift").text

            control("change_address").click()
            assert len(labels()) == 4
            assert not driver.find_elements(By.CSS_SELECTOR, ".address-details")
            control("change_address").send_keys(Keys.SPACE)
            assert control("address").get_attribute("value") == "上海市 测试区 示例路 88 号"
            saved()

            control("order_no").click()
            clipboard = '订单号\t礼品\t是否改地址\t姓名\t电话\t地址\t状态\r\n00009\t口袋保温杯+小米背包\t是\t测试姓名\t000123456\t测试地址\t已完成\r\n00010\t小米小背抱\t否\t\t\t\t\r\n'
            paste(clipboard)
            saved()
            assert store.read()["rows"][0]["order_no"] == "00009"
            assert store.read()["rows"][1]["order_no"] == "00010"
            assert store.read()["rows"][1]["change_address"] is False
            assert store.read()["rows"][0]["gift_ids"] == ["N01B0L1LE91VZS8S01", "N01B0J1JDK1V007H08"]
            assert store.read()["rows"][1]["gift_ids"] == ["N01B0J1JDK1V007H08"]
            assert "已完成" not in driver.find_element(By.CSS_SELECTOR, "#grid-body").text
            # 粘贴后选中整个区域；先重新选择单行，再验证单行删除。
            control("order_no").click()
            driver.find_element(By.ID, "delete-row").click()
            assert len(driver.find_elements(By.CSS_SELECTOR, "#grid-body tr[data-id]")) == 17
            driver.find_element(By.ID, "undo").click()
            assert control("order_no").get_attribute("value") == "00009"
            count = driver.find_element(By.ID, "add-count")
            count.clear(); count.send_keys("3")
            driver.find_element(By.ID, "add-row").click()
            assert len(driver.find_elements(By.CSS_SELECTOR, "#grid-body tr[data-id]")) == 21
            saved()

            driver.execute_script("document.querySelector('#grid-scroll').scrollTop=0")
            control("gift", 1).click()
            driver.find_element(By.ID, "gift-close").click()
            control("gift", 1).send_keys(Keys.NULL)
            paste("手")
            assert "未匹配" in control("gift", 1).text
            saved()
            driver.find_element(By.ID, "start-registration").click()
            assert not list(jobs.directory.glob("*.json"))
            control("gift", 1).send_keys(Keys.NULL)
            paste("小米背包")
            saved()
            control("order_no", 2).click()
            paste("00011\t小米背包\t否")
            saved()
            driver.find_element(By.ID, "start-registration").click()
            wait.until(lambda d: "待接入流程" in d.find_element(By.ID, "grid-body").text)
            assert len(list(jobs.directory.glob("*.json"))) == 1
            driver.find_element(By.ID, "start-registration").click()
            wait.until(lambda d: "已有相同任务" in d.find_element(By.ID, "toast").text)
            assert len(list(jobs.directory.glob("*.json"))) == 1
            driver.refresh()
            wait.until(lambda d: "待接入流程" in d.find_element(By.ID, "grid-body").text)
            driver.save_screenshot(str(output / "queued.png"))

            # 只在本次隔离测试中接入模拟平台，验证后台结果经只读轮询回传。
            fake_operations = FakeOperations({"00011": "no_new"})
            jobs.operations = fake_operations
            driver.find_element(By.ID, "start-registration").click()
            wait.until(lambda d: d.find_element(By.ID, "metric-success").text == "1" and d.find_element(By.ID, "metric-failed").text == "2")
            assert len(fake_operations.submissions) == 2
            assert [request.order_no for request in fake_operations.submissions] == ["00010", "00011"]
            assert all(order_no != "00009" for _, order_no in fake_operations.trace)
            address_failed = driver.find_elements(By.CSS_SELECTOR, '#grid-body tr[data-id] [data-field="status"]')[0]
            assert address_failed.text == "失败"
            assert "不支持改地址" in address_failed.find_element(By.TAG_NAME, "span").get_attribute("title")
            failed = driver.find_elements(By.CSS_SELECTOR, '#grid-body tr[data-id] [data-field="status"]')[2]
            assert failed.text == "失败"
            assert driver.execute_script("""
                const a=arguments[0].getBoundingClientRect(),b=arguments[0].querySelector('span').getBoundingClientRect();
                return Math.abs((a.left+a.right-b.left-b.right)/2)<2;
            """, failed)
            assert "未发现" in failed.find_element(By.TAG_NAME, "span").get_attribute("title")
            driver.find_element(By.ID, "start-registration").click()
            wait.until(lambda d: "沿用执行结果" in d.find_element(By.ID, "toast").text)
            assert len(fake_operations.submissions) == 2
            driver.save_screenshot(str(output / "workflow-results.png"))

            # 登录助手占用应在写任务前拒绝；解除占用后也必须再次明确点击开始。
            control("order_no", 3).click()
            paste("00012\t小米背包\t否")
            saved()
            draft_before_busy = store.read()["rows"]
            task_paths_before_busy = set(jobs.directory.glob("*.json"))
            busy_operations = LoginBusyOperations()
            jobs.operations = busy_operations
            # 仅记录本地真实 HTTP 返回，不替换响应；用状态轮询证明解除占用不会自动开始。
            driver.execute_script("""
                window.smokeOriginalFetch = window.fetch;
                window.smokeStartResponses = []; window.smokeStatusPolls = 0;
                window.fetch = async (...args) => {
                    const response = await window.smokeOriginalFetch(...args);
                    const path = new URL(typeof args[0] === 'string' ? args[0] : args[0].url, location.href).pathname;
                    if (path === '/api/registrations/start') {
                        const result = await response.clone().json();
                        window.smokeStartResponses.push({status: response.status, code: result.code});
                    }
                    if (path === '/api/registrations/status') window.smokeStatusPolls++;
                    return response;
                };
            """)
            driver.find_element(By.ID, "start-registration").click()
            wait.until(lambda d: "登录助手正在使用浏览器配置" in d.find_element(By.ID, "toast").text)
            assert "本次未启动登记" in driver.find_element(By.ID, "toast").text
            assert driver.execute_script("return window.smokeStartResponses") == [
                {"status": 409, "code": "registration_not_started"}]
            assert busy_operations.prepare_calls == 1
            assert driver.execute_script("return conflict") is False
            assert not busy_operations.trace and not busy_operations.submissions
            assert set(jobs.directory.glob("*.json")) == task_paths_before_busy
            assert store.read()["rows"] == draft_before_busy
            assert driver.find_elements(By.CSS_SELECTOR, '#grid-body tr[data-id] [data-field="status"]')[3].text == "待登记"
            driver.save_screenshot(str(output / "login-busy.png"))

            busy_operations.busy = False
            polls_before_release = driver.execute_script("return window.smokeStatusPolls")
            wait.until(lambda d: d.execute_script("return window.smokeStatusPolls") >= polls_before_release + 2)
            assert len(driver.execute_script("return window.smokeStartResponses")) == 1
            assert busy_operations.prepare_calls == 1
            assert not busy_operations.trace and not busy_operations.submissions
            assert set(jobs.directory.glob("*.json")) == task_paths_before_busy
            assert store.read()["rows"] == draft_before_busy
            assert driver.find_elements(By.CSS_SELECTOR, '#grid-body tr[data-id] [data-field="status"]')[3].text == "待登记"

            driver.find_element(By.ID, "start-registration").click()
            wait.until(lambda d: len(d.execute_script("return window.smokeStartResponses")) == 2)
            jobs.pending.join()
            new_identifier = store.read()["rows"][3]["id"]
            new_state = jobs.states().get(new_identifier, {})
            assert new_state.get("state") == "success", json.dumps(
                {"state": new_state, "trace": busy_operations.trace,
                 "responses": driver.execute_script("return window.smokeStartResponses")}, ensure_ascii=False)
            wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, '#grid-body tr[data-id] [data-field="status"]')[3].text == "成功")
            assert busy_operations.prepare_calls == 2
            assert [request.order_no for request in busy_operations.submissions] == ["00012"]
            assert busy_operations.trace == [("query", "00012"), ("submit", "00012"), ("query", "00012")]
            assert len(set(jobs.directory.glob("*.json")) - task_paths_before_busy) == 1
            assert store.read()["rows"] == draft_before_busy
            assert driver.execute_script("return window.smokeStartResponses.map(response => response.status)") == [409, 200]
            driver.execute_script("window.fetch = window.smokeOriginalFetch;")

            # 合成订单例子仅粘贴到隔离草稿，绝不点击开始登记。
            examples = [line.split(None, 1) for line in (ROOT / "example.md").read_text(encoding="utf-8").split("example:")[1].splitlines()
                        if line.strip() and not line.lstrip().startswith("#")]
            assert len(examples) == 2 and all(len(example) == 2 for example in examples)
            assert [example[0] for example in examples] == ["0000000000000000001", "0000000000000000002"]
            control("order_no").click()
            paste("\n".join("\t".join(example) for example in examples))
            saved()
            expected = [
                ["N00A0X070C0DXM5J01", "N00A0X070C0DXM5L01", "N00A0L1LE90DXM5001"],
                ["N00A0X070C0DXM7B01", "N00A0X070C0DXM5J01", "N00A0L1LE90DXM5001", "XS236TA350020WL001"],
            ]
            for index, (order_no, _) in enumerate(examples):
                assert store.read()["rows"][index]["order_no"] == order_no
                assert store.read()["rows"][index]["gift_ids"] == expected[index]
            assert len(fake_operations.submissions) == 2

            # 新增礼赠仅写入本次临时清单，刷新后仍可经别名匹配。
            original_catalog = catalog_store.read()
            driver.find_element(By.ID, "add-gift").click()
            driver.find_element(By.ID, "gift-create-code").send_keys("TEST-GIFT-00001")
            driver.find_element(By.ID, "gift-create-name").send_keys("隔离测试礼赠")
            driver.find_element(By.ID, "gift-create-aliases").send_keys("隔离礼赠简称、测试新礼赠")
            driver.find_element(By.ID, "gift-create-submit").click()
            wait.until(lambda d: not d.find_element(By.ID, "gift-create-dialog").get_property("open"))
            added_gift = {"id": "TEST-GIFT-00001", "name": "隔离测试礼赠", "aliases": ["隔离礼赠简称", "测试新礼赠"]}
            assert catalog_store.read() == original_catalog + [added_gift]
            driver.refresh()
            wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "#grid-body tr[data-id]")) == 21)
            assert driver.execute_script("return giftCatalog.at(-1)") == added_gift
            assert driver.execute_script("return matchGifts('隔离礼赠简称')") == {"ids": [added_gift["id"]], "unmatched": []}

            driver.set_window_size(430, 900)
            driver.save_screenshot(str(output / "mobile.png"))
            assert driver.execute_script("return document.documentElement.scrollWidth <= innerWidth")
            errors = [entry for entry in driver.get_log("browser") if entry["level"] == "SEVERE"]
            expected_conflicts = [entry for entry in errors if entry.get("source") == "network"
                                  and site.url + "api/registrations/start" in entry["message"]
                                  and "409 (Conflict)" in entry["message"]]
            assert len(expected_conflicts) == 1, json.dumps(errors, ensure_ascii=False)
            unexpected_errors = [entry for entry in errors if entry not in expected_conflicts]
            assert not unexpected_errors, json.dumps(unexpected_errors, ensure_ascii=False)
            print("PASS: 31 source catalog items; all names/codes/aliases; separated backpack options; two synthetic example clipboard matches without submission; real double-click editing; centered/resizable table; address visibility and unsupported-address rejection without platform calls; login-busy rejection preserves draft and creates no task, explicit later click submits once; simulated workflow polling; isolated gift creation survives refresh and matches aliases; mobile layout; only the expected HTTP 409 browser error.")
            print(f"Screenshots: {output}")
        finally:
            if driver:
                driver.quit()
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
