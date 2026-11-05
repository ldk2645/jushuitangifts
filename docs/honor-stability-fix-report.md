# 荣耀登记稳定性修复报告（2026-11-05）

执行来源：`docs/deepseek-harness-stability-repair-prompt-2026-11-05.md`。代码基线 `efd62f0`（版本 `1.3.0-ry.1`，edition=honor），唯一修改目标 `D:\UserData\Desktop\NL\gifts-ry`。本轮只做代码、文档和隔离测试；未访问真实平台提交，未操作真实订单，未重启工作台，未改账号/DPAPI/ACL/浏览器沙箱设置。用户工作区的 `config/gifts.json` 修改原样保留，没有被 reset 或覆盖。

## 1. 复现证据与基线

开始前记录了 HEAD、工作区状态、`config/gifts.json`、`config/gift_groups.local.json`、`data/registrations.json`、`data/manual_status.json` 的 SHA-256 及任务文件摘要，备份在 `.tmp/stability-baseline-20261105/`（`.tmp` 不入 Git）。Codex 交接复现全部先于修复执行：

| 复现脚本 | 修复前观测 |
| --- | --- |
| `.tmp/codex-review-20261105.py` | 3 单模拟浏览器故障 → 3 次查询、3 单全部 failed、0 提交；数量中断表单被永久保留（`form_still_open=true`、`next_query_can_refresh=false`）；2 秒时钟偏差的合成回执被判旧任务 |
| `.tmp/codex-review-quantity-20261105.cjs` | 来源 1 件复制/填充到目标原 5 件 → 实际仍为 5（期望 1） |
| `.tmp/codex-review-full-tests-20261105.log` | 627 项 1 error（`test_network...ConnectionAbortedError` 10053） |
| `.tmp/codex-review-http-retest-20261105.log` | 同用例 10 次复测 1 次 10053（间歇） |
| 任务存档扫描（只读） | 19 failed / 9 success；16:26–16:32 失败含班牛自动登录未确认，其中两条提交状态 uncertain；历史失败不能证明单一根因 |

以上复现均使用临时目录、合成订单、Mock/隔离 Selenium；没有拿真实客户订单测试。

## 2. P1：一单共享环境故障会消耗整批订单

### 触发条件

同一批次内先执行 A，再执行 B、C；A 在查询、打开表单、填写或提交阶段遇到共享浏览器/认证/页面故障，原队列只把 A 记为 failed，然后继续领取 B、C，导致每一单重复撞同一环境错误，3 单全部 failed，且日志把共享故障混进业务失败历史。原实现没有结构化的“本单业务失败 / 共享环境需恢复 / 需人工介入 / 提交不确定”分类，`_work` 与 `_process` 也缺少暂停信号。

### 根因

- `registration.py` 的串行 worker 在某行 failed 后无条件继续领取后续任务；没有批次级暂停状态。
- 页面适配层虽然已有固定错误提示和提交边界保护，但异常类型区分不足：认证、驱动断开、可恢复页面状态、未提交业务失败都表现为普通 `PlatformOperationError` 或未包装的 Selenium 异常。
- 中间表单只按“已选礼品必须与前缀完全匹配”判断能否关闭，数量中断（请求 2 件、表单只有默认 1 件）时无法恢复。
- 提交边界在可点击性等待之前就记录 `click_attempted=True`，无法区分“检查失败未点击”和“原生 click 已发起但结果不确定”。

### 修复

**结构化分类（不解析中文 detail）**

- `workflow.py` 新增 `ManualInterventionRequired`（`pause_class=manual`）与 `PageRecoverableError`（`pause_class=recoverable`）；`AuthenticationRequired` 改为人工介入子类。
- `Workflow.execute` 失败分支生成结构化 `pause` 字段（class/site/stage/recovery_condition）并随 failed 事件交给队列；提交阶段用异常上的 `click_attempted` 属性区分 `not_submitted` 与 uncertain，保留原始阶段、错误和提交边界。队列只消费 `pause` 字段，不解析 detail 文本。
- 页面层：表单归属未知/人工改动/清理不掉的表单→人工介入；提交后任意失败→人工介入 + uncertain；数量控件失效、可证明工具栏页面失败→可恢复页面错误；`/api/registrations/*` 环境准备失败仍保持 `registration_not_started`，不创建失败订单。
- 真实适配器把 `WebDriverException`（聚水潭查询/复查、班牛浏览器获取）包装为 `ManualInterventionRequired`；复查阶段平台错误也转人工暂停，未知 `PlatformOperationError` 不冒充环境故障。

