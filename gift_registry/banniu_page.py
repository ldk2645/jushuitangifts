"""班牛礼赠表单操作：同一订单的全部礼品加入同一任务，仅点击一次提交。"""

import secrets
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation

from selenium.common.exceptions import (ElementNotInteractableException, NoSuchElementException,
                                        StaleElementReferenceException, TimeoutException, WebDriverException)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from .workflow import (ManualInterventionRequired, PageRecoverableError, PlatformOperationError, Recipient,
                       RegistrationNotSubmittedError, RegistrationRequest, request_quantities, validate_snapshot)
from .edition import BANNIU_URL, BANNIU_TITLE


BANNIU_REGISTRATION_URL = BANNIU_URL
GIFT_COLUMNS = ("退换补类型", "商家编码", "货品名称", "退换补商品金额", "数量")

# 已在真实班牛任务和聚水潭同编码商品中核对的名称；只适用于此 SKU，不做模糊放行。
_OBSERVED_GIFT_NAMES = {
    'XS24AMA350021GK001': ('椰球夹耳式耳机', 'OWS31蓝牙耳机'),
    'XS24A7A35002PMJ002': ('S11 Pro Max 智能手表（白色）', 'S11 Pro Max 智能手表'),
    'N0800202036T000801': ('赠品《联想》蓝牙音箱', '《赠品》联想蓝牙音响'),
    'XS24ANA35002168001': ('K歌神器', '话筒音响一体无线麦克'),
    'N01B03070C1VZS7B01': ('赠品保护套', '小米平板7/7 Pro 赠品保护套'),
    'N00A0A0I1I0DXM5D01': ('胶囊雨伞（带胶囊盒）米白', '胶囊雨伞（带胶囊盒）'),
    'XS24A7A23002J0Y001': ('智能手表', 'S20智能手表'),
}

_REGISTRATION_LIST_READY = """
// gift-registration-list-ready: only reuse a visible worksheet, never its results.
const visible=e=>e.getClientRects().length && getComputedStyle(e).visibility!=='hidden' && Number(getComputedStyle(e).opacity)>0;
const shown=selector=>[...document.querySelectorAll(selector)].filter(visible);
const buttons=shown('button.edit-task-button-btn').filter(e=>e.textContent.trim()==='新建');
if(shown('div.title.el-dropdown-selfdefine').filter(e=>e.textContent.trim()==='聚水谭赠品补发—荣耀').length!==1 ||
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
    from .browser import stabilize_banniu_viewport
    stabilize_banniu_viewport(driver)
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


def gifts_match(records, gift_ids, gift_names, gift_quantities=None):
    if (len(records) != len(gift_ids) or len(set(gift_ids)) != len(gift_ids)
            or len(gift_names) != len(gift_ids)):
        return False
    if gift_quantities is None:
        quantities = (1,) * len(gift_ids)
    else:
        if len(gift_quantities) != len(gift_ids):
            return False
        try:
            quantities = tuple(int(value) for value in gift_quantities)
        except (TypeError, ValueError):
            return False
        if any(value < 1 for value in quantities):
            return False
    expected = dict(zip(gift_ids, gift_names))
    quantity = dict(zip(gift_ids, quantities))
    if {row["商家编码"] for row in records} != set(expected):
        return False
    for row in records:
        code = row["商家编码"]
        if row["退换补类型"] != "补发" or not _gift_name_matches(code, row['货品名称'], expected[code]):
            return False
        try:
            if Decimal(row["数量"]) != Decimal(quantity[code]) or Decimal(row["退换补商品金额"]) != Decimal(0):
                return False
        except InvalidOperation:
            return False
    return True


_GIFT_QUANTITY_INPUT = """
// gift-banniu-quantity-target: locate the editable quantity input of one selected gift row.
const f=arguments[0], code=arguments[1];
const table=f.querySelector('#col_27748 .children-box-body > .el-table');
if(!table) return null;
const norm=t=>(t||'').replace(/\\s+/g,'').replace(/^[*＊]/,'');
const headers=[...table.querySelectorAll(':scope > .el-table__header-wrapper th')]
  .filter(th=>!th.classList.contains('gutter'))
  .map(th=>(th.querySelector('.cell')||th).textContent||'');
