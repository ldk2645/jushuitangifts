"""聚水潭订单页的只读 Selenium 适配器；选择器来自实际页面 DOM 核对。"""

import re
import time

from selenium.common.exceptions import (NoSuchElementException, StaleElementReferenceException,
                                        TimeoutException, WebDriverException)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .workflow import GiftRecord, OrderSnapshot, PlatformOperationError


ORDER_URL = "https://dopre.jushuitan.com/app/order/order/list.aspx"
_PAGER = re.compile(r"总共\s*(\d+)\s*页\s*(\d+)\s*条记录")
_MASK = re.compile(r"[*＊●•]|\.{3}|…")


def page_count(text):
    match = _PAGER.search(text or "")
    if match is None:
        raise PlatformOperationError("聚水潭分页总数无法确认，已停止读取")
    pages, total = map(int, match.groups())
    if pages > 1:
        raise PlatformOperationError("聚水潭结果超过一页，尚未核对翻页规则，请人工核查")
    if (total > 0 and pages != 1) or total > 2000:
        raise PlatformOperationError("聚水潭分页总数异常，无法完整读取")
    return total


def parse_order_rows(order_no, rows, pager_text):
    """将已完整采集的 UI 行转换为业务快照，不猜测原订单行或关联凭证。"""
    if not isinstance(order_no, str) or not order_no.strip():
        raise PlatformOperationError("聚水潭查询订单号必须是非空文本")
    total = page_count(pager_text)
    if len(rows) != total:
        raise PlatformOperationError("聚水潭实际读取行数与分页总数不一致")
    if not total:
        return OrderSnapshot(order_no, False)
    identifiers, records = set(), []
    for row in rows:
        internal_id = row.get("internal_id")
        if (not isinstance(internal_id, str) or not internal_id.strip()
                or internal_id in identifiers):
            raise PlatformOperationError("聚水潭内部订单号缺失或重复，无法核对新增记录")
        identifiers.add(internal_id)
        if row.get("order_no") != order_no:
            raise PlatformOperationError("聚水潭结果中包含不匹配的线上订单号")
        gift_ids = []
        quantities = []
        for item in row.get("items", []):
            title = item.get("title")
            lines = title.splitlines() if isinstance(title, str) else []
            # 已观察到的工具提示依次是款号、商品编码、名称、规格和价格。
            if len(lines) < 3 or not lines[1].strip() or not lines[2].strip():
                raise PlatformOperationError("聚水潭商品工具提示缺少可核对的商品编码")
            sku = lines[1].strip()
            quantity_text = item.get("quantity", "")
            if not isinstance(quantity_text, str) or not re.fullmatch(r"[0-9]+", quantity_text.strip()):
                raise PlatformOperationError("聚水潭商品数量无法确认")
            quantity = int(quantity_text)
            if quantity <= 0 or sku in gift_ids:
                raise PlatformOperationError("聚水潭商品数量无效或商品编码重复")
            gift_ids.append(sku)
            quantities.append(quantity)
        if not gift_ids:
            raise PlatformOperationError("聚水潭订单商品未完整显示，无法核对礼品")
        total_quantity = row.get("total_quantity", "")
        if (not isinstance(total_quantity, str) or not re.fullmatch(r"[0-9]+", total_quantity.strip())
                or int(total_quantity) != sum(quantities)):
            raise PlatformOperationError("聚水潭商品明细数量与商品总数不一致")
        # 本版本仅支持原地址补发；按用户要求不采集或核对收件资料。
        records.append(GiftRecord(internal_id, tuple(gift_ids),
                                  gift_quantities=tuple(quantities)))
    return OrderSnapshot(order_no, True, tuple(records))


