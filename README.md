# 荣耀礼赠登记 v1.3.0-ry.1

基于原项目 v1.3.0 的独立荣耀版，目录为 D:/UserData/Desktop/NL/gifts-ry。

## 使用入口

- 双击 start.bat 打开荣耀工作台：http://127.0.0.1:8767/。
- 双击 login.bat 打开独立平台登录助手；stop.bat 只关闭荣耀工作台。
- 双击 update-gifts.bat 按 config/gifts-分组名.txt 更新礼品目录（只读查询聚水潭／班牛商品名称，确认后才写入）。
- 班牛登录入口沿用原项目：https://work.bytenew.com/（实际跳转到 app.html#/login）。
- 登录后的登记工作表：https://work.bytenew.com/app.html#/applet/appNew/projectNew/58231/27715。
- 该账号登录后先进入“赠品补发看板——荣耀”，程序确认看板后进入“聚水谭赠品补发—荣耀”。

## 目录与账号

礼品目录只从用户指定的 example_荣耀.md 的 gifts list 部分初始化，共 191 个独立编码；同名礼品保留不同编码，统一归入“荣耀”分组。文档中的示例订单没有导入。后续新增的“博锐电动剃须刀”使目录为 192 项；用户给出的 config/gifts-荣耀.txt（17 个编码）是目录更新工具的输入格式示例，不会把目录收敛为这 17 项。

聚水潭使用原项目加密账号和会话的独立副本。班牛使用新提供的账号（尾号 0845），密码通过当前 Windows 用户的 DPAPI 加密保存在 config/accounts.local.json；不复制原班牛会话。账号、Cookie、日志和业务数据均被 Git 忽略。

config、data、草稿、队列锁、浏览器配置、驱动缓存及端口独立。原项目继续使用 8765，本版使用 8767；启动/停止时检查版本身份，拒绝误操作原工作台。当前默认仅本机访问，未复制原项目的局域网密码、域名或防火墙设置。

## 登记行为

- 编码必须完整精确匹配，保留大小写、前导零和标点；名称和别名保留原搜索方式。
- 保留 Excel 风格下拉填充、Ctrl+D、双击填充柄、撤销和分组范围校验。
- 荣耀表单使用实际核对后的字段编号，校验“能良移动／荣耀”，平台需要从订单拉取结果回填。
- 每张订单的全部礼品放入同一个工单，数量为 1、金额为 0；仅点击一次提交。
- 同一礼品可登记多件（每种 1–99 件）：礼品选择器内用 ＋/− 调整数量，表格显示“名称 ×N”；班牛任务按“数量”列填写，回执及聚水潭新增记录按件数合计核对。
- 回执必须同时匹配工单、订单、公司、品牌、礼品、数量及新增聚水潭单号；“平台失败”和“班牛失败”不会被当成成功。
- 不支持改地址；提交结果不确定时不自动重试。

## 礼品目录更新（update-gifts.bat）

- 输入：config/ 下每个分组一个 UTF-8 文本文件 `gifts-<分组名>.txt`，每行一个完整商品编码，保留前导零、大小写和标点；空行忽略，重复行按首次出现去重，文件名中的分组名最长 40 个字符。
- 双击 update-gifts.bat 后，程序用独立 Selenium 配置打开班牛礼赠工作表的空白新建表单，按商家编码只读搜索每个编码，并以**商品列表第一行的商品名称**作为该编码在目录中的名称；不加入礼品、不输入订单、不提交工单。
- 同一编码有多条重复商品记录时不翻页，只取列表第一行；班牛未找到该编码时保留目录原名。
- 新名称与目录不同时写入 gifts.json，旧名称自动保留为别名；新编码必须取得班牛名称才新增；已有编码名称无法取得时保留原名，只按 txt 更新分组。名称比对忽略空白和全半角差异；未列出的礼品和分组保持不变。
- 预览会打印新增、改名、分组变化、名称待核实及阻止项；输入 Y 才写入 config/gifts.json 与 config/gift_groups.local.json。写入使用计划摘要、原文件摘要、备份和失败回滚，报告保存在 .tmp/catalog-maintenance/reports/。
- 只读查询需要班牛已登录（可先运行 login.bat）；验证码由用户完成。`--headless` 隐藏查询浏览器，`--offline` 仅按已有目录补充分组、不联网。

## 验收与当前限制

2026-11-05 已完成一单真实订单的 Selenium 实单验收：订单 6919195281288998795，礼品 XS24AMA23002RMG001（荣耀FlyClip 3i 耳夹式耳机）、XS235JA23002TSN001（荣耀WIN RT2钢化膜）。班牛工单 8453186 同步状态“成功”（2026-11-05 12:38:03），回传聚水潭记录 51742868；只读复查聚水潭确认该记录含两种礼品各 1 件。同批修复三处自动登记缺陷：同一编码存在新旧名称多条搜索结果时只认首条、重试沿用失败任务的旧目录名称、回执“创建时间”的秒级时钟偏差被误判为旧任务，详见 docs/releases/v1.3.0-ry.1.md。本次提交由修复前版本执行，工具曾把该单记为“提交异常、结果不确定”；核实平台已成功后已在工作台标记人工成功，原失败执行历史保留且不再进入重登列表。

