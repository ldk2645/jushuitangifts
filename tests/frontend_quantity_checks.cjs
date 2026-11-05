#!/usr/bin/env node
"use strict";

/*
 * 荣耀前端数量回归：复制/填充/结构化粘贴/撤销/>99 超限/表头移位。
 *
 * 设计：用 Node vm 从真实的 frontend/app.js 中按函数边界截取源码，在隔离上下文里执行，
 * 只对 DOM/网络相关的旁路函数（toast/render/focusCell/rangeBounds 等）提供无副作用桩。
 * 不复制、不改写被测函数源码；本文件是测试，不是实现。
 */

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const APP_PATH = path.join(__dirname, "..", "frontend", "app.js");
const APP_SOURCE = fs.readFileSync(APP_PATH, "utf8");

function slice(startMarker, endMarker) {
  const start = APP_SOURCE.indexOf(startMarker);
  if (start < 0) throw new Error(`frontend/app.js 缺少起始标记：${startMarker}`);
  const end = APP_SOURCE.indexOf(endMarker, start + startMarker.length);
  if (end < 0) throw new Error(`frontend/app.js 缺少结束标记：${endMarker}`);
  return APP_SOURCE.slice(start, end);
}

// 依赖真实 app.js：函数或其所在代码块必须存在且边界唯一，函数被删除/改名时测试会立即失败。
const CHUNKS = [
  ['const $ = (selector)', 'let rows = []'],
  ['let rows = []', 'let stateEpoch'],
  ['let giftCatalog = []', 'function currentScope('],
  ['function currentScope(', 'function persistScope('],
  ['function scopedGifts(', 'function acceptCatalogContext('],
  ['function inGiftGroup(', 'function groupSelectOptions('],
  ['let hostActionsAvailable', 'const recentGiftOrderLimit'],
  ['let selection = null, editing = null,', 'let manualWidths'],
  ['function newRowId(', 'function giftQuantity('],
  ['function giftQuantity(', 'function rowSignature('],
  ['function rowSignature(', 'function validGifts('],
  ['function validGifts(', 'function status('],
  ['function status(', 'function toast('],
  ['function remember(', 'function changed('],
  ['function parseTSV(', 'function selectionMatrix('],
  ['function selectionMatrix(', 'function textEditingTarget('],
  ['let fillDrag = null', 'function previewFill('],
  ['function asBoolean(', 'function handlePaste('],
  ['function undo(', '$("#undo").addEventListener'],
];
const LOADED_SOURCE = '"use strict";\n' + CHUNKS.map(([start, end]) => slice(start, end)).join("\n");

const CATALOG = [
  {id: "SYN-A", name: "合成收纳包", aliases: []},
  {id: "SYN-B", name: "合成自拍杆", aliases: []},
  {id: "SYN-C", name: "合成保温杯", aliases: []},
  {id: "SYN-SAME-1", name: "同名礼品", aliases: []},
  {id: "SYN-SAME-2", name: "同名礼品", aliases: []},
];

// ---------- 无副作用桩：只替换页面/渲染/网络旁路，不替换数量逻辑 ----------
const toasts = [];
let currentBounds = null;
const clipboardCalls = [];

function stubElement() {
  return {
    className: "", disabled: false, value: "", textContent: "", hidden: false,
    classList: {add() {}, remove() {}, contains() { return false; }, toggle() {}},
    setAttribute() {}, removeAttribute() {}, append() {}, replaceChildren() {},
    addEventListener() {}, removeEventListener() {}, focus() {}, select() {},
    querySelector() { return stubElement(); }, querySelectorAll() { return []; },
    getBoundingClientRect() { return {left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0}; },
  };
}

function escapeHtml(value) {
  return String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
}

// 模拟 clipboardContent() 使用的 <table>：只需 dataset / insertRow / insertCell / outerHTML。
function fakeTable() {
  const rows = [];
  const table = {
    dataset: {},
    insertRow() {
      const tr = {cells: [], insertCell() { const td = {textContent: "", setAttribute() {}}; tr.cells.push(td); return td; }};
      rows.push(tr); return tr;
    },
    get outerHTML() {
      const encoded = table.dataset.giftRegistryRange || "";
      const body = rows.map(tr => `<tr>${tr.cells.map(td => `<td>${escapeHtml(td.textContent)}</td>`).join("")}</tr>`).join("");
      return `<table data-gift-registry-range="${encoded}"><tbody>${body}</tbody></table>`;
    },
  };
  return table;
}

const documentStub = {
  querySelector: () => stubElement(),
  querySelectorAll: () => [],
  getElementById: () => stubElement(),
  createElement: tag => tag === "table" ? fakeTable() : stubElement(),
  addEventListener() {}, removeEventListener() {},
  execCommand() { return false; },
  body: stubElement(),
  hidden: false,
};
const windowStub = {addEventListener() {}, removeEventListener() {}, innerWidth: 1440, innerHeight: 960};

