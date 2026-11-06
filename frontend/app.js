"use strict";

const $ = (selector) => document.querySelector(selector);
const fields = {
  order_no: { label: "订单号", placeholder: "输入订单号", max: 200 },
  gift: { label: "礼品", placeholder: "多个礼品用逗号、顿号或空格分隔", max: 500 },
  status: { label: "状态" },
};
let rows = [], revision = 0, token = "", active = null, initialized = false;
let version = 0, savedVersion = 0, saveTimer, saving = null, conflict = false, toastTimer;
let stateEpoch = 0, pollSequence = 0;
let giftCatalog = [], rowStates = {}, pickerRowId = null, starting = false;
let creatingGift = false, createGiftRowId = null, catalogRefreshing = null;
let catalogEditable = false;
let giftGroups = { revision: 0, groups: [], memberships: {} }, selectedGiftGroup = "", selectedStatus = "", displayedRows = [];
let catalogVersion = "", scopeInvalid = false;
const scopeStorageKey = "gift_registry.input_scope.v1";
function currentScope() { return {group_id: selectedGiftGroup, group_revision: giftGroups.revision, catalog_version: catalogVersion}; }
function persistScope() { try { localStorage.setItem(scopeStorageKey, JSON.stringify(currentScope())); } catch { /* 页面内仍有效。 */ } }
function restoreScope() {
  try {
    const saved = JSON.parse(localStorage.getItem(scopeStorageKey) || "null");
    if (!saved) return;
    selectedGiftGroup = typeof saved.group_id === "string" ? saved.group_id : "";
    scopeInvalid = saved.group_revision !== giftGroups.revision || saved.catalog_version !== catalogVersion ||
      !["", "__ungrouped", ...giftGroups.groups.map(group => group.id)].includes(selectedGiftGroup);
  } catch { scopeInvalid = true; }
}
function scopedGifts() { return scopeInvalid ? [] : giftCatalog.filter(gift => inGiftGroup(gift.id, selectedGiftGroup)); }
function ensureGiftScope(ids) {
  if (!ids.length) return;
  if (scopeInvalid) throw new Error("分组或目录已更新，请重新选择礼品分组");
  const allowed = new Set(scopedGifts().map(gift => gift.id));
  if (ids.some(id => !allowed.has(id))) throw new Error("当前分组未找到，请切换分组；本次选择未应用");
}
function acceptCatalogContext(result) {
  const groups = result.gift_groups || giftGroups;
  if (catalogVersion && (catalogVersion !== result.catalog_version || giftGroups.revision !== groups.revision)) scopeInvalid = true;
  catalogVersion = result.catalog_version || catalogVersion;
  giftGroups = groups; giftMatcher = null; renderGroupFilter();
}
let hostActionsAvailable = false, groupSelection = new Set(), groupsSaving = false;
const statusOptions = { "": "全部状态", ready: "待登记", processing: "进行中", success: "成功", failed: "失败", cancelled: "已取消", manual_success: "人工标记成功", manual_failed: "人工标记失败", manual_pending: "人工待核查", skipped: "已跳过", incomplete: "待完善", empty: "空白", queued: "待接入" };
function gridRows() { return displayedRows; }
const recentGiftOrderLimit = 100;
let pickerGiftUsage = null;
let selection = null, editing = null, dragSelection = null, dragFrame = null, suppressCellClick = false;
let rowNodes = new Map(), selectedNodes = new Set(), forceRangeCopy = false;
const rangeClipboardType = "application/x-gift-registry-range+json";
const history = [];
const layoutStorageKey = "gift_registry.column_widths.v1";
const columnRules = { order_no: { min: 120, base: 180 }, gift: { min: 140, base: 190 }, status: { min: 112, base: 112 } };
let manualWidths = {}, layoutFrame = null, resizingColumn = null;
try {
  const saved = JSON.parse(localStorage.getItem(layoutStorageKey) || "{}");
  for (const field of Object.keys(columnRules)) if (Number.isFinite(saved?.[field])) manualWidths[field] = Math.max(columnRules[field].min, Math.min(2400, saved[field]));
} catch { /* 布局偏好不可用时按内容计算列宽。 */ }
const textCanvas = document.createElement("canvas"), textContext = textCanvas.getContext("2d");
const textMeasurements = new Map();

function textWidth(text, element, fallback = "12px sans-serif") {
  const style = element ? getComputedStyle(element) : null;
  const font = style ? `${style.fontWeight} ${style.fontSize} ${style.fontFamily}` : fallback;
  const key = `${font}|${text}`;
  if (!textMeasurements.has(key)) {
    if (textMeasurements.size > 5000) textMeasurements.clear();
    textContext.font = font;
    textMeasurements.set(key, textContext.measureText(String(text)).width);
  }
  return textMeasurements.get(key);
}
function saveColumnPreferences() {
  try { localStorage.setItem(layoutStorageKey, JSON.stringify(manualWidths)); } catch { /* 仍保留本次页面内的设置。 */ }
}
function naturalColumnWidths() {
  const widths = Object.fromEntries(Object.entries(columnRules).map(([field, rule]) => [field, rule.base]));
  const orderInput = $("#grid-body .order-input"), chip = $("#grid-body .gift-chip"), badge = $("#grid-body .status-badge");
  for (const row of rows) {
    widths.order_no = Math.max(widths.order_no, textWidth(row.order_no, orderInput, "13px Consolas") + [...row.order_no].length * .2 + 36);
    const names = giftNames(row.gift_ids || []), unmatched = unresolvedGiftLabels(row);
    const labels = [...names, ...(unmatched.length ? [`未匹配：${unmatched.join("、")}`] : [])];
    const giftsWidth = labels.reduce((total, label) => total + textWidth(label, chip, "11px sans-serif") + 16, 0) + Math.max(0, labels.length - 1) * 5;
    widths.gift = Math.max(widths.gift, giftsWidth + 58);
    const display = status(row), execution = rowStates[row.id];
    const action = hostActionsAvailable && execution?.signature === rowSignature(row)
      ? execution.state === "queued" ? "取消" : display.type === "failed" || display.type === "cancelled" && execution.retry_cancelled ? "↻ 重登" : "" : "";
    const actionWidth = action ? textWidth(action, $("#grid-body .row-retry"), "11px sans-serif") + 24 : 0;
    widths.status = Math.max(widths.status, textWidth(display.label, badge, "11px sans-serif") + 54 + actionWidth);
  }
  return Object.fromEntries(Object.entries(widths).map(([field, width]) => [field, Math.min(2400, Math.ceil(width))]));
}
function applyColumnWidths() {
  if (!$("#grid-columns").children.length) return;
  const natural = naturalColumnWidths(), widths = {};
  for (const field of columns()) widths[field] = manualWidths[field] ?? natural[field];
  const flexible = ["order_no", "gift"].filter(field => manualWidths[field] === undefined);
  const remaining = Math.max(0, Math.floor($("#grid-scroll").clientWidth) - 46 - Object.values(widths).reduce((sum, width) => sum + width, 0));
  if (flexible.length) {
    let distributed = 0;
    const totalWeight = flexible.reduce((sum, field) => sum + natural[field], 0);
    flexible.forEach((field, index) => {
      const extra = index === flexible.length - 1 ? remaining - distributed : Math.floor(remaining * natural[field] / totalWeight);
      widths[field] += extra; distributed += extra;
    });
  }
  $("#grid-columns .row-number").style.width = "46px";
  for (const field of columns()) {
    $("#grid-columns").querySelector(`.col-${field}`).style.width = `${widths[field]}px`;
    document.querySelectorAll(`.column-resizer[data-resize-field="${field}"]`).forEach(handle => {
      handle.setAttribute("aria-valuenow", String(widths[field]));
      handle.setAttribute("aria-valuetext", `${widths[field]} 像素${manualWidths[field] === undefined ? "，自动适配" : "，手动设置"}`);
    });
  }
  $("#registry-grid").style.width = `${46 + Object.values(widths).reduce((sum, width) => sum + width, 0)}px`;
  $("#registry-grid").style.minWidth = "0";
}
function scheduleColumnSizing() {
  if (layoutFrame !== null) return;
  layoutFrame = requestAnimationFrame(() => { layoutFrame = null; applyColumnWidths(); });
}
function resetColumnWidth(field) {
  if (field) delete manualWidths[field]; else manualWidths = {};
  saveColumnPreferences(); scheduleColumnSizing();
}
function createColumnResizer(field) {
  const handle = document.createElement("span");
  handle.className = "column-resizer"; handle.dataset.resizeField = field; handle.tabIndex = 0;
  handle.setAttribute("role", "separator"); handle.setAttribute("aria-orientation", "vertical");
  handle.setAttribute("aria-label", `调整${fields[field].label}列宽`); handle.setAttribute("aria-controls", "registry-grid");
  handle.setAttribute("aria-valuemin", String(columnRules[field].min)); handle.setAttribute("aria-valuemax", "2400");
  handle.title = "拖动调整列宽，双击恢复自动适配；方向键可微调";
  handle.addEventListener("pointerdown", event => {
    if (event.button !== 0) return;
    event.preventDefault(); event.stopPropagation(); closeGiftPicker();
    const startWidth = $("#grid-columns").querySelector(`.col-${field}`).getBoundingClientRect().width;
    resizingColumn = { field, pointerId: event.pointerId, startX: event.clientX, startWidth, previous: manualWidths[field] };
    manualWidths[field] = Math.round(startWidth);
    handle.setPointerCapture(event.pointerId); document.body.classList.add("column-resizing");
  });
  handle.addEventListener("pointermove", event => {
    if (resizingColumn?.pointerId !== event.pointerId || resizingColumn.field !== field) return;
    manualWidths[field] = Math.round(Math.max(columnRules[field].min, Math.min(2400, resizingColumn.startWidth + event.clientX - resizingColumn.startX)));
    scheduleColumnSizing();
  });
  function finish(event, cancel = false) {
    if (resizingColumn?.pointerId !== event.pointerId || resizingColumn.field !== field) return;
    if (cancel) {
      if (resizingColumn.previous === undefined) delete manualWidths[field]; else manualWidths[field] = resizingColumn.previous;
    }
    resizingColumn = null;
    if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId);
    document.body.classList.remove("column-resizing"); saveColumnPreferences(); scheduleColumnSizing();
  }
  handle.addEventListener("pointerup", event => finish(event));
  handle.addEventListener("pointercancel", event => finish(event, true));
  handle.addEventListener("dblclick", event => { event.preventDefault(); event.stopPropagation(); resetColumnWidth(field); });
  handle.addEventListener("keydown", event => {
    if (["Home", "Enter"].includes(event.key)) { event.preventDefault(); resetColumnWidth(field); }
    if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault(); event.stopPropagation();
    const current = $("#grid-columns").querySelector(`.col-${field}`).getBoundingClientRect().width;
    manualWidths[field] = Math.max(columnRules[field].min, Math.min(2400, Math.round(current + (event.key === "ArrowRight" ? 1 : -1) * (event.shiftKey ? 50 : 10))));
    saveColumnPreferences(); scheduleColumnSizing();
  });
  return handle;
}