**暂停状态与持久化**

- 新增 `waiting`（暂停待恢复）状态：不是 failed/cancelled/success，未查询、未提交、不计入失败历史。
- 暂停记录持久化到任务目录 `.queue-pause.state`（点号 + 非 `.json` 扩展名，不进入任务文件扫描）；包含原因、平台、阶段、恢复条件、失败任务/行、时间。
- 队列报告收到 `pause` 后：当前行按 failed（未提交或 uncertain）落盘 → 对其余所有 `queued` 未执行项落盘为 `waiting` → 排空 `pending`（review 任务标记 interrupted）→ `unfinished_tasks` 归零；工作台仍可访问、可取消、可正常关闭。
- 重启：加载暂停记录并保持暂停；若发现孤立 `waiting` 项而没有暂停记录，保守转成人工暂停，不自动派发。暂停期间 `start()`/`retry_failed()`/`retry_all_failed()` 拒绝新执行，自动只读复核也不会排入。
- 可恢复故障在订单间边界调用一次 `SeleniumOperations.recover_environment()`：只关闭本工具自有浏览器，重新预留配置，只读验证聚水潭订单页与班牛礼赠列表就绪；成功则继续 B、C（A 不重提），失败则升级为人工暂停。归属不明/人工修改的表单在页面层已经转人工，不会被自动刷新丢弃。
- 主机恢复：`POST /api/registrations/resume`（仅本机回环 + CSRF）先只读验证站点/页面就绪，再清除暂停、把 waiting 项改回 queued 重新入队；失败返回 409 `resume_not_ready` 且保持暂停。健康轮询和普通刷新永不自动恢复或重提。
- 取消：`cancel_queued` 扩展支持 `waiting` 项，只取消未领取、未提交项，记录 `cancelled_by=host` 与未提交依据；不会伪装成 failed/success。
- 前端：暂停横幅显示原因/平台/阶段/恢复条件/等待数量；等待行显示“暂停待恢复”并可取消；“主机恢复”按钮只对主机显示，调用恢复接口并展示结果；开始按钮在暂停时禁用；新增 `waiting` 状态筛选与配色。纯文本格式限制、数量限制文案同步在界面说明。

**表单中间状态与提交边界**

- 表单跟踪新增 `_form_progress`：按编码记录自动化实际写入的礼品及数量。`_discard_unsubmitted_form` 允许关闭“本次自动化写入、仍是请求子集、数量为 1 或已写入目标数量、非补发/零金额/名称不符均拒绝”的中间表单；未知编码、重复行、未知数量、人工编辑痕迹、已出现提交点击一律保留。数量中断的复现因此可安全清理，不再永久阻塞后续订单。
- 数量输入改为有界 3 次重新定位 + Ctrl+A 覆盖 + 失焦 + 只读核对；Vue 重渲染导致元素失效时先重新按编码定位，不做无界点击，不绕过遮挡。
- `_click_once` 新增 `before_click` 回调：所有可点击性/遮挡等待完成后、发起原生 click 前才记录提交边界与表单 token。可点击性失败 → 未提交并按中间表单规则清理；原生 click 一旦发起，即使抛异常也按 uncertain 处理，绝不再次点击/重试。

### 验收（正式回归）

