# Prompt 02：独立核验、逐单人工状态、任务业务日导出

## 范围与基线

开发目录为 `main-core-repair-20261111`，分支 `codex/main-verification-taskday-20261111`。继承 Prompt 01 的 `1734132ba3981a06dd2e44746ed7bada7e93995a`，其 main 历史基线为 `29c680bd8b6793a5721b7b116987713df8e96453`。本轮没有访问 Git honor 分支、生产电脑、真实平台或真实凭据，也没有部署、推送或合并 main。业务 SKU 分组中的 honor 仍按普通商品分组处理。

阶段提交：A `5c2c1b7`，B `7798ab5`，C `df3ad93`。后续修正、测试及性能证据由最终交付提交记录。Prompt 01 礼品匹配报告继续保留。

## 通道与持久状态

默认 `GIFT_DUAL_VERIFICATION=0`，维持原串行工作流。显式设为 `1` 后，登记 worker 负责原订单基线和唯一班牛提交，verification worker 只持有 `ReadOnlyVerification` 的查询能力，没有提交、重登或班牛入口。

主浏览器仍使用原配置和 app.lock；核验浏览器使用 `data/browser_review/jushuitan`，Edge 使用 `data/browser_review_edge/jushuitan`，另持 review.lock。驱动在所属工作线程内创建、使用和关闭，跨线程访问会拒绝。核验进程只调用同一 Windows 用户的 `SessionStore.load('jushuitan')`，沿用 `SiteBrowser.restore()` 内的域名、过期 Cookie 和 Storage origin 过滤；不调用 capture/save、不读取密码、不自动登录。主通道是原加密会话的唯一写者。

验证码、过期会话、互踢、驱动关闭、网络错误均不能推断平台登记结果。首次核验出错进入 manual_required 并保留订单号排他，诊断只记固定阶段、异常类型与关联摘要。真实双会话的页面兼容性和账号一致性仍需现场人工验证。