2026-11-05 已定位并修复“业务浏览器 Selenium 无法启动”：本机项目目录曾被 Windows 沙箱工具写入“低完整性级别”（Low Mandatory Level）权限标签，`.venv` 里的 Python 因继承了该标签而以低完整性进程运行，Selenium 启动的 Chrome / Edge 在启动阶段直接退出（Chrome SessionNotCreatedException、Edge tab crashed），与业务代码无关。已将项目目录的完整性级别恢复为普通级别；修复后 Selenium 空白页检查在 Chrome 与 Edge 的有头、无头模式全部通过（见 `.tmp/browser-startup-result.json`）。

verify-honor.bat 会在独立 Selenium 会话中只读检查登录和空白表单，报告保存到 .tmp/honor-page-structure.json。需要验证码时由用户完成；不会输入订单或点击提交。浏览器正常启动后，用此脚本检查 Selenium 路径，再由用户决定实际业务登记。

## 平台登录只打开一个平台浏览器时

先看登录助手该平台行显示的文字，以及 `data/status.json` 中该平台的 `state` / `warning`。

- 若显示“浏览器启动未完成（SessionNotCreatedException / WebDriverException…）”，或一直停在“正在启动浏览器”：
  1. 最常见原因：该平台浏览器配置仍被另一个本工具浏览器窗口/后台进程占用（聚水潭窗口能打开只说明聚水潭配置空闲）。正常流程应先双击 stop.bat 关闭工作台和业务浏览器，再打开登录助手。打开任务管理器→详细信息，结束命令行包含 `data\browser\banniu`（便携版 Edge 为 `data\browser_edge\banniu`）的 chrome.exe/msedge.exe，并关闭多余的 pythonw.exe 助手进程后重试。
  2. 双击 **diagnose-login.bat**：只启动两个平台浏览器做只读检查，报告写入 `.tmp/login-diagnostic.json`，逐项给出浏览器模式、Windows 完整性级别、登录配置锁、会话/账号能否解密、配置目录是否仍被占用、浏览器启动失败阶段。
  3. 若报告提示配置目录仍无法启动：关闭所有本工具窗口后，把 `data\browser\banniu`（或 `data\browser_edge\banniu`）改名为 `.bak` 备份，再双击 login.bat，程序会重建该平台配置；在打开的页面手动完成登录和验证码后点“我已登录，确认并保存”。
  4. 若报告 `SessionNotCreatedException`/`tab crashed` 或完整性级别为 `low`：双击 fix-browser-integrity.bat 修复已知的 Windows 低完整性标签问题（不是代码或账号问题），再运行 diagnose-selenium.bat 复查。
- 若显示“等待你手动登录”或页面已打开：浏览器其实已启动，先用 Alt+Tab/任务栏确认班牛窗口是否被聚水潭窗口挡住或最小化；完成班牛登录（含验证码）后点“我已登录，确认并保存”。
- 若提示“登录配置正在使用”：工作台或另一个登录助手仍在占用登录配置，先正常关闭占用进程再试。
- 换电脑后从旧电脑复制的 `config/accounts.local.json` 和 `data/sessions/*.dpapi` 无法在新 Windows 用户下解密：这不影响浏览器打开，但自动登录不会生效；在目标电脑手动登录并保存会话即可，需要自动登录时用 accounts.bat / settings.bat 重新输入并保存账号密码。
- 目标电脑必须能访问 https://work.bytenew.com/（公司代理、防火墙、DNS 可能只拦截该站点）；聚水潭能访问不代表班牛能访问。仍无法定位时，把 `.tmp/login-diagnostic.json`、`.tmp/browser-startup-result.json`、`data/status.json` 发给维护者。

## 业务浏览器无法启动时

先双击 diagnose-selenium.bat：它只启动独立空白页，分别用有头、无头模式检查 Chrome 与 Edge，并报告当前 Python 的 Windows 完整性级别；不读取账号、Cookie 或业务数据，结果写入 .tmp/browser-startup-result.json。

如果报告 `SessionNotCreatedException`、`tab crashed`，或完整性级别为 `low`：

- 原因：项目目录被 Windows 沙箱／权限工具再次写入了低完整性级别标签。低完整性进程启动的浏览器无法创建正常窗口与临时文件，会在启动阶段退出；这不是程序代码或账号问题。
- 修复：双击 fix-browser-integrity.bat，等待提示“修复完成”，再运行 diagnose-selenium.bat 复查。
- 该修复只调整 Windows 完整性级别标签，不修改任何文件内容或所有者；为修改标签会给当前用户补一条项目目录的完整控制权限。之后如需让自动化代理在“受限/workspace-write”沙箱中运行本项目，请改用完全权限，否则沙箱可能重新打上标签，再次需要运行一次修复。

原始示例中 XS26QHA350020WU002 不在 gifts list 内；还有一处相邻编码缺少分隔符。这些异常没有被猜测修复或导入。以已确认目录为准。

详细字段映射及验收范围见 docs/releases/v1.3.0-ry.1.md。原有 docs/releases 中较早版本的实单记录属于原项目历史，不是荣耀版实单验证。