- `tests/test_environment_recovery.py`：A 认证失败/可恢复页面故障/提交 uncertain/恢复失败/重启/取消/关闭 场景；B、C 未被批量 failed，恢复后各执行一次；重启后暂停仍在；取消 waiting 生效；原 A 不重提。
- `tests/test_batch_fault_matrix.py`：A 单在查询、打开表单、拉单、礼品选择、数量修改、提交前准备、原生提交、表单关闭、回执、聚水潭核验各阶段失败；核对后续领取数、提交次数、恢复次数、暂停/失败状态、恢复后执行次数和 A 防重。
- `tests/test_banniu_page.py` / `test_banniu_form_cleanup.py`：提交前可点击性失败不越界；原生 click 异常保留 uncertain；数量中断表单可安全关闭；未知数量/人工编辑/未知编码保留表单。
- `tests/test_frontend.py`：`/api/registrations/status` 返回 paused；恢复接口要求 CSRF、未就绪 409 保持暂停、就绪后 waiting 行执行一次且原失败行不重提。

## 3. P1：复制/下拉填充把多件数量错误保留为目标旧数量

修复 `frontend/app.js`：

- `assignGifts` 明确区分“未提供数量映射（undefined/null）→保留仍被选中编码原数量”“显式映射（含 `{}`）→未列出的编码按 1 件覆盖”。整体复制、Ctrl+D、填充柄、双击填充、结构化粘贴均传显式映射；来源 1 件现在能覆盖目标旧数量。
- `matchGifts` 同一礼品重复出现超过 99 次不再静默截断；返回 `overflow`，粘贴/公式编辑/新增礼赠路径提示并阻止整次应用。
- 剪贴板 v2 的 `matrix`/`giftIds`/`giftQuantities` 表头移除同步移位；行数不一致时退回纯文本（每项 1 件）。纯文本/Excel 往返不编码数量，界面明确说明限制；纯文本里的“×2”不会被静默当名称或 2 件。
- 回归：`tests/frontend_quantity_checks.cjs`（Node vm 加载真实 app.js，39 用例）、`tests/test_frontend_quantity_js.py`、`tests/smoke_quantity_ui.py`（隔离真实 Chrome，12 项）。原复现脚本修复后 `actual=1`；对基线 app.js 跑同一测试 22 项失败，证明测试能抓住原 bug。

## 4. P2：回执时钟偏差误判旧任务

- 班牛“创建时间”只有秒级，页面服务器与主机时钟可能有数秒偏差。原逻辑用“创建时间 < 本次尝试开始（去微秒）”直接判旧任务，2 秒偏差的合法回执被拒绝。
- 新策略：任务基线 ID 集合仍是识别旧任务的主要依据（`previous_ids` 之外的唯一新增任务）；创建时间作为受限二次保护：容忍秒级截断和 ≤5 秒服务器慢时钟（`RECEIPT_CLOCK_TOLERANCE`），快于本机 5 秒以上也拒绝。没有任意放大窗口；没有任务 ID/完整字段/数量/新单号任一依据仍不确认成功。
- 回归：`tests/test_banniu_receipt.py` 覆盖秒级截断、慢 2 秒接受、慢 6 秒拒绝、未来时间拒绝；正常成功/失败/待同步路径保持。

## 5. P2：HTTP 提前拒绝间歇 WinError 10053

- 根因（受控实验确认）：`mutate()` 的 Host/局域网/认证/Origin/CSRF/本机限制等提前拒绝分支在读取请求体之前返回；`send()` 写 403 后 handler 立即关闭 socket，而客户端已声明但尚未发送完的 body 仍在途/未读，Windows 关闭带未读数据的连接会发 RST，RST 可丢弃已写出的 403，客户端读状态行即 10053。受调度影响，历史约 1% 概率。
- 修复：`send()` 在存在未读 body 时显式 `Connection: close`，**先完整写响应并 flush，再按 Content-Length 有界排空（≤5MB，总时长 ≤2s），最后关闭**；非法/超大/chunked framing 不读取直接显式关闭。`mutate()` 读 body 前严格校验单一合法 Content-Length、无 Transfer-Encoding、0<长度≤5MB，读满才标记已消费；拒绝路径不放松任何权限校验，前端只读更新 token 的既有行为不变，有副作用请求不自动重放。
- 证据：受控时序下修复前延迟 body 每个时序 12/12 失败，修复后 8 组时序 320/320 正确 403；原 flaky 用例定向 110 次全通过；新模块连续 20 次重复 0 失败。
- 回归：`tests/test_http_rejection.py` 12 项（延迟分段 body、无/错 CSRF、错误 Host、非本机、非法/重复/缺失/超大 Content-Length、chunked、keep-alive/close、连续混合拒绝）。有意边界：客户端故意 >2s 才开始发送 body 或强行发送超大/非法 body 时，服务端有界等待后关闭，客户端仍可能看到连接重置；这是“不无限读取未授权 body”的取舍。

