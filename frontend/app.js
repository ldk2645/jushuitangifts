"use strict";

const $ = (selector) => document.querySelector(selector);
const fields = {
  order_no: { label: "订单号", placeholder: "输入订单号", max: 200 },
  gift: { label: "礼品", placeholder: "多个礼品用逗号、顿号或空格分隔", max: 500 },
  change_address: { label: "是否改地址" },
  recipient_name: { label: "姓名", placeholder: "收件人姓名", max: 100 },
  phone: { label: "电话", placeholder: "收件人电话", max: 60 },
  address: { label: "地址", placeholder: "填写完整收件地址", max: 2000 },
  status: { label: "状态" },
};
const addressFields = ["recipient_name", "phone", "address"];
let rows = [], revision = 0, token = "", active = null, initialized = false;
let version = 0, savedVersion = 0, saveTimer, saving = null, conflict = false, toastTimer;
let giftCatalog = [], rowStates = {}, pickerRowId = null, starting = false;
let creatingGift = false, createGiftRowId = null, catalogRefreshing = null;
let catalogEditable = false;
const recentGiftOrderLimit = 100;
let pickerGiftUsage = null;
let selection = null, editing = null, dragSelection = null, dragFrame = null, suppressCellClick = false;
let rowNodes = new Map(), selectedNodes = new Set(), forceRangeCopy = false;
const rangeClipboardType = "application/x-gift-registry-range+json";
const history = [];
const layoutStorageKey = "gift_registry.column_widths.v1";
const columnRules = { order_no: { min: 120, base: 180 }, gift: { min: 140, base: 190 }, change_address: { min: 112, base: 112 }, status: { min: 112, base: 112 } };
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
    widths.status = Math.max(widths.status, textWidth(status(row).label, badge, "11px sans-serif") + 54);
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