const context = vm.createContext({
  console,
  structuredClone,
  btoa,
  atob,
  TextEncoder,
  TextDecoder,
  crypto: {randomUUID: () => "row-" + Math.random().toString(16).slice(2)},
  document: documentStub,
  window: windowStub,
  localStorage: {getItem: () => null, setItem() {}},
  // 页面/网络旁路桩（真实实现依赖 DOM，与本任务的数量逻辑无关）。
  toast: (message, error = false) => { toasts.push({message: String(message), error: Boolean(error)}); },
  render() {},
  changed() {},
  focusCell() {},
  finishEditing() {},
  closeGiftPicker() {},
  rangeBounds: () => currentBounds,
});

vm.runInContext(LOADED_SOURCE, context, {filename: "frontend/app.js"});

// ---------- 测试框架 ----------
let total = 0, passed = 0, failed = 0;
const failureMessages = [];

function js(code) {
  return vm.runInContext(code, context, {filename: "frontend-quantity-check.js"});
}
function captureJs(code) {
  try { return {value: js(code)}; }
  catch (error) { return {error: String(error && error.message || error)}; }
}
function check(actual, expected, note = "") {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  if (a !== e) throw new Error(`${note ? note + "：" : ""}期望 ${e}，实际 ${a}`);
}
function checkTrue(condition, note) {
  if (!condition) throw new Error(note || "条件不成立");
}
function checkMatch(value, pattern, note) {
  if (!pattern.test(String(value))) throw new Error(`${note ? note + "：" : ""}${JSON.stringify(value)} 不匹配 ${pattern}`);
}
function test(name, fn) {
  total++;
  try {
    js("giftMatcher = null;");
    fn();
    passed++;
    console.log(`PASS ${name}`);
  } catch (error) {
    failed++;
    const message = String(error && error.stack || error);
    failureMessages.push(`${name}\n${message}`);
    console.log(`FAIL ${name}: ${error && error.message ? error.message : error}`);
  }
}

function giftNamesFor(ids) {
  return ids.map(id => (CATALOG.find(gift => gift.id === id) || {}).name).filter(Boolean);
}
function makeRow(id, orderNo, giftIds = [], quantities = {}) {
  const row = {id, order_no: orderNo, gift: giftNamesFor(giftIds).join("、"), gift_ids: [...giftIds],
    gift_quantities: {...quantities}, change_address: false, gift_unmatched: []};
  return row;
}

let seedCounter = 0;
function reseed(rows, options = {}) {
  seedCounter++;
  currentBounds = options.bounds || null;
  const displayedIndexes = options.displayedIndexes || null;
  const catalog = options.catalog || CATALOG;
  const groups = options.groups || {revision: 1, groups: [], memberships: {}};
  toasts.length = 0;
  clipboardCalls.length = 0;
  js(`
    giftCatalog = ${JSON.stringify(catalog)};
    giftGroups = ${JSON.stringify(groups)};
    selectedGiftGroup = ${JSON.stringify(options.selectedGiftGroup || "")};
    scopeInvalid = ${Boolean(options.scopeInvalid)};
    catalogVersion = "test-catalog-${seedCounter}";
    giftMatcher = null;
    rows = ${JSON.stringify(rows)};
    displayedRows = ${displayedIndexes ? `${JSON.stringify(displayedIndexes)}.map(index => rows[index])` : "rows"};
    rowStates = {};
    selectedStatus = ${JSON.stringify(options.selectedStatus || "")};
    initialized = true;
    active = null;
    selection = null;
    history.length = 0;
    version = 0;
  `);
  if (options.active) js(`active = {id: rows[${options.active.index}].id, field: ${JSON.stringify(options.active.field)}};`);
}
function quantitiesJson(rowIndex) { return js(`JSON.stringify(rows[${rowIndex}].gift_quantities || {})`); }
function rowJson(rowIndex) { return js(`JSON.stringify(rows[${rowIndex}])`); }
function lastToast() { return toasts.length ? toasts[toasts.length - 1] : null; }

function stablePayloadText(payload) { return JSON.stringify(payload); }

// ---------- 1. assignGifts 的“未提供 / 明确提供”数量语义 ----------
test("原复现：assignGifts(目标5件, ['SYN-A'], [], {}) 必须覆盖为 1 件", () => {
  reseed([makeRow("r1", "SYN-1", ["SYN-A"], {"SYN-A": 5})]);
  js("assignGifts(rows[0], ['SYN-A'], [], {})");
  check(quantitiesJson(0), "{}");
  check(js("giftQuantity(rows[0], 'SYN-A')"), 1);
});