function newRowId() {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  // 普通局域网 HTTP 不提供 randomUUID；getRandomValues 仍可生成随机 UUID v4。
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
function newRow() {
  return { id: newRowId(), order_no: "", gift: "", gift_ids: [], change_address: false, gift_unmatched: [] };
}
function normalizeGift(text) { return text.toLowerCase().replace(/[^\p{L}\p{N}]/gu, ""); }
function similarity(a, b) {
  let previous = Array.from({ length: b.length + 1 }, (_, index) => index);
  for (let i = 1; i <= a.length; i++) {
    const current = [i];
    for (let j = 1; j <= b.length; j++) current[j] = Math.min(current[j - 1] + 1, previous[j] + 1, previous[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
    previous = current;
  }
  return 1 - previous[b.length] / Math.max(a.length, b.length, 1);
}
function giftLabelScore(value, candidate) {
  if (candidate === value) return 1;
  if (candidate.includes(value) || value.includes(candidate)) return .8 + .15 * Math.min(candidate.length, value.length) / Math.max(candidate.length, value.length);
  return Math.min(value.length, candidate.length) >= 2 ? similarity(value, candidate) * .9 : 0;
}
function isGiftCodeInput(text) {
  const input = text.trim();
  // 编码按原字符串识别；疑似编码也不能借名称/别名的模糊匹配纠错。
  return giftCatalog.some(gift => normalizeGift(gift.id) === normalizeGift(input)) ||
    (/^[A-Za-z0-9_./:#@()+-]+$/u.test(input) && /[0-9_./:#@()+-]/u.test(input));
}
function giftScore(text, gift) {
  if (giftCatalog.some(item => item.id === text.trim()) || isGiftCodeInput(text)) return gift.id === text.trim() ? 1 : 0;
  const value = normalizeGift(text);
  if (!value) return 0;
  return Math.max(...[gift.name, ...(gift.aliases || [])].map(label => giftLabelScore(value, normalizeGift(label))));
}
let giftMatcher = null;
function matchingContext() {
  const scopeKey = JSON.stringify([selectedGiftGroup, giftGroups.revision, catalogVersion, scopeInvalid]);
  if (giftMatcher?.catalog === giftCatalog && giftMatcher.scopeKey === scopeKey) return giftMatcher;
  const exact = new Map(), codes = new Map(), entries = [], protectedLabels = [];
  let maxWords = 8;
  for (const gift of scopedGifts()) {
    codes.set(gift.id, gift.id);
    const labels = [gift.name, ...(gift.aliases || [])];
    entries.push({ gift, labels: [...new Set(labels.map(normalizeGift).filter(Boolean))] });
    for (const label of labels) {
      const key = normalizeGift(label);
      if (!exact.has(key)) exact.set(key, new Set());
      exact.get(key).add(gift.id);
      maxWords = Math.max(maxWords, label.trim().split(/\s+/u).length + 2);
      if (/[,，、;；+＋|｜\n]/u.test(label)) {
        const pattern = label.trim().split(/\s+/u).map(part => part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("\\s+");
        protectedLabels.push({ label, pattern: new RegExp(`^(?:${pattern})`, "iu") });
      }
    }
  }
  for (const gift of giftCatalog) {
    const escaped = gift.id.replace(/[.*+?^$()|[\]{}\\]/g, "\\$&");
    protectedLabels.push({label: gift.id, pattern: new RegExp("^(?:" + escaped + ")", "u")});
  }
  protectedLabels.sort((a, b) => b.label.length - a.label.length);
  giftMatcher = { scopeKey, catalog: giftCatalog, size: giftCatalog.length, exact, codes, entries, protectedLabels, maxWords, parts: new Map(), results: new Map() };
  return giftMatcher;
}
function resolveGiftPart(text, strict = false) {
  const context = matchingContext(), value = normalizeGift(text), key = `${strict ? "phrase" : "word"}:${text.trim()}`;
  if (context.parts.has(key)) return context.parts.get(key);
  const exact = context.exact.get(value);
  let result;
  if (giftCatalog.some(gift => gift.id === text.trim()) && !context.codes.has(text.trim())) result = { id: null, score: 0, exact: true };
  else if (context.codes.has(text.trim())) result = { id: context.codes.get(text.trim()), score: 1, exact: true };
  else if (isGiftCodeInput(text)) result = {id: null, score: 0, exact: true};
  else if (exact) result = { id: exact.size === 1 ? [...exact][0] : null, score: exact.size === 1 ? 1 : 0, exact: true };
  else {
    const matches = context.entries.map(({ gift, labels }) => {
      // 跨空格的短语必须整体相似，不能只因包含一个已知简称就吞掉其他礼品。
      const meaningful = value.length >= 3 || labels.some(label => Math.min(value.length, label.length) / Math.max(value.length, label.length, 1) >= .5);
      const score = strict ? Math.max(0, ...labels.map(label => {
        const coverage = Math.min(value.length, label.length) / Math.max(value.length, label.length, 1);
        if (value.length >= 3 && coverage >= .65 && label.includes(value)) return giftLabelScore(value, label);
        return coverage >= .8 ? similarity(value, label) * .9 : 0;
      })) : meaningful ? giftScore(text, gift) : 0;
      return { id: gift.id, score };
    }).sort((a, b) => b.score - a.score);
    const best = matches[0], next = matches[1];
    result = best && best.score >= (strict ? .72 : .59) && (!next || best.score - next.score >= .08)
      ? best : { id: null, score: 0 };
    if (strict && result.id) {
      const coverage = context.entries.map(({ gift, labels }) => ({ id: gift.id, score: Math.max(0, ...labels.map(label =>
        Math.min(value.length, label.length) / Math.max(value.length, label.length, 1) >= .65 ? giftLabelScore(value, label) : 0)) }))
        .sort((a, b) => b.score - a.score);
      if (coverage[0]?.id !== result.id || (coverage[1] && coverage[0].score - coverage[1].score < .08)) result = { id: null, score: 0 };
    }
  }
  if (context.parts.size > 3000) context.parts.clear();
  context.parts.set(key, result);
  return result;
}
function splitGiftParts(text) {
  // 逗号等为强分隔符；已知名称内部的标点和空格先保留。
  const delimiter = /[,，、;；+＋|｜\n]/;
  const { protectedLabels } = matchingContext();
  const parts = []; let part = "";
  for (let index = 0; index < text.length;) {
    let protectedText = null;
    if (!part.trim()) {
      for (const { pattern } of protectedLabels) {
        const matched = pattern.exec(text.slice(index))?.[0], next = matched ? text[index + matched.length] : null;
        if (matched && (!next || delimiter.test(next) || /\s/u.test(next))) { protectedText = matched; break; }
      }
    }
    if (protectedText) { part += protectedText; index += protectedText.length; }
    else if (delimiter.test(text[index])) { if (part.trim()) parts.push(part.trim()); part = ""; index++; }
    else part += text[index++];
  }
  if (part.trim()) parts.push(part.trim());
  return parts;
}
function matchSpaceGiftParts(text) {
  const whole = resolveGiftPart(text), words = text.trim().split(/\s+/u);
  // 完整名称、编码或别名不因内部空格而被拆开；歧义的完整名称也保留待确认。
  if (words.length === 1 || whole.exact) return [{ text, match: whole }];
  const context = matchingContext(), plans = Array(words.length + 1);
  plans[words.length] = { weight: 0, count: 0 };
  for (let start = words.length - 1; start >= 0; start--) {
    const single = resolveGiftPart(words[start]);
    let best = { weight: normalizeGift(words[start]).length * single.score + plans[start + 1].weight,
      count: plans[start + 1].count + 1, end: start + 1, text: words[start], match: single };
    for (let end = start + 2; end <= Math.min(words.length, start + context.maxWords); end++) {
      const phrase = words.slice(start, end).join(" "), match = resolveGiftPart(phrase, true);
      if (!match.id) continue;
      const candidate = { weight: normalizeGift(phrase).length * match.score + plans[end].weight,
        count: plans[end].count + 1, end, text: phrase, match };
      if (candidate.weight > best.weight + .000001 || (Math.abs(candidate.weight - best.weight) <= .000001 && candidate.count < best.count)) best = candidate;
    }
    plans[start] = best;
  }
  const parts = [];
  for (let start = 0; start < words.length;) {
    const plan = plans[start], previous = parts.at(-1);
    if (!plan.match.id && previous && !previous.match.id) previous.text += ` ${plan.text}`;
    else parts.push({ text: plan.text, match: plan.match });
    start = plan.end;
  }
  return parts;
}
function matchGifts(text) {
  const context = matchingContext(), cached = context.results.get(text);
  if (cached) return { ids: [...cached.ids], unmatched: [...cached.unmatched] };
  if (giftCatalog.some(gift => normalizeGift(gift.id) === normalizeGift(text.trim()))) {
    const match = resolveGiftPart(text);
    return {ids: match.id ? [match.id] : [], unmatched: match.id ? [] : [text.trim()]};
  }
  const ids = [], unmatched = [];
  for (const part of splitGiftParts(text)) {
    for (const segment of matchSpaceGiftParts(part)) {
      if (segment.match.id) { if (!ids.includes(segment.match.id)) ids.push(segment.match.id); }
      else unmatched.push(segment.text);
    }
  }
  if (context.results.size > 2000) context.results.clear();
  context.results.set(text, { ids: [...ids], unmatched: [...unmatched] });
  return { ids, unmatched };
}
function giftNames(ids) { return ids.map(id => giftCatalog.find(gift => gift.id === id)?.name).filter(Boolean); }
function unresolvedGiftLabels(row) {
  if (Array.isArray(row.gift_unmatched)) return row.gift_unmatched;
  const selected = giftNames(row.gift_ids || []).join("、");
  if (row.gift === selected) return [];
  // 已选编码是身份依据；同名 SKU 的展示名称不能再反向解析成未匹配项。
  const remaining = selected && row.gift.startsWith(selected + "、") ? row.gift.slice(selected.length + 1) : row.gift;
  const matched = matchGifts(remaining);
  if (matched.ids.some(id => !(row.gift_ids || []).includes(id))) return [remaining];
  const selectedLabels = new Set(giftCatalog.filter(gift => (row.gift_ids || []).includes(gift.id))
    .flatMap(gift => [gift.id, gift.name, ...(gift.aliases || [])]).map(normalizeGift));
  return matched.unmatched.filter(label => !selectedLabels.has(normalizeGift(label)));
}
function assignGifts(row, ids, unmatched = []) {
  const unique = [...new Set(ids)];
  ensureGiftScope(unique.filter(id => !(row.gift_ids || []).includes(id)));
  row.gift_ids = unique;
  row.gift_unmatched = [...unmatched];
  row.gift = [...giftNames(unique), ...unmatched].join("、");
  row.selection_scope = currentScope();
  row.gift_selection = {source: "confirmed", gift_ids: [...unique], label: giftNames(unique).join("、")};
}
function rowSignature(row) { return JSON.stringify([row.id, row.order_no.trim(), [...new Set(row.gift_ids || [])].sort(), row.change_address || false, row.gift_unmatched || []]); }
function taskIdentity(row) { return JSON.stringify([row.order_no.trim(), [...new Set(row.gift_ids || [])].sort()]); }
function validGifts(row) { return Boolean(row.gift_ids?.length && !unresolvedGiftLabels(row).length
  && row.gift_ids.every(id => giftCatalog.some(gift => gift.id === id))); }
function columns() { return ["order_no", "gift", "status"]; }
function filled(row) { return Boolean(row.order_no.trim() || row.gift.trim() || row.change_address); }
function status(row) {
  if (!filled(row)) return { label: "—", type: "empty" };
  if (row.change_address) return {label: "旧草稿不支持", type: "incomplete", detail: "该草稿要求改地址，本版本不支持。请核对原意后删除此行并新建原地址订单；不会自动执行，也不展示旧收件信息。"};
  const complete = row.order_no.trim() && validGifts(row);
  if (complete && rowStates[row.id]?.signature === rowSignature(row)) {
    const state = rowStates[row.id];
    const labels = { waiting_for_workflow: ["待接入流程", "queued"], queued: ["等待执行", "processing"], querying: ["查询订单", "processing"],
      submitting: ["正在登记", "processing"], verifying: ["复查确认", "processing"], success: ["成功", "success"], failed: ["失败", "failed"], skipped: ["已跳过", "skipped"], cancelled: ["已取消", "cancelled"] };
    const [label, type] = labels[state.state] || ["等待执行", "processing"];
    if (state.manual_status) return { label: statusOptions[`manual_${state.manual_status}`], type: `manual_${state.manual_status}`,
      detail: `主机人工标记；自动核验结果：${label}。${state.detail || ""}。不改写自动核验日志；人工成功持续保留并排除重登。` };
    const review = state.state === "failed" && state.review_due_at && !state.review_result
      ? `；等待自动复核一次（${new Date(state.review_due_at).toLocaleTimeString("zh-CN", { hour12: false })} 到期，不重新提交）` : "";
    return { label, type, detail: (state.detail || "") + review };
  }
  if (row.gift.trim() && !validGifts(row)) return { label: "待匹配礼品", type: "incomplete" };
  return complete ? { label: "待登记", type: "ready" } : { label: "待完善", type: "incomplete" };
}
function toast(message, error = false) {
  clearTimeout(toastTimer);
  $("#toast").textContent = message;
  $("#toast").className = `toast${error ? " error" : ""}`;
  $("#toast").hidden = false;
  toastTimer = setTimeout(() => { $("#toast").hidden = true; }, error ? 7000 : 3500);
}
function saveIndicator(message, type = "") {
  $("#save-label").textContent = message;
  $("#save-indicator").className = `save-indicator ${type}`;
  $("#sheet-save-state").textContent = message;
  $("#sheet-save-state").className = `sheet-save-state ${type}`;
  $("#sheet-save-state").title = message;
}
function remember() {
  history.push(structuredClone(rows));
  if (history.length > 50) history.shift();
  $("#undo").disabled = false;
}
function changed() {
  version++;
  $("#registry-grid").classList.remove("is-copied");
  updateSummary();
  updateDuplicateOrders();
  scheduleColumnSizing();
  clearTimeout(saveTimer);
  saveIndicator(conflict ? "保存冲突，请保留内容并刷新" : "有未保存的修改", conflict ? "error" : "saving");
  if (!conflict) saveTimer = setTimeout(() => saveNow(), 650);
}
async function api(path, options = {}) {
  const changesState = options.method === "POST" && (path.startsWith("/api/registrations/") || path.startsWith("/api/rows/"));
  const writing = options.method === "POST" || options.method === "PUT";
  if (changesState) stateEpoch++;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), writing ? 60000 : 15000);
  try {
    const response = await fetch(path, { ...options, signal: controller.signal, headers: { "Content-Type": "application/json", "X-CSRF-Token": token, ...(options.headers || {}) } });
    let result;
    try { result = await response.json(); } catch { throw new Error("工作台连接中断，请确认程序正在运行；操作结果请先查看状态"); }
    if (!response.ok) {
      if (response.status === 403 && result.code === "csrf_expired") {
        // 服务已明确拒绝写入：只更新连接凭据，保留编辑；由用户再次明确操作。
        try {
          const config = await api("/api/config");
          token = config.token;
        } catch { throw new Error("无法恢复工作台连接，未保存内容仍在页面中，请保留页面稍后再试"); }
        throw new Error("工作台连接已更新，本次操作未执行。未保存内容已保留，请再次点击保存或操作按钮");
      }
      if (path === "/api/rows" && response.status === 409 && !["registration_not_started", "gift_catalog_conflict", "gift_group_conflict", "selection_conflict"].includes(result.code)) conflict = true;
      if (result.code === "selection_conflict") { scopeInvalid = true; renderGroupFilter(); await refreshGiftCatalog(); }
      throw new Error(result.error || "请求失败，请检查连接和访问密码");
    }
    return result;
  } catch (error) {
    if (controller.signal.aborted || error instanceof TypeError) {
      throw new Error(writing ? "连接中断或请求超时，操作是否完成尚未确认；请保留页面并先查看状态，不会自动再次发送"
        : "无法连接工作台，正在等待恢复；当前显示为最后已知状态");
    }
    throw error;
  } finally {
    clearTimeout(timer);
    if (changesState) stateEpoch++;
  }
}
async function saveNow(manual = false) {
  clearTimeout(saveTimer);
  if (!initialized) return false;
  if (conflict) { if (manual) toast("表格已在其他窗口更新。请先复制当前内容，再刷新页面。", true); return false; }
  if (saving) {
    const succeeded = await saving;
    return succeeded && version !== savedVersion ? saveNow(manual) : succeeded;
  }
  if (version === savedVersion) { if (manual) toast("所有修改均已保存"); return true; }
  const currentVersion = version, records = structuredClone(rows);
  saveIndicator("正在保存…", "saving");
  saving = (async () => {
    try {
      const result = await api("/api/rows", { method: "PUT", body: JSON.stringify({ revision, rows: records }) });
      revision = result.revision;
      savedVersion = currentVersion;
      if (version === savedVersion) {
        saveIndicator(`已保存 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`);
        if (manual) toast("表格已保存");
      }
      return true;
    } catch (error) {
      saveIndicator(error.message, "error");
      toast(error.message, true);
      return false;
    } finally { saving = null; }
  })();
  const succeeded = await saving;
  if (succeeded && version !== savedVersion) return saveNow(manual);
  return succeeded;
}

function updateSummary() {
  $("#row-summary").textContent = `共 ${rows.length} 行 · 已填写 ${rows.filter(filled).length} 条${selectedStatus ? ` · 筛选显示 ${displayedRows.length} 行` : ""}`;
  const count = type => rows.filter(row => status(row).type === type).length;
  const manualCount = rows.filter(row => status(row).type.startsWith("manual_")).length;
  $("#ready-summary").textContent = `待登记 ${count("ready")} · 进行中 ${count("processing")} · 成功 ${count("success")} · 失败 ${count("failed")}${manualCount ? ` · 人工标记 ${manualCount}` : ""}`;
  $("#metric-ready").textContent = count("ready");
  $("#metric-success").textContent = count("success");
  $("#metric-failed").textContent = count("failed");
}
function updateDuplicateOrders() {
  const orders = new Map();
  rows.forEach((row, index) => {
    const orderNo = row.order_no.trim();
    if (!orderNo || !validGifts(row)) return;
    const key = taskIdentity(row);
    if (!orders.has(key)) orders.set(key, []);
    orders.get(key).push(index + 2);
  });
  let groups = 0, repeatedRows = 0;
  for (const positions of orders.values()) if (positions.length > 1) { groups++; repeatedRows += positions.length; }
  for (const row of rows) {
    const cell = rowElement(row.id)?.querySelector('td[data-field="order_no"]');
    if (!cell) continue;
    const positions = validGifts(row) ? orders.get(taskIdentity(row)) : null, duplicate = positions?.length > 1;
    const input = cell.querySelector("input");
    cell.classList.toggle("duplicate-order", Boolean(duplicate));
    if (duplicate) {
      const message = `订单号及礼品组合重复：第 ${positions.slice(0, 8).join("、")}${positions.length > 8 ? "…" : ""} 行，共 ${positions.length} 行`;
      cell.title = message; input.title = message; input.setAttribute("aria-invalid", "true");
      input.setAttribute("aria-describedby", "duplicate-summary"); cell.dataset.duplicateCount = positions.length;
    } else {
      cell.removeAttribute("title"); input.removeAttribute("title"); input.removeAttribute("aria-invalid");
      input.removeAttribute("aria-describedby"); delete cell.dataset.duplicateCount;
    }
  }
  const summary = $("#duplicate-summary"), text = `重复任务 ${groups} 组 · ${repeatedRows} 行`;
  summary.hidden = groups === 0;
  if (summary.textContent !== text) summary.textContent = text;
  summary.title = "订单号与整组礼品编码相同才算重复，忽略礼品排列顺序；名称、分组不参与判定。红色订单号可悬停查看所在行。";
}
let sheetFullscreenOwned = false, sheetFullscreenChanging = false;
function setSheetExpanded(expanded) {
  document.body.classList.toggle("sheet-expanded", expanded);
  $("#toggle-fullscreen").setAttribute("aria-pressed", String(expanded));
  $("#fullscreen-label").textContent = expanded ? "退出全屏" : "全屏表格";
  $("#toggle-fullscreen").title = expanded ? "退出表格全屏，可按 Esc 退出" : "全屏显示表格，可按 Esc 退出";
  $("#fullscreen-icon").setAttribute("d", expanded ? "M3 7h4V3m6 0v4h4M7 17v-4H3m14 0h-4v4" : "M7 3H3v4m10-4h4v4M3 13v4h4m10-4v4h-4");
  closeGiftPicker(); scheduleColumnSizing();
}
async function toggleSheetFullscreen() {
  if (!initialized || sheetFullscreenChanging) return;
  sheetFullscreenChanging = true; $("#toggle-fullscreen").disabled = true;
  finishEditing();
  try {
    if (document.body.classList.contains("sheet-expanded")) {
      if (sheetFullscreenOwned && document.fullscreenElement) {
        try { await document.exitFullscreen(); } catch { /* 仍允许恢复普通布局。 */ }
      }
      sheetFullscreenOwned = false; setSheetExpanded(false);
    } else {
      setSheetExpanded(true);
      // 对根节点全屏，礼品选项、提示与新增弹窗也能正常显示。
      if (!document.fullscreenElement && document.fullscreenEnabled && document.documentElement.requestFullscreen) {
        sheetFullscreenOwned = true;
        try { await document.documentElement.requestFullscreen(); }
        catch { sheetFullscreenOwned = false; /* 浏览器未开放全屏时，保持铺满窗口。 */ }
      }
    }
  } finally { sheetFullscreenChanging = false; $("#toggle-fullscreen").disabled = false; }
}
$("#toggle-fullscreen").addEventListener("click", toggleSheetFullscreen);
const overviewStorageKey = "gift_registry.overview_open.v1";
const overviewPanel = $("#workspace-overview");
try { overviewPanel.open = localStorage.getItem(overviewStorageKey) === "true"; }
catch { /* 无法保存偏好时仍可正常展开或折叠。 */ }
function syncOverviewPanel() {
  $("#overview-toggle").setAttribute("aria-expanded", String(overviewPanel.open));
  $("#overview-toggle-label").textContent = overviewPanel.open ? "收起" : "展开";
}
syncOverviewPanel();
overviewPanel.addEventListener("toggle", () => {
  syncOverviewPanel(); closeGiftPicker(); scheduleColumnSizing();
  try { localStorage.setItem(overviewStorageKey, String(overviewPanel.open)); } catch { /* 保留本次页面内的选择。 */ }
});
document.addEventListener("fullscreenchange", () => {
  if (sheetFullscreenOwned && !document.fullscreenElement) { sheetFullscreenOwned = false; setSheetExpanded(false); }
  scheduleColumnSizing();
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && !event.defaultPrevented && !event.isComposing && !editing && !pickerRowId && !document.querySelector("dialog[open]") && document.body.classList.contains("sheet-expanded")) {
    event.preventDefault(); toggleSheetFullscreen();
  }
});
function closeGiftPicker() { pickerRowId = null; pickerGiftUsage = null; $("#gift-picker").hidden = true; document.querySelectorAll('.gift-trigger[aria-expanded="true"]').forEach(button => button.setAttribute("aria-expanded", "false")); }
function recentGiftUsage() {
  const known = new Set(giftCatalog.map(gift => gift.id)), orders = new Map();
  // 新行追加在表格末尾；按末尾往前选最近的不同订单，而不是按订单号大小排序。
  for (let index = rows.length - 1; index >= 0; index--) {
    const row = rows[index], orderNo = row.order_no.trim();
    if (!orderNo) continue;
    const ids = (row.gift_ids || []).filter(id => known.has(id));
    if (!ids.length || (!orders.has(orderNo) && orders.size >= recentGiftOrderLimit)) continue;
    if (!orders.has(orderNo)) orders.set(orderNo, new Set());
    for (const id of ids) orders.get(orderNo).add(id);
  }
  const counts = new Map();
  for (const gifts of orders.values()) for (const id of gifts) counts.set(id, (counts.get(id) || 0) + 1);
  return { counts, orderCount: orders.size };
}
function renderGiftCell(cell, row, index) {
  cell.replaceChildren();
  const button = document.createElement("button");
  button.type = "button"; button.className = "gift-trigger";
  button.setAttribute("aria-label", `第 ${index + 2} 行 礼品（可多选）`);
  button.setAttribute("aria-haspopup", "dialog"); button.setAttribute("aria-expanded", String(pickerRowId === row.id));
  button.title = row.gift || "选择或粘贴多个礼品，可用中文逗号、英文逗号、顿号或空格分隔，自动模糊匹配名称或别名";
  const chips = document.createElement("span"); chips.className = "gift-chips";
  const names = giftNames(row.gift_ids || []), unmatched = unresolvedGiftLabels(row);
  names.forEach((name, index) => { const chip = document.createElement("span"); chip.className = "gift-chip"; chip.textContent = name; chip.title = `${name}（${row.gift_ids[index]}）`; chips.append(chip); });
  if (unmatched.length) { const chip = document.createElement("span"); chip.className = "gift-chip unmatched-chip"; chip.textContent = `未匹配：${unmatched.join("、")}`; chips.append(chip); }
  if (!names.length && !unmatched.length) { const placeholder = document.createElement("span"); placeholder.className = "gift-placeholder"; placeholder.textContent = "选择礼品，可多选"; chips.append(placeholder); }
  const caret = document.createElement("span"); caret.className = "gift-caret"; caret.textContent = "⌄";
  button.append(chips, caret);
  button.addEventListener("focus", () => activate(row.id, "gift"));
  button.addEventListener("click", () => { selectRange({ id: row.id, field: "gift" }); if (pickerRowId === row.id) closeGiftPicker(); else openGiftPicker(row, button); });
  button.addEventListener("paste", handlePaste);
  button.addEventListener("keydown", event => handleNavigation(event, row.id, "gift"));
  cell.append(button);
}
function updateGiftRow(row) {
  const tr = rowElement(row.id);
  renderGiftCell(tr.querySelector('[data-field="gift"]'), row, rows.indexOf(row));
  tr.querySelector('[data-field="status"]').replaceChildren(statusNode(row));
  activate(row.id, "gift"); changed();
}
function openGiftPicker(row, anchor) {
  closeGiftPicker(); pickerRowId = row.id;
  // 本次打开期间固定频率，勾选不会让鼠标下的选项突然换位。
  pickerGiftUsage = recentGiftUsage();
  const picker = $("#gift-picker"); picker.hidden = false;
  anchor.setAttribute("aria-expanded", "true");
  $("#gift-search").value = "";
  renderGiftOptions();
  const rect = anchor.getBoundingClientRect(), width = Math.min(340, innerWidth - 24);
  picker.style.width = `${width}px`;
  picker.style.left = `${Math.max(12, Math.min(rect.left, innerWidth - width - 12))}px`;
  picker.style.top = `${Math.max(12, Math.min(rect.bottom + 6, innerHeight - picker.offsetHeight - 12))}px`;
  $("#gift-search").focus();
  refreshGiftCatalog();
}
async function refreshGiftCatalog() {
  if (!initialized || !catalogEditable || creatingGift || catalogRefreshing) return;
  catalogRefreshing = (async () => {
    try {
      const result = await api("/api/gifts");
      acceptCatalogContext(result);
      if (JSON.stringify(result.gifts) !== JSON.stringify(giftCatalog)) {
        giftCatalog = result.gifts;
        if (pickerRowId) renderGiftOptions();
        scheduleColumnSizing();
      }
    } catch { /* 断连时保留当前选项；新增操作会明确显示保存结果。 */ }
    finally { catalogRefreshing = null; }
  })();
  return catalogRefreshing;
}
function renderGiftOptions() {
  const row = rows.find(row => row.id === pickerRowId);
  if (!row) return closeGiftPicker();
  const query = $("#gift-search").value.trim();
  const usage = pickerGiftUsage || recentGiftUsage();
  const candidates = scopedGifts().map((gift, index) => ({ gift, index, count: usage.counts.get(gift.id) || 0, score: query ? giftScore(query, gift) : 1 }))
    .filter(item => item.score >= .45 && inGiftGroup(item.gift.id, selectedGiftGroup)).sort((a, b) => b.score - a.score || b.count - a.count || a.index - b.index);
  $("#gift-sort-hint").textContent = usage.orderCount
    ? `${query ? "匹配优先 · " : ""}按近 ${usage.orderCount} 个订单的礼赠频率排序`
    : "暂无订单使用记录，按清单顺序显示";
  $("#gift-sort-hint").title = `统计当前表格末尾最近 ${recentGiftOrderLimit} 个有礼赠的不同订单；同一订单同一礼赠只计一次，同频保留原清单顺序。`;
  const list = $("#gift-options"); list.replaceChildren();
  for (const id of row.gift_ids.filter(id => !candidates.some(item => item.gift.id === id))) {
    const item = document.createElement("div"), remove = document.createElement("button");
    item.className = "gift-existing"; item.append(document.createTextNode(`${giftNames([id])[0] || id} · ${id}（原已选）`));
    remove.type = "button"; remove.textContent = "移除";
    remove.addEventListener("click", () => { remember(); assignGifts(row, row.gift_ids.filter(code => code !== id), unresolvedGiftLabels(row)); updateGiftRow(row); renderGiftOptions(); });
    item.append(remove); list.append(item);
  }
  for (const { gift, count } of candidates) {
    const label = document.createElement("label"), check = document.createElement("input"), text = document.createElement("span"), code = document.createElement("small");
    label.className = "gift-option"; check.type = "checkbox"; check.value = gift.id; check.checked = row.gift_ids.includes(gift.id);
    text.textContent = gift.name; code.textContent = gift.id;
    check.addEventListener("change", () => {
      remember();
      const ids = check.checked ? [...row.gift_ids, gift.id] : row.gift_ids.filter(id => id !== gift.id);
      const unmatched = unresolvedGiftLabels(row).filter(label => !check.checked ||
        ![gift.id, gift.name, ...(gift.aliases || [])].some(value => normalizeGift(value) === normalizeGift(label)));
      assignGifts(row, ids, unmatched);
      updateGiftRow(row); renderGiftOptions();
    });
    const meta = document.createElement("span"); meta.className = "gift-option-meta"; meta.append(code);
    if (count) {
      const frequency = document.createElement("em"); frequency.className = "gift-frequency";
      frequency.textContent = `${count} 单`; frequency.title = `最近 ${usage.orderCount} 个订单中，有 ${count} 单选择此礼赠`;
      meta.append(frequency);
    }
    label.append(check, text, meta); list.append(label);
  }
  if (!candidates.length) { const empty = document.createElement("p"); empty.className = "gift-no-results"; empty.textContent = scopeInvalid ? "分组或目录已更新，请重新选择礼品分组" : "当前分组未找到，请切换分组"; list.append(empty); }
  const unmatched = unresolvedGiftLabels(row), notice = $("#gift-unmatched"); notice.replaceChildren(); notice.hidden = !unmatched.length;
  if (unmatched.length) {
    const text = document.createElement("span"), clear = document.createElement("button");
    text.textContent = `未匹配：${unmatched.join("、")}`; clear.textContent = "清除未匹配"; clear.type = "button";
    clear.addEventListener("click", () => { remember(); assignGifts(row, row.gift_ids); updateGiftRow(row); renderGiftOptions(); });
    notice.append(text, clear);
  }
  $("#gift-count").textContent = `已选 ${row.gift_ids.length} 项`;
}
function openGiftCreate(fromPicker = false) {
  if (!catalogEditable) return toast("请先重新启动工作台服务，以启用新增礼赠功能", true);
  const suggestion = fromPicker ? $("#gift-search").value.trim() : "";
  createGiftRowId = fromPicker ? pickerRowId : null;
  closeGiftPicker(); finishEditing();
  $("#gift-create-form").reset();
  $("#gift-create-name").value = suggestion;
  $("#gift-create-select-wrap").hidden = !createGiftRowId;
  $("#gift-create-error").hidden = true;
  $("#gift-create-dialog").showModal();
  $("#gift-create-code").focus();
}
function focusAfterGiftCreate(selectedRowId = null) {
  if (selectedRowId && rows.some(row => row.id === selectedRowId)) focusCell(selectedRowId, "gift");
  else $("#add-gift").focus();
}
$("#add-gift").addEventListener("click", () => openGiftCreate());
$("#gift-add-option").addEventListener("click", () => openGiftCreate(true));
$("#gift-create-cancel").addEventListener("click", () => {
  if (!creatingGift) { $("#gift-create-dialog").close(); focusAfterGiftCreate(createGiftRowId); }
});
$("#gift-create-dialog").addEventListener("cancel", event => { if (creatingGift) event.preventDefault(); });
$("#gift-create-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (creatingGift) return;
  creatingGift = true;
  const submit = $("#gift-create-submit"), error = $("#gift-create-error");
  submit.disabled = true; submit.textContent = "正在保存…"; $("#gift-create-cancel").disabled = true;
  $("#gift-create-form").setAttribute("aria-busy", "true"); error.hidden = true;
  try {
    // 先等待其他窗口清单的只读刷新，避免旧响应覆盖此次新增结果。
    if (catalogRefreshing) await catalogRefreshing;
    const gift = { id: $("#gift-create-code").value.trim(), name: $("#gift-create-name").value.trim(),
      aliases: $("#gift-create-aliases").value.split(/[,，、;；\r\n]+/).map(value => value.trim()).filter(Boolean) };
    const result = await api("/api/gifts", { method: "POST", body: JSON.stringify(gift) });
    giftCatalog = result.gifts; catalogVersion = result.catalog_version; persistScope();
    const row = $("#gift-create-select").checked && createGiftRowId ? rows.find(row => row.id === createGiftRowId) : null;
    if (row && !scopeInvalid && inGiftGroup(result.gift.id, selectedGiftGroup)) {
      remember();
      const match = matchGifts(unresolvedGiftLabels(row).join("、"));
      assignGifts(row, [...row.gift_ids, ...match.ids, result.gift.id], match.unmatched); changed();
    }
    render(); $("#gift-create-dialog").close(); focusAfterGiftCreate(row?.id);
    if (row && !row.gift_ids.includes(result.gift.id)) return toast("礼赠已新增；当前分组不包含此编码，请由主机分配分组后重新选择", true);
    toast(`已新增“${result.gift.name}”${row ? "，并选中到当前订单" : "，可在礼赠列表中选择"}`);
  } catch (reason) { error.textContent = reason.message; error.hidden = false; }
  finally {
    creatingGift = false; submit.disabled = false; submit.textContent = "保存礼赠"; $("#gift-create-cancel").disabled = false;
    $("#gift-create-form").removeAttribute("aria-busy");
  }
});
function statusNode(row) {
  const state = status(row), node = document.createElement("span");
  node.className = state.type === "empty" ? "status-empty" : `status-badge ${state.type}`;
  node.textContent = state.label;
  node.title = state.detail || state.label;
  const queued = rowStates[row.id]?.state === "queued" && rowStates[row.id]?.signature === rowSignature(row);
  if (hostActionsAvailable && queued) {
    const wrapper = document.createElement("span"), button = document.createElement("button");
    wrapper.className = "row-status-actions";
    button.type = "button"; button.className = "row-retry row-cancel"; button.textContent = "取消";
    button.title = "仅取消尚未开始处理的排队任务";
    button.setAttribute("aria-label", "取消此单排队"); button.disabled = hostManaging;
    button.addEventListener("mousedown", event => event.stopPropagation());
    button.addEventListener("click", event => { event.stopPropagation(); openCancellation([row.id]); });
    wrapper.append(node, button); return wrapper;
  }
  if (hostActionsAvailable && (state.type === "failed" || state.type === "cancelled" && rowStates[row.id]?.retry_cancelled)) {
    const wrapper = document.createElement("span"), button = document.createElement("button");
    wrapper.className = "row-status-actions";
    button.type = "button"; button.className = "row-retry"; button.textContent = "↻ 重登";
    button.title = "按历史订单和礼品核查后重新登记";
    button.setAttribute("aria-label", "重新登记此单"); button.disabled = retrySubmitting;
    button.addEventListener("mousedown", event => event.stopPropagation());
    button.addEventListener("click", event => { event.stopPropagation(); retryRow(row, button); });
    wrapper.append(node, button); return wrapper;
  }
  return node;
}
function render() {
  finishEditing();
  closeGiftPicker();
  displayedRows = rows.filter(row => !selectedStatus || status(row).type === selectedStatus);
  const displayedIds = new Set(displayedRows.map(row => row.id));
  const visible = columns();
  $("#grid-columns").replaceChildren();
  for (const field of ["row-number", ...visible]) {
    const col = document.createElement("col");
    col.className = field === "row-number" ? field : `col-${field}`;
    $("#grid-columns").append(col);
  }
  const letters = document.createElement("tr"), headings = document.createElement("tr");
  letters.className = "letters";
  headings.className = "field-names";
  const corner = document.createElement("th");
  corner.className = "corner";
  corner.title = "选择全部数据（Ctrl+A）";
  corner.setAttribute("aria-label", "选择全部数据");
  letters.append(corner);
  const rowOne = document.createElement("th");
  rowOne.className = "corner";
  rowOne.textContent = "1";
  headings.append(rowOne);
  visible.forEach((field, index) => {
    const letter = document.createElement("th");
    letter.textContent = String.fromCharCode(65 + index);
    letter.dataset.field = field;
    letter.append(createColumnResizer(field));
    letters.append(letter);
    const heading = document.createElement("th");
    heading.scope = "col";
    heading.dataset.field = field;
    const label = document.createElement("div");
    label.className = "column-label";
    label.textContent = fields[field].label;
    heading.append(label);
    if (field === "status") {
      const filter = document.createElement("select"); filter.id = "status-filter"; filter.setAttribute("aria-label", "按状态筛选");
      filter.title = "仅筛选显示；开始登记仍按完整草稿执行，并跳过已处理订单";
      for (const [value, name] of Object.entries(statusOptions)) filter.add(new Option(name, value));
      filter.value = selectedStatus;
      filter.addEventListener("change", () => { selectedStatus = filter.value; active = null; selection = null; render(); });
      heading.append(filter);
    }
    heading.append(createColumnResizer(field));
    headings.append(heading);
  });
  $("#grid-head").replaceChildren(letters, headings);
  const body = document.createDocumentFragment();
  rows.forEach((row, index) => {
    if (!displayedIds.has(row.id)) return;
    const tr = document.createElement("tr");
    tr.dataset.id = row.id;
    const number = document.createElement("th"), rowButton = document.createElement("button");
    number.scope = "row";
    rowButton.type = "button";
    rowButton.textContent = index + 2;
    rowButton.setAttribute("aria-label", `选中第 ${index + 2} 行`);
    rowButton.addEventListener("click", event => {
      const anchor = event.shiftKey && selection ? selection.anchor.id : row.id;
      selectRange({ id: anchor, field: "order_no" }, { id: row.id, field: "status" });
      focusCell(active.id, active.field, true);
    });
    number.append(rowButton);
    tr.append(number);
    for (const field of visible) {
      const cell = document.createElement("td");
      cell.dataset.field = field;
      cell.setAttribute("role", "gridcell");
      if (field === "status") {
        cell.append(statusNode(row));
        cell.setAttribute("aria-readonly", "true");
        cell.tabIndex = 0;
      } else if (field === "gift") {
        renderGiftCell(cell, row, index);
      } else {
        const control = document.createElement("input");
        control.setAttribute("aria-label", `第 ${index + 2} 行 ${fields[field].label}`);
        control.dataset.field = field;
        {
          control.type = "text";
          control.autocomplete = "off";
          control.spellcheck = false;
          control.maxLength = fields[field].max;
          control.value = row[field];
          control.readOnly = true;
          if (index === 0) control.placeholder = fields[field].placeholder;
          if (field === "order_no") control.className = "order-input";
          let recorded = false;
          control.addEventListener("focus", () => { recorded = false; });
          control.addEventListener("editstart", () => { recorded = false; });
          control.addEventListener("input", () => {
            if (!recorded) { remember(); recorded = true; }
            row[field] = field === "order_no" ? control.value.trim() : control.value;
            tr.querySelector('[data-field="status"]').replaceChildren(statusNode(row));
            $("#formula-input").value = control.value;
            changed();
          });
          control.addEventListener("blur", () => { if (editing?.id === row.id && editing.field === field) finishEditing(); });
        }
        control.addEventListener("focus", () => activate(row.id, field));
        control.addEventListener("paste", handlePaste);
        control.addEventListener("keydown", (event) => handleNavigation(event, row.id, field));
        cell.append(control);
      }
      if (field === "status") cell.addEventListener("focus", () => activate(row.id, field));
      tr.append(cell);
    }
    body.append(tr);
  });
  $("#grid-body").replaceChildren(body);
  rowNodes = new Map([...$("#grid-body").children].filter(node => node.dataset.id).map(node => [node.dataset.id, node]));
  selectedNodes.clear();
  if (selection && !rangeBounds()) selection = null;
  updateSummary();
  updateDuplicateOrders();
  if (active && displayedIds.has(active.id)) activate(active.id, visible.includes(active.field) ? active.field : "order_no");
  else {
    active = null;
    selection = null;
    $("#delete-row").disabled = true;
    $("#formula-input").value = "";
    $("#formula-input").disabled = true;
    $("#cell-reference").textContent = "—";
    $("#selection-summary").textContent = "未选中单元格";
  }
  $("#undo").disabled = history.length === 0;
  scheduleColumnSizing();
}
function rowElement(id) { return rowNodes.get(id); }
function fieldElement(id, field) {
  return rowElement(id)?.querySelector(`td[data-field="${field}"]`);
}
function rangeBounds() {
  if (!selection) return null;
  const start = gridRows().findIndex(row => row.id === selection.anchor.id), end = gridRows().findIndex(row => row.id === selection.focus.id);
  const left = columns().indexOf(selection.anchor.field), right = columns().indexOf(selection.focus.field);
  if (Math.min(start, end, left, right) < 0) return null;
  return { top: Math.min(start, end), bottom: Math.max(start, end), left: Math.min(left, right), right: Math.max(left, right) };
}
function cellReference(row, column) { return `${String.fromCharCode(65 + column)}${row + 2}`; }
function paintSelection(keepHandle = false) {
  if (!keepHandle) document.querySelectorAll(".fill-handle").forEach(node => node.remove());
  for (const node of selectedNodes) {
    node.classList.remove("selected-cell", "range-top", "range-bottom", "range-left", "range-right", "selected-gutter");
    node.removeAttribute("aria-selected");
  }
  selectedNodes.clear();
  document.querySelectorAll(".active-cell,.active-row,.active-column").forEach(node => node.classList.remove("active-cell", "active-row", "active-column"));
  if (!active) return;
  rowElement(active.id)?.classList.add("active-row");
  fieldElement(active.id, active.field)?.classList.add("active-cell");
  const bounds = rangeBounds();
  if (bounds) {
    if (bounds.left < columns().indexOf("status") && !document.querySelector(".fill-handle")) {
      const handle = document.createElement("button");
      handle.type = "button"; handle.className = "fill-handle"; handle.tabIndex = -1;
      handle.title = "向下拖动填充；双击填充至相邻列末行；Ctrl+D 向下填充选区";
      handle.setAttribute("aria-label", "向下填充选区");
      fieldElement(gridRows()[bounds.bottom].id, columns()[bounds.right])?.append(handle);
    }
    for (let row = bounds.top; row <= bounds.bottom; row++) {
      const tr = rowElement(gridRows()[row].id);
      tr.firstElementChild.classList.add("selected-gutter"); selectedNodes.add(tr.firstElementChild);
      for (let col = bounds.left; col <= bounds.right; col++) {
        const cell = tr.children[col + 1];
        cell.classList.add("selected-cell"); cell.setAttribute("aria-selected", "true");
        for (const [edge, enabled] of Object.entries({ top: row === bounds.top, bottom: row === bounds.bottom, left: col === bounds.left, right: col === bounds.right })) {
          if (enabled) cell.classList.add(`range-${edge}`);
        }
        selectedNodes.add(cell);
      }
    }
    for (let col = bounds.left; col <= bounds.right; col++) {
      $("#grid-head").querySelector(`.letters [data-field="${columns()[col]}"]`)?.classList.add("active-column");
    }
    const first = cellReference(rows.indexOf(gridRows()[bounds.top]), bounds.left), last = cellReference(rows.indexOf(gridRows()[bounds.bottom]), bounds.right);
    $("#cell-reference").textContent = first === last ? first : `${first}:${last}`;
    const height = bounds.bottom - bounds.top + 1, width = bounds.right - bounds.left + 1;
    $("#selection-summary").textContent = height * width > 1 ? `已选 ${height} 行 × ${width} 列 · ${height * width} 个单元格` : `第 ${rows.indexOf(gridRows()[bounds.top]) + 2} 行 · ${fields[columns()[bounds.left]].label}`;
  } else {
    const index = rows.findIndex(row => row.id === active.id);
    $("#cell-reference").textContent = `${fields[active.field].label} ${index + 2}`;
    $("#selection-summary").textContent = `第 ${index + 2} 行 · ${fields[active.field].label}`;
  }
}
function selectRange(anchor, focus = anchor) {
  finishEditing();
  selection = { anchor: { ...anchor }, focus: { ...focus } };
  $("#registry-grid").classList.remove("is-copied");
  activate(anchor.id, anchor.field, true);
}
function activate(id, field, keepSelection = false) {
  const index = rows.findIndex(row => row.id === id);
  if (index === -1) return;
  if (!keepSelection && (!selection || selection.anchor.id !== id || selection.anchor.field !== field)) {
    selection = columns().includes(field) ? { anchor: { id, field }, focus: { id, field } } : null;
    $("#registry-grid").classList.remove("is-copied");
  }
  active = { id, field };
  paintSelection();
  $("#delete-row").disabled = false;
  const row = rows[index];
  $("#formula-input").disabled = field === "status";
  $("#formula-input").value = field === "status" ? status(row).label : row[field];
}
function scrollCellIntoView(id, field) {
  const cell = fieldElement(id, field);
  cell?.scrollIntoView({ block: "nearest", inline: "nearest" });
  const scroller = $("#grid-scroll");
  if (cell) {
    const top = scroller.getBoundingClientRect().top + $("#grid-head").offsetHeight;
    if (cell.getBoundingClientRect().top < top) scroller.scrollTop -= top - cell.getBoundingClientRect().top;
  }
}
function focusCell(id, field, keepSelection = false) {
  const cell = fieldElement(id, field);
  if (!keepSelection && columns().includes(field)) selectRange({ id, field });
  const control = cell?.querySelector("input,.gift-trigger,.address-toggle") || cell;
  control?.focus({ preventScroll: true });
  activate(id, field, keepSelection);
  scrollCellIntoView(id, field);
}
function finishEditing(cancel = false) {
  if (!editing) return;
  const current = editing; editing = null;
  const input = fieldElement(current.id, current.field)?.querySelector("input");
  if (input) { input.readOnly = true; input.closest("td")?.classList.remove("is-editing"); }
  const row = rows.find(row => row.id === current.id);
  if (input && row && current.field === "order_no") {
    input.value = cancel ? current.original : row.order_no;
    if (active?.id === current.id && active.field === current.field) $("#formula-input").value = input.value;
  }
  if (cancel) {
    if (row && row[current.field] !== current.original) {
      row[current.field] = current.original; if (input) input.value = current.original;
      rowElement(row.id)?.querySelector('[data-field="status"]').replaceChildren(statusNode(row));
      $("#formula-input").value = current.original; changed();
    }
  }
}
function beginEditing(id, field, replace = false) {
  const row = rows.find(row => row.id === id), cell = fieldElement(id, field);
  if (!row || field === "status") return;
  selectRange({ id, field }); focusCell(id, field, true);
  if (field === "gift") return openGiftPicker(row, cell.querySelector(".gift-trigger"));
  const input = cell.querySelector("input");
  if (!input) return;
  editing = { id, field, original: row[field] }; input.readOnly = false;
  input.dispatchEvent(new Event("editstart"));
  cell.classList.add("is-editing");
  if (replace) input.select(); else input.setSelectionRange(input.value.length, input.value.length);
}
function addRows(count = 1) {
  if (!Number.isInteger(count) || count < 1 || count > 10000) return toast("请输入 1 到 10000 之间的整数行数", true);
  if (rows.length + count > 10000) return toast(`最多支持 10000 行，当前还能新增 ${10000 - rows.length} 行`, true);
  remember();
  selectedStatus = "";
  const added = Array.from({ length: count }, newRow);
  rows.push(...added);
  changed(); render(); focusCell(added[0].id, "order_no");
  toast(`已新增 ${count} 行`);
}
function pointAt(clientX, clientY) {
  const scroller = $("#grid-scroll"), rect = scroller.getBoundingClientRect();
  const x = Math.max(rect.left + 2, Math.min(rect.right - 14, clientX));
  const y = Math.max(rect.top + $("#grid-head").offsetHeight + 2, Math.min(rect.bottom - 14, clientY));
  const target = document.elementFromPoint(x, y), cell = target?.closest('tr[data-id] td[data-field]');
  if (cell) return { id: cell.parentElement.dataset.id, field: cell.dataset.field };
  const detail = target?.closest("tr[data-for-row]");
  if (detail) {
    const main = rowElement(detail.dataset.forRow);
    for (const cell of [...main.children].slice(1)) {
      const box = cell.getBoundingClientRect();
      if (x >= box.left && x <= box.right) return { id: main.dataset.id, field: cell.dataset.field };
    }
  }
  return null;
}
function extendDrag() {
  if (!dragSelection?.moved) return;
  const point = pointAt(dragSelection.x, dragSelection.y);
  if (point && (point.id !== selection.focus.id || point.field !== selection.focus.field)) {
    selection.focus = point; paintSelection();
  }
}
function scrollWhileSelecting() {
  if (!dragSelection) { dragFrame = null; return; }
  if (dragSelection.moved) {
    const grid = $("#grid-scroll"), rect = grid.getBoundingClientRect(), { x, y } = dragSelection;
    const top = rect.top + $("#grid-head").offsetHeight;
    const speed = (value, low, high) => value < low + 24 ? -Math.min(32, Math.ceil((low + 24 - value) / 3)) : value > high - 24 ? Math.min(32, Math.ceil((value - high + 24) / 3)) : 0;
    grid.scrollLeft += speed(x, rect.left + 46, rect.right - 12);
    grid.scrollTop += speed(y, top, rect.bottom - 12);
    extendDrag();
  }
  dragFrame = requestAnimationFrame(scrollWhileSelecting);
}
$("#registry-grid").addEventListener("pointerdown", event => {
  if (event.target.closest(".fill-handle")) return;
  if (!initialized || event.button !== 0 || event.target.closest(".column-resizer,select")) return;
  const header = event.target.closest("thead th");
  if (header) {
    if (!gridRows().length) return;
    event.preventDefault(); closeGiftPicker();
    const field = header.dataset.field;
    const first = event.shiftKey && selection && field ? selection.anchor.field : field || "order_no";
    selectRange({ id: gridRows()[0].id, field: first }, { id: gridRows().at(-1).id, field: field || "status" });
    focusCell(active.id, active.field, true); return;
  }
  const cell = event.target.closest('tr[data-id] td[data-field]');
  if (!cell) return;
  const point = { id: cell.parentElement.dataset.id, field: cell.dataset.field };
  if (editing?.id === point.id && editing.field === point.field && !event.shiftKey) return;
  event.preventDefault(); closeGiftPicker();
  if (event.shiftKey && selection) { selectRange(selection.anchor, point); suppressCellClick = true; }
  else selectRange(point);
  focusCell(active.id, active.field, true);
  dragSelection = { pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, x: event.clientX, y: event.clientY, moved: false };
  document.body.classList.add("range-selecting");
  if (dragFrame === null) dragFrame = requestAnimationFrame(scrollWhileSelecting);
}, true);
document.addEventListener("pointermove", event => {
  if (dragSelection?.pointerId !== event.pointerId) return;
  dragSelection.x = event.clientX; dragSelection.y = event.clientY;
  if (Math.hypot(event.clientX - dragSelection.startX, event.clientY - dragSelection.startY) > 5) dragSelection.moved = true;
  if (dragSelection.moved) { event.preventDefault(); extendDrag(); }
});
function stopSelecting(event) {
  if (dragSelection?.pointerId !== event.pointerId) return;
  suppressCellClick ||= dragSelection.moved;
  dragSelection = null; document.body.classList.remove("range-selecting");
  if (dragFrame !== null) { cancelAnimationFrame(dragFrame); dragFrame = null; }
  setTimeout(() => { suppressCellClick = false; }, 0);
}
document.addEventListener("pointerup", stopSelecting);
document.addEventListener("pointercancel", stopSelecting);
window.addEventListener("blur", () => { if (dragSelection) stopSelecting({ pointerId: dragSelection.pointerId }); });
$("#registry-grid").addEventListener("click", event => {
  if (suppressCellClick || (event.shiftKey && event.target.closest('tr[data-id] td'))) { event.preventDefault(); event.stopPropagation(); }
}, true);
$("#registry-grid").addEventListener("dblclick", event => {
  const cell = event.target.closest('tr[data-id] td[data-field="order_no"]');
  if (cell) { event.preventDefault(); beginEditing(cell.parentElement.dataset.id, "order_no"); }
});
$("#registry-grid").addEventListener("keydown", event => {
  if (!active || !columns().includes(active.field) || event.target.closest(".column-resizer,.address-details,select") || event.isComposing) return;
  const modifier = event.ctrlKey || event.metaKey;
  if (modifier && event.key.toLowerCase() === "d" && !editing) {
    event.preventDefault(); event.stopPropagation(); fillSelectedDown(); return;
  }
  if (modifier && event.key.toLowerCase() === "a" && !editing) {
    event.preventDefault(); event.stopPropagation(); closeGiftPicker();
    selectRange({ id: gridRows()[0].id, field: "order_no" }, { id: gridRows().at(-1).id, field: "status" });
    focusCell(active.id, active.field, true); return;
  }
  if (editing && !["Enter", "Tab", "Escape"].includes(event.key)) return;
  if (event.key === "Escape") {
    event.preventDefault(); event.stopPropagation();
    if (!editing && document.body.classList.contains("sheet-expanded")) { toggleSheetFullscreen(); return; }
    finishEditing(true);
    selectRange(active); focusCell(active.id, active.field, true); return;
  }
  if (["Delete", "Backspace"].includes(event.key) && !editing) {
    event.preventDefault(); event.stopPropagation(); clearSelectedCells(); return;
  }
  if (event.key === "F2") { event.preventDefault(); event.stopPropagation(); beginEditing(active.id, active.field); return; }
  const arrows = { ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1] };
  if (arrows[event.key] || ["Tab", "Enter", "Home", "End"].includes(event.key)) {
    event.preventDefault(); event.stopPropagation(); finishEditing(); closeGiftPicker();
    const extending = event.shiftKey && event.key in arrows;
    const origin = extending && selection ? selection.focus : active;
    let row = gridRows().findIndex(item => item.id === origin.id), col = columns().indexOf(origin.field);
    if (event.key in arrows) { row += arrows[event.key][0]; col += arrows[event.key][1]; }
    if (event.key === "Enter") row += event.shiftKey ? -1 : 1;
    if (event.key === "Tab") { col += event.shiftKey ? -1 : 1; if (col >= columns().length) { row++; col = 0; } if (col < 0 && row > 0) { row--; col = columns().length - 1; } }
    if (event.key === "Home") { col = 0; if (modifier) row = 0; }
    if (event.key === "End") { col = columns().length - 1; if (modifier) row = gridRows().length - 1; }
    if (!selectedStatus && row >= gridRows().length && ["Tab", "Enter"].includes(event.key) && rows.length < 10000) { remember(); rows.push(newRow()); changed(); render(); }
    row = Math.max(0, Math.min(gridRows().length - 1, row)); col = Math.max(0, Math.min(columns().length - 1, col));
    const point = { id: gridRows()[row].id, field: columns()[col] };
    if (extending) { selectRange(selection?.anchor || active, point); focusCell(active.id, active.field, true); scrollCellIntoView(point.id, point.field); }
    else focusCell(point.id, point.field);
    return;
  }
  if (!modifier && !event.altKey && (event.key.length === 1 || event.key === "Process")) {
    if (active.field === "order_no") beginEditing(active.id, active.field, true);
    else if (active.field === "gift") {
      event.preventDefault(); event.stopPropagation(); beginEditing(active.id, active.field);
      if (event.key.trim()) { $("#gift-search").value = event.key; renderGiftOptions(); }

    }
  }
}, true);
function handleNavigation(event, id, field) {
  if (event.isComposing) return;
  const index = gridRows().findIndex(row => row.id === id);
  if (["Enter", "ArrowUp", "ArrowDown"].includes(event.key) && !(event.key === "Enter" && event.target.tagName === "BUTTON")) {
    event.preventDefault();
    const next = index + ((event.key === "ArrowUp" || (event.key === "Enter" && event.shiftKey)) ? -1 : 1);
    if (!selectedStatus && next >= gridRows().length && rows.length < 10000) { remember(); rows.push(newRow()); changed(); render(); }
    if (next >= 0 && next < gridRows().length) focusCell(gridRows()[next].id, field);
  }
}
function parseTSV(text) {
  const matrix = []; let line = [], cell = "", quoted = false;
  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    if (char === '"' && (quoted || cell === "")) {
      if (quoted && text[i + 1] === '"') { cell += '"'; i++; } else quoted = !quoted;
    } else if (!quoted && (char === "\t" || char === "\n" || char === "\r")) {
      line.push(cell); cell = "";
      if (char !== "\t") { matrix.push(line); line = []; if (char === "\r" && text[i + 1] === "\n") i++; }
    } else cell += char;
  }
  line.push(cell); matrix.push(line);
  // 只去掉 Excel 附加的末尾换行，不丢弃区域内有意义的空行。
  if (!quoted && /[\r\n]$/.test(text) && matrix.at(-1).length === 1 && matrix.at(-1)[0] === "") matrix.pop();
  return matrix;
}
function selectionMatrix() {
  const bounds = rangeBounds();
  if (!bounds) return null;
  return gridRows().slice(bounds.top, bounds.bottom + 1).map(row => columns().slice(bounds.left, bounds.right + 1).map(field =>
    field === "status" ? status(row).label : row[field]));
}
function clipboardContent() {
  const matrix = selectionMatrix();
  if (!matrix) return null;
  const bounds = rangeBounds();
  const giftIds = gridRows().slice(bounds.top, bounds.bottom + 1).map(row => columns().slice(bounds.left, bounds.right + 1)
    .map(field => field === "gift" && validGifts(row) ? [...row.gift_ids] : null));
  const payload = JSON.stringify({ version: 2, matrix, giftIds });
  const text = matrix.map(line => line.map(value => /[\t\r\n"]/.test(value) ? `"${value.replaceAll('"', '""')}"` : value).join("\t")).join("\r\n");
  const encoded = btoa(Array.from(new TextEncoder().encode(payload), byte => String.fromCharCode(byte)).join(""));
  const table = document.createElement("table"); table.dataset.giftRegistryRange = encoded;
  for (const line of matrix) {
    const tr = table.insertRow();
    for (const value of line) {
      const td = tr.insertCell(); td.textContent = value;
      // Excel 的 HTML 剪贴板格式：长订单号按文本导入，保留前导零。
      td.setAttribute("x:str", "");
    }
  }
  // Excel 专用格式仅写入剪贴板字符串，不在工作台 DOM 应用内联样式（CSP）。
  const tableHtml = table.outerHTML.replaceAll("<td ", '<td style="mso-number-format:&quot;\\@&quot;;white-space:pre-wrap" ');
  return { text, json: payload, html: `<html xmlns:x="urn:schemas-microsoft-com:office:excel"><body><!--StartFragment-->${tableHtml}<!--EndFragment--></body></html>` };
}
function structuredClipboard(data) {
  try {
    let value = data.getData(rangeClipboardType);
    if (!value) {
      const encoded = data.getData("text/html").match(/data-gift-registry-range="([A-Za-z0-9+/=]+)"/)?.[1];
      if (encoded) value = new TextDecoder().decode(Uint8Array.from(atob(encoded), char => char.charCodeAt(0)));
    }
    if (!value) return null;
    const payload = JSON.parse(value), matrix = payload.version === 2 ? payload.matrix : payload;
    if (!(Array.isArray(matrix) && matrix.length > 0 && matrix.length <= 10000 && matrix.every(line => Array.isArray(line) && line.length > 0 && line.length <= 7 && line.every(cell => typeof cell === "string")))) return null;
    if (payload.version === 2 && Array.isArray(payload.giftIds)) matrix.giftIds = payload.giftIds;
    return matrix;
  } catch { return null; }
}
function textEditingTarget(target) {
  return target?.closest?.("#formula-input,#gift-picker") || (target?.tagName === "INPUT" && !target.readOnly);
}
function markCopied() { $("#registry-grid").classList.add("is-copied"); toast("已复制选中区域，可粘贴到 Excel 或其他单元格"); }
document.addEventListener("copy", event => {
  if (!initialized || (!forceRangeCopy && (!event.target.closest?.("#registry-grid") || textEditingTarget(event.target)))) return;
  const content = clipboardContent();
  if (!content || !event.clipboardData) return;
  event.preventDefault(); event.clipboardData.setData("text/plain", content.text); event.clipboardData.setData("text/html", content.html);
  try { event.clipboardData.setData(rangeClipboardType, content.json); } catch { /* HTML 同时携带区域边界及空白单元格。 */ }
  markCopied();
});
async function copySelectedRange() {
  finishEditing();
  const content = clipboardContent();
  if (!content) return toast("请先选择要复制的单元格区域");
  try {
    if (typeof ClipboardItem !== "undefined" && navigator.clipboard?.write) {
      await navigator.clipboard.write([new ClipboardItem({ "text/plain": new Blob([content.text], { type: "text/plain" }), "text/html": new Blob([content.html], { type: "text/html" }) })]);
    } else await navigator.clipboard.writeText(content.text);
    markCopied();
  } catch {
    forceRangeCopy = true;
    try { if (!document.execCommand("copy")) throw new Error(); }
    catch { focusCell(active.id, active.field, true); toast("请按 Ctrl+C 复制选中区域"); }
    finally { forceRangeCopy = false; }
  }
}
let fillDrag = null, fillFrame = null;
function fillPlan(bounds) {
  return {bounds: {...bounds}, version, scope: JSON.stringify(currentScope()),
    visibleIds: gridRows().map(row => row.id), filtered: Boolean(selectedStatus),
    source: structuredClone(gridRows().slice(bounds.top, bounds.bottom + 1))};
}
function applyFill(plan, end) {
  const {bounds, source, visibleIds} = plan;
  if (end <= bounds.bottom) return;
  try {
    if (plan.version !== version || plan.scope !== JSON.stringify(currentScope()) ||
        JSON.stringify(visibleIds) !== JSON.stringify(gridRows().map(row => row.id))) throw new Error("填充期间表格或目录已变化，请重新选择");
    if (end >= 10000 || (plan.filtered && end >= visibleIds.length)) throw new Error("填充超出当前可用行，请先清除筛选或减少范围");
    const extra = Math.max(0, end + 1 - visibleIds.length);
    if (rows.length + extra > 10000) throw new Error("填充后将超过 10000 行");
    const updated = structuredClone(rows), targets = [];
    for (let index = bounds.bottom + 1; index <= end; index++) {
      let target = index < visibleIds.length ? updated.findIndex(row => row.id === visibleIds[index]) : updated.length;
      if (target === updated.length) updated.push(newRow());
      const original = source[(index - bounds.bottom - 1) % source.length], row = updated[target];
      for (let col = bounds.left; col <= bounds.right; col++) {
        const field = columns()[col];
        if (field === "gift") {
          const ids = original.gift_ids || [];
          ensureGiftScope(ids);
          assignGifts(row, ids, unresolvedGiftLabels(original));
        } else if (field === "order_no") row.order_no = original.order_no.trim();
      }
      targets.push(row);
    }
    if (JSON.stringify(updated) === JSON.stringify(rows)) return;
    remember(); rows = updated; changed();
    selection = {anchor: {id: visibleIds[bounds.top], field: columns()[bounds.left]},
      focus: {id: targets.at(-1).id, field: columns()[bounds.right]}};
    active = selection.anchor;
    render(); focusCell(active.id, active.field, true);
    toast("已向下填充 " + targets.length + " 行，单元格内容按原文复制，可按 Ctrl+Z 撤销");
  } catch (error) { toast(error.message, true); }
}
function fillSelectedDown() {
  const bounds = rangeBounds();
  if (!bounds || bounds.bottom <= bounds.top) return toast("请先选中来源行和下方要填充的区域");
  if (columns()[bounds.left] === "status") return toast("状态由登记结果更新，不能填充");
  finishEditing(); closeGiftPicker();
  applyFill(fillPlan({...bounds, bottom: bounds.top}), bounds.bottom);
}
function previewFill() {
  if (!fillDrag) return;
  document.querySelectorAll(".fill-preview").forEach(node => node.classList.remove("fill-preview"));
  const point = pointAt(fillDrag.x, fillDrag.y);
  let end = point ? gridRows().findIndex(row => row.id === point.id) : fillDrag.plan.bounds.bottom;
  const last = rowElement(gridRows().at(-1)?.id)?.getBoundingClientRect();
  const rect = $("#grid-scroll").getBoundingClientRect();
  if (!fillDrag.plan.filtered && last && fillDrag.y > last.bottom && last.bottom < rect.bottom) {
    end = gridRows().length - 1 + Math.max(1, Math.ceil((Math.min(fillDrag.y, rect.bottom - 2) - last.bottom) / Math.max(24, last.height)));
  }
  fillDrag.end = Math.min(9999, Math.max(fillDrag.plan.bounds.bottom, end));
  const {bounds} = fillDrag.plan;
  for (let row = bounds.bottom + 1; row <= Math.min(fillDrag.end, gridRows().length - 1); row++) {
    for (let col = bounds.left; col <= bounds.right; col++) {
      if (columns()[col] !== "status") fieldElement(gridRows()[row].id, columns()[col])?.classList.add("fill-preview");
    }
  }
  $("#selection-summary").textContent = "向下填充 " + (fillDrag.end - bounds.bottom) + " 行 · 松开应用，Esc 取消";
}
function scrollWhileFilling() {
  if (!fillDrag) { fillFrame = null; return; }
  const grid = $("#grid-scroll"), rect = grid.getBoundingClientRect();
  const lower = rect.bottom - 20, upper = rect.top + $("#grid-head").offsetHeight + 20;
  if (fillDrag.y > lower) grid.scrollTop += Math.min(28, (fillDrag.y - lower) / 2);
  if (fillDrag.y < upper) grid.scrollTop -= Math.min(28, (upper - fillDrag.y) / 2);
  previewFill(); fillFrame = requestAnimationFrame(scrollWhileFilling);
}
function stopFilling(commit = false) {
  if (!fillDrag) return;
  const drag = fillDrag; fillDrag = null;
  if (fillFrame !== null) cancelAnimationFrame(fillFrame);
  fillFrame = null; document.body.classList.remove("range-filling");
  document.querySelectorAll(".fill-preview").forEach(node => node.classList.remove("fill-preview"));
  if (commit) applyFill(drag.plan, drag.end);
  paintSelection(drag.end === drag.plan.bounds.bottom);
  setTimeout(() => { suppressCellClick = false; }, 0);
}
$("#registry-grid").addEventListener("pointerdown", event => {
  if (!event.target.closest(".fill-handle") || event.button !== 0) return;
  event.preventDefault(); event.stopPropagation(); finishEditing(); closeGiftPicker();
  const bounds = rangeBounds(); if (!bounds) return;
  suppressCellClick = true;
  fillDrag = {pointerId: event.pointerId, x: event.clientX, y: event.clientY, end: bounds.bottom, plan: fillPlan(bounds)};
  document.body.classList.add("range-filling"); fillFrame = requestAnimationFrame(scrollWhileFilling);
}, true);
document.addEventListener("pointermove", event => {
  if (fillDrag?.pointerId !== event.pointerId) return;
  event.preventDefault(); fillDrag.x = event.clientX; fillDrag.y = event.clientY; previewFill();
});
document.addEventListener("pointerup", event => { if (fillDrag?.pointerId === event.pointerId) stopFilling(true); });
document.addEventListener("pointercancel", event => { if (fillDrag?.pointerId === event.pointerId) stopFilling(); });
window.addEventListener("blur", () => stopFilling());
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && fillDrag) { event.preventDefault(); event.stopImmediatePropagation(); stopFilling(); }
}, true);
$("#registry-grid").addEventListener("dblclick", event => {
  if (!event.target.closest(".fill-handle")) return;
  event.preventDefault(); event.stopPropagation();
  const bounds = rangeBounds(); if (!bounds) return;
  const adjacent = columns().filter((field, index) => field !== "status" && (index < bounds.left || index > bounds.right));
  let end = bounds.bottom;
  while (end + 1 < gridRows().length && adjacent.some(field => Boolean(gridRows()[end + 1][field]?.trim()))) end++;
  if (end === bounds.bottom) return toast("相邻列没有连续内容，请拖动填充柄指定范围");
  applyFill(fillPlan(bounds), end);
}, true);
function asBoolean(value) {
  const normalized = value.trim().toLowerCase();
  if (["是", "true", "1", "yes"].includes(normalized)) return true;
  if (["否", "false", "0", "no", ""].includes(normalized)) return false;
  throw new Error("旧复制格式的地址标志无效，未粘贴");
}
function pasteText(text, clipboardMatrix = null) {
  if (!initialized) return;
  finishEditing(); closeGiftPicker();
  let matrix = clipboardMatrix || parseTSV(text);
  if (matrix[0]?.[0]?.trim() === "订单号" && ["礼品", "礼赠", "赠品"].includes(matrix[0]?.[1]?.trim())) { matrix.shift(); matrix.giftIds?.shift(); }
  if (!matrix.length) return;
  const bounds = rangeBounds();
  const start = bounds?.top ?? (active ? Math.max(0, gridRows().findIndex(row => row.id === active.id)) : 0);
  const visible = columns();
  const originalWidth = Math.max(...matrix.map(line => line.length));
  const legacy = [4, 7].includes(originalWidth) && (!bounds || bounds.left === 0) && (!active || active.field === "order_no");
  if (legacy) {
    try { if (matrix.some(line => asBoolean(line[2] || ""))) return toast("旧复制内容包含不支持的改地址请求，本次未粘贴；请核对原意", true); }
    catch (error) { return toast(error.message, true); }
    const giftIds = matrix.giftIds;
    matrix = matrix.map(line => [line[0], line[1], line[originalWidth - 1] || ""]);
    if (giftIds) matrix.giftIds = giftIds.map(line => [line?.[0], line?.[1], null]);
  }
  const sourceWidth = Math.max(...matrix.map(line => line.length));
  const mapping = visible;
  const colStart = mapping === visible ? bounds?.left ?? Math.max(0, visible.indexOf(active?.field)) : Math.max(0, mapping.indexOf(active?.field));
  if (sourceWidth > mapping.length - colStart) return toast("粘贴的列数超出表格范围，请重新选择起始单元格", true);
  let height = matrix.length, width = sourceWidth;
  if (bounds && !legacy) {
    const selectedHeight = bounds.bottom - bounds.top + 1, selectedWidth = bounds.right - bounds.left + 1;
    // 单个起点按来源扩展；区域粘贴按各维较大范围循环填充，末尾不足一组也填满。
    height = Math.max(height, selectedHeight); width = Math.max(width, selectedWidth);
  }
  if (start + height > 10000) return toast("粘贴后将超过 10000 行，请分批录入", true);
  if (selectedStatus && start + height > gridRows().length) return toast("筛选中只能粘贴到可见行；请清除状态筛选后再新增行", true);
  if (mapping.slice(colStart, colStart + width).every(field => field === "status")) return toast("状态列由登记结果自动更新，不能手动填写");
  const updated = structuredClone(rows);
  const targets = Array.from({ length: height }, (_, offset) => selectedStatus ? rows.indexOf(gridRows()[start + offset]) : start + offset);
  try {
    for (let offset = 0; offset < height; offset++) {
      const target = targets[offset];
      while (updated.length <= target) updated.push(newRow());
      for (let column = 0; column < width; column++) {
        const field = mapping[colStart + column];
        const raw = matrix[offset % matrix.length][column % sourceWidth] ?? "";
        const value = field === "order_no" ? raw.trim() : raw;
        if (!field || field === "status") continue;
        if (field === "gift") {
          const codes = matrix.giftIds?.[offset % matrix.length]?.[column % sourceWidth];
          if (Array.isArray(codes) && codes.length) { ensureGiftScope(codes); assignGifts(updated[target], codes); }
          else { const match = matchGifts(value); assignGifts(updated[target], match.ids, match.unmatched); }
          if (updated[target].gift.length > fields.gift.max) throw new Error("礼品内容过长，请减少单行礼品数量");
        }
        else {
          if (value.length > fields[field].max) throw new Error(`“${fields[field].label}”内容过长，请检查粘贴的数据`);
          updated[target][field] = value;
        }
      }
    }
    remember(); rows = updated; changed();
    if (mapping === visible || legacy) selection = { anchor: { id: rows[targets[0]].id, field: visible[legacy ? 0 : colStart] }, focus: { id: rows[targets.at(-1)].id, field: visible[Math.min(visible.length - 1, colStart + width - 1)] } };
    active = selection?.anchor || { id: rows[targets[0]].id, field: mapping[colStart] };
    render(); if (active) focusCell(active.id, active.field, true);
    const unmatched = targets.map(index => updated[index]).filter(row => row.gift && !validGifts(row)).length;
    toast(`已粘贴 ${height} 行 × ${width} 列${unmatched ? `，其中 ${unmatched} 行礼品需手动匹配` : ""}${mapping.slice(colStart, colStart + width).includes("status") ? "；状态列保持自动更新" : ""}`, unmatched > 0);
  } catch (error) { toast(error.message, true); }
}
function handlePaste(event) {
  if (event.defaultPrevented || !event.clipboardData) return;
  const text = event.clipboardData.getData("text/plain");
  if (textEditingTarget(event.target)) return;
  if (!event.clipboardData.types.includes("text/plain") && !event.clipboardData.types.includes("text/html")) return;
  event.preventDefault(); pasteText(text, structuredClipboard(event.clipboardData));
}
$("#registry-grid").addEventListener("paste", handlePaste);
function clearSelectedCells() {
  const bounds = rangeBounds();
  if (!bounds) return;
  if (columns()[bounds.left] === "status") return toast("状态列由登记结果自动更新，不能手动清空");
  finishEditing(); closeGiftPicker();
  const updated = structuredClone(rows);
  for (let index = bounds.top; index <= bounds.bottom; index++) {
    const target = rows.indexOf(gridRows()[index]);
    for (let col = bounds.left; col <= bounds.right; col++) {
      const field = columns()[col];
      if (field === "gift") assignGifts(updated[target], []);
      else if (field !== "status") updated[target][field] = "";
    }
  }
  if (JSON.stringify(updated) === JSON.stringify(rows)) return;
  remember(); rows = updated; changed(); render(); if (active) focusCell(active.id, active.field, true);
  toast("已清空选区内容，可按 Ctrl+Z 撤销");
}

