# Prompt 02 验证与交付结果（2026-11-12）

开发分支：`codex/main-verification-taskday-20261111`。隔离目录：`D:/UserData/Desktop/NL/main-core-repair-20261111`。

继承基线：`1734132ba3981a06dd2e44746ed7bada7e93995a`；完成实现与测试提交：`99a2087ad0800b53e72ecad0b616550796f455a4`。最终文档提交见交付时 HEAD。

A `5c2c1b7` → B `7798ab5` → C `df3ad93` → D `65efa21` → 边界修正 `99a2087`。全部基于修复后的 main 派生开发分支，未部署、推送或合并。

## 结果

- 不同订单的提交和只读核验可以同时进行；同订单不同 SKU 在未核验/不确定时被拦截。
- 本单终态可人工修改显示，其他排队/提交单不阻塞；旧执行版本或混选整体拒绝，不改平台证据。
- 1000 行 600/400 跨日用例：01:00 没有原日 final，04:00 全部 1000 行归入 11/09；11/10 的开放批次独立。
- 延迟只读复核修订原日，新人工重登计入新日并带来源批次和执行 ID；原始每行都保留。
- 1000 行持续工作下，核验积压可压到上限 4 验证背压；延迟复核在最多 3 个首次核验后获得机会。

## 自动化和界面验证

```text
python -m unittest discover -s tests -v
Ran 620 tests in 169.979s
OK

node tests/core_matcher_checks.cjs ../merged-usb-111-zw7v2tcm
Read-only supplied catalog: reported safety examples passed (14-case file absent).
18 synthetic cases and 1100 deterministic repeated rows passed.

python tests/smoke_frontend.py
PASS: current main frontend acceptance (large grids, two clients, native fill gestures).

python tests/smoke_prompt02.py
{"result": "passed", "browser": "157.0.8103.40", "original_rows": 1000, "done": 600, "partial_download": true, "manual_edit_only_terminal": true}
```

浏览器使用临时 profile、本机回环页面和 fake 操作。Chrome 157.0.8103.40；测试驱动为已安装的 156.0.8078.73。本轮未连接真实平台。完整输出见 [证据目录](prompt02-evidence/)。最后 UI 文案/导出后版本刷新修改后，新增浏览器验收及 JS 语法检查通过；最终边界修正后全量 620 项通过。

覆盖恢复窗口：点击后回执丢失、交接落盘后未入队、交接完成但行投影未写、结果保存而 claim 未 done、过期结果、会话过期/验证码/互踢等认证失败、driver 启动异常、只读能力与线程所有权、串行模式恢复。另验证 63 个延迟 claim 时只再领取 1 个，以及定时检查保留仍有效的 partial 下载。会话测试为 fake SessionStore/driver，不是现场平台验证。

兼容测试更新包括：旧“全局忙则不可人工标记”改为逐单允许；旧 timer 生成空完成日日报改为仅按已接受任务导出。同订单另一组 SKU 在前次核验失败未解锁时的新提交也被安全拦截。旧维护显式清理、权限、CSRF、关闭和兼容下载回归通过。

初始沙箱全量测试的回环套接字被 Windows 10013 拦截，随后在允许本机回环的隔离环境重跑。新增浏览器脚本首次因管理区未展开而点不到导出按钮，修正脚本后通过；没有由此修改真实平台行为。

收尾回归曾发现关闭检查读取延迟 claim 与 worker 原子替换文件并发，Windows 拒绝读取并继发临时目录清理错误。已统一 jobs.lock → condition 锁顺序，关闭检查不再改变公平调度计数；损坏 claim 的选择异常也会唤醒等待方明确报错，原始证据保留。修前失败输出见 [回归故障记录](prompt02-evidence/prompt02-regression-failure-before-fix.log)，修后定向及最终全量输出一起交付。

## 真实 fake 性能

命令：`python tools/benchmark_prompt02.py`。原始逐次 HTTP 时延、资源和锁数据在 [benchmark-prompt02.json](benchmark-prompt02.json)。每次 fake 查询等待 2 ms，提交等待 2 ms，核验额外等待 8 ms，包含真实磁盘持久化、tracemalloc 和每 0.5 秒本地 HTTP 轮询；是单次样本，不作统计显著性或生产速度承诺。

|行数|模式|总秒数|提交数/错误数|最大平台并发|Python 峰值 MiB|HTTP P95 ms|首次核验最长等待 ms|最长持锁 ms|
|---:|---|---:|---:|---:|---:|---:|---:|---:|
|100|串行|9.14|100/0|1|3.25|164.67|0.00|166.47|
|100|双通道|10.57|100/0|2|2.87|219.20|334.80|327.27|
|500|串行|57.13|500/0|1|18.49|294.49|0.00|286.42|
|500|双通道|56.55|500/0|2|15.70|293.08|270.73|265.74|
|1000|串行|147.49|1000/0|1|35.46|449.66|0.00|477.80|
|1000|双通道|135.64|1000/0|2|27.89|484.84|441.32|480.23|

