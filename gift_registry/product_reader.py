"""只读商品查询：2026-11-05 实际页面核对，独立 Selenium 配置与会话副本。"""
import re
import time
import uuid
from pathlib import Path

from selenium.common.exceptions import (NoSuchElementException, StaleElementReferenceException,
                                        TimeoutException, WebDriverException)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from .authentication import ENTRY_URLS, AuthenticationRequired
from .banniu_page import BanniuPage, _open_registration_list, _REGISTRATION_LIST_READY
from .browser import SiteBrowser, create_driver
from .catalog_maintenance import normalize_name, timestamp
from .config import DATA_DIR, SITES
from .driver_shutdown import quit_owned_driver
from .sessions import SessionStore
from .workflow import ManualInterventionRequired, PlatformOperationError


class ProductQueryBlocked(ManualInterventionRequired):
    """查询窗口出现无法证明归属的内容：停止自动查询并保留窗口交人工处理。"""


_QUERY_OWNER_STATE = """
// gift-product-query-owner: read-only ownership/progress snapshot.
const root=arguments[0], token=arguments[1];
const state=root && root.isConnected ? root.__giftRegistrationOwner : null;
const dialog=arguments[2];
const order=document.querySelector('#col_27724 textarea');
const giftRows=document.querySelectorAll('#col_27748 .children-box-body > .el-table .el-table__body-wrapper > table > tbody > tr');
return {rootConnected: !!(root && root.isConnected),
        sameRoot: root===dialog,
        tokenMatch: !!state && state.token===token,
        submitSeen: !!state && state.submitSeen===true,
        manualEdited: !!state && state.manualEdited===true,
        automation: state ? state.automation : null,
        order: order ? (order.value || '').trim() : null,
        giftCount: giftRows.length};
"""


def query_form_state_problem(state):
    """判断查询表单是否仍可安全视为本工具的空白未提交页面；返回中文原因。"""
    if not isinstance(state, dict) or not state.get("rootConnected") or not state.get("sameRoot"):
        return "班牛查询表单已被替换或关闭，无法证明归属"
    if not state.get("tokenMatch") or state.get("automation") is None:
        return "班牛查询表单归属标记缺失或被外部替换"
    if state.get("manualEdited"):
        return "检测到人工修改操作，已停止自动查询并保留当前窗口供人工处理"
    if state.get("submitSeen"):
        return "检测到提交类点击，已停止自动查询并保留当前窗口供人工核对"
    if state.get("order"):
        return "班牛查询表单已有订单号，已停止自动查询，不会刷新覆盖人工内容"
    if state.get("giftCount"):
        return "班牛查询表单已有商品明细，已停止自动查询，不会刷新覆盖人工内容"
    return None

# 来源：聚水潭主导航“商品”→“商品及库存管理(普通商品资料)”的 iframe src。
JUSHUITAN_PRODUCTS_URL = ('https://dailisrc.jushuitan.com/erp-web-group/erp-scm-goods/'
                         'goodsInventoryManagement?tabAllow=camera&_c=jst-epaas&epaas=true')

_JST_STATE = """
// Read only rendered product identifiers/names and completeness markers.
const shown=e=>!!e && e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
const heads=[...document.querySelectorAll('.art-table-header th')].map(e=>e.textContent.trim());
const ci=heads.indexOf('商品编码'), ni=heads.indexOf('商品名称');
const valid=heads.filter(x=>x==='商品编码').length===1 && heads.filter(x=>x==='商品名称').length===1;
const rows=[...document.querySelectorAll('.art-table-body tbody tr.art-table-row')]
.filter(row=>!row.querySelector('.art-empty-wrapper')).map(row=>{
  const cells=[...row.querySelectorAll(':scope > td')];
  return {code:valid ? cells[ci]?.textContent.trim() : null,
          name:valid ? cells[ni]?.textContent.trim() : null};
});
const p=document.querySelector('.jst-erp-pagination');
return {valid,rows,total:p?.querySelector('.jst-pagination-total-text')?.textContent,
        page:p?.querySelector('.goods-pagination-simple-pager input')?.value,
        end:p?.querySelector('.goods-pagination-next')?.getAttribute('aria-disabled')==='true',
        start:p?.querySelector('.goods-pagination-prev')?.getAttribute('aria-disabled')==='true',
        loading:[...document.querySelectorAll('.goods-spin-spinning,.art-loading-indicator')].some(shown)};
"""