$("#add-row").addEventListener("click", () => addRows(Number($("#add-count").value)));
$("#add-count").addEventListener("keydown", event => { if (event.key === "Enter") addRows(Number(event.target.value)); });
$("#add-bottom").addEventListener("click", () => addRows(1));
$("#save").addEventListener("click", () => saveNow(true));
$("#auto-fit-columns").addEventListener("click", () => { resetColumnWidth(); toast("已根据内容自动调整所有列宽"); });
window.addEventListener("resize", scheduleColumnSizing);
window.addEventListener("focus", refreshGiftCatalog);
if (typeof ResizeObserver !== "undefined") new ResizeObserver(scheduleColumnSizing).observe($("#grid-scroll"));
if (document.fonts?.ready) document.fonts.ready.then(scheduleColumnSizing);
$("#reload").addEventListener("click", () => location.reload());
$("#delete-row").addEventListener("click", () => {
  if (!active) return;
  const bounds = rangeBounds(), index = bounds?.top ?? gridRows().findIndex(row => row.id === active.id);
  const removed = new Set(gridRows().slice(index, bounds ? bounds.bottom + 1 : index + 1).map(row => row.id));
  remember(); rows = rows.filter(row => !removed.has(row.id));
  if (!rows.length) rows.push(newRow());
  selection = null;
  active = { id: rows[Math.min(index, rows.length - 1)].id, field: "order_no" };
  changed(); render(); if (active) focusCell(active.id, active.field);
});
function undo() {
  if (!history.length) return;
  rows = history.pop(); changed(); render();
  if (active) focusCell(active.id, active.field, true);
}
$("#undo").addEventListener("click", undo);
$("#copy-range").addEventListener("click", copySelectedRange);
$("#paste").addEventListener("click", async () => {
  try {
    if (navigator.clipboard.read) {
      const items = await navigator.clipboard.read(), content = new Map();
      for (const item of items) for (const type of ["text/plain", "text/html"]) {
        if (item.types.includes(type) && !content.has(type)) content.set(type, await (await item.getType(type)).text());
      }
      if (!content.size) return toast("剪贴板中没有可粘贴的表格文字", true);
      const data = { getData: type => content.get(type) || "" };
      pasteText(data.getData("text/plain"), structuredClipboard(data));
    } else pasteText(await navigator.clipboard.readText());
  }
  catch { toast("请选中起始单元格后，按 Ctrl+V 粘贴 Excel 数据"); }
});
function formulaEditing() { return document.activeElement === $("#formula-input") && !$("#formula-input").disabled; }
function formulaHasChanges() {
  if (!formulaEditing() || !active) return false;
  const row = rows.find(row => row.id === active.id);
  if (!row) return false;
  const original = row[active.field];
  return $("#formula-input").value !== String(original ?? "");
}
function draftSyncAllowed() { return !hostManaging && !editing && !pickerRowId && !formulaEditing() && !document.querySelector("dialog[open]"); }
$("#formula-input").addEventListener("change", () => {
  if (!active) return;
  const row = rows.find(row => row.id === active.id), field = active.field, value = $("#formula-input").value;
  if (!row || field === "status") return;
  try {
    const next = field === "order_no" ? value.trim() : value;
    if (next.length > fields[field].max) throw new Error("单元格内容过长");
    if (field === "order_no") $("#formula-input").value = next;
    if (next === row[field]) return;
    remember();
    if (field === "gift") { const match = matchGifts(value); assignGifts(row, match.ids, match.unmatched); }
    else row[field] = next;
    changed(); render(); focusCell(row.id, field);
  } catch (error) { toast(error.message, true); activate(row.id, field); }
});
$("#formula-input").addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.isComposing) { event.preventDefault(); event.target.blur(); }
});
$("#formula-input").addEventListener("paste", handlePaste);
$("#login-button").addEventListener("click", async () => {
  const button = $("#login-button"); button.disabled = true;
  try { await api("/api/login", { method: "POST" }); toast("正在打开登录助手；登录后请点击“保存并关闭浏览器”，再开始登记"); }
  catch (error) { toast(error.message, true); }
  finally { setTimeout(() => { button.disabled = false; }, 2000); }
});
$("#gift-search").addEventListener("input", renderGiftOptions);
$("#gift-close").addEventListener("click", () => { const id = pickerRowId; closeGiftPicker(); if (id) focusCell(id, "gift"); });
$("#gift-done").addEventListener("click", () => { const id = pickerRowId; closeGiftPicker(); if (id) focusCell(id, "gift"); });
$("#gift-clear").addEventListener("click", () => { const row = rows.find(row => row.id === pickerRowId); if (row) { remember(); assignGifts(row, []); updateGiftRow(row); renderGiftOptions(); } });
document.addEventListener("pointerdown", event => { if (pickerRowId && !event.target.closest("#gift-picker,.gift-trigger")) closeGiftPicker(); });
$("#grid-scroll").addEventListener("scroll", closeGiftPicker);
document.addEventListener("keydown", event => { if (event.key === "Escape" && pickerRowId) { const id = pickerRowId; closeGiftPicker(); focusCell(id, "gift"); } });
$("#start-registration").addEventListener("click", async () => {
  if (starting) return;
  const nonempty = rows.filter(filled);
  if (!nonempty.length) return toast("请先填写订单号并选择礼品", true);
  const incomplete = rows.findIndex(row => filled(row) && !(row.order_no.trim() && validGifts(row) && !row.change_address));
  if (incomplete >= 0) { toast(`请先完善第 ${incomplete + 2} 行的订单号、礼品；旧地址请求需核对原意后删除并新建`, true); focusCell(rows[incomplete].id, !rows[incomplete].order_no.trim() ? "order_no" : !validGifts(rows[incomplete]) ? "gift" : "gift"); return; }
  starting = true; const button = $("#start-registration"); button.disabled = true;
  try {
    if (!(await saveNow())) return;
    const result = await api("/api/registrations/start", { method: "POST", body: JSON.stringify({ revision }) });
    rowStates = result.row_states; render();
    toast(result.workflow_configured ? `${result.reused ? "已有相同任务，沿用执行结果" : "登记任务已开始"}，共 ${result.count} 条。失败不会自动重试。`
      : `${result.reused ? "已有相同任务" : "已保存登记任务"}，共 ${result.count} 条。具体平台操作待接入。`);
  } catch (error) { toast(error.message, true); }
  finally { starting = false; button.disabled = false; }
});
document.addEventListener("keydown", event => {
  if (!initialized || event.isComposing) return;
  if ($("#gift-create-dialog").open) {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault(); if (!creatingGift) $("#gift-create-form").requestSubmit();
    }
    return;
  }
  if (document.querySelector("dialog[open]")) return;
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
    event.preventDefault(); if (formulaEditing()) $("#formula-input").blur(); saveNow(true);
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z" && !event.shiftKey) { event.preventDefault(); undo(); }
});
document.addEventListener("visibilitychange", () => { if (document.hidden && initialized) saveNow(); });
window.addEventListener("beforeunload", event => { if (version !== savedVersion || formulaHasChanges()) { event.preventDefault(); event.returnValue = ""; } });
async function refreshExecutionStates() {
  const epoch = stateEpoch, sequence = ++pollSequence;
  try {
    if (initialized) {
      const result = await api("/api/registrations/status");
      if (sequence !== pollSequence || epoch !== stateEpoch) return;
      document.getElementById("connection-notice").hidden = true;
      document.getElementById("shutdown-notice").hidden = !result.server_stopping;
      if (epoch !== stateEpoch || hostManaging || starting || retrySubmitting) return;
      rowStates = result.row_states;
      if (Number.isInteger(result.draft_revision) && result.draft_revision > revision && !saving) {
        if (version === savedVersion && draftSyncAllowed()) {
          const remoteDraft = await api("/api/rows");
          if (epoch !== stateEpoch || sequence !== pollSequence) return;
          // HTTP 等待期间可能又开始编辑，不能用后台清理结果覆盖新的输入。
          if (remoteDraft.revision > revision && version === savedVersion && !saving && draftSyncAllowed()) {
            revision = remoteDraft.revision; rows = remoteDraft.rows.length ? remoteDraft.rows : [newRow()];
            rowStates = remoteDraft.row_states || {}; history.length = 0; selection = null; active = null; render();
          }
        }
        if ((version !== savedVersion || formulaHasChanges()) && !conflict && result.draft_revision > revision) {
          conflict = true; saveIndicator("后台草稿已更新，请保留未保存内容后刷新", "error");
          toast("后台归档或其他窗口更新了草稿。你的未保存内容已保留，请复制后刷新，避免覆盖更新。", true);
        }
      }
      const nextDisplayed = rows.filter(row => !selectedStatus || status(row).type === selectedStatus);
      if (!editing && !pickerRowId && !formulaEditing() && JSON.stringify(nextDisplayed.map(row => row.id)) !== JSON.stringify(displayedRows.map(row => row.id))) render();
      for (const row of rows) rowElement(row.id)?.querySelector('[data-field="status"]').replaceChildren(statusNode(row));
      if (active?.field === "status") activate(active.id, active.field);
      updateSummary();
      scheduleColumnSizing();
    }
  } catch {
    // 保留最后已知结果；恢复连接只读状态，永不发起登记请求。
    if (epoch === stateEpoch && sequence === pollSequence) document.getElementById("connection-notice").hidden = false;
  }
  finally { setTimeout(refreshExecutionStates, document.hidden ? 5000 : 1500); }
}