1000 行串行 147.49 秒、双通道 135.64 秒：这里只报告本次小延迟 fake 工作量的单次结果，不推断真实平台加速倍数。双通道确认了独立线程与同时在途查询/提交。Python 内存不包含真实浏览器 RSS；目标机器真实浏览器内存、网络和平台等待未测。状态投影在 1000 行时仍有数百毫秒锁占用，是后续容量评估需关注的现有限制，导出构建本身不持该锁。

|XLSX 行数|发布秒数|Python 峰值 MiB|最大导出持锁 ms|清单查询 ms|文件字节|
|---:|---:|---:|---:|---:|---:|
|100|0.220|2.56|4.787|16.423|20920|
|500|1.013|12.35|3.886|26.064|81294|
|1000|2.053|24.67|4.206|25.100|156361|
|2000|3.936|49.33|4.385|42.988|307270|

清单查询 ms 为 `inventory()` 服务层测量，登记表 HTTP 时延另见上表。构建性能含 ZIP/XML、行数/分类对账、哈希、历史和当前版发布；命令输出原文随证据交付。本轮性能测量源码提交为 `99a2087ad0800b53e72ecad0b616550796f455a4`，未与全量回归并发运行。

## 匿名 1000 行样例与原生 Excel 兼容

生成命令：`python tools/generate_prompt02_sample.py`。样例：[gift-registration-2026-11-09.xlsx](samples/gift-registration-2026-11-09.xlsx)；日期映射：[batch-day-example.json](samples/batch-day-example.json)。全部订单、执行 ID 和内部记录均为合成值。

样例 SHA-256：`9705e7d8c558939916e99fb4ece25af61e09f9604b41922718d72f4890b8bd73`。

样例包含成功登记、失败登记、任务汇总、任务明细、后续复核五表。任务明细 1000 行，原始 600 行完成于 11/09 23:00，400 行完成于 11/10 04:00，统一业务日 11/09。冻结表头、文本订单号、数值计数、ISO 偏移时间均已检查。

用 @oai/artifact-tool 导入检查并渲染全部五表、检查无公式错误；其预览把数字样式的文本订单显示成科学计数法。因此另用已安装 LibreOffice headless 进行原生 XLSX 往返和 PDF 检查：原文件、原生往返文件均保持文本 `0001000000000000000000` 至 `0001000000000000000999`，PDF 文本与可视结果亦保留前导零。没有用预览库重导出交付文件，也未改默认 Office 关联。

```powershell
& 'C:\Program Files\LibreOffice\program\soffice.com' '-env:UserInstallation=file:///D:/UserData/Desktop/NL/main-core-repair-20261111/.tmp/lo-profile' --headless --convert-to 'xlsx:Calc MS Excel 2007 XML' --outdir .tmp\lo-qa docs\samples\gift-registration-2026-11-09.xlsx
& 'C:\Program Files\LibreOffice\program\soffice.com' '-env:UserInstallation=file:///D:/UserData/Desktop/NL/main-core-repair-20261111/.tmp/lo-profile' --headless --convert-to 'pdf:calc_pdf_Export:{"PageRange":{"type":"string","value":"1-2"}}' --outdir .tmp\lo-qa docs\samples\gift-registration-2026-11-09.xlsx
```

原生往返及 600/400 边界检查输出见 [native-xlsx-qa](prompt02-evidence/prompt02-native-xlsx-qa.log)，并保留 [原生订单文本图](prompt02-evidence/native-excel-order-text.png) 与 [任务日志对话框](prompt02-evidence/task-log-dialog.png)。PDF 是技术验证中间文件，交付原始 XLSX 不依赖 PDF。

## 改动、保留和风险

完整代码清单：[changed-code-files.txt](prompt02-evidence/changed-code-files.txt)。核心涉及 workflow、registration、job_storage、review_browser、verification_lane、host_management、business_day、task_day_export、daily_export、maintenance、frontend_server 与前端；新增跨日、并发、会话、API 和浏览器测试及测量工具。

原工作区既有文件未改写；修改均在独立克隆内。没有读取/复制/提交 Cookie、密码或真实业务 Excel，没有操作生产服务。Prompt 01 matcher 文档与原性能文件保留。没有访问 Git honor 分支。

默认串行回退可用，双通道须明确启用。真实平台页面、同账号双会话、目标电脑驱动/锁/网络、真实 14 条匹配夹具未现场验证。未知提交保持排他；日期不可信保持人工核对；损坏清单不覆盖修复。配置回退与旧二进制数据兼容限制详见 [设计、迁移与人工部署检查表](task-day-export-design.md)。