def complete_rows(state):
    """分页/虚拟行不完整时禁止把首屏当成全部结果。"""
    if not state.get('valid') or state.get('loading'):
        return None
    total = re.fullmatch(r'共\s*(\d+)\s*条', (state.get('total') or '').strip())
    rows = state.get('rows')
    if (not total or not isinstance(rows, list) or state.get('page') != '1'
            or state.get('start') is not True or state.get('end') is not True
            or len(rows) != int(total[1])):
        return None
    if any(not isinstance(row, dict) or not isinstance(row.get('code'), str) or not row['code']
           or not isinstance(row.get('name'), str) or not row['name'] for row in rows):
        return None
    return rows


def observation(platform, code, rows, url, **extra):
    # 班牛同一编码会返回大量重复商品记录；按（编码，名称）去重后只保留核实需要的信息。
    matches, seen = [], set()
    for row in rows:
        if row['code'] != code:
            continue
        key = (row['code'], row['name'])
        if key in seen:
            continue
        seen.add(key)
        matches.append(row)
    # 名称可能只差全角/半角或空白；比较时忽略空白。
    names = {normalize_name(row['name']) for row in matches}
    result = {'platform': platform, 'queried_code': code, 'observed_at': timestamp(), 'source_url': url,
              'status': 'missing' if not matches else 'exact' if len(names) == 1 else 'conflict',
              'matches': matches}
    result.update(extra)
    return result


def _fresh_rows(driver, read, previous, *, timeout=25):
    last, since = None, None
    started = time.monotonic()

    def ready(_):
        nonlocal last, since
        rows = complete_rows(read())
        if rows is None:
            last, since = None, time.monotonic()
            return False
        if rows == previous:
            # 与上次相同：非空结果可判定为未刷新；空结果无法用内容区分刷新，
            # 只允许在点击搜索后留出短暂刷新窗口再接受，避免连续空结果误判。
            if rows or time.monotonic() - started < 2.0:
                last, since = None, time.monotonic()
                return False
        if rows != last:
            last, since = rows, time.monotonic()
        return (rows,) if time.monotonic() - since >= .8 else False

    try:
        return WebDriverWait(driver, timeout, poll_frequency=.2,
            ignored_exceptions=(NoSuchElementException, StaleElementReferenceException)).until(ready)[0]
    except TimeoutException:
        raise PlatformOperationError('查询结果未刷新、分页或表格未完整加载，保留原配置') from None


def banniu_first_row_state(state):
    """班牛同编码商品列表：只取第一行商品名称作为该编码的名称。

    返回 (总数, [第一行])；分页仍在加载、页码/总数不明确或首行名称不完整时返回 None。
    同编码重复记录可能很多，按用户规则不翻页、不比对其它行。
    """
    total = re.fullmatch(r'共\s*(\d+)\s*条', (state.get('total') or '').strip())
    if not total or state.get('loading') or state.get('page') != '1' or state.get('start') is not True:
        return None
    count = int(total[1])
    rows = state.get('rows')
    if not isinstance(rows, list):
        return None
    if count == 0:
        return 0, []
    if not rows:
        return None
    first = rows[0]
    if not first.get('code') or not first.get('name'):
        return None
    return count, [first]