let retrySubmitting = false;
let retryLoading = false, retryTimer = null, retryFailureIds = [];
function inGiftGroup(id, group) {
  const memberships = giftGroups.memberships[id] || [];
  return !group || (group === "__ungrouped" ? memberships.length === 0 : memberships.includes(group));
}
function groupSelectOptions(select, value) {
  select.replaceChildren(new Option("全部礼品", ""), new Option("未分组", "__ungrouped"));
  for (const group of giftGroups.groups) select.add(new Option(group.name, group.id));
  select.value = [...select.options].some(option => option.value === value) ? value : "";
}
function renderGroupFilter() {
  for (const select of [$("#toolbar-gift-group"), $("#gift-group-filter")]) {
    groupSelectOptions(select, selectedGiftGroup);
    select.options[0].text = "全部礼品（未限定）";
    if (scopeInvalid) { select.add(new Option("分组已变更，请重新选择", "__invalid")); select.value = "__invalid"; }
  }
  $("#gift-scope-notice").hidden = !scopeInvalid;
}
function groupManagerGifts() {
  const query = normalizeGift($("#group-gift-search").value);
  return giftCatalog.filter(gift => inGiftGroup(gift.id, $("#group-manage-filter").value)
    && (!query || [gift.id, gift.name, ...(gift.aliases || [])].some(value => normalizeGift(value).includes(query))));
}
function renderGroupManagerList() {
  const list = $("#group-gift-list"); list.replaceChildren();
  for (const gift of groupManagerGifts()) {
    const label = document.createElement("label"), check = document.createElement("input"), text = document.createElement("span"), meta = document.createElement("small");
    label.className = "group-gift-row"; check.type = "checkbox"; check.value = gift.id; check.checked = groupSelection.has(gift.id);
    text.textContent = gift.name;
    meta.textContent = gift.id + " · " + (giftGroups.groups.filter(group => (giftGroups.memberships[gift.id] || []).includes(group.id)).map(group => group.name).join("、") || "未分组");
    check.addEventListener("change", () => { if (check.checked) groupSelection.add(gift.id); else groupSelection.delete(gift.id); updateGroupCount(); });
    const description = document.createElement("span"); description.append(text, meta); label.append(check, description); list.append(label);
  }
  if (!list.children.length) list.textContent = "当前分组没有匹配礼品";
  updateGroupCount();
}
function updateGroupCount() {
  $("#group-selected-count").textContent = `已选 ${groupSelection.size} 项`;
  $("#assign-groups").disabled = !groupSelection.size || groupsSaving;
}
function renderGroupTargets() {
  const targets = $("#group-targets"); targets.replaceChildren();
  const legend = document.createElement("legend"); legend.textContent = "选择分组（可多选）"; targets.append(legend);
  for (const group of giftGroups.groups) {
    const label = document.createElement("label"), check = document.createElement("input"); check.type = "checkbox"; check.value = group.id;
    label.append(check, document.createTextNode(group.name)); targets.append(label);
  }
}
async function openGroupManager() {
  if (!hostActionsAvailable) return;
  closeGiftPicker(); groupSelection.clear(); $("#group-error").textContent = "";
  try {
    const [catalog, groups] = await Promise.all([api("/api/gifts"), api("/api/gift-groups")]);
    giftCatalog = catalog.gifts; acceptCatalogContext({...catalog, gift_groups: groups});
    groupSelectOptions($("#group-manage-filter"), ""); $("#group-gift-search").value = "";
    renderGroupTargets(); renderGroupManagerList(); renderGroupFilter(); $("#groups-dialog").showModal();
  } catch (error) { toast(error.message, true); }
}
async function updateGroups(update) {
  if (groupsSaving) return;
  groupsSaving = true; $("#create-group").disabled = true; updateGroupCount(); $("#group-error").textContent = "";
  try {
    const updatedGroups = await api("/api/gift-groups", { method: "PUT", body: JSON.stringify({ revision: giftGroups.revision, ...update }) });
    acceptCatalogContext({catalog_version: catalogVersion, gift_groups: updatedGroups});
    groupSelectOptions($("#group-manage-filter"), $("#group-manage-filter").value);
    renderGroupTargets(); renderGroupManagerList(); renderGroupFilter();
    $("#new-group-name").value = ""; toast("礼品分组已保存");
  } catch (error) { $("#group-error").textContent = error.message; }
  finally { groupsSaving = false; $("#create-group").disabled = false; updateGroupCount(); }
}
for (const id of ["#toolbar-gift-group", "#gift-group-filter"]) $(id).addEventListener("change", event => {
  if (event.target.value === "__invalid") return;
  selectedGiftGroup = event.target.value; scopeInvalid = false; persistScope(); giftMatcher = null;
  renderGroupFilter(); if (pickerRowId) renderGiftOptions();
});
$("#manage-groups").addEventListener("click", openGroupManager);
$("#picker-manage-groups").addEventListener("click", openGroupManager);
$("#groups-close").addEventListener("click", () => $("#groups-dialog").close());
$("#group-gift-search").addEventListener("input", renderGroupManagerList);
$("#group-manage-filter").addEventListener("change", renderGroupManagerList);
$("#group-select-visible").addEventListener("click", () => { for (const gift of groupManagerGifts()) groupSelection.add(gift.id); renderGroupManagerList(); });
$("#create-group").addEventListener("click", () => updateGroups({ action: "create", name: $("#new-group-name").value }));
$("#assign-groups").addEventListener("click", () => updateGroups({ action: "assign", mode: $("#group-assign-mode").value, gift_ids: [...groupSelection], group_ids: [...$("#group-targets").querySelectorAll("input:checked")].map(input => input.value) }));
async function refreshMaintenanceStatus() {
  try {
    const status = await api("/api/maintenance/status"), label = $("#maintenance-summary"); label.hidden = false;
    label.textContent = status.last_error || `北京时间每日 01:00 自动导出上一日日志并归档成功行。下次：${status.next_run.slice(0, 10)} 01:00。日志目录：${status.export_directory}${status.last_export_day ? `；最近导出 ${status.last_export_day}，清理 ${status.last_removed_count} 行` : ""}`;
  } catch { /* 断连不影响正在编辑的草稿。 */ }
  finally { setTimeout(refreshMaintenanceStatus, 30000); }
}
async function submitFailureRetry(failureId) {
  if (!hostActionsAvailable || retrySubmitting) return;
  retrySubmitting = true;
  document.querySelectorAll(".row-retry, #retry-list button, #retry-all").forEach(button => { button.disabled = true; });
  try {
    const response = await api("/api/registrations/retry", { method: "POST", body: JSON.stringify({ failure_id: failureId }) });
    rowStates = response.row_states; render();
    toast(response.reused ? "已沿用该记录的重登请求。" : "已加入串行核查队列，每单最多提交一次。");
  } catch (error) { toast(error.message, true); }
  finally { retrySubmitting = false; render(); refreshRetryList(); }
}
async function retryRow(row, button) {
  if (retrySubmitting) return;
  button.disabled = true;
  try {
    await prepareHostAction();
    const result = await api("/api/registrations/failures");
    const failure = result.failures.find(item => taskIdentity(item) === taskIdentity(row));
    if (!failure) throw new Error("该订单失败记录已更新，请刷新状态后查看。");
    if (!failure.eligible) throw new Error(failure.blocked_reason || "该记录不能安全重登");
    await submitFailureRetry(failure.failure_id);
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}
async function refreshRetryList() {
  if (retryLoading || retrySubmitting || !$("#retry-dialog").open) return;
  retryLoading = true;
  try {
    const result = await api("/api/registrations/failures");
    retryFailureIds = result.failures.filter(failure => failure.eligible).map(failure => failure.failure_id);
    $("#retry-summary").textContent = `当前失败 ${result.failures.length} 单 · 可核查重登 ${retryFailureIds.length} 单 · 需人工处理 ${result.failures.length - retryFailureIds.length} 单`;
    $("#retry-all").disabled = result.busy || !retryFailureIds.length;
    const list = $("#retry-list"); list.replaceChildren();
    $("#retry-message").textContent = result.busy ? "登记任务执行中；可继续为其他失败订单排队，逐单核查。" : result.failures.length ? "选择一条失败记录，核查后重新登记。" : "没有需要处理的失败订单。";
    for (const failure of result.failures) {
      const card = document.createElement("section"); card.className = `retry-card ${failure.eligible ? "retry-eligible" : "retry-blocked"}`;
      const title = document.createElement("strong"); title.textContent = failure.order_no;
      const gift = document.createElement("p"); gift.textContent = `${failure.gift} · 编码：${failure.gift_ids.join("、")}`;
      const detail = document.createElement("p"); detail.textContent = `失败原因：${failure.detail}`;
      const note = document.createElement("p"); note.className = "retry-note";
      note.textContent = failure.blocked_reason || "可发起重登核查；不会清除防重记录。";
      const button = document.createElement("button"); button.className = "button primary";
      button.type = "button"; button.textContent = failure.eligible ? "核查并重登" : "需人工核查";
      button.disabled = !failure.eligible || retrySubmitting;
      button.addEventListener("click", () => submitFailureRetry(failure.failure_id));
      const heading = document.createElement("div"); heading.className = "retry-card-heading";
      heading.append(title, button); card.append(heading, gift, detail, note); list.append(card);
    }
  } catch (error) { $("#retry-message").textContent = error.message; }
  finally {
    retryLoading = false; clearTimeout(retryTimer);
    if ($("#retry-dialog").open) retryTimer = setTimeout(refreshRetryList, 3000);
  }
}
$("#retry-failures").addEventListener("click", async () => {
  if (!hostActionsAvailable) return;
  try { await prepareHostAction(); $("#retry-batch-result").hidden = true; $("#retry-dialog").showModal(); refreshRetryList(); }
  catch (error) { toast(error.message, true); }
});
$("#retry-close").addEventListener("click", () => $("#retry-dialog").close());
$("#retry-dialog").addEventListener("close", () => clearTimeout(retryTimer));
$("#retry-all").addEventListener("click", async () => {
  if (retryLoading || retrySubmitting || !retryFailureIds.length) return;
  const failureIds = [...retryFailureIds];
  retryLoading = true; $("#retry-all").disabled = true;
  $("#retry-list").querySelectorAll("button").forEach(button => { button.disabled = true; });
  try {
    const result = await api("/api/registrations/retry-all", { method: "POST", body: JSON.stringify({ failure_ids: failureIds }) });
    rowStates = result.row_states; render();
    const accepted = result.results.filter(item => item.accepted), blocked = result.results.filter(item => !item.accepted);
    const label = $("#retry-batch-result"); label.hidden = false;
    label.textContent = `本次已接收 ${accepted.length} 条（含复用 ${accepted.filter(item => item.reused).length} 条），拦截 ${blocked.length} 条。` +
      [...new Set(blocked.map(item => item.reason))].join("；") + " 每单独立核查，新的失败不会自动重登。";
    toast(`批量核查已接收 ${accepted.length} 条，拦截 ${blocked.length} 条`);
  } catch (error) { toast(error.message, true); }
  finally { retryLoading = false; refreshRetryList(); }
});

let hostManaging = false, statusSelectedIds = [];
let cancellationTargets = [];
function openCancellation(identifiers) {
  if (!hostActionsAvailable || hostManaging) return;
  cancellationTargets = identifiers.flatMap(id => {
    const row = rows.find(row => row.id === id), state = rowStates[id];
    return row && state?.state === "queued" && state.signature === rowSignature(row)
      ? [{ row_id: id, task_id: state.task_id }] : [];
  });
  if (!cancellationTargets.length) return toast("所选订单中没有内容未修改的排队任务。已开始处理的订单不能取消。", true);
  $("#cancel-selection").textContent = `所选 ${identifiers.length} 行中，${cancellationTargets.length} 行正在等待执行。`;
  $("#cancel-dialog").showModal();
}
$("#cancel-queued").addEventListener("click", () => openCancellation(selectedRowIds()));
$("#cancel-close").addEventListener("click", () => $("#cancel-dialog").close());
$("#cancel-apply").addEventListener("click", async () => {
  if (hostManaging || !hostActionsAvailable || !cancellationTargets.length) return;
  hostManaging = true; $("#cancel-apply").disabled = true;
  try {
    const result = await api("/api/registrations/cancel", { method: "POST",
      body: JSON.stringify({ revision, targets: cancellationTargets }) });
    rowStates = result.row_states;
    $("#cancel-dialog").close();
    const skipped = result.results.filter(item => !item.cancelled).length;
    toast(`已取消 ${result.count} 单的排队（共 ${result.task_count} 个任务）。${skipped ? `${skipped} 行已开始、已结束或已变化，未取消。` : "记录已保留。"}`, !result.count);
  } catch (error) { toast(error.message, true); }
  finally { hostManaging = false; $("#cancel-apply").disabled = false; render(); }
});
function selectedRowIds() {
  const bounds = rangeBounds();
  return bounds ? gridRows().slice(bounds.top, bounds.bottom + 1).map(row => row.id) : active ? [active.id] : [];
}
async function prepareHostAction() {
  if (formulaHasChanges()) $("#formula-input").dispatchEvent(new Event("change"));
  finishEditing(); closeGiftPicker();
  await saveNow();
  if (conflict || version !== savedVersion) throw new Error("请先保存当前草稿并处理编辑冲突");
}
$("#edit-status").addEventListener("click", () => {
  if (!hostActionsAvailable) return;
  statusSelectedIds = selectedRowIds();
  if (!statusSelectedIds.length) return toast("请先选择订单行", true);
  $("#status-selection").textContent = `所选 ${statusSelectedIds.length} 行`;
  $("#status-dialog").showModal();
});
$("#status-close").addEventListener("click", () => $("#status-dialog").close());
$("#status-apply").addEventListener("click", async () => {
  if (hostManaging || !hostActionsAvailable) return;
  hostManaging = true; $("#status-apply").disabled = true;
  try {
    await prepareHostAction();
    const result = await api("/api/rows/status", { method: "POST", body: JSON.stringify({ revision, row_ids: statusSelectedIds, status: $("#manual-status").value }) });
    rowStates = result.row_states; render(); $("#status-dialog").close(); toast(`已更新 ${result.count} 行人工状态`);
  } catch (error) { toast(error.message, true); }
  finally { hostManaging = false; $("#status-apply").disabled = false; }
});
async function deleteHostRows(all) {
  if (hostManaging || !hostActionsAvailable) return;
  const ids = selectedRowIds();
  if (!all && !ids.length) return toast("请先选择订单行", true);
  const count = all ? rows.length : ids.length;
  if (!confirm(`删除${all ? "全部（含筛选隐藏行）" : "所选"} ${count} 行草稿订单？删除前会备份，任务历史与防重记录保留，可撤销。`)) return;
  hostManaging = true;
  try {
    await prepareHostAction();
    const previous = structuredClone(rows), actionVersion = version;
    const result = await api("/api/rows/delete", { method: "POST", body: JSON.stringify({ revision, row_ids: ids, all }) });
    if (version !== actionVersion || formulaHasChanges() || editing) {
      conflict = true; throw new Error("删除期间发生了新编辑；已保留当前输入，请复制后刷新查看删除结果");
    }
    history.push(previous); revision = result.revision; rows = result.rows.length ? result.rows : [newRow()];
    rowStates = result.row_states; active = null; selection = null; render();
    toast(`已删除 ${result.removed} 行草稿订单，备份和任务历史已保留`);
  } catch (error) { toast(error.message, true); }
  finally { hostManaging = false; }
}
$("#host-delete-selected").addEventListener("click", () => deleteHostRows(false));
$("#host-delete-all").addEventListener("click", () => deleteHostRows(true));
$("#export-today").addEventListener("click", async () => {
  const button = $("#export-today"); button.disabled = true;
  try {
    const response = await fetch("/api/registrations/export-today", { cache: "no-store" });
    if (!response.ok) throw new Error((await response.json()).error || "日志导出失败");
    const blob = await response.blob(), url = URL.createObjectURL(blob);
    const link = document.createElement("a"); link.href = url;
    link.download = response.headers.get("Content-Disposition")?.match(/filename="([^"]+)"/)?.[1] || "gift-registration.xlsx";
    document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 60000);
    toast("今日日志已导出，包含成功与失败两个工作表。");
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
});