## 6. P2：只读商品查询恢复会丢失/覆盖人工内容

- 原 `BanniuProductPage._blank` 只报错，外层读取器捕获后调用 `recover()`，后者直接 `_open_registration_list`（可能刷新页面）并重建表单；finally 还会关闭浏览器。若有人在查询窗口中填写/修改，人工内容可能被刷新和关闭。
- 修复：查询表单建立独立 token/自动化计数/人工编辑/提交点击跟踪；`_blank` 与恢复前先检查 root 身份、token、空白状态、人工编辑、提交交互。发现人工内容立即抛 `ProductQueryBlocked`（人工介入），停止本平台后续编码，**不刷新、不关闭、不写回目录**，保留窗口交人工处理；只有确认是本工具空白表单时才安全关闭并重开。读取器对该异常不执行 finally 关闭，并明确提示窗口保留。
- 回归：`tests/test_product_reader_safety.py` 覆盖状态判据、未知/人工表单禁止刷新关闭、空白工具表单可恢复、人工内容出现后不再输入后续编码、浏览器窗口保留。

## 7. P2：重登对同编码不同数量的判定

- 原 `check_distinct_gifts` 只比较编码集合，同编码不同数量时无法说明意图。现明确规则：旧任务编码集合与本次请求相同且数量也相同 → “同组合同数量”停止重登；编码相同但数量不同 → 也停止重登并要求人工核查。理由：无法证明上次补发意图没有覆盖数量差异，降低旧任务保护去放行重登不安全；正常登记与失败重登保护不变。回归在 `tests/test_banniu_receipt.py`/`test_host_management.py` 相关用例。

## 8. 融合版历史问题 → 荣耀实现位置 → 回归测试