test("未提供数量参数（undefined）保留仍被选中编码的原数量", () => {
  reseed([makeRow("r1", "SYN-1", ["SYN-A", "SYN-B"], {"SYN-A": 5, "SYN-B": 2})]);
  js("assignGifts(rows[0], ['SYN-A', 'SYN-B', 'SYN-C'])");
  check(quantitiesJson(0), JSON.stringify({"SYN-A": 5, "SYN-B": 2}));
  js("assignGifts(rows[0], ['SYN-A'])");
  check(quantitiesJson(0), JSON.stringify({"SYN-A": 5}));
});

test("未提供数量参数（null）同样保留原数量", () => {
  reseed([makeRow("r1", "SYN-1", ["SYN-A"], {"SYN-A": 5})]);
  js("assignGifts(rows[0], ['SYN-A'], [], null)");
  check(quantitiesJson(0), JSON.stringify({"SYN-A": 5}));
});

test("明确空映射 {} 时未列出的编码按 1 件覆盖", () => {
  reseed([makeRow("r1", "SYN-1", ["SYN-A", "SYN-B"], {"SYN-A": 5, "SYN-B": 7})]);
  js("assignGifts(rows[0], ['SYN-A', 'SYN-B'], [], {})");
  check(quantitiesJson(0), "{}");
});

test("明确映射只覆盖列出的编码，其余编码按 1 件", () => {
  reseed([makeRow("r1", "SYN-1", ["SYN-A", "SYN-B"], {"SYN-A": 5, "SYN-B": 7})]);
  js("assignGifts(rows[0], ['SYN-A', 'SYN-B'], [], {'SYN-A': 3})");
  check(quantitiesJson(0), JSON.stringify({"SYN-A": 3}));
});

test("显式数量 1 覆盖目标旧数量 5", () => {
  reseed([makeRow("r1", "SYN-1", ["SYN-A"], {"SYN-A": 5})]);
  js("assignGifts(rows[0], ['SYN-A'], [], {'SYN-A': 1})");
  check(quantitiesJson(0), "{}");
});

test("显式数量超过 99 时阻止应用且不改动目标行", () => {
  reseed([makeRow("r1", "SYN-1", ["SYN-A"], {"SYN-A": 5})]);
  const result = captureJs("assignGifts(rows[0], ['SYN-A'], [], {'SYN-A': 100})");
  checkTrue(Boolean(result.error), "超过 99 件应抛错阻止应用");
  checkMatch(result.error, /99 件/, "错误信息应说明 99 件上限");
  check(quantitiesJson(0), JSON.stringify({"SYN-A": 5}));
});

test("选择器取消一个礼品时保留其它礼品的数量", () => {
  reseed([makeRow("r1", "SYN-1", ["SYN-A", "SYN-B"], {"SYN-A": 5, "SYN-B": 2})]);
  js("assignGifts(rows[0], ['SYN-A'])");
  check(js("JSON.stringify(rows[0].gift_ids)"), JSON.stringify(["SYN-A"]));
  check(quantitiesJson(0), JSON.stringify({"SYN-A": 5}));
  js("assignGifts(rows[0], ['SYN-A', 'SYN-B'])");
  check(quantitiesJson(0), JSON.stringify({"SYN-A": 5}));
});

// ---------- 2. matchGifts 重复计数与 >99 超限信号 ----------
test("matchGifts 重复 3 次计数为 3，且不返回 overflow 字段", () => {
  reseed([]);
  const result = js("JSON.stringify(matchGifts('合成收纳包、合成收纳包、合成收纳包'))");
  check(result, JSON.stringify({ids: ["SYN-A"], unmatched: [], quantities: {"SYN-A": 3}}));
  check(js("'overflow' in matchGifts('合成收纳包、合成收纳包')"), false);
});

test("matchGifts 重复 99 次仍为正常计数", () => {
  reseed([]);
  const text = Array(99).fill("合成收纳包").join("、");
  const result = js(`JSON.stringify(matchGifts(${JSON.stringify(text)}))`);
  check(result, JSON.stringify({ids: ["SYN-A"], unmatched: [], quantities: {"SYN-A": 99}}));
});

test("matchGifts 重复 100 次不再静默截断，明确返回 overflow", () => {
  reseed([]);
  const text = Array(100).fill("合成收纳包").join("、");
  const result = JSON.parse(js(`JSON.stringify(matchGifts(${JSON.stringify(text)}))`));
  check(result.ids, ["SYN-A"]);
  check(result.quantities, {"SYN-A": 100});
  check(result.overflow, [{id: "SYN-A", quantity: 100}]);
});