let networkChangeId = null, networkNoticeKey = null, dismissedNetworkNotice = null;
try { dismissedNetworkNotice = localStorage.getItem("gift_registry.dismissed_network_notice.v1"); } catch { /* 本页仍可隐藏。 */ }
function renderNetworkNotice(status) {
  const notice = $("#network-notice");
  if (status.state === "ok") {
    dismissedNetworkNotice = null;
    try { localStorage.removeItem("gift_registry.dismissed_network_notice.v1"); } catch { /* 内存状态已恢复。 */ }
  }
  networkNoticeKey = JSON.stringify([status.state, status.change_id, status.configured_ip, status.domain, status.message]);
  notice.hidden = networkNoticeKey === dismissedNetworkNotice || !["changed", "offline", "unavailable"].includes(status.state);
  $("#network-notice-text").textContent = status.message || "";
  $("#network-notice .network-notice-actions a").hidden = !status.domain;
  networkChangeId = status.change_id;
  $("#network-ack").hidden = status.state !== "changed" ||
    !status.current_addresses?.some(address => address.ip === status.configured_ip);
}
$("#network-dismiss").addEventListener("click", () => {
  dismissedNetworkNotice = networkNoticeKey; $("#network-notice").hidden = true;
  try { localStorage.setItem("gift_registry.dismissed_network_notice.v1", dismissedNetworkNotice); } catch { /* 本页生效。 */ }
});
async function refreshNetworkNotice() {
  try { renderNetworkNotice(await api("/api/network/status")); }
  catch { /* 保留已显示的提醒，网络探测不影响草稿和业务队列。 */ }
  finally { setTimeout(refreshNetworkNotice, 10000); }
}
$("#network-ack").addEventListener("click", async () => {
  const button = $("#network-ack"); button.disabled = true;
  try { renderNetworkNotice(await api("/api/network/ack", { method: "POST", body: JSON.stringify({ change_id: networkChangeId }) })); }
  catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
});

