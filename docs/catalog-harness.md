# v1.3.0 受限目录维护工具

状态：代码与离线协议可运行；2026-11-05 已配置真实只读商品查询适配器（gift_registry/product_reader.py），并新增面向用户的 `update-gifts.bat` / `tools/update_gifts.py`（从 config/gifts-<分组名>.txt 更新目录，只使用班牛商品列表第一行名称）。查询只打开独立 Selenium 配置与加密会话副本；不加入礼品、不输入订单、不提交工单。未登录或页面不完整时如实返回 blocked，不猜测名称。

2026-11-03 只读配置快照含 217 个目录编码，分组 revision 为 16；分组仍引用目录中不存在的 `XS24AMA25000R17001`、`XS26QHA350020WU002`。真实快照的 status/plan 因交叉引用校验拒绝继续，文件原样保留。此项须由主机核对两份配置的当前版本与用户意图，不能由维护代理猜测补商品或删除归属。

## 运行与边界

在本隔离副本运行 `.venv\Scripts\python.exe -X utf8 tools\catalog_harness.py`，通过 stdin 传一个 JSON 对象。`--stdio` 接受一行一个 JSON 请求，输出一行一个结果。Python 仅使用当前代码所在根目录，不接受模型输入的 root、path、URL、脚本、shell 或动态模块名。

项目配置写入目标固定为 `config/gifts.json` 和 `config/gift_groups.local.json`。计划、证据、备份、事务日志、批准和独立浏览器目录都位于 `.tmp/catalog-maintenance/`。生产目录 `D:\UserData\Desktop\NL` 及其子目录、`E:\gifts` 及其子目录均拒绝作为本开发工具的应用目标；所有路径检查符号链接和 junction。

模型路由仅开放 `status/query/plan/apply/recover`。主机 `--approve PLAN_ID` 不属于模型路由。`apply` 必须有主机对该摘要计划的批准；同一计划重复应用不重复追加别名或推进 revision。不得将普通 shell、Python 执行或文件写入工具同时交给维护代理。

本轮按用户要求交付代码和 prompt，未安装或修改实际 DeepSeek harness。`catalog-harness-policy.json` 是供宿主适配的权限规范，**不是已安装的 OS 沙箱或某个厂商 harness 的有效配置声明**。调用方须把固定启动命令封装成单一 JSON 工具，禁止模型改变启动参数、调用批准命令及读写源码。若宿主无法剥离广泛文件/执行工具，只允许 dry-run，不向该会话开放 apply。代码白名单和宿主权限路由是两个防线，不能用 prompt 代替权限隔离。

## JSON 协议

默认 `op` 为 `plan`，不会修改两份配置。

```json
{"op":"status"}
{"op":"query","codes":["SYN-001","SYN-002"]}
{"op":"plan","evidence_ids":["工具实际返回的64位证据摘要"],"changes":[{"code":"SYN-001","name":"经两平台精确核实的合成名称","aliases_add":[]}]}
{"op":"plan","changes":[{"code":"SYN-001","groups":{"mode":"add","ids":["synthetic-group"]}}]}
{"op":"apply","plan_id":"经主机批准的64位计划摘要"}
{"op":"recover"}
```

上面的名称和编码都是协议示例，不是平台核验结果。已有分组维护 `mode` 支持 `add/remove/replace`；`replace` 空列表表示明确移出所有分组。创建分组必须显式给出 `create_groups:[{"id":"synthetic-group","name":"合成分组"}]`。新增商品必须同时给出 `add_items` 与完全相同的 `allow_add_codes`，且名称须有两个平台的精确查询证据；默认不增加、删除或改写编码。

更名仅接受请求平台（`platforms` 字段，默认两个平台）对同一完整编码观察到相同名称的工具证据；未找到、页面不完整、认证失效、同编码冲突或名称不一致均进入 pending，整个计划不可应用。update-gifts 按主机规则只要求班牛平台，并以商品列表第一行名称作为目录名称。别名只接受已有有效名称/别名或实际观察到的名称。更名保留原名与有效旧别名，超出实际 30 项/别名 100 字符/名称 200 字符限制时拒绝，不截断。历史空白别名在普通读取时兼容忽略，更名移除空白时会出现在完整前后差异中。