test("matchGifts 混合多个编码时分别计数，只报告超限编码", () => {
  reseed([]);
  const text = [...Array(100).fill("合成收纳包"), ...Array(2).fill("合成自拍杆")].join("、");
  const result = JSON.parse(js(`JSON.stringify(matchGifts(${JSON.stringify(text)}))`));
  check(result.quantities, {"SYN-A": 100, "SYN-B": 2});
  check(result.overflow, [{id: "SYN-A", quantity: 100}]);
});

test("matchGifts 精确编码路径返回 1 件且无 overflow", () => {
  reseed([]);
  const result = js("JSON.stringify(matchGifts('SYN-B'))");
  check(result, JSON.stringify({ids: ["SYN-B"], unmatched: [], quantities: {"SYN-B": 1}}));
});

test("matchGifts 命中缓存后仍会报告 overflow", () => {
  reseed([]);
  const text = Array(100).fill("合成收纳包").join("、");
  js(`matchGifts(${JSON.stringify(text)})`);
  const result = JSON.parse(js(`JSON.stringify(matchGifts(${JSON.stringify(text)}))`));
  check(result.overflow, [{id: "SYN-A", quantity: 100}]);
  check(result.quantities, {"SYN-A": 100});
});

test("assertGiftQuantityLimit 对 99 放行、对 100 抛错", () => {
  reseed([]);
  const ok = captureJs("assertGiftQuantityLimit(matchGifts('合成收纳包、合成收纳包'))");
  check(ok.value, undefined);
  const over = Array(100).fill("合成收纳包").join("、");
  const bad = captureJs(`assertGiftQuantityLimit(matchGifts(${JSON.stringify(over)}))`);
  checkTrue(Boolean(bad.error), "100 件必须抛错");
  checkMatch(bad.error, /99 件/, "错误信息应说明 99 件上限");
});

// ---------- 3. 私有剪贴板矩阵与结构化粘贴 ----------
function copyAndPaste({sourceBounds, targetBounds}) {
  currentBounds = sourceBounds;
  const content = js("clipboardContent()");
  context.__content = content;
  currentBounds = targetBounds;
  js(`pasteText(__content.text, structuredClipboard({getData: type =>
    type === "text/plain" ? __content.text : type === "text/html" ? __content.html : ""}))`);
  return content;
}

test("clipboardContent 的 matrix/giftIds/giftQuantities 逐行对齐", () => {
  reseed([
    makeRow("r0", "SYN-1", ["SYN-A"], {}),
    makeRow("r1", "SYN-2", ["SYN-A", "SYN-B"], {"SYN-A": 3}),
  ], {bounds: {top: 0, bottom: 1, left: 1, right: 1}});
  const content = js("clipboardContent()");
  const payload = JSON.parse(content.json);
  check(payload.version, 2);
  check(payload.matrix, [["合成收纳包"], ["合成收纳包、合成自拍杆"]]);
  check(payload.giftIds, [[["SYN-A"]], [["SYN-A", "SYN-B"]]]);
  check(payload.giftQuantities, [[{}], [{"SYN-A": 3}]]);
  check(payload.matrix.length, payload.giftIds.length);
  check(payload.matrix.length, payload.giftQuantities.length);
});

test("私有剪贴板类型粘贴完整保留来源数量并覆盖目标旧数量", () => {
  reseed([
    makeRow("r0", "SYN-1", ["SYN-A"], {}),
    makeRow("r1", "SYN-2", ["SYN-A", "SYN-B"], {"SYN-A": 3}),
    makeRow("r2", "SYN-3", ["SYN-A"], {"SYN-A": 5}),
    makeRow("r3", "SYN-4", ["SYN-A", "SYN-B"], {"SYN-A": 5, "SYN-B": 7}),
  ]);
  currentBounds = {top: 0, bottom: 1, left: 1, right: 1};
  const content = js("clipboardContent()");
  context.__content = content;
  currentBounds = {top: 2, bottom: 2, left: 1, right: 1};
  js(`pasteText(__content.text, structuredClipboard({getData: type => type === "text/plain" ? __content.text : type === "text/html" ? __content.html : ""}))`);
  check(js("JSON.stringify(rows[2].gift_ids)"), JSON.stringify(["SYN-A"]));
  check(quantitiesJson(2), "{}");
  check(js("JSON.stringify(rows[3].gift_ids)"), JSON.stringify(["SYN-A", "SYN-B"]));
  check(quantitiesJson(3), JSON.stringify({"SYN-A": 3}));
});