class BanniuProductPage:
    def __init__(self, driver):
        self.driver = driver
        self.page = BanniuPage(driver)
        self.previous = []
        _open_registration_list(driver)
        self.page._wait(lambda _: driver.execute_script(_REGISTRATION_LIST_READY) is True,
                        '班牛登记列表尚未就绪')
        self.page._click_once(self.page._one('button.edit-task-button-btn', text='新建'))
        self.root = self.page._dialog()
        self._track_query_form()
        self._blank()

    def _track_query_form(self):
        # 复用 BanniuPage 的归属跟踪：记录人工输入/提交点击，自动填写通过 _form_action 计数。
        self._marker = object()
        self.page._track_form(self.root, self._marker)
        self.token = self.page._owned_form[2]

    def _owner_state(self):
        return self.driver.execute_script(_QUERY_OWNER_STATE, self.root, self.token,
                                          self.page._dialog())

    def _ensure_ownership(self):
        problem = query_form_state_problem(self._owner_state())
        if problem:
            raise ProductQueryBlocked(problem + "；查询已停止，窗口保留供人工处理")

    def _blank(self):
        problem = query_form_state_problem(self._owner_state())
        if problem:
            raise ProductQueryBlocked(problem + "；查询已停止，窗口保留供人工处理")
        self.page._validate_honor_context()
        try:
            with self.page._form_action():
                self.page._close_search()  # 读完后关闭结果面板，避免残留面板或遮罩影响下一编码。
        except PlatformOperationError:
            pass  # 关闭失败交给下一编码或恢复流程处理，不能据此认定为已填写表单。

    def recover(self):
        """只读查询卡住时恢复空白查询页；先核对人工内容，绝不在未知表单上刷新。"""
        self.driver.switch_to.default_content()
        dialog = self.page._dialog() if self._dialog_visible() else None
        if dialog is not None:
            problem = query_form_state_problem(self._owner_state())
            if problem:
                raise ProductQueryBlocked(problem + "；未刷新、未关闭该窗口，请人工处理后再运行")
            # 本工具自己的空白表单：安全关闭，再重新打开空白查询页。
            close = self.page._one('.w-dialog-title > .w-dialog-close', root=self.root)
            self.page._click_once(close)
            self.page._wait(lambda _: not self.driver.execute_script(
                "return !!document.querySelector('.w-edit-task-dialog')"),
                '班牛商品查询表单未关闭，已停止自动恢复')
        _open_registration_list(self.driver)
        self.page._wait(lambda _: self.driver.execute_script(_REGISTRATION_LIST_READY) is True,
                        '班牛商品查询恢复时登记列表尚未就绪')
        self.page._click_once(self.page._one('button.edit-task-button-btn', text='新建'))
        self.root = self.page._dialog()
        self._track_query_form()
        self._blank()

    def _dialog_visible(self):
        try:
            return bool(self.driver.find_elements(By.CSS_SELECTOR, '.w-edit-task-dialog'))
        except WebDriverException:
            return False

    def _read(self):
        panel = self.page._one('#col_27748 .child-search-search-task', root=self.root)
        _, values = self.page._table('#col_27748 .child-search-search-task-table > .el-table')
        state = self.driver.execute_script("""
            const p=arguments[0], visible=e=>e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
            return {total:p.querySelector('.el-pagination__total')?.textContent,
                    page:p.querySelector('.el-pager .active')?.textContent.trim(),
                    start:p.querySelector('.btn-prev')?.disabled===true,
                    end:p.querySelector('.btn-next')?.disabled===true,
                    loading:[...p.querySelectorAll('.el-loading-mask')].some(visible)};
        """, panel)
        return {**state, 'valid': True,
                'rows': [{'code': row['商家编码'], 'name': row['货品名称']} for row in values]}

    def query(self, code):
        started = time.monotonic()
        self._blank()
        self.page._close_search()
        if self.page._value('#col_27748 .child-search-search-select input') != '商家编码':
            raise PlatformOperationError('班牛搜索字段不是商家编码')
        self.page._set_text('#col_27748 .child-search-search-condition input', code)
        self._ensure_ownership()
        self.page._click_once(self.page._one('#col_27748 .child-search-search-condition button',
                                            root=self.root, text='筛选'))
        result = {}
        last, since = None, time.monotonic()

        def ready(_):
            nonlocal last, since
            parsed = banniu_first_row_state(self._read())
            if parsed is None:
                last, since = None, time.monotonic()
                return False
            if parsed[1] == self.previous:
                # 与上次相同：非空可判定未刷新；空结果留 2 秒刷新窗口后再接受。
                if parsed[1] or time.monotonic() - started < 2.0:
                    last, since = None, time.monotonic()
                    return False
            if parsed != last:
                last, since = parsed, time.monotonic()
            if time.monotonic() - since < .8:
                return False
            result.update({'total': parsed[0], 'rows': parsed[1]})
            return True

        self.page._wait(ready, '班牛商品列表未完整加载或未刷新，保留原配置', timeout=45)
        self.previous = result['rows']
        self._ensure_ownership()
        self._blank()
        return observation('banniu', code, result['rows'], ENTRY_URLS['banniu'],
                           total=result['total'])


