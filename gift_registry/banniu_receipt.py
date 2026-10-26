"""读取班牛已保存任务的页面回执，不调用业务接口或执行登记。"""

import re
import time
from datetime import datetime

from selenium.common.exceptions import WebDriverException

from .workflow import PlatformOperationError, SubmissionReceipt


class TaskListNotReadyError(PlatformOperationError):
    """只读筛选未取得完整新结果，可有界刷新列表；不适用于登记动作。"""


_LIST = """
// gift-banniu-list: rendered table only, including offscreen virtual columns.
const visible=e=>e.getClientRects().length && getComputedStyle(e).visibility!=='hidden' && Number(getComputedStyle(e).opacity)>0;
const tables=[...document.querySelectorAll('.vxe-table')].filter(visible);
const pagers=[...document.querySelectorAll('.el-pagination__total')].filter(visible);
if(tables.length!==1 || [...document.querySelectorAll('.el-loading-mask,.task-details,.worksheet-main-details-back,.w-edit-task-dialog')].some(visible)) return null;
const table=tables[0];
// 真实空结果会隐藏分页，只在表格明确显示空态且没有任务行时认定为零条。
if(pagers.length===0 && table.classList.contains('is--empty') &&
   !table.querySelector('.vxe-table--body tr[rowid]') &&
   [...table.querySelectorAll('.w-vxeTable-empty')].some(e=>visible(e) &&
       e.textContent.trim()==='呀~暂时还没有内容呢~')) return {pager:'共 0 条',rows:[]};
if(pagers.length!==1) return null;
const columns={};
for(const th of table.querySelectorAll('.vxe-table--header th[colid]')) {
  const label=th.textContent.trim();
  if(['订单号','创建时间'].includes(label)) columns[label]=th.getAttribute('colid');
}
if(!columns['订单号'] || !columns['创建时间']) return null;
const rows=[];
for(const row of table.querySelectorAll('.vxe-table--body tr[rowid]')) {
  const value=label=>row.querySelector('td[colid="'+columns[label]+'"] .worksheetColumn');
  const order=value('订单号'), created=value('创建时间');
  if(!order || !created) continue; // fixed-column copies have neither.
  rows.push({task_id:row.getAttribute('rowid'),order_no:order.textContent.trim(),
             created_at:created.textContent.trim()});
}
return {pager:pagers[0].textContent.trim(),rows};
"""
_OBSERVE = """
// gift-banniu-observe
if(window.__giftBanniuObserver) window.__giftBanniuObserver.disconnect();
window.__giftBanniuChange={count:0,last:performance.now()};
window.__giftBanniuObserver=new MutationObserver(changes=>{
  if(changes.some(c=>c.target.nodeType===1 && c.target.closest('.vxe-table,.el-pagination') ||
                    c.target.parentElement?.closest('.vxe-table,.el-pagination'))) {
    window.__giftBanniuChange.count++; window.__giftBanniuChange.last=performance.now();
  }
});
window.__giftBanniuObserver.observe(document.body,{childList:true,characterData:true,subtree:true});
"""
_FRESH = """
// gift-banniu-fresh
const s=window.__giftBanniuChange;
return !!s && s.count>0 && performance.now()-s.last>=600;
"""
_STOP = """
if(window.__giftBanniuObserver) window.__giftBanniuObserver.disconnect();
delete window.__giftBanniuObserver; delete window.__giftBanniuChange;
"""
_DETAIL = """
// gift-banniu-detail: restrict to fields required for the receipt; no recipient data.
const roots=[...document.querySelectorAll('.task-details')].filter(e=>e.getClientRects().length);
if(roots.length!==1) return null;
const root=roots[0], fields={};
const wanted=['工单编号','创建时间','同步状态','售后类型','补发类型','订单号',
              '问题类型','是否自动确认售后单','售后单内部单号'];
for(const label of root.querySelectorAll('.task-details-component-title')) {
  if(!wanted.includes(label.title)) continue;
  const box=label.closest('.task-details-component-box');
  const node=box.querySelector('.show-value-content') || box.querySelector('.task-details-component-behavior');
  if(!node || fields[label.title]!==undefined) return null;
  const clone=node.cloneNode(true);
  clone.querySelectorAll('.el-popover,.show-value-right-btn-box').forEach(e=>e.remove());
  fields[label.title]=clone.textContent.trim();
}
const tables=[...root.querySelectorAll('#col_1915 .el-table')];
if(tables.length!==1) return null;
const table=tables[0];
const headers=[...table.querySelectorAll(':scope > .el-table__header-wrapper th')]
  .filter(e=>!e.classList.contains('gutter')).map(e=>(e.querySelector('.cell')||e).textContent.trim());
const rows=[...table.querySelectorAll(':scope > .el-table__body-wrapper > table > tbody > tr')]
  .map(row=>[...row.querySelectorAll(':scope > td')].map(e=>{
     const clone=e.cloneNode(true); clone.querySelectorAll('.el-popover').forEach(p=>p.remove());
     return clone.textContent.trim();
  }));
return {fields,headers,rows};
"""