| 历史问题 | 荣耀实现位置 | 回归测试 | 状态 |
| --- | --- | --- | --- |
| 班牛遗留表单阻塞后续订单 | `banniu_page._refresh_submitted_form`、`_discard_unsubmitted_form`；`banniu_receipt._query_tasks_once`；新增中间表单与暂停 | `test_banniu_stalled_form`、`test_banniu_form_cleanup`（新增数量中断/人工内容用例） | 已保留并补齐中间状态 |
| 错误平台/阶段被混写成聚水潭失败；次生认证覆盖主异常 | `Workflow.execute` 固定标签 + `click_attempted`/原始异常保留；`banniu_page.submit` 次生认证只作补充 | `test_workflow`、`test_banniu_page`（次生认证/边界用例） | 已保留 |
| 横向虚拟列导致订单号不在初始 DOM | `banniu_receipt._query_tasks_once` 稳定快照 + 有界滚动 + 只在首次原生点击前定位 | `test_banniu_receipt.TaskListTests`、`test_banniu_stalled_form` | 已保留 |
| Vue 清空后重建输入框 | `banniu_page._set_text`；新增数量框重新定位+有界重试 | `test_banniu_page`（数量控件）、`smoke_banniu_input_rebuild.py` | 已保留并扩展到数量框 |
| 空列表隐藏分页/裁切列/旧筛选残留 | `complete_task_rows`、`_read`/`_table` 同快照完整列、`_order_filter` 清残留 | `test_banniu_receipt`（不完整/旧行/分页）、`smoke_banniu_virtual_columns.py` | 已保留 |
| 关闭标签但仍有其它标签 | `business_browser._session_closed` 单存活标签恢复/多标签停止；`ManualInterventionRequired` 分类 | `test_business_browser`、`test_browser_close`、`test_environment_recovery` | 已保留 |
| 登录助手持锁导致新单失败 | `RegistrationQueue.start` 先 `prepare_start()`；失败不落失败记录；暂停恢复也先验证/预留 | `test_start_preflight`、`test_frontend.EnvironmentPauseHTTPTests` | 已保留 |
| 登录关闭超时/两站清理/保存失败 | 既有 login shutdown 有界清理与两站独立关闭；不按进程名批量杀 | `test_login_shutdown`、`smoke_login_shutdown.py` | 已保留，未改 |
| 旧快照缺 items/回执外新增记录 | `_load` 保守终态；`verify_new_records` 完整新增集合 | `test_registration_recovery`、`test_workflow` | 已保留 |
| 延迟复核覆盖/重复/归档 | 执行身份绑定 + 只读一次 + 180 秒 + 独立人工审计 | `test_v111.AutoReviewTests`、`test_manual_completion`、`test_v120.ManagementTests` | 已保留 |
| 无有效回执只凭点击+同礼品升级成功 | 正常登记/延迟复核都必须有效回执、新单号、完整编码和数量；`receipt_from_detail` | `test_banniu_receipt`、`test_v111`、`test_workflow` | 已保留，新增受控时钟容差 |
| 迟到轮询覆盖人工状态；人工成功被撤销 | `pollSequence`/`stateEpoch`/行签名身份；暂停 UI 不改写状态 | `test_v120`、`test_v130_status`、`test_frontend`（暂停状态） | 已保留并加入暂停状态 |
| 关闭 HTTP 监听但队列仍执行/重开无响应 | 停止接收→取消入口→结束在途→关闭浏览器/监听→释放锁；暂停排空 pending 不阻塞关闭 | `test_shutdown_endpoint`、`test_frontend`、`test_environment_recovery` | 已保留 |
| 取消与领取竞态 | 同一锁下领取+落盘；只取消未领取未提交；新增 waiting 取消 | `test_queue_cancellation`、`test_environment_recovery` | 已保留并扩展 |
| 编码大小写/前导零/同名歧义；数量身份兼容 | `registration.py` 精确编码、数量签名仅在非默认数量时加入；1–99 多件 | `test_task_identity`、`test_v130_selection`、`test_workflow` | 已保留，数量身份兼容已用旧任务/人工标记复算 |
| 局域网 UUID/Host/CSRF/断线提示 | `frontend_server` 权限校验未变；10053 修复只改响应收尾 | `test_network`、`test_http_rejection`、`test_frontend` | 已保留并修复 10053 |

诊断脚本口径：`SessionNotCreatedException` 不能单独证明低完整性级别；本轮未改诊断脚本，仍按 process_integrity、profile 占用、驱动版本/启动日志分流。本轮未改浏览器/系统 ACL、不按进程名批量关闭浏览器。

## 9. 主机操作说明（暂停/恢复）

1. 工作台出现“登记已暂停，后续未执行订单未提交”横幅时，先读原因、平台和阶段；当前行可能是 failed（未提交或结果不确定），后续未执行行显示“暂停待恢复”，不是失败。
2. 需要处理的情况：平台登录失效/验证码、浏览器窗口被关闭、遗留表单归属无法确认、提交结果不确定。只允许主机处理，不要清空 `data/`、不要删除历史、不要批量重登失败订单。
3. 主机可在本机点击“主机恢复”；程序先只读验证聚水潭订单页与班牛礼赠列表、登录状态，未就绪会拒绝恢复并保持暂停。恢复成功后 waiting 行重新排队，各执行一次；原 failed/uncertain 行不重提。
4. 暂停期间可正常关闭工作台：pending 已排空，不会挂起；重启后仍保持暂停。也可用“取消所选排队”取消尚未执行的 waiting/queued 行，取消不提交、不计入失败。
5. 健康轮询/刷新不会自动恢复或重提。恢复后如需处理原失败行，走既有失败清单和明确重登核查，不要在暂停期间绕过。

## 10. 测试、日志与静态检查

