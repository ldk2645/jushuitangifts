# main 核心修复验收与 Prompt 02 交接

执行日期：2026-11-11。参考日期沿用任务要求的文件名。本次仅在独立 clone `main-core-repair-20261111` 修改；原工作区的未提交改动保持原状。

## 基线与提交

- 远端 main / 修复基线：`29c680bd8b6793a5721b7b116987713df8e96453`。
- 分支：`codex/main-core-safety-20261111`；仅 clone main，没有读取、复制或操作其他 Git 分支。
- `c251f7c`：全目录精确匹配，未确认选择阻止登记。
- `e43f142`：维护降级、提交点击边界、恢复分类和故障注入。
- 最终核心代码提交：`191bfc3c8bf7051ae0d7cffaa82041b881a6e2c8`。
- 未推送、未合并 main、未部署、未连接生产电脑、聚水潭或班牛。浏览器测试只使用本机临时 profile 和 fake Operations。

## 修改与行为

|范围|最终行为|主要文件|
|---|---|---|
|礼品匹配|全目录精确候选优先；跨组、同名 SKU、未完整解析的片段保留全文；模糊候选不进入 gift_ids|frontend/app.js、selection.py、registration.py|
|维护状态|坏游标返回 degraded / needs_attention，归档暂停；只读复核独立调度；定时导出不自动删除草稿|maintenance.py、frontend_server.py|
|原子写|同目录唯一临时文件、flush/fsync、原子替换、失败清理；Windows 短暂共享冲突仅重试文件替换|sessions.py|
|提交与恢复|等待可点击失败明确未提交；原生 click 开始后不确定且不重提；queued 未领取恢复为待明确开始，querying 与提交阶段按证据区别处理|banniu_page.py、registration.py|
|只读复核|缺基线、明确未提交、缺有效关联回执的记录形成 manual_required claim，不开启浏览器；每个安全订单边界最多处理一个到期复核|registration.py|
|批量重登|持久化批次受理与执行分离；固定失败集合派生幂等键；重启、丢响应、重复请求复用同一批次|retry_batches.py、host_management.py、frontend_server.py|
|任务存储|逐行增量落盘、结果索引、状态缓存；最终整理在队列锁外准备，落盘前检查版本|job_storage.py、queue_metrics.py、registration.py|
|前端大表|每页 200 行，全部数据与跨页选区保留；无状态变化时不替换状态节点|frontend/app.js|
|诊断|只记录时间、组件、阶段、错误类型、匿名关联值；响应断连不误报为存档失败或再次发送 500|diagnostics.py、frontend_server.py|

旧成功证据、人工审计、任务键、执行身份和历史时间保留。`daily_records()` 对外日归属没有改变。没有实现第二核验浏览器、异步提交后核验、单单人工状态新规则或 business_day 导出模型。

## 可复现验证

在独立修复目录运行，全部业务操作均由 fake Operations 代替：

```powershell
python -m unittest discover -s tests -v
node tests/core_matcher_checks.cjs ../merged-usb-111-zw7v2tcm
$env:GIFT_CHROMEDRIVER='C:\Users\17003\.cache\selenium\chromedriver\win64\156.0.8078.73\chromedriver.exe'
python tests/smoke_frontend.py
python tools/benchmark_core.py --output docs/benchmark-core-after.json
python tools/benchmark_matrix.py --label after
```

浏览器驱动路径是本次机器的已安装测试路径，其他机器需指定匹配驱动，不使用生产 profile。第一次基线测试在沙箱内因本机 socket 权限失败；允许本机连接后原 main 578 项通过。一次完整回归出现关闭接口 WinError 10053；该接口独立复查及后续全量回归通过，没有将其归因为生产故障。

新增测试覆盖全零/截断/缺失/权限/写失败维护状态、并发原子替换、点击前等待与原生调用、queued/querying/submitting/verifying 恢复、损坏/错位任务、编排异常仅影响已领取行、批次响应丢失/重复/重启/部分拒绝、未知提交拦截、同线程订单边界公平复核、结果已写而 claim 未完成的恢复、增量记录错位及整理崩溃/并发版本冲突。

原 `tests/smoke_frontend.py` 在原 main 和修复版都要求已移除的“是否改地址”第四列，首个页面断言失败。原脚本完整保留为 `tests/legacy_smoke_frontend_v110.py`；当前入口运行三套现行三列表格验收：大表与跨页操作、v1.3 双客户端/权限/分组、原生鼠标填充。与本轮明确变更有关的旧断言已更新，其他断言保留；还修正了旧测试辅助函数覆盖 unittest.TestCase.fail、可能掩盖断言失败的问题。

最终输出：`Ran 596 tests in 74.799s` / `OK`；当前 smoke 入口三套浏览器验收全部 PASS；Node 为 18 个合成案例、1100 行确定性检查及所提供样本的报告输入通过。`check_core_downgrade.py --legacy-repo .tmp/core-baseline` 验证原 main 对未合并增量的模拟提交拒绝重登，平台调用为零。完整本机输出保留在 release-tests.log、release-smoke.log；仓库保存脱敏摘要与原始性能 JSON。

中间提交不是独立发布版本：e43f142 后的 Windows 并发写测试发现共享冲突，已在最终提交加入写者串行保护、受限文件替换重试和失败清理。阶段结果以最终代码的统一验收为准。原始性能数据保存在 `benchmark-*.json` 和 `perf-*.json`，汇总见 `main-core-performance.md`。

