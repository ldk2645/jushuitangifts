r"""隔离真实 Chrome 验收：共享环境暂停横幅、等待行状态与主机恢复。

使用临时目录、临时礼品清单、127.0.0.1 临时前端服务和模拟平台，绝不连接真实平台，
也不改动用户 config/data/队列。文件名 smoke_*，不会被 unittest 自动发现；需要 Chrome。

运行：.\.venv\Scripts\python.exe -X utf8 tests\smoke_pause_resume.py
"""

import json
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from fakes import FakeOperations
from gift_registry.authentication import AuthenticationRequired
from gift_registry.browser import create_driver
from gift_registry.config import Site
from gift_registry.frontend_server import RowStore, make_server
from gift_registry.gift_catalog import GiftCatalogStore
from gift_registry.registration import RegistrationQueue
from gift_registry.workflow import ManualInterventionRequired


OUT = ROOT / ".tmp" / "ui-checks"
CATALOG = [
    {"id": "SYN-A", "name": "合成收纳包", "aliases": []},
    {"id": "SYN-B", "name": "合成自拍杆", "aliases": []},
]
ROWS = [
    {"id": "row-A", "order_no": "SYN-PAUSE-A", "gift": "合成收纳包",
     "gift_ids": ["SYN-A"], "change_address": False},
    {"id": "row-B", "order_no": "SYN-PAUSE-B", "gift": "合成自拍杆",
     "gift_ids": ["SYN-B"], "change_address": False},
]


class PausingOperations(FakeOperations):
    """首单认证失败触发暂停；主机恢复后允许只读就绪检查并继续未执行订单。"""

    def __init__(self):
        super().__init__()
        self.ready = False

    def query_order(self, order_no):
        if not self.ready:
            raise AuthenticationRequired("SYNTHETIC: 需要人工登录或验证码")
        return super().query_order(order_no)

    def recover_environment(self):
        if not self.ready:
            raise ManualInterventionRequired("SYNTHETIC: 站点/登录仍未就绪")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    (ROOT / ".tmp").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pause-resume-", dir=ROOT / ".tmp") as directory:
        folder = Path(directory)
        catalog_path = folder / "gifts.json"
        catalog_path.write_text(json.dumps(CATALOG, ensure_ascii=False), encoding="utf-8")
        store = RowStore(folder / "rows.json")
        catalog_store = GiftCatalogStore(catalog_path)
        operations = PausingOperations()
        jobs = RegistrationQueue(folder / "jobs", operations=operations)
        try:
            store.write(0, ROWS)
            jobs.start(ROWS, CATALOG)
            jobs.pending.join()
            assert jobs.paused, "预期首单认证失败后进入暂停"
            assert jobs.states()['row-A']['state'] == 'failed'
            assert jobs.states()['row-B']['state'] == 'waiting'

            server = make_server(0, store=store, queue=jobs, catalog=catalog_store)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            driver = None
            try:
                url = f"http://127.0.0.1:{server.server_port}/"
                site = Site("pause-resume-ui", "暂停恢复验收", url, ("127.0.0.1",))
                driver = create_driver(site, headless=True, profile=folder / "profile")
                driver.set_window_size(1500, 1000)
                driver.get(url)
                wait = WebDriverWait(driver, 25)
                wait.until(lambda d: d.execute_script("return initialized"))
                wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "#grid-body tr[data-id]")) == 2)

                notice = driver.find_element(By.ID, "pause-notice")
                wait.until(lambda d: notice.is_displayed())
                text = driver.find_element(By.ID, "pause-notice-text").text
                assert "原因：" in text and "暂停待恢复" in text, text
                assert "1 条" in text, text
                assert driver.find_element(By.ID, "start-registration").get_attribute("disabled") is not None

                badges = driver.find_elements(By.CSS_SELECTOR, '#grid-body tr[data-id] td[data-field="status"]')
                assert "失败" in badges[0].text, badges[0].text
                assert "暂停待恢复" in badges[1].text, badges[1].text
                assert driver.find_elements(By.CSS_SELECTOR, '#grid-body tr[data-id] .row-cancel'), "等待行应可取消"
                driver.save_screenshot(str(OUT / "pause-notice.png"))

                operations.ready = True  # 模拟主机完成登录/验证码处理
                driver.find_element(By.ID, "resume-queue").click()
                wait.until(lambda d: not d.find_element(By.ID, "pause-notice").is_displayed())
                wait.until(lambda d: "成功" in driver.find_elements(
                    By.CSS_SELECTOR, '#grid-body tr[data-id] td[data-field="status"]')[1].text)
                badges = driver.find_elements(By.CSS_SELECTOR, '#grid-body tr[data-id] td[data-field="status"]')
                assert "失败" in badges[0].text, badges[0].text
                assert [entry.order_no for entry in operations.submissions] == ["SYN-PAUSE-B"]
                assert not jobs.paused
                driver.save_screenshot(str(OUT / "pause-resumed.png"))

                severe = [entry for entry in driver.get_log("browser") if entry["level"] == "SEVERE"]
                assert not severe, severe
                print("PASS: 暂停横幅显示原因/等待数量；失败行失败、后续行暂停待恢复且可取消；"
                      "开始按钮禁用；主机恢复只读验证后未执行行执行一次，原失败行不重提；无严重浏览器错误。")
                print(f"Screenshots: {OUT / 'pause-notice.png'}, {OUT / 'pause-resumed.png'}")
            finally:
                if driver:
                    driver.quit()
                server.shutdown()
                server.server_close()
        finally:
            jobs.close()


if __name__ == "__main__":
    main()
