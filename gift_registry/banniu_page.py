"""班牛礼赠表单操作：同一订单的全部礼品加入同一任务，仅点击一次提交。"""

import secrets
import unicodedata
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation

from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException, TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from .workflow import PlatformOperationError, Recipient, RegistrationNotSubmittedError, RegistrationRequest, validate_snapshot


BANNIU_REGISTRATION_URL = "https://work.bytenew.com/app.html#/applet/appNew/projectNew/54119/1883"
GIFT_COLUMNS = ("退换补类型", "商家编码", "货品名称", "退换补商品金额", "数量")

# 已在真实班牛任务和聚水潭同编码商品中核对的名称；只适用于此 SKU，不做模糊放行。
_OBSERVED_GIFT_NAMES = {
    'XS24AMA350021GK001': ('椰球夹耳式耳机', 'OWS31蓝牙耳机'),
    'XS24A7A35002PMJ002': ('S11 Pro Max 智能手表（白色）', 'S11 Pro Max 智能手表'),
    'N0800202036T000801': ('赠品《联想》蓝牙音箱', '《赠品》联想蓝牙音响'),
}

_REGISTRATION_LIST_READY = """
// gift-registration-list-ready: only reuse a visible worksheet, never its results.
const visible=e=>e.getClientRects().length && getComputedStyle(e).visibility!=='hidden' && Number(getComputedStyle(e).opacity)>0;
const shown=selector=>[...document.querySelectorAll(selector)].filter(visible);
const buttons=shown('button.edit-task-button-btn').filter(e=>e.textContent.trim()==='新建');
if(shown('div.title.el-dropdown-selfdefine').filter(e=>e.textContent.trim()==='聚水潭赠品补发-融合').length!==1 ||
   buttons.length!==1 || buttons[0].disabled || shown('.vxe-table').length!==1 ||
   shown('.task-details,.worksheet-main-details-back,.w-edit-task-dialog,.el-loading-mask').length) return false;
const button=buttons[0], r=button.getBoundingClientRect();
const x=(Math.max(0,r.left)+Math.min(innerWidth,r.right))/2;
const y=(Math.max(0,r.top)+Math.min(innerHeight,r.bottom))/2;
const hit=document.elementFromPoint(x,y);
return !!hit && (hit===button || button.contains(hit));
"""


def _open_registration_list(driver):
    """调用方先拒绝未关闭的新建表单；详情或其它页面仍正常重新进入列表。"""
    # 切回复用的班牛窗口，避免后台窗口延迟处理加载层的淡出动画。
    driver.switch_to.window(driver.current_window_handle)
    if driver.current_url != BANNIU_REGISTRATION_URL:
        driver.get(BANNIU_REGISTRATION_URL)
    elif driver.execute_script(_REGISTRATION_LIST_READY) is not True:
        # 班牛使用 hash 路由；get 同一带片段 URL 可能只做同文档导航，保留旧详情遮罩。
        # 仅在没有可见编辑表单、且列表不能交互时刷新一次，不在点击失败后重调登记。
        driver.refresh()


def _normalize(value):
    return "".join(unicodedata.normalize("NFKC", value).split())


def _gift_name_matches(code, actual, expected):
    actual, expected = _normalize(actual), _normalize(expected)
    if actual == expected:
        return True
    observed = {_normalize(name) for name in _OBSERVED_GIFT_NAMES.get(code, ())}
    return actual in observed and expected in observed


def table_records(headers, rows):
    """按已观察到的表头映射，不能依赖布局列号或忽略缺失列。"""
    normalized = [_normalize(header).lstrip("*") for header in headers]
    if any(normalized.count(name) != 1 for name in GIFT_COLUMNS):
        raise PlatformOperationError("班牛商品表格列发生变化，未继续填写")
    indices = {name: normalized.index(name) for name in GIFT_COLUMNS}
    result = []
    for cells in rows:
        if any(index >= len(cells) for index in indices.values()):
            raise PlatformOperationError("班牛商品表格数据不完整，未继续填写")
        result.append({name: cells[index].strip() for name, index in indices.items()})
    return result