class JushuitanProductPage:
    def __init__(self, driver):
        self.driver = driver
        driver.get(JUSHUITAN_PRODUCTS_URL)
        self.wait = WebDriverWait(driver, 25, poll_frequency=.2,
                                  ignored_exceptions=(NoSuchElementException, StaleElementReferenceException))
        self.wait.until(lambda d: d.find_element(By.CSS_SELECTOR, '#sku_id').is_displayed())
        # 商品页默认只显示“启用”；清除这个查询条件，才能核对停用但仍在目录中的编码。
        status = driver.find_element(By.CSS_SELECTOR, 'input[placeholder="商品状态"]')
        if status.get_attribute('value'):
            clear = status.find_element(By.XPATH, '..').find_element(By.CSS_SELECTOR, '.goods-input-clear-icon')
            try:
                clear.click()  # 图标可能被遮罩或尺寸为 0，原生点击不可用时退回 DOM 点击。
            except WebDriverException:
                driver.execute_script("arguments[0].click()", clear)
            self.wait.until(lambda d: not d.find_element(By.CSS_SELECTOR, 'input[placeholder="商品状态"]').get_attribute('value'))

    def query(self, code):
        driver = self.driver
        field = driver.find_element(By.CSS_SELECTOR, '#sku_id')
        mode = field.find_element(By.XPATH, '..').find_elements(By.CSS_SELECTOR, '.j-bmi-mode-exact')
        if len(mode) != 1:
            raise PlatformOperationError('聚水潭商品编码筛选不是精确匹配，请在查询窗口切换为“精”')
        previous = driver.execute_script(_JST_STATE)['rows']
        field.send_keys(Keys.CONTROL, 'a')
        field.send_keys(code)
        self.wait.until(lambda d: d.find_element(By.CSS_SELECTOR, '#sku_id').get_attribute('value') == code)
        buttons = [button for button in driver.find_elements(By.CSS_SELECTOR, 'button')
                   if button.is_displayed() and ''.join(button.text.split()) == '搜索']
        if len(buttons) != 1:
            raise PlatformOperationError('聚水潭商品查询按钮不唯一')
        buttons[0].click()
        rows = _fresh_rows(driver, lambda: driver.execute_script(_JST_STATE), previous)
        if driver.find_element(By.CSS_SELECTOR, '#sku_id').get_attribute('value') != code:
            raise PlatformOperationError('商品筛选编码被修改，未采用查询结果')
        return observation('jushuitan', code, rows, JUSHUITAN_PRODUCTS_URL)