- 全量测试：**662 项全部通过**（`Ran 662 tests in 64.487s — OK`，日志 `.tmp/stability-final-tests.log`）。基线 627 项；新增 34 项回归：环境暂停/恢复 5、批次故障矩阵 1（覆盖 12 个阶段）、提交边界/中间表单/数量/回执/HTTP/商品查询/前端数量等。修复过程中的失败记录保留在 `.tmp/stability-progress-tests*.log`，未删除。
- 跨模块隔离冒烟（真实 Chrome，本地合成页面/临时目录）：`smoke_banniu_stalled_form`、`smoke_banniu_form_cleanup`、`smoke_page_reuse`、`smoke_banniu_input_rebuild`、`smoke_banniu_virtual_columns`、`smoke_closed_business_tab`、`smoke_queue_cancellation`、`smoke_v130_fill`、`smoke_draft_sync`、`smoke_login_shutdown` 共 10 个脚本全部 PASS；`smoke_frontend.py` 仍是旧融合版脚本（读取 `example.md` 的 31 项旧清单，在 gifts-ry 分类目录下解析失败），本轮未改，记录为过时脚本。
- 新增隔离真实浏览器验收：`tests/smoke_quantity_ui.py`（12 项 PASS：填充柄 1→5、Ctrl+D、撤销、结构化复制粘贴、超 99 阻止、刷新存档一致）；`tests/smoke_pause_resume.py`（PASS：暂停横幅、失败/暂停待恢复行、开始按钮禁用、等待行可取消、主机恢复后未执行行执行一次且原失败行不重提）。截图：`.tmp/ui-checks/quantity-ui-regression.png`、`.tmp/ui-checks/pause-notice.png`、`.tmp/ui-checks/pause-resumed.png`。均未连接真实平台。
- 静态检查：`git diff` 审查全部改动；`git diff --check` 无空白错误；新文件不含凭据、DPAPI、会话或真实订单数据；`config/gifts.json` 等账号/本地配置仍由 `.gitignore` 排除。
- 配置/业务数据：修复前后 SHA-256 完全一致——`config/gifts.json` `3FAAE5C8…`、`config/gift_groups.local.json` `F5B0BCF2…`、`data/registrations.json` `DAF170F7…`、`data/manual_status.json` `21116AF3…`（用户既有修改保留；对账文件 `.tmp/stability-baseline-20261105/hashes-before.json`、`hashes-after.json`）。
- 真实平台访问：无。未提交、未重登、未取消真实订单，未人工改状态，未重启 8767 工作台。`data/` 仅在测试临时目录写入合成数据。

## 11. 仍待验证 / 证据缺口

- 未做真实平台整单验收：没有提交、重登、取消真实订单或人工改状态；所有页面行为来自隔离 DOM/Mock。班牛真实“数量”列、真实登录/验证码处理后恢复、真实浏览器连接中断的时序仍是待验证项。
- 窗口响应 10053 原为概率性，无法声称所有机器/时序绝对为零；受控复现和定向重复提供了修复前后强对比，但真实局域网环境仍需观察。
- 客户端故意延迟 >2 秒才发送 body、或强行发送超大/非法 body 时可能仍看到连接重置，属于“不无限读取未授权请求体”的有意边界。
- 纯文本/Excel 往返不保留多件数量是显式限制，界面已写明；需要跨 Excel 保留数量需另行设计可解析文本格式。
- 部分既有 `smoke_*.py` 是过时脚本（如 `smoke_v120.py` 的 SelectionPolicy 恢复、`smoke_task_identity.py` 的旧默认分组名、`smoke_frontend.py` 的旧 31 项清单），与本次改动无关；未在本轮修改，避免扩大范围。
- 诊断脚本/文档口径未改；`SessionNotCreatedException` 与运行中会话中止仍需分渠道排查。

## 12. 是否需要重启服务加载代码

本轮没有重启正在运行的 8767 工作台，也没有在运行中的服务上加载新代码。要让本轮 Python 改动（暂停/恢复、回执、HTTP 修复等）在现有工作台生效，需要正常关闭工作台后重新启动；前端静态文件刷新页面即可加载新 JS/CSS，但恢复接口与队列暂停逻辑要求后端进程重启。重启前应确认没有在途任务，并保留现有 `data/` 与草稿。