_INSTALL_OBSERVER = """
// gift-query-observer-install: only observes rendered DOM, not application state.
if (window.__giftQueryObserver) window.__giftQueryObserver.disconnect();
window.__giftQueryChanges = {count: 0, last: performance.now()};
window.__giftQueryObserver = new MutationObserver(changes => {
  if (changes.some(c => c.type === 'childList' || c.type === 'characterData')) {
    window.__giftQueryChanges.count++;
    window.__giftQueryChanges.last = performance.now();
  }
});
for (const id of ['_jt_body_list', '_jt_pagebar']) {
  const node = document.getElementById(id);
  if (!node) return false;
  window.__giftQueryObserver.observe(node, {childList:true, characterData:true, subtree:true});
}
return true;
"""
_READ_REFRESH = """
// gift-query-observer-read
const state = window.__giftQueryChanges;
return state && {changed: state.count > 0, age: performance.now() - state.last};
"""
_STOP_OBSERVER = """
// gift-query-observer-stop
if (window.__giftQueryObserver) window.__giftQueryObserver.disconnect();
delete window.__giftQueryObserver;
delete window.__giftQueryChanges;
"""
_GRID_STATE = """
// gift-grid-state: rendered cell text and attributes only.
const body = document.getElementById('_jt_body');
const mode = arguments[0];
const text = (node) => node ? (node.innerText || '').trim() : null;
const rows = [...document.querySelectorAll('#_jt_body_list .rowList ._jt_row[index]')].map(row => {
  const index = row.getAttribute('index');
  if (mode === 'identity') {
    const order = row.querySelector('[data-id="so_id"]');
    const online = order ? [...order.childNodes].filter(n=>n.nodeType===Node.TEXT_NODE)
        .map(n=>n.textContent).join('').trim() : null;
    const items = [...row.querySelectorAll('[data-id="its"] div[title]')]
      .map(item=>({title:item.getAttribute('title'), quantity:text(item.querySelector('.sku_qty'))}));
    return {index, internal_id:text(row.querySelector('[data-id="o_id"] .o_id')),
            order_no:online, items, total_quantity:text(row.querySelector('[data-id="skuCount"] .skuCount'))};
  }
  const cell = row.querySelector('[data-id="address"]');
  return {index, recipient:cell ? {name:text(cell.querySelector('.receiver_name')),
          phone:text(cell.querySelector('.receiver_mobile')),
          address:text(cell.querySelector('.receiver_address_detail'))} : null};
});
return {rows, top:body.scrollTop, height:body.clientHeight, max:Math.max(0, body.scrollHeight-body.clientHeight),
        pager:text(document.getElementById('_jt_pagebar'))};
"""
_SCROLL = """
// gift-grid-scroll: UI scrolling only.
const body = document.getElementById('_jt_body');
if (arguments[0] === 'identity') body.scrollLeft = 0;
else {
  const head = document.querySelector('#_jt_row_head_list [data-id="address"]');
  const list = document.getElementById('_jt_row_head_list');
  if (!head || !list) return false;
  body.scrollLeft = head.getBoundingClientRect().left - list.getBoundingClientRect().left;
}
body.scrollTop = arguments[1];
return true;
"""