def complete_task_rows(snapshot, order_no):
    """完整结果才可作班牛提交前基线；旧行/分页加载期间返回未就绪。"""
    if not snapshot:
        return None
    match = re.fullmatch(r"共\s*(\d+)\s*条", snapshot.get("pager", ""))
    if not match:
        return None
    unique = {}
    for row in snapshot["rows"]:
        identifier = row.get("task_id")
        if not isinstance(identifier, str) or not identifier.isdecimal():
            return None
        if row["order_no"] != order_no:
            return None
        if identifier in unique and unique[identifier] != row:
            raise PlatformOperationError("班牛任务列表读取中发生变化，未继续登记")
        unique[identifier] = row
    return list(unique.values()) if len(unique) == int(match[1]) else None


def receipt_from_detail(snapshot, request, before, task_id, *, not_before=None):
    from .banniu_page import gifts_match, table_records
    if not snapshot:
        return None
    fields = snapshot["fields"]
    expected = {"工单编号": task_id, "售后类型": "补发", "补发类型": "非原单原地址",
                "订单号": request.order_no, "问题类型": "赠品补发", "是否自动确认售后单": "是"}
    if any(fields.get(key) != value for key, value in expected.items()):
        raise PlatformOperationError("班牛已保存任务与本次登记不一致；禁止再次提交")
    if not gifts_match(table_records(snapshot["headers"], snapshot["rows"]), request.gift_ids, request.gift_names):
        raise PlatformOperationError("班牛已保存任务的礼品、数量或金额不一致；禁止再次提交")
    try:
        created = datetime.strptime(fields.get("创建时间", ""), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        raise PlatformOperationError("班牛任务创建时间无法核对；禁止再次提交") from None
    if not_before is not None and created < not_before.replace(microsecond=0):
        raise PlatformOperationError("班牛回执属于本次提交前的旧任务；禁止再次提交")
    state = fields.get("同步状态", "")
    if state == "失败":
        raise PlatformOperationError("班牛任务同步未成功，请人工核查；禁止再次提交")
    identifier = fields.get("售后单内部单号", "")
    # 异步同步尚未成功时继续有界只读核对；未知状态也绝不当作成功。
    if state != "成功" or not identifier:
        return None
    if not identifier.isdecimal() or identifier in {record.record_id for record in before.records}:
        raise PlatformOperationError("班牛回传的聚水潭单号无效或提交前已存在；禁止再次提交")
    return SubmissionReceipt(True, created_record_ids=(identifier,))


class BanniuReceiptReader:
    def __init__(self, page):
        self.page = page
        self.driver = page.driver
        # 只继承显式注入的回调，避免 Mock 的动态属性被误当成生产认证入口。
        self.authenticate = vars(page).get('authenticate')

    def _order_filter(self):
        """只调整本次查询条件，不点击保存，也不修改任何业务任务。"""
        self.page._one('.advancedFilter')
        # 用户可删除默认订单号条件、添加日期/店铺限制，不能沿用这类子集作基线。
        for _ in range(100):
            others = self.page._visible(self.driver, '.advancedFilter-top-item:not(#list_1891)')
            if not others:
                break
            count = len(others)
            self.page._one('.bnRule-del', root=others[0]).click()
            self.page._wait(lambda _: len(self.page._visible(
                self.driver, '.advancedFilter-top-item:not(#list_1891)')) < count,
                "班牛其他筛选条件未清除，无法取得完整订单任务基线")
        else:
            raise PlatformOperationError("班牛筛选条件过多，无法确认完整任务基线")
        if not self.page._visible(self.driver, '#list_1891 textarea'):
            self.page._one('.height-search-bottom-right-add', text='添加筛选条件').click()
            search = self.page._one('.height-search-bottom-right-input input')
            search.clear()
            search.send_keys('订单号')
            self.page._one('.height-search-bottom-right-drop[title="订单号"]').click()
        field = self.page._one('#list_1891 textarea')
        # 添加条件是多选菜单，选中后仍覆盖底部按钮；点击无副作用的字段标题收起。
        if self.page._visible(self.driver, '.height-search-bottom-right-input input'):
            self.page._one('#list_1891 .advancedFilter-title[title="订单号"]').click()
        self.page._wait(lambda _: not self.page._visible(self.driver, '.height-search-bottom-right-input input'),
                        "班牛添加筛选条件菜单未收起")
        operator = self.page._one('#list_1891 .bnRule-down')
        if operator.text.strip() != '等于':
            operator.click()
            self.page._one('.bnDropdown-drop-title[title="等于"]').click()
        self.page._wait(lambda _: self.page._one('#list_1891 .bnRule-down').text.strip() == '等于',
                        "班牛订单号筛选未设置为精确匹配")
        return field

    def query_tasks(self, order_no):
        refresh = False
        for attempt in range(2):
            try:
                return self._query_tasks_once(order_no, refresh=True) if refresh else self._query_tasks_once(order_no)
            except TaskListNotReadyError:
                if attempt:
                    raise
                refresh = True
            except (PlatformOperationError, WebDriverException):
                if attempt or self.authenticate is None or not self.authenticate():
                    raise

    def _query_tasks_once(self, order_no, *, refresh=False):
        from .banniu_page import BANNIU_REGISTRATION_URL, _REGISTRATION_LIST_READY, _open_registration_list
        self.driver.switch_to.default_content()
        if self.page._visible(self.driver, '.w-edit-task-dialog'):
            raise PlatformOperationError("班牛已有未关闭的任务表单，不能切换回执查询")
        if refresh and self.driver.current_url == BANNIU_REGISTRATION_URL:
            self.driver.refresh()  # 只读筛选未刷新时最多一次；上方已拒绝未关闭表单。
        else:
            _open_registration_list(self.driver)
        if self.authenticate is not None:
            self.authenticate(target_url=BANNIU_REGISTRATION_URL)
        self.page._one('div.title.el-dropdown-selfdefine', text='聚水潭赠品补发-融合')
        # 页面标题先于列表及保存的筛选展开状态出现；等列表加载完成后再决定是否展开。
        self.page._wait(lambda _: self.driver.execute_script(_REGISTRATION_LIST_READY) is True
                        and self.driver.execute_script(_LIST) is not None
                        and not self.page._visible(self.driver, '.el-loading-mask'),
                        "班牛任务列表尚未加载完成或仍被详情遮罩覆盖；未进行筛选或登记")
        if not self.page._visible(self.driver, '.advancedFilter'):
            toggle = self.page._one('button.worksheet-main-content-bar-item-set', text='筛选')
            if 'open-style' not in (toggle.get_attribute('class') or '').split():
                toggle.click()
        field = self._order_filter()
        field.clear()
        field.send_keys(order_no)
        if field.get_attribute('value') != order_no:
            raise PlatformOperationError("班牛回执查询订单号输入不完整")
        button = self.page._one('button.el-button--primary.el-button--mini', text='筛选')
        self.driver.execute_script(_OBSERVE)
        try:
            button.click()  # 只读筛选，每轮一次。
            def ready(_):
                if not self.driver.execute_script(_FRESH):
                    return False
                rows = complete_task_rows(self.driver.execute_script(_LIST), order_no)
                return {"rows": rows} if rows is not None else False
            try:
                return self.page._wait(ready, "班牛回执列表未完整刷新或结果超过一页；未重复提交")["rows"]
            except PlatformOperationError as exc:
                # 解析冲突等确定异常不得转成可重查的刷新超时。
                if str(exc) != "班牛回执列表未完整刷新或结果超过一页；未重复提交":
                    raise
                raise TaskListNotReadyError(str(exc)) from None
        finally:
            self.driver.execute_script(_STOP)

    def open_task(self, order_no, task_id):
        if not isinstance(task_id, str) or not task_id.isdecimal():
            raise PlatformOperationError("班牛任务编号无效")
        element = self.page._one(f'.vxe-table--body tr[rowid="{task_id}"] .worksheetColumn-hover', text=order_no)
        self.page._click_once(element)
        self.page._one('.task-details')

    def read_detail(self, request, before, task_id, *, not_before=None):
        snapshot = self.page._wait(lambda _: self.driver.execute_script(_DETAIL), "班牛任务详情尚未完整显示")
        return receipt_from_detail(snapshot, request, before, task_id, not_before=not_before)

    def wait_for_receipt(self, request, before, previous_ids, submitted_at, *, queries=8, interval=3):
        if not 1 <= queries <= 8 or not 0 <= interval <= 5:
            raise ValueError("班牛回执只读查询次数或间隔超出上限")
        candidate_id = None
        for attempt in range(queries):
            try:
                rows = self.query_tasks(request.order_no)
                added = [row for row in rows if row['task_id'] not in previous_ids]
                if len(added) > 1:
                    raise PlatformOperationError("班牛出现多个新增任务，无法唯一确认本次回执；禁止再次提交")
                if added:
                    task_id = added[0]['task_id']
                    if candidate_id is not None and candidate_id != task_id:
                        raise PlatformOperationError("班牛待核验的新增任务发生变化；禁止再次提交")
                    candidate_id = task_id
                    self.open_task(request.order_no, task_id)
                    receipt = self.read_detail(request, before, task_id, not_before=submitted_at)
                    if receipt is not None:
                        return receipt
            except (PlatformOperationError, WebDriverException):
                # 详情读取被认证打断时，可继续下一次只读查询；基线和提交时间不变。
                if (attempt + 1 >= queries or self.authenticate is None
                        or not self.authenticate()):
                    raise
            if attempt + 1 < queries:
                time.sleep(interval)
        raise PlatformOperationError("班牛提交后未取得明确回执或同步仍未成功，请人工核查；禁止再次提交")
