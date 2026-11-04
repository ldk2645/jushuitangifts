"""在独立 Selenium 会话中只读核对荣耀工作表和空白表单，不读取订单或提交任务。"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from gift_registry.authentication import ENTRY_URLS, SessionAuthenticator, AuthenticationRequired
from gift_registry.browser import SiteBrowser, stabilize_banniu_viewport
from gift_registry.business_browser import ProfileLock
from gift_registry.config import SITES
from gift_registry.edition import BANNIU_URL
from gift_registry.sessions import SessionStore


class InspectionAuthenticator(SessionAuthenticator):
    def _ready(self, driver, parts):
        if not super()._ready(driver, parts):
            return False
        create = self._one(driver, 'button.edit-task-button-btn')
        return create is not None and create.is_enabled() and create.text.strip() == '新建'


def main():
    ENTRY_URLS['banniu'] = BANNIU_URL
    output = ROOT / '.tmp' / 'honor-page-structure.json'
    browser = SiteBrowser(SITES[1], SessionStore(ROOT / 'data' / 'sessions'),
                          authenticator=InspectionAuthenticator(SITES[1]))
    result = {'target': BANNIU_URL, 'submitted': False, 'orders_entered': False}
    lock = ProfileLock(ROOT / 'data' / 'app.lock')
    try:
        lock.acquire()
        result.update(status='running', stage='browser_start')
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        browser.open()
        driver = browser.driver
        result['stage'] = 'authentication'
        print('If verification is shown, complete it in this independent browser. No orders will be submitted.', flush=True)
        def ready(_):
            try:
                browser.ensure_authenticated()
            except AuthenticationRequired:
                return False
            return browser.authenticator.is_authenticated(driver)
        WebDriverWait(driver, 300).until(ready)
        result['title'] = browser.authenticator._one(driver, 'div.title.el-dropdown-selfdefine').text.strip()
        browser.capture(deadline=time.monotonic() + 10)
        stabilize_banniu_viewport(driver)
        create = WebDriverWait(driver, 30).until(lambda _: browser.authenticator._one(driver, 'button.edit-task-button-btn'))
        assert create.text.strip() == '新建' and driver.current_url == BANNIU_URL
        create.click()
        result['stage'] = 'empty_form'
        form = WebDriverWait(driver, 30).until(lambda _: browser.authenticator._one(driver, '.w-edit-task-dialog'))
        result['fields'] = driver.execute_script('''
            return [...arguments[0].querySelectorAll('[id^="col_"]')].map(e=>({
              id:e.id, classes:e.className,
              labels:[...e.querySelectorAll('[title]')].map(x=>x.title).filter(Boolean),
              text:e.textContent.trim().slice(0,500),
              inputs:[...e.querySelectorAll('input,textarea')].map(x=>({tag:x.tagName,type:x.type,placeholder:x.placeholder})),
              headers:[...e.querySelectorAll('th')].map(x=>x.textContent.trim())
            }));
        ''', form)
        result['status'] = 'empty_form_observed'
    except Exception as exc:
        result['status'] = 'incomplete'
        result['error_type'] = type(exc).__name__
        if result.get('stage') == 'browser_start':
            # 启动阶段尚未读取密码；保留诊断首行，避免将后续页面异常正文写入日志。
            result['startup_error'] = str(exc).split('Stacktrace:')[0][:1400]
    finally:
        if browser.driver is not None:
            try:
                browser.close()
            except Exception:
                result['close_warning'] = True
        lock.release()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print('Status: ' + result['status'], flush=True)
        print('Report: ' + str(output), flush=True)


if __name__ == '__main__':
    main()