test("HTML 回退通道的结构化粘贴同样完整保留数量", () => {
  reseed([
    makeRow("r0", "SYN-1", ["SYN-A", "SYN-B"], {"SYN-B": 2}),
    makeRow("r1", "SYN-2", ["SYN-A"], {"SYN-A": 5}),
  ]);
  currentBounds = {top: 0, bottom: 0, left: 1, right: 1};
  const content = js("clipboardContent()");
  context.__content = content;
  const matrix = js(`structuredClipboard({getData: type => type === "text/html" ? __content.html : ""})`);
  checkTrue(Array.isArray(matrix) && matrix.length === 1, "HTML 剪贴板应可解析出 1 行");
  check(js(`JSON.stringify(Array.from(structuredClipboard({getData: type => type === "text/html" ? __content.html : ""}).giftQuantities))`),
    JSON.stringify([[{"SYN-B": 2}]]));
  currentBounds = {top: 1, bottom: 1, left: 1, right: 1};
  js(`pasteText(__content.text, structuredClipboard({getData: type => type === "text/html" ? __content.html : ""}))`);
  check(quantitiesJson(1), JSON.stringify({"SYN-B": 2}));
});

test("结构化粘贴：来源默认 1 件覆盖目标 5 件", () => {
  reseed([
    makeRow("r0", "SYN-1", ["SYN-A"], {}),
    makeRow("r1", "SYN-2", ["SYN-A"], {"SYN-A": 5}),
  ]);
  copyAndPaste({sourceBounds: {top: 0, bottom: 0, left: 1, right: 1}, targetBounds: {top: 1, bottom: 1, left: 1, right: 1}});
  check(quantitiesJson(1), "{}");
});

test("v2 表头行移除时 matrix、giftIds、giftQuantities 必须一起移位", () => {
  reseed([makeRow("r0", "SYN-OLD-1"), makeRow("r1", "SYN-OLD-2")]);
  // 编码/数量矩阵与文本矩阵同宽：礼品在第 2 列（索引 1），与复制时生成的元数据一致。
  const payload = {
    version: 2,
    matrix: [["订单号", "礼品"], ["SYN-H-1", "合成收纳包"], ["SYN-H-2", "合成自拍杆"]],
    giftIds: [[null, null], [null, ["SYN-A"]], [null, ["SYN-B"]]],
    giftQuantities: [[null, null], [null, {"SYN-A": 3}], [null, {"SYN-B": 2}]],
  };
  context.__payload = stablePayloadText(payload);
  currentBounds = {top: 0, bottom: 0, left: 0, right: 0};
  js(`pasteText(__payload, structuredClipboard({getData: type => type === rangeClipboardType ? __payload : ""}))`);
  check(js("rows[0].order_no"), "SYN-H-1");
  check(js("JSON.stringify(rows[0].gift_ids)"), JSON.stringify(["SYN-A"]));
  check(quantitiesJson(0), JSON.stringify({"SYN-A": 3}));
  check(js("rows[1].order_no"), "SYN-H-2");
  check(js("JSON.stringify(rows[1].gift_ids)"), JSON.stringify(["SYN-B"]));
  check(quantitiesJson(1), JSON.stringify({"SYN-B": 2}));
});

test("旧 4 列格式转换时编码/数量矩阵保持对齐", () => {
  reseed([makeRow("r0", "SYN-OLD-1"), makeRow("r1", "SYN-OLD-2")], {active: {index: 0, field: "order_no"}});
  const payload = {
    version: 2,
    matrix: [["订单号", "礼品", "是否改地址", "状态"],
      ["SYN-L-1", "合成收纳包", "否", "成功"],
      ["SYN-L-2", "合成自拍杆", "否", "成功"]],
    giftIds: [[null, null, null, null], [null, ["SYN-A"], null, null], [null, ["SYN-B"], null, null]],
    giftQuantities: [[null, null, null, null], [null, {"SYN-A": 4}, null, null], [null, {"SYN-B": 2}, null, null]],
  };
  context.__payload = stablePayloadText(payload);
  js(`pasteText(__payload, structuredClipboard({getData: type => type === rangeClipboardType ? __payload : ""}))`);
  check(js("rows[0].order_no"), "SYN-L-1");
  check(quantitiesJson(0), JSON.stringify({"SYN-A": 4}));
  check(js("rows[1].order_no"), "SYN-L-2");
  check(quantitiesJson(1), JSON.stringify({"SYN-B": 2}));
  check(js("JSON.stringify(Object.keys(rows[0]).filter(key => ['recipient_name', 'phone', 'address'].includes(key)))"), "[]");
});

// ---------- 4. 纯文本/Excel 往返与 >99 阻止 ----------
test("纯文本粘贴 100 次重复被阻止且表格不变", () => {
  reseed([makeRow("r0", "SYN-1", ["SYN-A"], {"SYN-A": 5})], {bounds: {top: 0, bottom: 0, left: 1, right: 1}});
  const before = rowJson(0);
  const text = Array(100).fill("合成收纳包").join("、");
  js(`pasteText(${JSON.stringify(text)})`);
  check(rowJson(0), before);
  check(js("history.length"), 0);
  const toast = lastToast();
  checkTrue(Boolean(toast && toast.error), "应显示错误提示");
  checkMatch(toast.message, /99 件/, "提示应说明 99 件上限");
  checkMatch(toast.message, /未应用/, "提示应说明本次未应用");
});

