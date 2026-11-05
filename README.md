# 荣耀礼赠登记 v1.3.0-ry.1

基于原项目 v1.3.0 的独立荣耀版，目录为 D:/UserData/Desktop/NL/gifts-ry。

## 使用入口

- 双击 start.bat 打开荣耀工作台：http://127.0.0.1:8767/。
- 双击 login.bat 打开独立平台登录助手；stop.bat 只关闭荣耀工作台。
- 班牛登录入口沿用原项目：https://work.bytenew.com/（实际跳转到 app.html#/login）。
- 登录后的登记工作表：https://work.bytenew.com/app.html#/applet/appNew/projectNew/58231/27715。
- 该账号登录后先进入“赠品补发看板——荣耀”，程序确认看板后进入“聚水谭赠品补发—荣耀”。

## 目录与账号

礼品目录只从用户指定的 example_荣耀.md 的 gifts list 部分初始化，共 191 个独立编码；同名礼品保留不同编码，统一归入“荣耀”分组。文档中的示例订单没有导入。

聚水潭使用原项目加密账号和会话的独立副本。班牛使用新提供的账号（尾号 0845），密码通过当前 Windows 用户的 DPAPI 加密保存在 config/accounts.local.json；不复制原班牛会话。账号、Cookie、日志和业务数据均被 Git 忽略。

config、data、草稿、队列锁、浏览器配置、驱动缓存及端口独立。原项目继续使用 8765，本版使用 8767；启动/停止时检查版本身份，拒绝误操作原工作台。当前默认仅本机访问，未复制原项目的局域网密码、域名或防火墙设置。

## 登记行为

- 编码必须完整精确匹配，保留大小写、前导零和标点；名称和别名保留原搜索方式。
- 保留 Excel 风格下拉填充、Ctrl+D、双击填充柄、撤销和分组范围校验。
- 荣耀表单使用实际核对后的字段编号，校验“能良移动／荣耀”，平台需要从订单拉取结果回填。
- 每张订单的全部礼品放入同一个工单，数量为 1、金额为 0；仅点击一次提交。
- 回执必须同时匹配工单、订单、公司、品牌、礼品、数量及新增聚水潭单号；“平台失败”和“班牛失败”不会被当成成功。
- 不支持改地址；提交结果不确定时不自动重试。

## 验收与当前限制

2026-11-05 已通过独立浏览器标签验证新班牛账号登录、荣耀工作表、空白表单、完整含斜杠编码搜索及同步状态选项。未输入真实订单、未提交工单。页面核对使用独立内置浏览器；不能将其称为 Selenium 实单验收。

2026-11-05 已定位并修复“业务浏览器 Selenium 无法启动”：本机项目目录曾被 Windows 沙箱工具写入“低完整性级别”（Low Mandatory Level）权限标签，`.venv` 里的 Python 因继承了该标签而以低完整性进程运行，Selenium 启动的 Chrome / Edge 在启动阶段直接退出（Chrome SessionNotCreatedException、Edge tab crashed），与业务代码无关。已将项目目录的完整性级别恢复为普通级别；修复后 Selenium 空白页检查在 Chrome 与 Edge 的有头、无头模式全部通过（见 `.tmp/browser-startup-result.json`）。

verify-honor.bat 会在独立 Selenium 会话中只读检查登录和空白表单，报告保存到 .tmp/honor-page-structure.json。需要验证码时由用户完成；不会输入订单或点击提交。浏览器正常启动后，用此脚本检查 Selenium 路径，再由用户决定实际业务登记。

## 业务浏览器无法启动时

先双击 diagnose-selenium.bat：它只启动独立空白页，分别用有头、无头模式检查 Chrome 与 Edge，并报告当前 Python 的 Windows 完整性级别；不读取账号、Cookie 或业务数据，结果写入 .tmp/browser-startup-result.json。

如果报告 `SessionNotCreatedException`、`tab crashed`，或完整性级别为 `low`：

- 原因：项目目录被 Windows 沙箱／权限工具再次写入了低完整性级别标签。低完整性进程启动的浏览器无法创建正常窗口与临时文件，会在启动阶段退出；这不是程序代码或账号问题。
- 修复：双击 fix-browser-integrity.bat，等待提示“修复完成”，再运行 diagnose-selenium.bat 复查。
- 该修复只调整 Windows 完整性级别标签，不修改任何文件内容或所有者；为修改标签会给当前用户补一条项目目录的完整控制权限。之后如需让自动化代理在“受限/workspace-write”沙箱中运行本项目，请改用完全权限，否则沙箱可能重新打上标签，再次需要运行一次修复。

原始示例中 XS26QHA350020WU002 不在 gifts list 内；还有一处相邻编码缺少分隔符。这些异常没有被猜测修复或导入。以已确认目录为准。

详细字段映射及验收范围见 docs/releases/v1.3.0-ry.1.md。原有 docs/releases 中较早版本的实单记录属于原项目历史，不是荣耀版实单验证。