def gifts_match(records, gift_ids, gift_names):
    if len(records) != len(gift_ids) or len(set(gift_ids)) != len(gift_ids):
        return False
    expected = dict(zip(gift_ids, gift_names))
    if len(expected) != len(gift_names) or {row["商家编码"] for row in records} != set(expected):
        return False
    for row in records:
        if row["退换补类型"] != "补发" or not _gift_name_matches(row['商家编码'], row['货品名称'], expected[row['商家编码']]):
            return False
        try:
            if Decimal(row["数量"]) != Decimal(1) or Decimal(row["退换补商品金额"]) != Decimal(0):
                return False
        except InvalidOperation:
            return False
    return True


class BanniuPage:
    """所有动作均通过 Selenium；等待条件只读取，不重试点击或提交。"""

    def __init__(self, driver, *, timeout=20, authenticate=None):
        self.driver = driver
        self.timeout = timeout
        self._attempted = set()
        self._owned_form = None
        self.authenticate = authenticate

    def _wait(self, predicate, detail):
        try:
            return WebDriverWait(self.driver, self.timeout, poll_frequency=.2,
                                 ignored_exceptions=(NoSuchElementException, StaleElementReferenceException)).until(predicate)
        except TimeoutException:
            raise PlatformOperationError(detail) from None

    @staticmethod
    def _visible(root, selector):
        return [element for element in root.find_elements(By.CSS_SELECTOR, selector) if element.is_displayed()]

    def _one(self, selector, *, root=None, text=None):
        root = root if root is not None else self.driver

        def locate(_):
            elements = self._visible(root, selector)
            if text is not None:
                elements = [element for element in elements if element.text.strip() == text]
            if len(elements) > 1:
                raise PlatformOperationError("班牛页面出现多个同名控件，未继续填写")
            return elements[0] if elements else False

        return self._wait(locate, "班牛页面控件未出现，请检查登录状态或页面版本")

    def _dialog(self):
        return self._one(".w-edit-task-dialog")

    def _click_once(self, element):
        """等待真实点击位置可用，然后只点击一次；不绕过遮挡、不重试点击。"""
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center',inline:'center'})", element)

        def ready(_):
            if not element.is_displayed() or not element.is_enabled():
                return False
            return self.driver.execute_script("""
                // gift-banniu-click-ready: transparent overlays can still receive pointer events.
                const element=arguments[0],r=element.getBoundingClientRect();
                if(!r.width || !r.height) return false;
                const x=(Math.max(0,r.left)+Math.min(innerWidth,r.right))/2;
                const y=(Math.max(0,r.top)+Math.min(innerHeight,r.bottom))/2;
                const hit=document.elementFromPoint(x,y);
                return !!hit && (hit===element || element.contains(hit));
            """, element) is True

        self._wait(ready, "班牛目标控件仍被遮罩覆盖或不可用，未点击；请人工核查")
        with self._form_action():
            element.click()

    def _field(self, selector):
        return self._one(selector, root=self._dialog())

    def _track_form(self, root, request):
        token = secrets.token_urlsafe(16)
        tracked = self.driver.execute_script("""
            // gift-track-owned-form: retain DOM identity and any human/program submit interaction.
            const root=arguments[0], token=arguments[1];
            if(!root.isConnected || root.__giftRegistrationOwner) return false;
            const state={token,submitSeen:false,manualEdited:false,automation:0};
            state.click=e=>{
                const button=e.target.closest('button');
                if(button && root.contains(button) && ['提交','提交并新增'].includes(button.textContent.trim())) state.submitSeen=true;
            };
            state.submit=()=>{state.submitSeen=true};
            state.edit=e=>{if(e.isTrusted && state.automation===0) state.manualEdited=true};
            root.addEventListener('click',state.click,true);
            root.addEventListener('submit',state.submit,true);
            for(const event of ['input','change','keydown','pointerdown','click']) root.addEventListener(event,state.edit,true);
            root.__giftRegistrationOwner=state;
            return true;
        """, root, token)
        if tracked is not True:
            raise PlatformOperationError('班牛新建表单归属无法确认，未继续填写')
        self._owned_form = (root, request, token)

    @contextmanager
    def _form_action(self):
        owned = self._owned_form
        if owned is None:
            yield
            return
        root, _, token = owned
        if self.driver.execute_script("""
            const state=arguments[0].__giftRegistrationOwner;
            if(!state || state.token!==arguments[1] || state.manualEdited) return false;
            state.automation++;return true;
        """, root, token) is not True:
            raise PlatformOperationError('班牛表单已被人工操作或归属变化，未继续自动填写或点击')
        try:
            yield
        finally:
            try:
                self.driver.execute_script("""
                    const state=arguments[0].__giftRegistrationOwner;
                    if(state?.token===arguments[1]) state.automation=Math.max(0,state.automation-1);
                """, root, token)
            except WebDriverException:
                pass

    def _send_keys(self, element, *keys):
        with self._form_action():
            element.send_keys(*keys)

    def _forget_form(self):
        owned, self._owned_form = self._owned_form, None
        if owned is not None:
            try:
                self.driver.execute_script("""
                    const root=arguments[0],state=root.__giftRegistrationOwner;
                    if(state?.token===arguments[1]) {
                        root.removeEventListener('click',state.click,true);
                        root.removeEventListener('submit',state.submit,true);
                        for(const event of ['input','change','keydown','pointerdown','click']) root.removeEventListener(event,state.edit,true);
                        delete root.__giftRegistrationOwner;
                    }
                """, owned[0], owned[2])
            except WebDriverException:
                pass

    def _discard_unsubmitted_form(self, request):
        """只关闭本次创建、未触发提交、订单及已选礼品仍符合请求的表单。"""
        if self._owned_form is None or self._owned_form[1] != request:
            return 'none'
        root, _, token = self._owned_form
        try:
            forms = self._visible(self.driver, '.w-edit-task-dialog')
            if not forms:
                self._forget_form()
                return 'none'
            if len(forms) != 1 or forms[0] != root:
                return 'preserved'
            if self._value('#col_1891 textarea') not in ('', request.order_no):
                return 'preserved'
            selected = (self._selected_gifts() if self._visible(root, '#col_1915 .children-box-body > .el-table') else [])
            count = len(selected)
            if not gifts_match(selected, request.gift_ids[:count], request.gift_names[:count]):
                return 'preserved'
            if self.driver.execute_script("""
                // gift-owned-form-can-close: never adopt a pre-existing form or a submit-uncertain form.
                const root=arguments[0],state=root.__giftRegistrationOwner;
                return root.isConnected && state?.token===arguments[1] && state.submitSeen===false && state.manualEdited===false;
            """, root, token) is not True:
                return 'preserved'
            self._click_once(self._one('.w-dialog-title > .w-dialog-close', root=root))
            self._wait(lambda _: not self._visible(self.driver, '.w-edit-task-dialog'),
                       '班牛本次未提交表单未关闭')
            self._forget_form()
            return 'closed'
        except (PlatformOperationError, WebDriverException):
            return 'preserved'

    def _value(self, selector):
        return self._field(selector).get_attribute("value") or ""

    def _set_text(self, selector, value):
        element = self._field(selector)
        with self._form_action():
            element.clear()
            element.send_keys(value)
        self._wait(lambda _: self._value(selector) == value, "班牛字段未完整接收输入，未继续填写")
        return element

    def _radio_selected(self, column, title):
        label = self._field(f'#{column} .radio-box-txt[title="{title}"]')
        return bool(label.find_element(By.XPATH, "..").find_elements(By.CSS_SELECTOR, ".icon-chose2"))

    def _choose_radio(self, column, title):
        if not self._radio_selected(column, title):
            self._click_once(self._field(f'#{column} .radio-box-txt[title="{title}"]'))
            self._wait(lambda _: self._radio_selected(column, title), "班牛单选项未生效，未继续填写")

    def _table(self, selector):
        table = self._field(selector)
        # 横向滚动区之外的真实控件也需核对。只读取 DOM，不用 .text / is_displayed
        # 判断单元格内容；它们会丢掉被裁切的金额、数量列。只取主表直接 wrapper，
        # 避开 Element UI 为固定列生成的复制表格。
        snapshot = self.driver.execute_script("""
            const table = arguments[0];
            const popups = '.el-select-dropdown,.el-popover,.virtualPopover';
            const headers = [...table.querySelectorAll(':scope > .el-table__header-wrapper th')]
                .filter(th => !th.classList.contains('gutter'))
                .map(th => (th.querySelector('.cell') || th).textContent || '');
            const rows = [...table.querySelectorAll(':scope > .el-table__body-wrapper > table > tbody > tr')];
            let ambiguousCell = false;
            const values = rows.map(row => [...row.querySelectorAll(':scope > td')].map(cell => {
                const inputs = [...cell.querySelectorAll('input,textarea')].filter(input => !input.closest(popups));
                if (inputs.length > 1) ambiguousCell = true;
                if (inputs.length === 1) return inputs[0].value || '';
                const clone = cell.cloneNode(true);
                clone.querySelectorAll(popups).forEach(popup => popup.remove());
                return clone.textContent || '';
            }));
            return {headers, rows, values, ambiguousCell};
        """, table)
        if snapshot["ambiguousCell"]:
            raise PlatformOperationError("班牛商品单元格结构发生变化，未继续填写")
        return snapshot["rows"], table_records(snapshot["headers"], snapshot["values"])

    def _selected_gifts(self):
        return self._table("#col_1915 .children-box-body > .el-table")[1]

    def _close_search(self):
        panels = self._visible(self._dialog(), "#col_1915 .child-search-search-task")
        if panels:
            if len(panels) != 1:
                raise PlatformOperationError("班牛礼品搜索结果区域不唯一，未继续填写")
            self._click_once(self._one(":scope > .icon-guanbi", root=panels[0]))
            self._wait(lambda _: not self._visible(self._dialog(), "#col_1915 .child-search-search-task"),
                       "班牛礼品搜索结果未关闭，未继续填写")

    def _add_gift(self, gift_id, gift_name, expected_ids, expected_names):
        self._close_search()
        if self._value("#col_1915 .child-search-search-select input") != "商家编码":
            raise PlatformOperationError("班牛礼品搜索方式不是商家编码，未继续填写")
        self._set_text("#col_1915 .child-search-search-condition input", gift_id)
        self._click_once(self._one("#col_1915 .child-search-search-condition button", root=self._dialog(), text="筛选"))

        def first_matching(_):
            rows, values = self._table("#col_1915 .child-search-search-task-table > .el-table")
            if not rows or values[0]["商家编码"] != gift_id or not _gift_name_matches(gift_id, values[0]['货品名称'], gift_name):
                return False
            return rows[0]

        first = self._wait(first_matching, "班牛首条礼品搜索结果与所选编码及名称不符，未添加礼品")
        self._click_once(first)
        self._wait(lambda _: gifts_match(self._selected_gifts(), expected_ids, expected_names),
                   "班牛已选礼品、数量或金额与本次请求不一致，未继续填写")

    def _address_matches(self, address):
        parts = [self._value(f'#col_1921 input[placeholder="选择{level}"]') for level in ("省", "市", "区")]
        if not all(parts):
            return False
        streets = self._visible(self._dialog(), '#col_1921 input[placeholder="选择街道"]')
        if streets:
            if len(streets) != 1:
                return False
            parts.append(streets[0].get_attribute("value") or "")
        detail = self._value('#col_1921 textarea[placeholder="输入详细地址"]')
        return bool(detail) and _normalize("".join(parts) + detail) == _normalize(address)

    def _fill_address(self, recipient):
        self._set_text('#col_1921 input[placeholder="自动识别地址"]', recipient.address).send_keys(Keys.TAB)
        self._wait(lambda _: self._address_matches(recipient.address),
                   "班牛未完整识别新地址，省市区及详细地址不能与输入核对一致")
        self._set_text("#col_1924 textarea", recipient.name)
        self._set_text("#col_1925 textarea", recipient.phone)

    def _validate_prepared(self, request):
        if (self._value("#col_1891 textarea") != request.order_no or not self._value("#col_1892 textarea").strip()
                or self._value("#col_1913 input") != "赠品补发"
                or not self._radio_selected("col_1887", "补发")
                or not self._radio_selected("col_1890", "非原单改地址" if request.recipient else "非原单原地址")
                or not self._radio_selected("col_1926", "是")
                or not gifts_match(self._selected_gifts(), request.gift_ids, request.gift_names)):
            raise PlatformOperationError("班牛表单最终核对不一致，未提交")
        if request.recipient and (self._value("#col_1924 textarea") != request.recipient.name
                                  or self._value("#col_1925 textarea") != request.recipient.phone
                                  or not self._address_matches(request.recipient.address)):
            raise PlatformOperationError("班牛新收件信息最终核对不一致，未提交")

    def prepare(self, request, before):
        """填写并复读核对表单；本方法不点击提交，也不表示登记成功。"""
        if isinstance(request, RegistrationRequest) and request.recipient is not None:
            raise PlatformOperationError("本版本暂不支持改地址；未打开班牛表单")
        if (not isinstance(request, RegistrationRequest) or not isinstance(request.order_no, str) or not request.order_no
                or not isinstance(request.gift_ids, (tuple, list)) or not isinstance(request.gift_names, (tuple, list))
                or not request.gift_ids or len(request.gift_ids) != len(request.gift_names)
                or any(not isinstance(value, str) or not value.strip() for value in (*request.gift_ids, *request.gift_names))
                or len(set(request.gift_ids)) != len(request.gift_ids)):
            raise PlatformOperationError("登记请求缺少有效订单号或礼品信息，未打开班牛表单")
        validate_snapshot(before, request.order_no)
        if not before.found:
            raise PlatformOperationError("聚水潭尚未确认原订单存在，未打开班牛表单")
        if request.recipient is not None and (not isinstance(request.recipient, Recipient)
                or any(not isinstance(value, str) or not value.strip() for value in
                       (request.recipient.name, request.recipient.phone, request.recipient.address))):
            raise PlatformOperationError("新收件信息不完整，未打开班牛表单")
        self.driver.switch_to.default_content()
        if self._visible(self.driver, ".w-edit-task-dialog"):
            raise PlatformOperationError("班牛已有未关闭的任务表单，请先人工处理该表单")
        _open_registration_list(self.driver)
        if self.authenticate is not None:
            self.authenticate(target_url=BANNIU_REGISTRATION_URL)
        try:
            self._wait(lambda _: self.driver.execute_script(_REGISTRATION_LIST_READY) is True,
                       "班牛礼赠列表仍被详情或遮罩覆盖，未打开新建表单")
            self._fill_prepared(request)
        except (PlatformOperationError, WebDriverException):
            if self.authenticate is not None and self.authenticate():
                raise PlatformOperationError("班牛会话已恢复，但原登记表单已失效；未重新填写或提交") from None
            raise

    def _fill_prepared(self, request):
        self._click_once(self._one("button.edit-task-button-btn", text="新建"))
        root = self._dialog()
        if self._value("#col_1891 textarea").strip():
            raise PlatformOperationError("班牛新建表单已有订单号，未覆盖原内容")
        self._track_form(root, request)
        self._choose_radio("col_1887", "补发")
        self._choose_radio("col_1890", "非原单改地址" if request.recipient else "非原单原地址")
        self._send_keys(self._set_text("#col_1891 textarea", request.order_no), Keys.ENTER)

        def order_loaded(_):
            column = self._field("#col_1891")
            loaded = [element for element in column.find_elements(By.XPATH, ".//*[normalize-space(.)='已拉取最新数据']")
                      if element.is_displayed()]
            return bool(loaded) and bool(self._value("#col_1892 textarea").strip()) and self._value("#col_1891 textarea") == request.order_no

        self._wait(order_loaded, "班牛未确认拉取到该订单的最新数据，未继续填写")
        if self._selected_gifts():
            raise PlatformOperationError("班牛新建表单已有商品明细，未覆盖原内容")
        self._click_once(self._field("#col_1913 input"))
        self._click_once(self._one('.el-select-dropdown__item[title="赠品补发"]'))
        self._wait(lambda _: self._value("#col_1913 input") == "赠品补发", "班牛问题类型未生效，未继续填写")
        for index, (gift_id, gift_name) in enumerate(zip(request.gift_ids, request.gift_names), 1):
            self._add_gift(gift_id, gift_name, request.gift_ids[:index], request.gift_names[:index])
        self._close_search()
        if request.recipient:
            self._fill_address(request.recipient)
        self._choose_radio("col_1926", "是")
        self._validate_prepared(request)

    def submit(self, request, before):
        from .banniu_receipt import BanniuReceiptReader
        if request.recipient is not None:
            raise PlatformOperationError("本版本暂不支持改地址；未进行班牛登记")
        key = (request.order_no, tuple(sorted(request.gift_ids)))
        if key in self._attempted:
            raise PlatformOperationError("本次业务已调用登记，禁止再次提交")
        # 持久化防重由外层队列负责；这里再保护同一页面实例的重复调用。
        self._attempted.add(key)
        reader = BanniuReceiptReader(self)
        stage = '查询提交前任务基线'
        click_attempted = False
        try:
            previous_ids = {row['task_id'] for row in reader.query_tasks(request.order_no)}
            stage = '填写登记表单'
            self.prepare(request, before)
            stage = '核对登记表单'
            self._require_prepared_session()
            self._validate_prepared(request)
            button = self._one('button.el-button--primary', root=self._dialog(), text='提交')
            if not button.is_enabled():
                raise PlatformOperationError("班牛提交按钮不可用；未提交")
            self._require_prepared_session()
            submitted_at = datetime.now()
            stage = '点击提交或读取回执'
            click_attempted = True
            self._click_once(button)  # 唯一提交点；异常、超时也绝不重试。
            self._wait(lambda _: not self._visible(self.driver, '.w-edit-task-dialog'),
                       "班牛提交后表单未关闭，结果不确定，请人工核查；禁止再次提交")
            self._forget_form()
            return reader.wait_for_receipt(request, before, previous_ids, submitted_at)
        except (PlatformOperationError, WebDriverException) as exc:
            cleanup = self._discard_unsubmitted_form(request) if not click_attempted else 'none'
            # 允许恢复登录供人工核查；整个 submit 不重调，按钮也绝不重点击。
            if self.authenticate is not None and self.authenticate():
                error = PlatformOperationError if click_attempted else RegistrationNotSubmittedError
                raise error("班牛会话已恢复，本次登记仍需人工核查；禁止再次提交") from None
            if not click_attempted:
                detail = (str(exc) if isinstance(exc, PlatformOperationError)
                          else f'班牛{stage}失败（{type(exc).__name__}），未触发提交按钮')
                detail += {'closed': '；已关闭本次未提交表单', 'preserved': '；表单归属或内容无法安全确认，已保留，请人工处理', 'none': ''}[cleanup]
                raise RegistrationNotSubmittedError(detail) from None
            if isinstance(exc, WebDriverException):
                outcome = '结果可能不确定，请人工核查' if click_attempted else '未触发提交按钮'
                raise PlatformOperationError(f'班牛{stage}失败（{type(exc).__name__}），{outcome}；禁止再次提交') from None
            raise

    def _require_prepared_session(self):
        if self.authenticate is not None and self.authenticate():
            raise PlatformOperationError("班牛会话已恢复，但原登记表单已失效；未提交，请人工核查")