test("纯文本粘贴 99 次重复按 99 件应用", () => {
  reseed([makeRow("r0", "SYN-1")], {bounds: {top: 0, bottom: 0, left: 1, right: 1}});
  const text = Array(99).fill("合成收纳包").join("、");
  js(`pasteText(${JSON.stringify(text)})`);
  check(quantitiesJson(0), JSON.stringify({"SYN-A": 99}));
  check(js("history.length"), 1);
  checkTrue(!toasts.some(item => item.error), "99 件不应报错");
});

test("纯文本中的 ×2 不会被当作编码/名称身份，也不会无条件当成数量", () => {
  reseed([makeRow("r0", "SYN-1", ["SYN-A"], {"SYN-A": 5})], {bounds: {top: 0, bottom: 0, left: 1, right: 1}});
  js("pasteText('合成收纳包 ×2')");
  check(js("JSON.stringify(rows[0].gift_ids)"), JSON.stringify(["SYN-A"]));
  // “×2”不是名称/编码身份的一部分，也不是可安全解析的数量标记：作为未匹配内容明确暴露，数量按 1 件。
  check(js("JSON.stringify(rows[0].gift_unmatched)"), JSON.stringify(["×2"]));
  check(js("rows[0].gift"), "合成收纳包、×2");
  checkTrue(!js("JSON.stringify(rows[0].gift_ids)").includes("×"), "×2 不得进入编码");
  // Excel/纯文本往返不携带私有数量：按 1 件记录，限制在界面提示（见 test_frontend_quantity_js.py）。
  check(quantitiesJson(0), "{}");
  check(js("giftQuantity(rows[0], 'SYN-A')"), 1);
  // “×2”无法安全解析为数量，界面提示该行需手动匹配，而不是静默当作 2 件或名称。
  checkTrue(toasts.some(item => /需手动匹配/.test(item.message)), "应提示该行需手动匹配，而不是静默处理 ×2");
});

test("复制纯文本不写 ×数量 标记，数量只存在私有剪贴板元数据", () => {
  reseed([makeRow("r0", "SYN-1", ["SYN-A"], {"SYN-A": 3})], {bounds: {top: 0, bottom: 0, left: 1, right: 1}});
  const content = js("clipboardContent()");
  check(content.text, "合成收纳包");
  checkTrue(!content.text.includes("×"), "纯文本不应写入 ×数量");
  checkTrue(content.html.includes('data-gift-registry-range="'), "HTML 应携带私有区域数据");
  check(JSON.parse(content.json).giftQuantities, [[{"SYN-A": 3}]]);
});

// ---------- 5. applyFill / Ctrl+D / 拖动 / 双击共用填充路径 ----------
test("applyFill：来源默认 1 件覆盖目标 5 件", () => {
  reseed([
    makeRow("r0", "SYN-1", ["SYN-A"], {}),
    makeRow("r1", "SYN-2", ["SYN-A"], {"SYN-A": 5}),
  ], {bounds: {top: 0, bottom: 0, left: 1, right: 1}});
  js("applyFill(fillPlan(rangeBounds()), 1)");
  check(quantitiesJson(1), "{}");
  check(js("giftQuantity(rows[1], 'SYN-A')"), 1);
});

test("applyFill：来源缺少 gift_quantities 属性时仍按默认 1 件覆盖", () => {
  const source = makeRow("r0", "SYN-1", ["SYN-A"]);
  delete source.gift_quantities;
  reseed([source, makeRow("r1", "SYN-2", ["SYN-A"], {"SYN-A": 5})],
    {bounds: {top: 0, bottom: 0, left: 1, right: 1}});
  js("applyFill(fillPlan(rangeBounds()), 1)");
  check(quantitiesJson(1), "{}");
});

test("applyFill：来源 5 件覆盖目标 1 件", () => {
  reseed([
    makeRow("r0", "SYN-1", ["SYN-A"], {"SYN-A": 5}),
    makeRow("r1", "SYN-2", ["SYN-A"], {}),
  ], {bounds: {top: 0, bottom: 0, left: 1, right: 1}});
  js("applyFill(fillPlan(rangeBounds()), 1)");
  check(quantitiesJson(1), JSON.stringify({"SYN-A": 5}));
});