|记录|作用|
|---|---|
|registration_jobs/*.json + item_updates|原批次快照及逐行真实状态，执行身份和 state_version|
|verification_tasks/*.json|首次核验的持久交接：batch/task/row/execution/task_key、行签名、订单、SKU/数量、完整基线、回执、提交边界、创建/到期时间、预期版本|
|auto_reviews/*.json|约 180 秒的延迟核验 claim；旧 claim 不删除、不重新领取|
|execution_history/*.json|首次终态历史证据；复核不能抹去原失败|
|manual_status.json|独立显示覆盖和追加审计，不改变平台证据|

初次执行：queued → querying → submitting → verification_pending → verifying → success / failed / manual_required。明确未提交失败保留 not_submitted；点击后未读到可信回执视为 uncertain，双通道下进入 manual_required。所有登记尝试先保存 submit_attempted，再调用适配器；原生点击不重试。

交接先原子保存 ready 记录，再保存行状态投影，最后唤醒 worker。记录足以修复“已保存交接、尚未更新行”的崩溃窗口。ready 可在重启后恢复只读核验；running 中断转人工，不重领。结果已持久化但 claim 未 done 时，启动只做对账。关闭双通道后，已有 ready 交接交给原串行 worker，只恢复读取，不重放提交。

完成结果重新校验 execution_id、row_signature、task_key、state_version。过期结果保留 interrupted，不能覆盖当前任务。后台复核不改变草稿的行归属；人工覆盖保持独立。

订单号排他来自持久状态，进程启动重建。任何未完成或结果不确定提交会拦截该订单的另一组 SKU；拦截行保留“未提交”，允许其他订单继续。失败并不自动释放排他，核验确认成功或明确未提交才可安全解除相应占用。

首次待核验上限 128，达到后登记 worker 在查询下一单基线之前背压等待。最多连续 3 个初次核验后给延迟核验一次机会；每次调度最多 16 个延迟 claim，延迟队列已有 64 个时停止新领取。没有无限的内存核验队列。正常关闭先停止受理，再排空已接受登记/核验、补偿导出、关闭各自浏览器，最后释放目录锁。

核验领取和关闭检查遵循 jobs.lock → 核验 condition 的锁顺序，与 claim 写入互斥，避免 Windows 原子替换期间读取被拒绝。等待时释放 jobs.lock；查询平台期间不持这两把锁。关闭检查只观察队列，不消耗公平调度额度。损坏/不可读取的 claim 会明确停止通道并唤醒等待方报错，保留文件及目录锁，避免死线程导致关闭永久等待。

## 人工状态的并发规则

`/api/rows/status` 请求需带 revision、draft_revision、row_ids、status 和逐行 targets。每个 target 包含 row_id、task_id、execution_id、row_signature、state_version。旧页面缺少版本或任何选中行变化时，整组 HTTP 409，不部分保存。

已持久化终态的 A 可在 B/C 仍执行时修改。该执行的延迟核验 queued/running 时暂不允许；仅 scheduled、done、interrupted 不阻止已终态行。提交不确定的行不能人工标为成功；可标为待核查。明确未提交失败保留原显示覆盖规则。

覆盖层保留 actor、时间、旧值、新值、原平台状态和所见执行版本。真实 state、submission_status、evidence、verified_record_ids、completed_at、重登安全规则不受影响。原有同 task_key 人工成功持续排除重登的行为保留；延迟核验改变真实基础状态后，当前覆盖仍显示，用户需明确 reset 才移除。UI 仅对内容与签名相符且 manual_editable 的选中行开放按钮，沿用分页和局部状态刷新。

## 业务日和原始行账本

新任务接受时保存 batch_id=task_id、created_at、business_day/log_day。日期用显式 UTC+08:00（Asia/Shanghai），不依赖 Windows 时区或 DST。业务日不可被完成/复核时间改写。started/submitted/verified/completed/reviewed 是独立 ISO8601 事件时间。

旧数据仅从带时区 created_at 推导；缺失、无时区或与已有 business_day 冲突的记录显示 needs_manual_reconciliation，不归入猜测日期。旧 JSON/Excel 不自动重写；旧 review_export_cursor 不再决定 V4 导出。旧 auto_review log_day 不作为 V4 日期来源，同秒事件以完整源摘要识别，不靠大于某时间戳跳过事件。

报表按每个 job 的 rows/items 一一对账，绝不通过 `_attempt_records()` 去重删掉原始行。汇总互斥分类为成功、失败、历史复用、跳过、取消、需人工核查、待完成，之和必须等于输入行。真实提交数只统计有 submit_clicked 的唯一 execution_id，排除 reused_result；缺证据不补计。历史成功复用可展示其基础成功，但汇总单列“历史复用”，不算本行远程提交。

次日明确重登创建新的 execution_id 与次日业务日，保留 origin_batch_id、parent_execution_id。后续只读复核仍修订原批次，不产生新登记次数。

## 发布、故障和锁

V4 文件独立写入 `exports/v4/`，防止与旧完成日报表混用。当前文件 `gift-registration-YYYY-MM-DD.xlsx`，非最终版增加 `-partial`；单批次下载增加批次摘要后缀。历史完整文件在 `exports/v4/history/`，导出清单在 `task_day_manifests/YYYY-MM-DD.json`。

清单保存 batch_state、export_state、每批输入/完成/剩余、版本、导出时间、源摘要、文件摘要和历史记录。状态为 pending / partial / final / needs_refresh / error。final 指截至该源版本的全部已知批次首次终态；迟到批次、复核、人工覆盖变化会产生明确新版本。旧文件被 Excel 占用或磁盘写入失败时保留旧字节与错误状态。

源未变化时，定时检查保留有效 partial 版本；单批次未完成的下载请求不修改整日清单。失败清单在进度查询中继续显示 error，直到后续成功发布或明确处置，不能被普通进度查询改成“待导出”。

锁顺序：导出服务锁 → rows.lock → jobs.lock。在短锁内取得文件/增量 epoch 版本，锁外读取并检查版本；锁外构建 XLSX、解析 ZIP/XML、核对每张表行数与汇总；同目录原子写入历史，发布前再校验该业务日版本。只关注目标日，下一日的持续更新不会让旧日发布反复失效。最终原子替换及清单提交处短暂持锁；最长持锁时间有原始测量。发现并发变化保留旧报告、标 needs_refresh，下一次事件或定时补偿重导。

批次/行状态变化唤醒维护线程，250 ms 合并频繁通知，另有最长 30 秒补偿。启动和正常关闭同样对账。不受全局登记队列非空限制。维护游标损坏不阻止独立 V4 报表，但禁止草稿清理。timer 永不删除草稿；显式旧维护清理也需来源一致、摘要匹配的 V4 final 和清理前备份。未知提交、开放批次、版本变化、导出失败均保留草稿。

发布后若进程在写 manifest 前中断，完整文件和历史仍在，manifest 不会先报 final；下次重新验证/发布。禁止通过手工改清单为 final 来绕过校验。损坏清单保留原字节并报错，不初始化为空覆盖历史。

## API 与表结构

|接口|语义|
|---|---|
|GET /api/registrations/task-logs|主机本地读取业务日、批次、进度、导出版本和不可推断日期列表|
|POST /api/registrations/task-logs/export|主机 + Origin/Host/CSRF；day、可选 batch_id、partial；仍开放且未请求 partial 返回 409|
|GET /api/registrations/task-logs/download|下载已生成文件；日版检查源未变化及文件摘要，旧版本请求返回 409|
|GET /api/registrations/export-today|保留旧完成日两表下载语义，只作兼容；新 UI 不再调用|
|GET /api/maintenance/status|保留旧字段，增加核验模式、积压、完成数、等待/耗时、claim 和锁指标|

Excel 保留成功登记、失败登记，新增任务汇总、任务明细、后续复核。订单号、SKU、执行 ID 均为文本，保留前导零和长数字；汇总计数为数值。时间列保留含偏移的 ISO 原文，避免 Excel 丢掉时区。失败说明使用固定脱敏分类，细致证据留在受保护本机记录中。

## 1000 行跨日例子

`synthetic-1000-cross-midnight → 2026-11-09`，接受于 11/09 20:00 +08:00。600 行完成于 11/09 23:00，400 行完成于 11/10 04:00。01:00 不能发布 final；04:00 后任务明细恰好 1000 行，11/10 的新批次独立计账。样例和映射位于 docs/samples，全部为合成数据。

## 迁移和回退

1. 本轮只交付开发分支，生产保持原样。后续部署前先停受理、排空工作、备份整个数据目录和浏览器配置；不得复制正在使用的浏览器 profile。
2. 在离线副本核对 rows/items、所有 execution_id、交接、claim、日期和报表摘要。缺日期的记录人工核对，不能取完成日补齐。
3. 默认串行。完成独立会话现场检查后再启用双通道；配置切换须在正常退出后重启生效。
4. 优先回退配置为串行，保持本版本处理持久交接。回到 Prompt 01 二进制前必须离线对账并排空/隔离新增交接；旧版不认识 verification_pending/manual_required，不能直接复用其数据启动。
5. 全部历史、manual_status、execution_history、item_updates、auto_reviews、verification_tasks、清单和版本文件随备份一起保留。禁止只回滚某个 job JSON 或删除 claim 来重新执行。

## 部署前人工检查（本轮未现场验证）

- 目标机器 Chrome/Edge 驱动匹配、两独立 profile/锁、同用户 DPAPI 解密和只读会话恢复。
- 同账号两个会话的实际页面标识、验证码、失效和互踢表现；识别失败必须停在人工核查。
- 真实聚水潭新增记录的稳定 ID、SKU、数量与班牛回执关联仍需授权后验证，不能用模拟测试代替。
- 目标磁盘空间、Excel 打开时的共享锁、备份恢复和正常关闭演练；本地权限/局域网/CSRF 检查。
- Prompt 01 缺失的真实 14 条匹配夹具仍需补齐，不能宣称已覆盖。
- 先评估实测 fake 性能，不承诺双通道一定更快；安排人工部署与可回退窗口。