(async () => {
  const buttons = [...document.querySelectorAll(".toolbar button,#add-bottom,#login-button,#toggle-fullscreen")];
  buttons.forEach(button => { button.disabled = true; });
  try {
    const config = await api("/api/config"); token = config.token; giftCatalog = config.gifts || []; catalogEditable = config.frontend_version >= 4;
    giftGroups = config.gift_groups || giftGroups; catalogVersion = config.catalog_version || ""; restoreScope(); renderGroupFilter();
    hostActionsAvailable = config.host_actions_available === true;
    $("#picker-manage-groups").hidden = !hostActionsAvailable;
    if (!giftCatalog.length) throw new Error("未配置礼品选项，请重启工作台");
    const document = await api("/api/rows"); revision = document.revision; rowStates = document.row_states || {};
    rows = document.rows.length ? document.rows : Array.from({ length: 18 }, newRow);
    const before = JSON.stringify(rows);
    // Shared drafts keep confirmed codes and unresolved text; loading never reinterprets them with this device's scope.
    initialized = true;
    $("#host-actions").hidden = config.host_actions_available !== true;
    $("#retry-failures").hidden = !hostActionsAvailable;
    $("#edit-status").hidden = !hostActionsAvailable;
    $("#cancel-queued").hidden = !hostActionsAvailable;
    if (hostActionsAvailable && config.frontend_version >= 6) refreshMaintenanceStatus();
    if (config.network_settings_available) refreshNetworkNotice();
    buttons.forEach(button => { button.disabled = false; });
    if (config.platform_login_available === false) {
      const login = $("#login-button"); login.disabled = true; login.textContent = "平台登录由主机维护";
      login.title = "如需登录或处理验证码，请在运行工具的主机上打开登录助手";
    }
    render(); saveIndicator("草稿自动保存");
    if (JSON.stringify(rows) !== before) changed();
    focusCell(rows[0].id, "order_no");
    setTimeout(refreshExecutionStates, 1500);
  } catch (error) {
    $("#registry-grid").hidden = true;
    $("#load-error").hidden = false;
    saveIndicator("载入失败，请刷新重试", "error");
  }
})();