class SeleniumProductReader:
    def __init__(self, *, headless=False, progress=None, platforms=('banniu', 'jushuitan'),
                 login_wait_seconds=0):
        if (not isinstance(platforms, (tuple, list)) or not platforms
                or any(platform not in ('banniu', 'jushuitan') for platform in platforms)
                or len(set(platforms)) != len(platforms)):
            raise ValueError('商品查询平台配置无效')
        if type(login_wait_seconds) is not int or not 0 <= login_wait_seconds <= 600:
            raise ValueError('人工登录等待秒数必须在 0 到 600 之间')
        self.headless = headless
        self.progress = progress or (lambda message: None)
        self.platforms = tuple(platforms)
        self.login_wait_seconds = login_wait_seconds

    def _save_session(self, site, browser):
        """人工登录成功后刷新加密会话快照，供后续查询/登录助手复用。"""
        try:
            browser.capture(deadline=time.monotonic() + 10)
            if browser.snapshot:
                SessionStore(DATA_DIR / 'sessions').save(site.key, browser.snapshot)
        except Exception:
            self.progress(f'{site.name}：会话快照保存未完成，不影响本次只读查询')

    def _authenticate(self, site, browser):
        try:
            browser.ensure_authenticated()
            return
        except AuthenticationRequired as exc:
            first_error = exc
        if self.headless or self.login_wait_seconds <= 0:
            raise first_error
        self.progress(f'{site.name}：{first_error}')
        self.progress(f'{site.name}：请在已打开的浏览器窗口完成登录（含验证码），'
                      f'程序最多等待 {self.login_wait_seconds} 秒；完成后自动继续查询')
        deadline = time.monotonic() + self.login_wait_seconds
        last_error = str(first_error)
        while time.monotonic() < deadline:
            time.sleep(2)
            try:
                browser.ensure_authenticated()
            except AuthenticationRequired as pending:
                last_error = str(pending)
                continue
            if browser.authenticator is None or browser.authenticator.is_authenticated(browser.driver):
                self.progress(f'{site.name}：人工登录已确认，保存会话并继续商品查询')
                self._save_session(site, browser)
                return
        raise AuthenticationRequired(last_error + '；等待人工登录超时')

    def query(self, codes, profile_directory):
        run = Path(profile_directory) / uuid.uuid4().hex
        result = []
        # 每次运行独立配置；读取既有 DPAPI 快照，仅保存到维护目录下的会话副本。
        for site in SITES:
            if site.key not in self.platforms:
                continue
            browser = SiteBrowser(site, SessionStore(run / 'sessions'))
            completed = set()
            blocked_manual = None
            try:
                self.progress(site.name + '：打开独立只读商品查询')
                browser.driver = create_driver(site, headless=self.headless, profile=run / site.key)
                snapshot = SessionStore(DATA_DIR / 'sessions').load(site.key)
                if snapshot:
                    browser.restore(snapshot)
                browser.navigate(ENTRY_URLS[site.key])
                self._authenticate(site, browser)
                page = (BanniuProductPage(browser.driver) if site.key == 'banniu'
                        else JushuitanProductPage(browser.driver))
                failures = 0
                for index, code in enumerate(codes, 1):
                    self.progress(f'{site.name}：{index}/{len(codes)} {code}')
                    try:
                        result.append(page.query(code))
                        failures = 0
                    except ProductQueryBlocked as exc:
                        blocked_manual = str(exc)
                        failures = 0
                        result.append({'platform': site.key, 'queried_code': code, 'observed_at': timestamp(),
                                       'status': 'blocked', 'matches': [], 'reason': blocked_manual})
                    except PlatformOperationError as exc:
                        # 单个编码的只读查询失败不能中断同平台其它编码的核实；先尝试
                        # 恢复空白查询页（刷新表单，不提交），成功则继续下一编码。
                        result.append({'platform': site.key, 'queried_code': code, 'observed_at': timestamp(),
                                       'status': 'blocked', 'matches': [], 'reason': str(exc)})
                        recover = getattr(page, 'recover', None)
                        recovered = False
                        if callable(recover):
                            try:
                                recover()
                                recovered = True
                            except ProductQueryBlocked as block:
                                blocked_manual = str(block)
                            except (PlatformOperationError, WebDriverException):
                                recovered = False
                        failures = 0 if recovered else failures + 1
                        if failures >= 2:
                            raise PlatformOperationError(f'{site.name}商品查询连续失败，停止本平台查询') from None
                    completed.add(code)
                    if blocked_manual:
                        # 人工内容/提交操作出现：立即停止本平台查询，保留窗口供人工处理；
                        # 后续编码记为 blocked，绝不刷新覆盖或继续输入。
                        for remaining in codes[index:]:
                            if remaining not in completed:
                                result.append({'platform': site.key, 'queried_code': remaining,
                                               'observed_at': timestamp(), 'status': 'blocked',
                                               'matches': [], 'reason': blocked_manual})
                                completed.add(remaining)
                        self.progress(f'{site.name}：检测到人工内容，已停止自动查询并保留窗口，请人工处理')
                        break
            except (PlatformOperationError, WebDriverException, OSError, ValueError) as exc:
                reason = str(exc) if isinstance(exc, PlatformOperationError) else '浏览器、登录快照或商品页面不可用，请运行登录助手后重试'
                for code in codes:
                    if code not in completed:
                        result.append({'platform': site.key, 'queried_code': code, 'observed_at': timestamp(),
                                       'status': 'blocked', 'matches': [], 'reason': reason})
            finally:
                if blocked_manual:
                    # 保留窗口与内容供人工处理；不刷新、不关闭、不写回目录。
                    self.progress(f'{site.name}：窗口已保留，请在处理人工内容后关闭该独立查询浏览器')
                elif browser.driver is not None:
                    quit_owned_driver(browser.driver)
        return result