test("applyFill：两个 SKU 其中一个默认 1 件", () => {
  reseed([
    makeRow("r0", "SYN-1", ["SYN-A", "SYN-B"], {"SYN-A": 3}),
    makeRow("r1", "SYN-2", ["SYN-A", "SYN-B"], {"SYN-A": 9, "SYN-B": 7}),
  ], {bounds: {top: 0, bottom: 0, left: 1, right: 1}});
  js("applyFill(fillPlan(rangeBounds()), 1)");
  check(js("JSON.stringify(rows[1].gift_ids)"), JSON.stringify(["SYN-A", "SYN-B"]));
  check(quantitiesJson(1), JSON.stringify({"SYN-A": 3}));
  check(js("JSON.stringify([giftQuantity(rows[1], 'SYN-A'), giftQuantity(rows[1], 'SYN-B')])"), JSON.stringify([3, 1]));
});

test("applyFill：同名不同编码按编码身份复制", () => {
  reseed([
    makeRow("r0", "SYN-1", ["SYN-SAME-1"], {"SYN-SAME-1": 2}),
    makeRow("r1", "SYN-2", ["SYN-SAME-2"], {"SYN-SAME-2": 3}),
  ], {bounds: {top: 0, bottom: 0, left: 1, right: 1}});
  js("applyFill(fillPlan(rangeBounds()), 1)");
  check(js("JSON.stringify(rows[1].gift_ids)"), JSON.stringify(["SYN-SAME-1"]));
  check(quantitiesJson(1), JSON.stringify({"SYN-SAME-1": 2}));
});

test("fillSelectedDown（Ctrl+D 路径）覆盖目标旧数量", () => {
  reseed([
    makeRow("r0", "SYN-1", ["SYN-A"], {}),
    makeRow("r1", "SYN-2", ["SYN-A"], {"SYN-A": 5}),
  ], {bounds: {top: 0, bottom: 1, left: 1, right: 1}});
  js("fillSelectedDown()");
  check(quantitiesJson(1), "{}");
  checkMatch(lastToast() && lastToast().message, /已向下填充 1 行/, "应提示填充 1 行");
});

test("拖动/双击填充路径：多行来源循环复制编码与数量", () => {
  reseed([
    makeRow("r0", "SYN-1", ["SYN-A"], {}),
    makeRow("r1", "SYN-2", ["SYN-B"], {"SYN-B": 2}),
    makeRow("r2", "SYN-3", ["SYN-A", "SYN-B"], {"SYN-A": 9, "SYN-B": 9}),
    makeRow("r3", "SYN-4", ["SYN-A", "SYN-B"], {"SYN-A": 9, "SYN-B": 9}),
  ], {bounds: {top: 0, bottom: 1, left: 1, right: 1}});
  js("applyFill(fillPlan(rangeBounds()), 3)");
  check(js("JSON.stringify(rows[2].gift_ids)"), JSON.stringify(["SYN-A"]));
  check(quantitiesJson(2), "{}");
  check(js("JSON.stringify(rows[3].gift_ids)"), JSON.stringify(["SYN-B"]));
  check(quantitiesJson(3), JSON.stringify({"SYN-B": 2}));
});

test("筛选时填充只影响可见行，隐藏行数量保持不变", () => {
  reseed([
    makeRow("r0", "SYN-1", ["SYN-A"], {}),
    makeRow("r1", "SYN-2", ["SYN-A"], {"SYN-A": 5}),
    makeRow("r2", "SYN-3", ["SYN-A"], {"SYN-A": 9}),
  ], {displayedIndexes: [0, 2], selectedStatus: "ready",
    bounds: {top: 0, bottom: 0, left: 1, right: 1}});
  js("applyFill(fillPlan(rangeBounds()), 1)");
  check(quantitiesJson(2), "{}");
  check(quantitiesJson(1), JSON.stringify({"SYN-A": 5}));
});

test("填充向目标行传递显式数量映射（默认 1 件时也是 {}，不是 undefined）", () => {
  reseed([
    makeRow("r0", "SYN-1", ["SYN-A"], {}),
    makeRow("r1", "SYN-2", ["SYN-A"], {"SYN-A": 5}),
  ], {bounds: {top: 0, bottom: 0, left: 1, right: 1}});
  const realAssign = context.assignGifts;
  try {
    context.assignGifts = (...args) => {
      clipboardCalls.push(args.map(value => {
        if (value === undefined) return "undefined";
        if (value === null) return "null";
        return JSON.stringify(value);
      }));
      return realAssign(...args);
    };
    js("applyFill(fillPlan(rangeBounds()), 1)");
  } finally {
    context.assignGifts = realAssign;
  }
  check(clipboardCalls.length, 1);
  check(JSON.parse(clipboardCalls[0][1]), ["SYN-A"]);
  check(JSON.parse(clipboardCalls[0][2]), []);
  check(clipboardCalls[0][3], "{}");
});