function newRow() {
  return { id: crypto.randomUUID(), order_no: "", gift: "", gift_ids: [], change_address: false,
    recipient_name: "", phone: "", address: "" };
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
function giftScore(text, gift) {
  const value = normalizeGift(text);
  if (!value) return 0;
  return Math.max(...[gift.id, gift.name, ...(gift.aliases || [])].map(label => giftLabelScore(value, normalizeGift(label))));
}
let giftMatcher = null;
function matchingContext() {
  if (giftMatcher?.catalog === giftCatalog && giftMatcher.size === giftCatalog.length) return giftMatcher;
  const exact = new Map(), entries = [], protectedLabels = [];
  let maxWords = 8;
  for (const gift of giftCatalog) {
    const labels = [gift.id, gift.name, ...(gift.aliases || [])];
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
  protectedLabels.sort((a, b) => b.label.length - a.label.length);
  giftMatcher = { catalog: giftCatalog, size: giftCatalog.length, exact, entries, protectedLabels, maxWords, parts: new Map(), results: new Map() };
  return giftMatcher;
}
function resolveGiftPart(text, strict = false) {
  const context = matchingContext(), value = normalizeGift(text), key = `${strict ? "phrase" : "word"}:${value}`;
  if (context.parts.has(key)) return context.parts.get(key);
  const exact = context.exact.get(value);
  let result;
  if (exact) result = { id: exact.size === 1 ? [...exact][0] : null, score: exact.size === 1 ? 1 : 0, exact: true };
  else {
    const matches = context.entries.map(({ gift, labels }) => {
      // 跨空格的短语必须整体相似，不能只因包含一个已知简称就吞掉其他礼品。
      const meaningful = value.length >= 3 || labels.some(label => Math.min(value.length, label.length) / Math.max(value.length, label.length, 1) >= .5);
      const score = strict ? Math.max(0, ...labels.map(label => Math.min(value.length, label.length) / Math.max(value.length, label.length, 1) >= .8 ? similarity(value, label) * .9 : 0)) : meaningful ? giftScore(text, gift) : 0;
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
  const unmatched = matchGifts(row.gift).unmatched;
  return unmatched.length || !row.gift || row.gift === giftNames(row.gift_ids || []).join("、") ? unmatched : [row.gift];
}
function assignGifts(row, ids, unmatched = []) {
  row.gift_ids = [...new Set(ids)].filter(id => giftCatalog.some(gift => gift.id === id));
  row.gift = [...giftNames(row.gift_ids), ...unmatched].join("、");
}
function rowSignature(row) { return JSON.stringify([row.id, row.order_no, row.gift, row.gift_ids || [], row.change_address, row.recipient_name, row.phone, row.address]); }
function validGifts(row) { return row.gift_ids?.length && row.gift === giftNames(row.gift_ids).join("、") && row.gift_ids.every(id => giftCatalog.some(gift => gift.id === id)); }
function columns() {
  return ["order_no", "gift", "change_address", "status"];
}
function filled(row) { return Boolean(row.order_no.trim() || row.gift.trim() || row.change_address); }
function status(row) {
  if (!filled(row)) return { label: "—", type: "empty" };
  const complete = row.order_no.trim() && validGifts(row)
    && (!row.change_address || addressFields.every(field => row[field].trim()));
  if (complete && rowStates[row.id]?.signature === rowSignature(row)) {
    const state = rowStates[row.id];
    const labels = { waiting_for_workflow: ["待接入流程", "queued"], queued: ["等待执行", "processing"], querying: ["查询订单", "processing"],
      submitting: ["正在登记", "processing"], verifying: ["复查确认", "processing"], success: ["成功", "success"], failed: ["失败", "failed"] };
    const [label, type] = labels[state.state] || ["等待执行", "processing"];
    return { label, type, detail: state.detail || "" };
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
  scheduleColumnSizing();
  clearTimeout(saveTimer);
  saveIndicator(conflict ? "保存冲突，请保留内容并刷新" : "有未保存的修改", conflict ? "error" : "saving");
  if (!conflict) saveTimer = setTimeout(() => saveNow(), 650);
}
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { "Content-Type": "application/json", "X-CSRF-Token": token, ...(options.headers || {}) } });
  let result;
  try { result = await response.json(); } catch { throw new Error("工作台连接中断，请确认程序正在运行"); }
  if (!response.ok) {
    if (response.status === 409 && !["registration_not_started", "gift_catalog_conflict"].includes(result.code)) conflict = true;
    throw new Error(result.error || "请求失败，请重试");
  }
  return result;
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
  $("#row-summary").textContent = `共 ${rows.length} 行 · 已填写 ${rows.filter(filled).length} 条`;
  const count = type => rows.filter(row => status(row).type === type).length;
  $("#ready-summary").textContent = `待登记 ${count("ready")} · 进行中 ${count("processing")} · 成功 ${count("success")} · 失败 ${count("failed")}`;
  $("#metric-ready").textContent = count("ready");
  $("#metric-success").textContent = count("success");
  $("#metric-failed").textContent = count("failed");
}
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
  for (const name of names) { const chip = document.createElement("span"); chip.className = "gift-chip"; chip.textContent = name; chips.append(chip); }
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
  const candidates = giftCatalog.map((gift, index) => ({ gift, index, count: usage.counts.get(gift.id) || 0, score: query ? giftScore(query, gift) : 1 }))
    .filter(item => item.score >= .45).sort((a, b) => b.score - a.score || b.count - a.count || a.index - b.index);
  $("#gift-sort-hint").textContent = usage.orderCount
    ? `${query ? "匹配优先 · " : ""}按近 ${usage.orderCount} 个订单的礼赠频率排序`
    : "暂无订单使用记录，按清单顺序显示";
  $("#gift-sort-hint").title = `统计当前表格末尾最近 ${recentGiftOrderLimit} 个有礼赠的不同订单；同一订单同一礼赠只计一次，同频保留原清单顺序。`;
  const list = $("#gift-options"); list.replaceChildren();
  for (const { gift, count } of candidates) {
    const label = document.createElement("label"), check = document.createElement("input"), text = document.createElement("span"), code = document.createElement("small");
    label.className = "gift-option"; check.type = "checkbox"; check.value = gift.id; check.checked = row.gift_ids.includes(gift.id);
    text.textContent = gift.name; code.textContent = gift.id;
    check.addEventListener("change", () => {
      remember();
      const ids = check.checked ? [...row.gift_ids, gift.id] : row.gift_ids.filter(id => id !== gift.id);
      assignGifts(row, ids, matchGifts(row.gift).unmatched);
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
  if (!candidates.length) { const empty = document.createElement("p"); empty.className = "gift-no-results"; empty.textContent = "没有匹配选项，请尝试其他名称或别名"; list.append(empty); }
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
    giftCatalog = result.gifts;
    const row = $("#gift-create-select").checked && createGiftRowId ? rows.find(row => row.id === createGiftRowId) : null;
    if (row) {
      remember();
      const match = matchGifts(row.gift);
      assignGifts(row, [...row.gift_ids, ...match.ids, result.gift.id], match.unmatched); changed();
    }
    render(); $("#gift-create-dialog").close(); focusAfterGiftCreate(row?.id);
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
  return node;
}
function addressDetail(row, index) {
  const detail = document.createElement("tr"), gutter = document.createElement("th"), cell = document.createElement("td");
  detail.className = "address-details"; detail.dataset.forRow = row.id;
  gutter.setAttribute("aria-hidden", "true"); gutter.textContent = "↳";
  cell.colSpan = 4;
  const panel = document.createElement("div"), heading = document.createElement("div"), grid = document.createElement("div");
  panel.className = "address-panel"; heading.className = "address-panel-heading"; grid.className = "address-fields";
  const title = document.createElement("strong"), hint = document.createElement("span");
  title.textContent = "修改收件信息"; hint.textContent = "仅应用于本行订单"; heading.append(title, hint);
  for (const field of addressFields) {
    const wrapper = document.createElement("label"), label = document.createElement("span"), control = document.createElement("input");
    wrapper.className = `address-field field-${field}`; wrapper.dataset.field = field;
    label.textContent = fields[field].label;
    control.type = "text"; control.value = row[field]; control.maxLength = fields[field].max; control.autocomplete = "off"; control.spellcheck = false;
    control.placeholder = fields[field].placeholder; control.setAttribute("aria-label", `第 ${index + 2} 行 ${fields[field].label}`);
    if (field === "phone") { control.inputMode = "tel"; control.className = "phone-input"; }
    let recorded = false;
    control.addEventListener("focus", () => { recorded = false; activate(row.id, field); });
    control.addEventListener("input", () => {
      if (!recorded) { remember(); recorded = true; }
      row[field] = control.value; rowElement(row.id).querySelector('[data-field="status"]').replaceChildren(statusNode(row));
      $("#formula-input").value = control.value; changed();
    });
    control.addEventListener("paste", handlePaste);
    control.addEventListener("keydown", event => handleNavigation(event, row.id, field));
    wrapper.addEventListener("pointerdown", () => activate(row.id, field));
    wrapper.append(label, control); grid.append(wrapper);
  }
  panel.append(heading, grid); cell.append(panel); detail.append(gutter, cell);
  return detail;
}
function render() {
  finishEditing();
  closeGiftPicker();
  const visible = columns();
  $("#registry-grid").classList.toggle("address-mode", visible.length > 4);
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
    if (addressFields.includes(field)) heading.className = "address-heading";
    const label = document.createElement("div");
    label.className = "column-label";
    label.textContent = fields[field].label;
    if (field === "change_address") heading.title = "空心圆点为否，实心圆点为是";
    heading.append(label);
    heading.append(createColumnResizer(field));
    headings.append(heading);
  });
  $("#grid-head").replaceChildren(letters, headings);
  const body = document.createDocumentFragment();
  rows.forEach((row, index) => {
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
      } else if (field === "change_address") {
        // 本版本不支持改地址：已有草稿只允许从“是”关回“否”，不能再开启。
        const locked = !row.change_address;
        const toggle = document.createElement("button");
        toggle.type = "button"; toggle.className = `address-toggle${row.change_address ? " is-yes" : ""}${locked ? " is-locked" : ""}`;
        toggle.setAttribute("role", "checkbox"); toggle.setAttribute("aria-checked", String(row.change_address));
        toggle.setAttribute("aria-disabled", String(locked));
        toggle.setAttribute("aria-label", `第 ${index + 2} 行 是否改地址：${row.change_address ? "是" : "否"}${locked ? "（已禁用）" : ""}`);
        toggle.title = locked ? "本版本暂不支持改地址，请保持“否”" : "是 · 点击关闭改地址（本版本不支持新地址）";
        const dot = document.createElement("span"); dot.className = "address-dot"; dot.setAttribute("aria-hidden", "true"); toggle.append(dot);
        toggle.addEventListener("focus", () => activate(row.id, field));
        toggle.addEventListener("click", () => {
          if (locked) return;
          selectRange({ id: row.id, field }); remember(); row.change_address = false; changed(); render(); focusCell(row.id, field);
        });
        if (!locked) toggle.addEventListener("paste", handlePaste);
        toggle.addEventListener("keydown", event => handleNavigation(event, row.id, field));
        cell.append(toggle);
      } else if (addressFields.includes(field) && !row.change_address) {
        const dash = document.createElement("span");
        dash.className = "inactive-value";
        dash.textContent = "—";
        cell.append(dash);
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
          if (index === 0 || addressFields.includes(field)) control.placeholder = fields[field].placeholder;
          if (field === "order_no") control.className = "order-input";
          if (field === "phone") { control.className = "phone-input"; control.inputMode = "tel"; }
          let recorded = false;
          control.addEventListener("focus", () => { recorded = false; });
          control.addEventListener("editstart", () => { recorded = false; });
          control.addEventListener("input", () => {
            if (!recorded) { remember(); recorded = true; }
            row[field] = control.value;
            tr.querySelector('[data-field="status"]').replaceChildren(statusNode(row));
            $("#formula-input").value = control.value;
            changed();
          });
          control.addEventListener("blur", () => { if (editing?.id === row.id && editing.field === field) finishEditing(); });
        }
        if (addressFields.includes(field)) cell.classList.add("address-active");
        control.addEventListener("focus", () => activate(row.id, field));
        control.addEventListener("paste", handlePaste);
        control.addEventListener("keydown", (event) => handleNavigation(event, row.id, field));
        cell.append(control);
      }
      if (field === "status") cell.addEventListener("focus", () => activate(row.id, field));
      tr.append(cell);
    }
    body.append(tr);
    if (row.change_address) body.append(addressDetail(row, index));
  });
  $("#grid-body").replaceChildren(body);
  rowNodes = new Map([...$("#grid-body").children].filter(node => node.dataset.id).map(node => [node.dataset.id, node]));
  selectedNodes.clear();
  if (selection && !rangeBounds()) selection = null;
  updateSummary();
  if (active && rows.some(row => row.id === active.id)) activate(active.id, visible.includes(active.field) || (addressFields.includes(active.field) && rows.find(row => row.id === active.id).change_address) ? active.field : "order_no");
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
  if (addressFields.includes(field)) return [...$("#grid-body").children].find(element => element.dataset.forRow === id)?.querySelector(`.address-field[data-field="${field}"]`);
  return rowElement(id)?.querySelector(`td[data-field="${field}"]`);
}
function rangeBounds() {
  if (!selection) return null;
  const start = rows.findIndex(row => row.id === selection.anchor.id), end = rows.findIndex(row => row.id === selection.focus.id);
  const left = columns().indexOf(selection.anchor.field), right = columns().indexOf(selection.focus.field);
  if (Math.min(start, end, left, right) < 0) return null;
  return { top: Math.min(start, end), bottom: Math.max(start, end), left: Math.min(left, right), right: Math.max(left, right) };
}
function cellReference(row, column) { return `${String.fromCharCode(65 + column)}${row + 2}`; }
function paintSelection() {
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
    for (let row = bounds.top; row <= bounds.bottom; row++) {
      const tr = rowElement(rows[row].id);
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
    const first = cellReference(bounds.top, bounds.left), last = cellReference(bounds.bottom, bounds.right);
    $("#cell-reference").textContent = first === last ? first : `${first}:${last}`;
    const height = bounds.bottom - bounds.top + 1, width = bounds.right - bounds.left + 1;
    $("#selection-summary").textContent = height * width > 1 ? `已选 ${height} 行 × ${width} 列 · ${height * width} 个单元格` : `第 ${bounds.top + 2} 行 · ${fields[columns()[bounds.left]].label}`;
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
  $("#formula-input").disabled = field === "status" || (addressFields.includes(field) && !row.change_address);
  $("#formula-input").value = field === "status" ? status(row).label : field === "change_address" ? (row.change_address ? "是" : "否") : row[field];
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
  if (cancel) {
    const row = rows.find(row => row.id === current.id);
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
  if (field === "change_address") return cell.querySelector(".address-toggle").click();
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
  if (!initialized || event.button !== 0 || event.target.closest(".column-resizer")) return;
  const header = event.target.closest("thead th");
  if (header) {
    event.preventDefault(); closeGiftPicker();
    const field = header.dataset.field;
    const first = event.shiftKey && selection && field ? selection.anchor.field : field || "order_no";
    selectRange({ id: rows[0].id, field: first }, { id: rows.at(-1).id, field: field || "status" });
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
  if (!active || !columns().includes(active.field) || event.target.closest(".column-resizer,.address-details") || event.isComposing) return;
  const modifier = event.ctrlKey || event.metaKey;
  if (modifier && event.key.toLowerCase() === "a" && !editing) {
    event.preventDefault(); event.stopPropagation(); closeGiftPicker();
    selectRange({ id: rows[0].id, field: "order_no" }, { id: rows.at(-1).id, field: "status" });
    focusCell(active.id, active.field, true); return;
  }
  if (editing && !["Enter", "Tab", "Escape"].includes(event.key)) return;
  if (event.key === "Escape") {
    event.preventDefault(); event.stopPropagation(); finishEditing(true);
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
    let row = rows.findIndex(item => item.id === origin.id), col = columns().indexOf(origin.field);
    if (event.key in arrows) { row += arrows[event.key][0]; col += arrows[event.key][1]; }
    if (event.key === "Enter") row += event.shiftKey ? -1 : 1;
    if (event.key === "Tab") { col += event.shiftKey ? -1 : 1; if (col > 3) { row++; col = 0; } if (col < 0 && row > 0) { row--; col = 3; } }
    if (event.key === "Home") { col = 0; if (modifier) row = 0; }
    if (event.key === "End") { col = 3; if (modifier) row = rows.length - 1; }
    if (row >= rows.length && ["Tab", "Enter"].includes(event.key) && rows.length < 10000) { remember(); rows.push(newRow()); changed(); render(); }
    row = Math.max(0, Math.min(rows.length - 1, row)); col = Math.max(0, Math.min(3, col));
    const point = { id: rows[row].id, field: columns()[col] };
    if (extending) { selectRange(selection?.anchor || active, point); focusCell(active.id, active.field, true); scrollCellIntoView(point.id, point.field); }
    else focusCell(point.id, point.field);
    return;
  }
  if (!modifier && !event.altKey && (event.key.length === 1 || event.key === "Process")) {
    if (active.field === "order_no") beginEditing(active.id, active.field, true);
    else if (active.field === "gift") {
      event.preventDefault(); event.stopPropagation(); beginEditing(active.id, active.field);
      if (event.key.trim()) { $("#gift-search").value = event.key; renderGiftOptions(); }
    } else if (active.field === "change_address" && event.key === " ") {
      event.preventDefault(); event.stopPropagation(); beginEditing(active.id, active.field);
    }
  }
}, true);
function handleNavigation(event, id, field) {
  if (event.isComposing) return;
  const index = rows.findIndex(row => row.id === id);
  if (["Enter", "ArrowUp", "ArrowDown"].includes(event.key) && !(event.key === "Enter" && event.target.tagName === "BUTTON")) {
    event.preventDefault();
    const next = index + ((event.key === "ArrowUp" || (event.key === "Enter" && event.shiftKey)) ? -1 : 1);
    if (next >= rows.length && rows.length < 10000) { remember(); rows.push(newRow()); changed(); render(); }
    if (next >= 0 && next < rows.length) focusCell(rows[next].id, addressFields.includes(field) && !rows[next].change_address ? "order_no" : field);
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
  return rows.slice(bounds.top, bounds.bottom + 1).map(row => columns().slice(bounds.left, bounds.right + 1).map(field =>
    field === "status" ? status(row).label : field === "change_address" ? (row.change_address ? "是" : "否") : row[field]));
}
function clipboardContent() {
  const matrix = selectionMatrix();
  if (!matrix) return null;
  const text = matrix.map(line => line.map(value => /[\t\r\n"]/.test(value) ? `"${value.replaceAll('"', '""')}"` : value).join("\t")).join("\r\n");
  const encoded = btoa(Array.from(new TextEncoder().encode(JSON.stringify(matrix)), byte => String.fromCharCode(byte)).join(""));
  const table = document.createElement("table"); table.dataset.giftRegistryRange = encoded;
  for (const line of matrix) {
    const tr = table.insertRow();
    for (const value of line) {
      const td = tr.insertCell(); td.textContent = value;
      // Excel 的 HTML 剪贴板格式：长订单号按文本导入，保留前导零。
      td.setAttribute("style", 'mso-number-format:"\\@";white-space:pre-wrap'); td.setAttribute("x:str", "");
    }
  }
  return { text, json: JSON.stringify(matrix), html: `<html xmlns:x="urn:schemas-microsoft-com:office:excel"><body><!--StartFragment-->${table.outerHTML}<!--EndFragment--></body></html>` };
}
function structuredClipboard(data) {
  try {
    let value = data.getData(rangeClipboardType);
    if (!value) {
      const encoded = data.getData("text/html").match(/data-gift-registry-range="([A-Za-z0-9+/=]+)"/)?.[1];
      if (encoded) value = new TextDecoder().decode(Uint8Array.from(atob(encoded), char => char.charCodeAt(0)));
    }
    if (!value) return null;
    const matrix = JSON.parse(value);
    return Array.isArray(matrix) && matrix.length > 0 && matrix.length <= 10000 && matrix.every(line => Array.isArray(line) && line.length > 0 && line.length <= 7 && line.every(cell => typeof cell === "string")) ? matrix : null;
  } catch { return null; }
}
function textEditingTarget(target) {
  return target?.closest?.("#formula-input,#gift-picker,.address-field") || (target?.tagName === "INPUT" && !target.readOnly);
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
function asBoolean(value) {
  const normalized = value.trim().toLowerCase();
  if (["是", "true", "1", "yes"].includes(normalized)) return true;
  if (["否", "false", "0", "no", ""].includes(normalized)) return false;
  throw new Error("“是否改地址”请填写“是”或“否”");
}
function pasteText(text, clipboardMatrix = null) {
  if (!initialized) return;
  finishEditing(); closeGiftPicker();
  const matrix = clipboardMatrix || parseTSV(text);
  if (matrix[0]?.[0]?.trim() === "订单号" && ["礼品", "礼赠", "赠品"].includes(matrix[0]?.[1]?.trim())) matrix.shift();
  if (!matrix.length) return;
  const bounds = rangeBounds();
  const start = bounds?.top ?? (active ? Math.max(0, rows.findIndex(row => row.id === active.id)) : 0);
  const visible = columns();
  const sourceWidth = Math.max(...matrix.map(line => line.length));
  const legacy = sourceWidth === 7 && (!bounds || bounds.left === 0) && (!active || active.field === "order_no");
  const mapping = legacy || addressFields.includes(active?.field) ? Object.keys(fields) : visible;
  const colStart = mapping === visible ? bounds?.left ?? Math.max(0, visible.indexOf(active?.field)) : Math.max(0, mapping.indexOf(active?.field));
  if (sourceWidth > mapping.length - colStart) return toast("粘贴的列数超出表格范围，请重新选择起始单元格", true);
  let height = matrix.length, width = sourceWidth;
  if (bounds && !legacy) {
    const selectedHeight = bounds.bottom - bounds.top + 1, selectedWidth = bounds.right - bounds.left + 1;
    if (selectedHeight % height === 0 && selectedWidth % width === 0) { height = selectedHeight; width = selectedWidth; }
    else if (selectedHeight > height || selectedWidth > width) return toast("选区大小与粘贴内容不匹配，请选择单个起点或相同大小的区域", true);
  }
  if (start + height > 10000) return toast("粘贴后将超过 10000 行，请分批录入", true);
  if (mapping.slice(colStart, colStart + width).every(field => field === "status")) return toast("状态列由登记结果自动更新，不能手动填写");
  const updated = structuredClone(rows);
  try {
    for (let offset = 0; offset < height; offset++) {
      while (updated.length <= start + offset) updated.push(newRow());
      for (let column = 0; column < width; column++) {
        const value = matrix[offset % matrix.length][column % sourceWidth] ?? "";
        const field = mapping[colStart + column];
        if (!field || field === "status") continue;
        if (field === "change_address") updated[start + offset][field] = asBoolean(value);
        else if (field === "gift") {
          const match = matchGifts(value); assignGifts(updated[start + offset], match.ids, match.unmatched);
          if (updated[start + offset].gift.length > fields.gift.max) throw new Error("礼品内容过长，请减少单行礼品数量");
        }
        else {
          if (value.length > fields[field].max) throw new Error(`“${fields[field].label}”内容过长，请检查粘贴的数据`);
          updated[start + offset][field] = value;
        }
      }
    }
    remember(); rows = updated; changed();
    if (mapping === visible || legacy) selection = { anchor: { id: rows[start].id, field: visible[legacy ? 0 : colStart] }, focus: { id: rows[start + height - 1].id, field: visible[legacy ? 3 : colStart + width - 1] } };
    active = selection?.anchor || { id: rows[start].id, field: mapping[colStart] };
    render(); focusCell(active.id, active.field, true);
    const unmatched = updated.slice(start, start + height).filter(row => row.gift && !validGifts(row)).length;
    toast(`已粘贴 ${height} 行 × ${width} 列${unmatched ? `，其中 ${unmatched} 行礼品需手动匹配` : ""}${mapping.slice(colStart, colStart + width).includes("status") ? "；状态列保持自动更新" : ""}`, unmatched > 0);
  } catch (error) { toast(error.message, true); }
}
function handlePaste(event) {
  if (event.defaultPrevented || !event.clipboardData) return;
  const text = event.clipboardData.getData("text/plain");
  const addressBlock = event.target.closest?.(".address-field") && /[\t\r\n]/.test(text);
  if (textEditingTarget(event.target) && !addressBlock) return;
  if (!event.clipboardData.types.includes("text/plain") && !event.clipboardData.types.includes("text/html")) return;
  event.preventDefault(); pasteText(text, structuredClipboard(event.clipboardData));
}
$("#registry-grid").addEventListener("paste", handlePaste);
function clearSelectedCells() {
  const bounds = rangeBounds();
  if (!bounds) return;
  if (bounds.left === 3) return toast("状态列由登记结果自动更新，不能手动清空");
  finishEditing(); closeGiftPicker();
  const updated = structuredClone(rows);
  for (let index = bounds.top; index <= bounds.bottom; index++) {
    for (let col = bounds.left; col <= bounds.right; col++) {
      const field = columns()[col];
      if (field === "gift") assignGifts(updated[index], []);
      else if (field === "change_address") updated[index][field] = false;
      else if (field !== "status") updated[index][field] = "";
    }
  }
  if (JSON.stringify(updated) === JSON.stringify(rows)) return;
  remember(); rows = updated; changed(); render(); focusCell(active.id, active.field, true);
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
  const bounds = rangeBounds(), index = bounds?.top ?? rows.findIndex(row => row.id === active.id);
  remember(); rows.splice(index, bounds ? bounds.bottom - bounds.top + 1 : 1);
  if (!rows.length) rows.push(newRow());
  selection = null;
  active = { id: rows[Math.min(index, rows.length - 1)].id, field: "order_no" };
  changed(); render(); focusCell(active.id, active.field);
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
$("#formula-input").addEventListener("change", () => {
  if (!active) return;
  const row = rows.find(row => row.id === active.id), field = active.field, value = $("#formula-input").value;
  if (!row || field === "status") return;
  try {
    const next = field === "change_address" ? asBoolean(value) : value;
    if (field !== "change_address" && value.length > fields[field].max) throw new Error("单元格内容过长");
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
  const incomplete = rows.findIndex(row => filled(row) && !(row.order_no.trim() && validGifts(row) && (!row.change_address || addressFields.every(field => row[field].trim()))));
  if (incomplete >= 0) { toast(`请先完善第 ${incomplete + 2} 行的订单号、礼品及收件信息`, true); focusCell(rows[incomplete].id, !rows[incomplete].order_no.trim() ? "order_no" : !validGifts(rows[incomplete]) ? "gift" : "recipient_name"); return; }
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
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") { event.preventDefault(); saveNow(true); }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z" && !event.shiftKey) { event.preventDefault(); undo(); }
});
document.addEventListener("visibilitychange", () => { if (document.hidden && initialized) saveNow(); });
window.addEventListener("beforeunload", event => { if (version !== savedVersion) { event.preventDefault(); event.returnValue = ""; } });
async function refreshExecutionStates() {
  try {
    if (initialized) {
      const result = await api("/api/registrations/status");
      rowStates = result.row_states;
      for (const row of rows) rowElement(row.id)?.querySelector('[data-field="status"]').replaceChildren(statusNode(row));
      if (active?.field === "status") activate(active.id, active.field);
      updateSummary();
      scheduleColumnSizing();
    }
  } catch { /* 保留最后已知结果；轮询只读状态，永不发起登记请求。 */ }
  finally { setTimeout(refreshExecutionStates, document.hidden ? 5000 : 1500); }
}

let networkChangeId = null;
function renderNetworkNotice(status) {
  const notice = $("#network-notice");
  notice.hidden = !["changed", "offline", "unavailable"].includes(status.state);
  $("#network-notice-text").textContent = status.message || "";
  networkChangeId = status.change_id;
  $("#network-ack").hidden = status.state !== "changed" ||
    !status.current_addresses?.some(address => address.ip === status.configured_ip);
}
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
  const buttons = [...document.querySelectorAll(".toolbar button,#add-bottom,#login-button")];
  buttons.forEach(button => { button.disabled = true; });
  try {
    const config = await api("/api/config"); token = config.token; giftCatalog = config.gifts || []; catalogEditable = config.frontend_version >= 4;
    if (!giftCatalog.length) throw new Error("未配置礼品选项，请重启工作台");
    const document = await api("/api/rows"); revision = document.revision; rowStates = document.row_states || {};
    rows = document.rows.length ? document.rows : Array.from({ length: 18 }, newRow);
    const before = JSON.stringify(rows);
    rows.forEach(row => { const match = matchGifts(row.gift); assignGifts(row, match.ids, match.unmatched); });
    initialized = true;
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