class JushuitanPage:
    def __init__(self, driver, *, timeout=30, reveal_recipient=None, authenticate=None):
        self.driver = driver
        self.timeout = timeout
        self.reveal_recipient = reveal_recipient or self._reveal_recipient
        self.authenticate = authenticate

    def _wait(self, predicate, detail):
        try:
            return WebDriverWait(self.driver, self.timeout, poll_frequency=.2,
                                 ignored_exceptions=(NoSuchElementException, StaleElementReferenceException)).until(predicate)
        except TimeoutException:
            raise PlatformOperationError(detail) from None

    def _reveal_recipient(self, driver, index):
        if not isinstance(index, str) or not index.isdecimal():
            raise PlatformOperationError("聚水潭收件信息所在行无法确认")
        scope = f'#_jt_body_list .rowList ._jt_row[index="{index}"] [data-id="address"]'
        cell = self._wait(EC.visibility_of_element_located((By.CSS_SELECTOR, scope)),
                          "聚水潭收件地址列未就绪")
        ActionChains(driver).move_to_element(cell).perform()
        button = self._wait(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, scope + ' .h_btn_decrypt_all[title="查看收货人信息"]')),
            "聚水潭查看收货人信息按钮未就绪")
        # 已在真实页面核对：悬停后点击该按钮可直接显示完整收件信息。
        button.click()

        def revealed(_):
            current = driver.execute_script(_GRID_STATE, "address")
            rows = [row for row in current["rows"] if row.get("index") == index]
            return bool(len(rows) == 1 and rows[0].get("recipient") and all(
                isinstance(value, str) and value.strip() and not _MASK.search(value)
                for value in rows[0]["recipient"].values()))

        self._wait(revealed, "聚水潭收件信息解密未完成，无法核验姓名、电话和地址")

    def _query(self, order_no):
        self.driver.switch_to.default_content()
        self.driver.get(ORDER_URL)
        if self.authenticate is not None:
            self.authenticate(target_url=ORDER_URL)
        self._wait(EC.presence_of_element_located((By.ID, "_jt_body")),
                   "聚水潭订单页面未就绪，请确认已登录且可进入订单界面")
        try:
            frame = self._wait(EC.presence_of_element_located((By.ID, "s_filter_frame")),
                               "聚水潭订单查询区域未就绪")
            self.driver.switch_to.frame(frame)
            try:
                field = self._wait(EC.element_to_be_clickable((By.ID, "so_id")),
                                   "聚水潭线上订单号输入框未就绪")
                field.clear()
                field.send_keys(order_no)
                if field.get_attribute("value") != order_no:
                    raise PlatformOperationError("聚水潭订单号输入后与请求不一致")
                # 输入完成后才观察，避免把初始加载误认为本次回车的刷新。
                self.driver.switch_to.parent_frame()
                if not self.driver.execute_script(_INSTALL_OBSERVER):
                    raise PlatformOperationError("聚水潭查询结果区域发生变化，无法确认刷新")
                self.driver.switch_to.frame(frame)
                field.send_keys(Keys.ENTER)
            finally:
                self.driver.switch_to.parent_frame()
            def refreshed(_):
                state = self.driver.execute_script(_READ_REFRESH)
                if not (state and state.get("changed") and state.get("age", 0) >= 600):
                    return False
                grid = self.driver.execute_script(_GRID_STATE, "identity")
                if not _PAGER.search(grid.get("pager") or ""):
                    return False
                total = page_count(grid["pager"])
                if not total:
                    return grid if not grid["rows"] else False
                return grid if self._ready_rows(grid["rows"], "identity") and all(
                    row["order_no"] == order_no for row in grid["rows"]) else False
            return self._wait(refreshed, "聚水潭查询未确认刷新，不能使用页面原有结果")
        finally:
            self.driver.execute_script(_STOP_OBSERVER)

    @staticmethod
    def _ready_rows(rows, mode):
        if mode == "identity":
            return bool(rows) and all(row.get("internal_id") and row.get("order_no")
                                      and row.get("items") and row.get("total_quantity") is not None
                                      for row in rows)
        return bool(rows) and all(row.get("recipient") and all(
            row["recipient"].get(field) is not None for field in ("name", "phone", "address")) for row in rows)

    def _collect(self, mode, total, pager_text):
        collected = {}
        position, iterations = 0, 0
        deadline = time.monotonic() + self.timeout
        while len(collected) < total:
            iterations += 1
            if iterations > total * 3 + 5 or time.monotonic() > deadline:
                raise PlatformOperationError("聚水潭虚拟表格未在限定时间内完整显示")
            if not self.driver.execute_script(_SCROLL, mode, position):
                raise PlatformOperationError("聚水潭所需列未显示，无法完整核验订单")

            def ready(_):
                current = self.driver.execute_script(_GRID_STATE, mode)
                # 等待滚动位置和所需列同时完成渲染，不使用固定短暂停顿。
                if (abs(current["top"] - min(position, current["max"])) > 2
                        or not self._ready_rows(current["rows"], mode)):
                    return False
                if collected and all(row.get("index") in collected for row in current["rows"]):
                    return False
                return current

            state = self._wait(ready, "聚水潭虚拟表格所需行或列未刷新完成")
            if page_count(state["pager"]) != total:
                raise PlatformOperationError("聚水潭读取过程中结果数量发生变化，请人工核查")
            if mode == "address" and self.reveal_recipient is not None:
                for row in state["rows"]:
                    if any(_MASK.search(value or "") for value in row["recipient"].values()):
                        self.reveal_recipient(self.driver, row["index"])
                state = self.driver.execute_script(_GRID_STATE, mode)
            for row in state["rows"]:
                index = row.get("index")
                if not isinstance(index, str) or not index.isdecimal() or int(index) >= total:
                    raise PlatformOperationError("聚水潭虚拟表格行号异常，无法对应收件信息")
                if index in collected and collected[index] != row:
                    raise PlatformOperationError("聚水潭读取过程中行内容发生变化，请人工核查")
                collected[index] = row
            if len(collected) == total:
                break
            if state["top"] >= state["max"] - 1:
                raise PlatformOperationError("聚水潭已到表格底部，但未读取全部订单记录")
            position = min(state["max"], state["top"] + max(1, state["height"] * .7))
        return collected

    def query_order(self, order_no):
        if not isinstance(order_no, str) or not order_no.strip():
            raise PlatformOperationError("聚水潭查询订单号必须是非空文本")
        for attempt in range(2):
            try:
                return self._query_order_once(order_no)
            except PlatformOperationError:
                # 只读采集被登录打断时，只有明确恢复了认证才完整重查一次。
                if attempt or self.authenticate is None or not self.authenticate():
                    raise

    def _query_order_once(self, order_no):
        try:
            refreshed = self._query(order_no)
            pager = refreshed["pager"]
            total = page_count(pager)
            if not total:
                state = self.driver.execute_script(_GRID_STATE, "identity")
                if state["rows"]:
                    raise PlatformOperationError("聚水潭分页显示无记录但表格仍有旧数据")
                return OrderSnapshot(order_no, False)
            identities = self._collect("identity", total, pager)
            # 复读稳定标识和商品，避免虚拟表格在采集期间变动。
            if self._collect("identity", total, pager) != identities:
                raise PlatformOperationError("聚水潭读取前后订单行不一致")
            rows = [identities[index] for index in sorted(identities, key=int)]
            return parse_order_rows(order_no, rows, pager)
        except PlatformOperationError:
            raise
        except WebDriverException:
            raise PlatformOperationError("聚水潭浏览器读取失败，请检查登录状态和页面是否发生变化") from None