test("结构化粘贴向目标行传递显式数量映射（默认 1 件时也是 {}）", () => {
  reseed([
    makeRow("r0", "SYN-1", ["SYN-A"], {}),
    makeRow("r1", "SYN-2", ["SYN-A"], {"SYN-A": 5}),
  ]);
  currentBounds = {top: 0, bottom: 0, left: 1, right: 1};
  const content = js("clipboardContent()");
  context.__content = content;
  const realAssign = context.assignGifts;
  try {
    context.assignGifts = (...args) => {
      clipboardCalls.push(args.map(value => value === undefined ? "undefined" : value === null ? "null" : JSON.stringify(value)));
      return realAssign(...args);
    };
    currentBounds = {top: 1, bottom: 1, left: 1, right: 1};
    js(`pasteText(__content.text, structuredClipboard({getData: type => type === "text/plain" ? __content.text : type === "text/html" ? __content.html : ""}))`);
  } finally {
    context.assignGifts = realAssign;
  }
  check(clipboardCalls.length, 1);
  check(clipboardCalls[0][3], "{}");
  check(quantitiesJson(1), "{}");
});

test("撤销恢复填充前的整行数量", () => {
  reseed([
    makeRow("r0", "SYN-1", ["SYN-A"], {}),
    makeRow("r1", "SYN-2", ["SYN-A"], {"SYN-A": 5}),
  ], {bounds: {top: 0, bottom: 0, left: 1, right: 1}});
  const before = rowJson(1), signatureBefore = js("rowSignature(rows[1])");
  js("applyFill(fillPlan(rangeBounds()), 1)");
  check(quantitiesJson(1), "{}");
  js("undo()");
  check(rowJson(1), before);
  check(js("rowSignature(rows[1])"), signatureBefore);
  check(js("history.length"), 0);
});

// ---------- 6. 身份签名兼容 / 归一化 / 跨组拒绝 ----------
test("数量参与身份，但历史全部 1 件的签名保持兼容", () => {
  reseed([]);
  js(`
    __rowBase = {id: "r", order_no: "SYN-1", gift: "合成收纳包", gift_ids: ["SYN-A"], change_address: false, gift_unmatched: []};
    __rowEmpty = {...__rowBase, gift_quantities: {}};
    __rowMissing = {...__rowBase};
    __rowCounted = {...__rowBase, gift_quantities: {"SYN-A": 2}};
  `);
  check(js("rowSignature(__rowEmpty)"), js("rowSignature(__rowMissing)"));
  check(js("taskIdentity(__rowEmpty)"), js("taskIdentity(__rowMissing)"));
  checkTrue(js("rowSignature(__rowCounted) !== rowSignature(__rowEmpty)"), "有数量时签名必须变化");
  checkTrue(js("taskIdentity(__rowCounted) !== taskIdentity(__rowEmpty)"), "有数量时任务身份必须变化");
});

test("normalizeRow 归一化数量：1 件不存字段、>1 保留", () => {
  reseed([]);
  js("__normalized = normalizeRow({gift_ids: ['SYN-A'], gift_quantities: {'SYN-A': 1}});");
  check(js("JSON.stringify(__normalized.gift_quantities)"), "{}");
  js("__normalized = normalizeRow({gift_ids: ['SYN-A'], gift_quantities: {'SYN-A': 3}});");
  check(js("JSON.stringify(__normalized.gift_quantities)"), JSON.stringify({"SYN-A": 3}));
});

test("跨组来源填充被整体拒绝且不修改任何行", () => {
  const groups = {revision: 2, groups: [{id: "g", name: "甲组"}, {id: "other", name: "乙组"}],
    memberships: {"SYN-A": ["g"]}};
  reseed([
    makeRow("r0", "SYN-1", ["SYN-A"]),
    makeRow("r1", "SYN-2", ["SYN-A"], {"SYN-A": 5}),
  ], {bounds: {top: 0, bottom: 0, left: 1, right: 1}, groups, selectedGiftGroup: "other"});
  const before = js("JSON.stringify(rows)");
  js("applyFill(fillPlan(rangeBounds()), 1)");
  check(js("JSON.stringify(rows)"), before);
  checkMatch(lastToast() && lastToast().message, /当前分组/, "应提示当前分组不可用");
  checkTrue(Boolean(lastToast().error), "跨组填充应显示错误");
});

// ---------- 输出 ----------
const summary = {total, passed, failed};
console.log(`SOURCE ${APP_PATH}`);
console.log(`SUMMARY ${JSON.stringify(summary)}`);
if (failed) {
  console.error(`\n${failed} 个数量回归用例失败：`);
  for (const message of failureMessages) console.error(`--- ${message}`);
  process.exitCode = 1;
}