主机必须先核对 plan 返回的完整编码、前后名称/别名、各项分组前后差异及新增分组，然后明确执行：

```powershell
# 仅主机操作；PLAN_ID 必须来自刚审核过的计划。
.\.venv\Scripts\python.exe -X utf8 tools\catalog_harness.py --approve PLAN_ID
# 再向模型路由提交 {"op":"apply","plan_id":"PLAN_ID"}
```

计划绑定两个原始文件 SHA-256 和分组 revision，预览后任意文件变化都会拒绝应用。普通更名不重写分组文件；实际归属变化才增加 revision。

## 中断恢复

两个 `replace` 不是真正的文件系统跨文件原子事务。工具在互斥锁内先持久化 pending 日志和原始备份，再替换变更目标；目录与分组读端共用互斥及事务检查。进程中断后，读端拒绝读取半套配置，主机调用 `recover` 回滚到原始两文件。若发现事务外修改，则保持阻塞并要求主机核查，不覆盖新内容。备份位于维护工作目录，项目历史任务文件不会被修改。

## 平台查询适配（2026-11-05 已完成）

`gift_registry/product_reader.py` 实现只读商品查询。`update-gifts` 只启用班牛：在礼赠工作表空白新建表单的“非原单商品明细”里按商家编码筛选，同一编码只读取结果列表第一行的商品名称作为目录名称（不翻页）。聚水潭商品资料页查询代码保留在读取器中供其它适配使用，但 update-gifts 不再调用。读取器不加入礼品、不输入订单、不点击提交；会话使用 DPAPI 加密快照的独立副本。

reader 返回 `platform / queried_code / observed_at / source_url / status / matches[{code,name}] / reason`，班牛观察另含 `total`。名称比对忽略空白与全半角差异（NFKC 后去掉空白）。`plan()` 支持 `platforms` 字段（默认两平台；update-gifts 传 `["banniu"]`）：要求平台均存在精确结果且名称归一化一致才允许新增或更名；无法核实时已有编码保留原名、仅更新分组，新编码阻止整批。认证失败或页面不完整时返回 blocked，不猜测名称。

`update-gifts.bat` / `tools/update_gifts.py` 是主机侧便捷入口：读取 `config/gifts-*.txt`，只按班牛第一行名称更新，预览差异并要求输入 Y 后才写入。班牛未登录或会话过期时保留可见浏览器并等待人工登录（默认 300 秒，`--login-wait` 可调；`--headless` 不等待），登录成功后保存加密会话并继续查询。`tools/catalog_harness.py --live` 是 JSON 协议入口。两者都不导入登记队列、订单查询或提交编排。测试中的 `SyntheticReader` 只在测试模块显式注入；CLI 不暴露任意代码执行。合成夹具与离线测试不等于真实平台查询已经验证。

## 实际离线示例

合成夹具 `00-Ab/1` 原名“合成旧名称”，别名为“合成旧别名”，属于 a、b 两组。用显式注入的 SyntheticReader 产生标记为合成的两平台结果后，预览更名为“合成核实名称”、移出 a 组：新别名为“合成旧别名、合成旧名称”，保留 b 组及自定义字段；另一同名编码 `00-Ab/2` 不变。dry-run 两文件字节未变，主机批准后在临时合成目录应用成功，第二次 apply 返回 idempotent=true，revision 只增加一次。完整实际输出保存在开发副本 `.tmp/harness-demo-result.json`。

真实 CLI 在未登录或会话失效时返回 blocked；2026-11-05 已用班牛真实登录会话对 config/gifts-荣耀.txt 执行只读核实（报告见 .tmp/catalog-maintenance/reports/，说明见 docs/releases/v1.3.0-ry.1.md）。临时夹具的应用结果不得作为真实平台或生产目录更新证据。