## 证据边界与未验证项

可用隔离目录的 gifts.json 和 gift_groups.local.json 仅只读加载，实测 255 SKU、106 个名称含空格，与 prompt 报告的 260/108 不是相同完整样本；报告中的小米与普通商品组 honor 错配输入安全断言通过。未找到 `gift_matching_regression_cases_main.json`，不能声称真实 14/14。新增合成用例与 100/1000 行确定性测试独立通过。

没有提供生产 maintenance.json、HTTP 原日志或 auto_reviews.zip，因此全零文件的实际成因、生产 503/500 的全部原因、25 条复核记录的实际分布，仍仅属于用户报告，不能声称已在生产复现或解决。没有真实平台回执验证、真实订单提交或生产机器性能结果。性能样本为 Windows 上合成数据，开启 tracemalloc，内存值是 Python 分配峰值；不能外推为生产 SLA。

## 状态机与接口交接

|项目|约定|
|---|---|
|执行状态|waiting_for_workflow → queued → querying → submitting → verifying → success/failed；另有 skipped、cancelled。恢复不自动重新派发普通登记|
|未领取恢复|queued、未领取、零尝试、无点击/回执/提交事件的记录恢复 waiting_for_workflow，需明确开始；已受理持久化重登批次可恢复其已授权、未领取的子任务|
|提交证据|submit_attempted、attempts、submission_status、claimed_at、before_snapshot/before_record_ids、submit_clicked、receipt、verified_record_ids；缺证据绝不补成功|
|snapshot_guard|仅新存储版本的未领取快照保护值。旧版读取中断快照时会拒绝重登，不能把尚未合并的 item_updates 误认为明确未提交|
|防重身份|task_key 仍为订单号和完整排序编码集合；execution_id 保留实际尝试身份；批次键为排序、去重校验后的 failure_ids 的 SHA-256|
|批次受理|POST /api/registrations/retry-all 返回 batch_id、accepted_count、rejected_count、results。accepted 只代表持久化受理，不能作为平台成功|
|批次进度|GET /api/registrations/batches/{batch_id}，仅本机；accepted/preparing/dispatched/needs_attention 为批次阶段，逐条 execution_state 来源于任务记录|
|复核调度|enqueue_due_reviews(current_time) 原子创建 claim；_review_once 仅由原 worker 执行；_service_review_boundary 每单后最多领取一个已排期只读复核|
|claim 状态|queued/running/done/interrupted/manual_required/skipped；已有 claim 不删除、不重新查询。任务已有 review_result 而 claim 未完成时只对账，不再操作浏览器|
|健康状态|GET /api/maintenance/status 保留旧字段，增加 health、needs_attention、diagnostic 和 queue；包含 claim 计数、队列等待/锁等待及最长持锁时间|
|锁顺序|主机管理：rows.lock → jobs.lock。归档：maintenance.lock → rows.lock → jobs.lock。任务 worker 只持 jobs.lock 更新持久状态，浏览器调用在其外；pending.mutex 不跨持有 jobs.lock|
|增量存储|item_updates/{job}/{index}.json 绑定 storage_generation、行身份和原 execution_id；完整快照记录 retired_generations。整理使用版本检查，旧增量目录移走后再清理，仅清理本轮生成的临时增量文件|
|可扩展边界|Prompt 02 若增加独立核验 worker，必须独立浏览器所有权并复用 claim 与幂等对账；不得将批次受理或核验排期改为平台成功|

## 维护修复与回滚

`repair_cursor(expected_sha256=..., next_day=..., review_export_cursor=...)` 是独立维护动作：先验证原文件摘要，将原始字节备份到 maintenance_backups，再写入人工明确给定的游标。不会从坏文件推断清理日期。本轮只在合成全零文件上验证，包含备份和精确字节回滚。

`reconcile_archive(expected_revision)` 是独立归档对账动作，要求草稿版本一致；不是定时调用路径。原先清理前备份、导出失败不清理等行为由显式对账测试保留。不能将生产坏文件替换为空初始状态，也不能因到达 01:00 自动授权清理。

代码回滚：本次尚未部署，直接保留或放弃独立修复分支即可；原工作区和生产不受影响。若后续部署后回滚，应先停止受理并等待 worker 完成，离线备份整个数据目录，包括 item_updates、retired_item_updates、retry_batches、execution_history 和 auto_reviews，再用修复版只读核对/整理副本。不可只恢复旧任务主 JSON 而丢弃增量证据，不可在未知提交上创建新的重登身份。确认无未整理增量和待派发批次后，才评估将已对账数据交给旧版。禁止覆盖既有人工审计和成功记录。

## 部署前人工检查

- 获取缺失的真实 14 条回归夹具及生产离线故障证据，在隔离副本重新验收。
- 核对各分组、同名 SKU 和常用粘贴格式，检查待确认片段未进入 gift_ids。
- 对生产数据离线副本核对每个任务的 rows/items、提交证据、执行身份、复核 claim 及备份可恢复性。
- 检查目标机器浏览器/驱动和登录配置，真实平台只读或提交测试需另行明确授权。
- 部署后所有工作台客户端必须刷新页面，取得对应的新脚本与 CSRF 令牌。
- 评估合成性能数据与目标电脑差异，安排停机窗口、完整备份及经过演练的回滚；本次不执行部署。