const index=headers.map(norm).indexOf('数量'), codeIndex=headers.map(norm).indexOf('商家编码');
if(index<0 || codeIndex<0) return null;
for(const row of table.querySelectorAll(':scope > .el-table__body-wrapper > table > tbody > tr')){
  const cells=[...row.querySelectorAll(':scope > td')];
  if(index>=cells.length || codeIndex>=cells.length) continue;
  const codeInput=cells[codeIndex].querySelector('input');
  if(!codeInput || codeInput.value!==code) continue;
  const input=cells[index].querySelector('input');
  return input || cells[index];
}
return null;
"""


class BanniuPage:
    """所有动作均通过 Selenium；等待条件只读取，不重试点击或提交。"""

    def __init__(self, driver, *, timeout=20, order_load_timeout=60, authenticate=None):
        self.driver = driver
        self.timeout = timeout
        self.order_load_timeout = order_load_timeout
        self._attempted = set()
        self._owned_form = None
        self._form_progress = None
        self._submitted_form_token = None
        self._refresh_attempted_token = None
        self.authenticate = authenticate

    def _wait(self, predicate, detail, *, timeout=None):
        try:
            return WebDriverWait(self.driver, self.timeout if timeout is None else timeout, poll_frequency=.2,
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

        detail = ("班牛新建按钮未出现：已固定业务视口，请检查工作表权限、登录状态或页面版本"
                  if selector == "button.edit-task-button-btn" and text == "新建"
                  else "班牛页面控件未出现，请检查登录状态或页面版本")
        return self._wait(locate, detail)

    def _dialog(self):
        return self._one(".w-edit-task-dialog")

    def _click_once(self, element, *, resolve=None, before_click=None):
        """等待真实点击位置可用，然后只点击一次；不绕过遮挡、不重试点击。

        before_click 在所有可点击性等待完成、即将发起原生点击前调用一次：提交边界
        必须在该回调内部记录，回调之前的失败可安全清理未提交表单。
        """
        positioned = None
        if resolve is None:
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center',inline:'nearest'})", element)

        def ready(_):
            nonlocal element, positioned
            if resolve is not None:
                current = resolve()
                if not current:
                    return False
                element = current
                if positioned != element:
                    self.driver.execute_script("arguments[0].scrollIntoView({block:'center',inline:'nearest'})", element)
                    positioned = element
                    return False  # 滚动会重建虚拟行；下一轮重新按身份查找，尚未点击。
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
        if before_click is not None:
            before_click()  # 原生点击调用边界：此后再无“未点击”的确定性证明。
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
            raise ManualInterventionRequired('班牛新建表单归属无法确认，未继续填写；请主机检查并人工处理该表单')
        self._owned_form = (root, request, token)
        # 记录本次自动化实际写入的礼品及数量，用于中断后安全区分“工具中间表单”和“人工表单”。
        self._form_progress = {'token': token, 'gifts': {}}
        self._submitted_form_token = None
        self._refresh_attempted_token = None

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
            raise ManualInterventionRequired('班牛表单已被人工操作或归属变化，未继续自动填写或点击；请人工处理该表单')
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
        self._form_progress = None
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

    def _intermediate_gifts_safe(self, selected, request):
        """允许关闭本次自动化写入、且仍是请求子集的中间表单。

        数量为 1 或已写入目标数量都算安全中间态；未知编码、重复行、非补发、非零金额、
        名称不符或人工改动痕迹都视为无法安全确认，保留表单交人工处理。
        """
        progress = self._form_progress
        if not isinstance(progress, dict):
            # 旧页面对象/测试夹具没有进度记录：退化为完整请求前缀核对（原安全规则）。
            count = len(selected)
            return gifts_match(selected, request.gift_ids[:count], request.gift_names[:count],
                               request_quantities(request)[:count])
        if self._owned_form is not None and progress.get('token') != self._owned_form[2]:
            return False
        added = progress.get('gifts') or {}
        expected_quantities = dict(zip(request.gift_ids, request_quantities(request)))
        expected_names = dict(zip(request.gift_ids, request.gift_names))
        seen = set()
        for row in selected:
            code = row.get('商家编码', '')
            if code not in expected_quantities or code in seen or code not in added:
                return False
            seen.add(code)
            if row.get('退换补类型') != '补发':
                return False
            if not _gift_name_matches(code, row.get('货品名称', ''), expected_names[code]):
                return False
            try:
                quantity = Decimal(row.get('数量', ''))
                amount = Decimal(row.get('退换补商品金额', ''))
            except (InvalidOperation, TypeError):
                return False
            if amount != Decimal(0):
                return False
            if quantity not in {Decimal(1), Decimal(expected_quantities[code])}:
                return False
        return True

    def _discard_unsubmitted_form(self, request):
        """只关闭本次创建、未触发提交、内容仍可确认为本工具中间态的表单。"""
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
            if self._value('#col_27724 textarea') not in ('', request.order_no):
                return 'preserved'
            selected = (self._selected_gifts() if self._visible(root, '#col_27748 .children-box-body > .el-table') else [])
            if not self._intermediate_gifts_safe(selected, request):
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

    def _refresh_submitted_form(self):
        """下一次只读查询前恢复本工具提交后遗留的表单，不重放上一单。"""
        owned = self._owned_form
        if owned is None:
            return False
        root, request, token = owned
        if self._submitted_form_token != token:
            return False
        forms = self._visible(self.driver, '.w-edit-task-dialog')
        if (len(forms) != 1 or forms[0] != root or self.driver.current_url != BANNIU_REGISTRATION_URL
                or self._value('#col_27724 textarea') != request.order_no
                or not gifts_match(self._selected_gifts(), request.gift_ids, request.gift_names,
                                   request_quantities(request))):
            return False
        if self.driver.execute_script("""
            // gift-submitted-form-can-refresh: only our submitted, untouched form.
            const root=arguments[0],state=root.__giftRegistrationOwner;
            return root.isConnected && state?.token===arguments[1] &&
                   state.submitSeen===true && state.manualEdited===false && state.automation===0;
        """, root, token) is not True:
            return False
        if self._refresh_attempted_token == token:
            raise PlatformOperationError('班牛遗留表单已刷新过一次仍未恢复，请人工核查；未重新提交上一单')
        # 刷新超时也消耗本表单的唯一恢复机会；不清除 _attempted 或任何持久化记录。
        self._refresh_attempted_token = token
        self.driver.refresh()
        self._wait(lambda _: not self._visible(self.driver, '.w-edit-task-dialog'),
                   '班牛刷新后仍有任务表单，已停止后续登记；未重新提交上一单')
        self._forget_form()
        if self.authenticate is not None:
            self.authenticate(target_url=BANNIU_REGISTRATION_URL)
        self._wait(lambda _: self.driver.execute_script(_REGISTRATION_LIST_READY) is True,
                   '班牛刷新后列表仍不可用，已停止后续登记；未重新提交上一单')
        return True

    def _value(self, selector):
        return self._field(selector).get_attribute("value") or ""

    def _set_text(self, selector, value):
        element = self._field(selector)
        with self._form_action():
            element.clear()
            # 清空会触发 Vue 重建输入框；按原字段重新定位，绝不重复输入动作。
            element = self._field(selector)
            if element.get_attribute("value"):
                raise PlatformOperationError("班牛字段清空后仍有内容，未继续填写")
            element.send_keys(value)
        self._wait(lambda _: self._value(selector) == value, "班牛字段未完整接收输入，未继续填写")
        return self._field(selector)

    def _radio_selected(self, column, title):
        label = self._field(f'#{column} .radio-box-txt[title="{title}"]')
        return bool(label.find_element(By.XPATH, "..").find_elements(By.CSS_SELECTOR, ".icon-chose2"))

    def _choose_radio(self, column, title):
        if not self._radio_selected(column, title):
            locate = lambda: self._field(f'#{column} .radio-box-txt[title="{title}"]')
            self._click_once(locate(), resolve=locate)
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
        return self._table("#col_27748 .children-box-body > .el-table")[1]

    def _close_search(self):
        panels = self._visible(self._dialog(), "#col_27748 .child-search-search-task")
        if panels:
            if len(panels) != 1:
                raise PlatformOperationError("班牛礼品搜索结果区域不唯一，未继续填写")
            self._click_once(self._one(":scope > .icon-guanbi", root=panels[0]))
            self._wait(lambda _: not self._visible(self._dialog(), "#col_27748 .child-search-search-task"),
                       "班牛礼品搜索结果未关闭，未继续填写")

    def _add_gift(self, gift_id, gift_name, quantity, expected_ids, expected_names, expected_quantities):
        self._close_search()
        if self._value("#col_27748 .child-search-search-select input") != "商家编码":
            raise PlatformOperationError("班牛礼品搜索方式不是商家编码，未继续填写")
        self._set_text("#col_27748 .child-search-search-condition input", gift_id)
        self._click_once(self._one("#col_27748 .child-search-search-condition button", root=self._dialog(), text="筛选"))

        def first_matching(_):
            # 同一商家编码可能同时存在新旧名称的多条商品记录；只精确匹配编码和名称，
            # 不要求它必须是页面首条，也不能退化为按名称相似或其它编码放行。
            rows, values = self._table("#col_27748 .child-search-search-task-table > .el-table")
            for row, value in zip(rows, values):
                if value["商家编码"] == gift_id and _gift_name_matches(gift_id, value['货品名称'], gift_name):
                    return row
            return False

        first = self._wait(first_matching, "班牛礼品搜索结果中未找到与所选编码及名称一致的商品，未添加礼品")
        self._click_once(first, resolve=lambda: first_matching(self.driver))
        self._progress_record(gift_id, 1)  # 先记录已加入（默认 1 件），数量中断时仍可安全识别中间态。
        if quantity != 1:
            self._set_gift_quantity(gift_id, quantity)
            self._progress_record(gift_id, quantity)
        self._wait(lambda _: gifts_match(self._selected_gifts(), expected_ids, expected_names, expected_quantities),
                   "班牛已选礼品、数量或金额与本次请求不一致，未继续填写")

    def _progress_record(self, gift_id, quantity):
        progress = self._form_progress
        if progress is None or self._owned_form is None or progress.get('token') != self._owned_form[2]:
            return
        progress['gifts'][gift_id] = quantity

    def _quantity_applied(self, gift_id, quantity):
        for row in self._selected_gifts():
            if row.get('商家编码') != gift_id:
                continue
            try:
                return Decimal(row.get('数量', '')) == Decimal(quantity)
            except (InvalidOperation, TypeError):
                return False
        return False

    def _quantity_input(self, gift_id):
        return self.driver.execute_script(_GIFT_QUANTITY_INPUT, self._dialog(), gift_id)

    def _set_gift_quantity(self, gift_id, quantity):
        """在已加入的礼品行把“数量”改为指定件数。

        Vue 全表重渲染会让旧元素失效：每次原生动作前重新按编码定位，最多 3 次有界尝试；
        每次先用 Ctrl+A 覆盖再输入，成功后只读核对；不做无界重复点击，不绕过遮挡。
        """
        last_error = None
        for _ in range(3):
            if self._quantity_applied(gift_id, quantity):
                return
            target = self._quantity_input(gift_id)
            if target is None:
                last_error = '班牛礼品数量控件未找到'
                time.sleep(0.2)
                continue
            try:
                with self._form_action():
                    target.click()
                    target.send_keys(Keys.CONTROL, 'a')
                    target.send_keys(str(quantity))
                    target.send_keys(Keys.TAB)  # 失焦提交输入值
            except (StaleElementReferenceException, NoSuchElementException, ElementNotInteractableException) as exc:
                last_error = type(exc).__name__
                time.sleep(0.2)
                continue
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    if self._quantity_applied(gift_id, quantity):
                        return
                except WebDriverException:
                    pass
                time.sleep(0.1)
            last_error = '班牛礼品数量未生效'
            time.sleep(0.2)
        raise PageRecoverableError(f'班牛礼品数量未生效（{last_error or "未知原因"}），未继续填写；请人工核查该数量控件')

    def _address_matches(self, address):
        parts = [self._value(f'#col_27754 input[placeholder="选择{level}"]') for level in ("省", "市", "区")]
        if not all(parts):
            return False
        streets = self._visible(self._dialog(), '#col_27754 input[placeholder="选择街道"]')
        if streets:
            if len(streets) != 1:
                return False
            parts.append(streets[0].get_attribute("value") or "")
        detail = self._value('#col_27754 textarea[placeholder="输入详细地址"]')
        return bool(detail) and _normalize("".join(parts) + detail) == _normalize(address)

    def _fill_address(self, recipient):
        self._set_text('#col_27754 input[placeholder="自动识别地址"]', recipient.address).send_keys(Keys.TAB)
        self._wait(lambda _: self._address_matches(recipient.address),
                   "班牛未完整识别新地址，省市区及详细地址不能与输入核对一致")
        self._set_text("#col_27757 textarea", recipient.name)
        self._set_text("#col_27758 textarea", recipient.phone)

    def _validate_honor_context(self, *, require_platform=False):
        if (not self._radio_selected('col_27771', '能良移动')
                or self._value('#col_27777 input').strip() != '荣耀'):
            raise PlatformOperationError('班牛公司或品牌不是能良移动／荣耀，未继续登记')
        if require_platform and not self._value('#col_27776 input').strip():
            raise PlatformOperationError('班牛订单平台尚未回填，未提交，请核对订单拉取结果')

    def _validate_prepared(self, request):
        self._validate_honor_context(require_platform=True)
        if (self._value("#col_27724 textarea") != request.order_no or not self._value("#col_27725 textarea").strip()
                or self._value("#col_27746 input") != "赠品补发"
                or not self._radio_selected("col_27720", "补发")
                or not self._radio_selected("col_27723", "非原单改地址" if request.recipient else "非原单原地址")
                or not self._radio_selected("col_27759", "是")
                or not gifts_match(self._selected_gifts(), request.gift_ids, request.gift_names,
                                   request_quantities(request))):
            raise PlatformOperationError("班牛表单最终核对不一致，未提交")
        if request.recipient and (self._value("#col_27757 textarea") != request.recipient.name
                                  or self._value("#col_27758 textarea") != request.recipient.phone
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
                or len(set(request.gift_ids)) != len(request.gift_ids)
                or not isinstance(request.gift_quantities, (tuple, list))
                or (request.gift_quantities and len(request.gift_quantities) != len(request.gift_ids))
                or any(type(value) is not int or not 1 <= value <= 99 for value in request.gift_quantities)):
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
            raise ManualInterventionRequired("班牛已有未关闭的任务表单，请先人工处理该表单；未打开新表单")
        _open_registration_list(self.driver)
        if self.authenticate is not None:
            self.authenticate(target_url=BANNIU_REGISTRATION_URL)
        try:
            self._wait(lambda _: self.driver.execute_script(_REGISTRATION_LIST_READY) is True,
                       "班牛礼赠列表仍被详情或遮罩覆盖，未打开新建表单")
            self._fill_prepared(request)
        except (PlatformOperationError, WebDriverException):
            try:
                recovered = self.authenticate is not None and self.authenticate()
            except Exception:
                recovered = False  # 次生认证故障不能覆盖原始填写异常。
            if recovered:
                raise PlatformOperationError("班牛会话已恢复，但原登记表单已失效；未重新填写或提交") from None
            raise

    def _fill_prepared(self, request):
        self._prepare_step = "打开新建表单"
        locate = lambda: self._one("button.edit-task-button-btn", text="新建")
        self._click_once(locate(), resolve=locate)
        root = self._dialog()
        if self._value("#col_27724 textarea").strip():
            raise PlatformOperationError("班牛新建表单已有订单号，未覆盖原内容")
        self._track_form(root, request)
        self._validate_honor_context()
        self._prepare_step = "选择补发类型"
        self._choose_radio("col_27720", "补发")
        self._choose_radio("col_27723", "非原单改地址" if request.recipient else "非原单原地址")
        self._prepare_step = "输入订单并拉单"
        self._send_keys(self._set_text("#col_27724 textarea", request.order_no), Keys.ENTER)

        def order_loaded(_):
            column = self._field("#col_27724")
            loaded = [element for element in column.find_elements(By.XPATH, ".//*[normalize-space(.)='已拉取最新数据']")
                      if element.is_displayed()]
            return bool(loaded) and bool(self._value("#col_27725 textarea").strip()) and self._value("#col_27724 textarea") == request.order_no

        self._wait(order_loaded, "班牛未确认拉取到该订单的最新数据，未继续填写",
                   timeout=self.order_load_timeout)
        if self._selected_gifts():
            raise PlatformOperationError("班牛新建表单已有商品明细，未覆盖原内容")
        self._prepare_step = "选择问题类型"
        locate = lambda: self._field("#col_27746 input")
        self._click_once(locate(), resolve=locate)
        self._click_once(self._one('.el-select-dropdown__item[title="赠品补发"]'))
        self._wait(lambda _: self._value("#col_27746 input") == "赠品补发", "班牛问题类型未生效，未继续填写")
        quantities = request_quantities(request)
        for index, (gift_id, gift_name, quantity) in enumerate(zip(request.gift_ids, request.gift_names, quantities), 1):
            self._prepare_step = "添加礼品"
            self._add_gift(gift_id, gift_name, quantity,
                           request.gift_ids[:index], request.gift_names[:index], quantities[:index])
        self._close_search()
        if request.recipient:
            self._fill_address(request.recipient)
        self._choose_radio("col_27759", "是")
        self._validate_prepared(request)

    def submit(self, request, before, *, require_empty_baseline=False, require_distinct_gifts=False, on_submitted=None):
        from .banniu_receipt import BanniuReceiptReader
        if request.recipient is not None:
            raise PlatformOperationError("本版本暂不支持改地址；未进行班牛登记")
        key = (request.order_no, tuple(sorted(zip(request.gift_ids, request_quantities(request)))))
        if key in self._attempted:
            raise PlatformOperationError("本次业务已调用登记，禁止再次提交")
        # 持久化防重由外层队列负责；这里再保护同一页面实例的重复调用。
        self._attempted.add(key)
        # 班牛“创建时间”只有秒级，且与本机时钟可能存在秒级偏差；本单登记表单与任务
        # 都由本次调用创建，因此回执不能早于本次尝试开始时间，不能用点击提交的瞬时时间。
        attempt_started_at = datetime.now()
        reader = BanniuReceiptReader(self)
        stage = '查询提交前任务基线'
        click_attempted = False
        try:
            rows = reader.query_tasks(request.order_no)
            previous_ids = {row['task_id'] for row in rows}
            if require_empty_baseline and previous_ids:
                raise RegistrationNotSubmittedError("重登核查后班牛出现了该订单任务，停止重登；未触发提交按钮")
            if require_distinct_gifts and rows and reader.check_distinct_gifts(request, rows) is not True:
                raise RegistrationNotSubmittedError("重登核查后班牛旧任务礼品无法核对，停止重登；未触发提交按钮")
            stage = '填写登记表单'
            self._prepare_step = '等待表单'
            self.prepare(request, before)
            stage = '核对登记表单'
            self._require_prepared_session()
            self._validate_prepared(request)
            button = self._one('button.el-button--primary', root=self._dialog(), text='提交')
            if not button.is_enabled():
                raise PlatformOperationError("班牛提交按钮不可用；未提交")
            self._require_prepared_session()
            stage = '提交前可点击性检查'

            def begin_submit():
                nonlocal click_attempted, stage
                if self._owned_form is not None:
                    self._submitted_form_token = self._owned_form[2]
                click_attempted = True
                stage = '点击提交或读取回执'

            # 边界在真正发起原生 click 之前记录：可点击性等待失败仍可安全清理未提交表单；
            # 原生 click 一旦发起，即使抛异常也按结果不确定处理，绝不重试。
            self._click_once(button, before_click=begin_submit)
            if on_submitted is not None:
                on_submitted()
            self._wait(lambda _: not self._visible(self.driver, '.w-edit-task-dialog'),
                       "班牛提交后表单未关闭，结果不确定，请人工核查；禁止再次提交")
            self._forget_form()
            return reader.wait_for_receipt(request, before, previous_ids, attempt_started_at)
        except (PlatformOperationError, WebDriverException) as exc:
            cleanup = self._discard_unsubmitted_form(request) if not click_attempted else 'none'
            # 次生登录检查异常只作补充，不能丢失主异常及提交边界。
            recovery_note = ""
            try:
                if self.authenticate is not None and self.authenticate():
                    recovery_note = "；班牛会话已恢复，本次登记仍停止，未重新填写或提交"
            except Exception:
                recovery_note = "；登录状态检查亦未完成，请在主机检查浏览器"
            reason = str(exc) if isinstance(exc, PlatformOperationError) else type(exc).__name__
            if not click_attempted:
                step = getattr(self, "_prepare_step", "控件核对") if stage == '填写登记表单' else stage
                detail = f"班牛{stage}失败（{reason}；{step}），未触发提交按钮" + recovery_note
                detail += {'closed': '；已关闭本次未提交表单', 'preserved': '；表单归属或内容无法安全确认，已保留，请人工处理', 'none': ''}[cleanup]
                # 保留 RegistrationNotSubmittedError 的确定性边界；共享环境故障通过结构化
                # pause_class 标记，由队列暂停后续任务，不靠解析 detail 文本。
                error = RegistrationNotSubmittedError(detail)
                if cleanup == 'preserved' or isinstance(exc, (ManualInterventionRequired, WebDriverException)):
                    error.pause_class = 'manual'
                    error.recovery_condition = '请在主机检查浏览器、登录或遗留表单，人工处理后恢复暂停队列'
                elif isinstance(exc, PageRecoverableError) or getattr(exc, 'pause_class', None) == 'recoverable':
                    error.pause_class = 'recoverable'
                    error.recovery_condition = '可在订单间安全关闭本工具页面并重开浏览器，随后继续下一独立订单'
                error.click_attempted = False
                error.stage = stage
                raise error from None
            error = ManualInterventionRequired(
                f"班牛{stage}失败（{reason}），结果可能不确定，请人工核查" + recovery_note + "；禁止再次提交")
            error.click_attempted = True
            error.stage = stage
            raise error from None

    def _require_prepared_session(self):
        if self.authenticate is not None and self.authenticate():
            raise ManualInterventionRequired("班牛会话已恢复，但原登记表单已失效；未提交，请人工核查")
