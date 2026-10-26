# 礼赠自动登记工具

Version 1（`v1.0.0`，2026-10-26 更新）。面向 Windows 的 Python + Selenium 工具，通过本地表格工作台管理订单与礼品，在聚水潭查询订单、向班牛登记，再回聚水潭核对新增记录。

**本版本仅支持原地址礼赠。** 改地址的草稿交互保留，但此类任务会在查询或提交前失败。本版本不读取或核对收件信息。

## 安装

需要 Windows、Python 3.12（含 tkinter）和已安装的 Chrome。在项目目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
.\.venv\Scripts\python.exe main.py --version
```

安装脚本创建或复用 `.venv`，并安装 `requirements.txt` 中的依赖。首次启动浏览器时，Selenium Manager 需要联网获取与 Chrome 匹配的驱动。前端无需 Node.js 或构建步骤。

## 使用

1. 双击 `start.bat`，打开本地工作台 `http://127.0.0.1:8765/`。
2. 点击“平台登录”，或双击 `login.bat`。在工具打开的两个独立 Chrome 窗口中完成登录；验证码、扫码和二次验证由人工完成。
3. 登录成功后点击对应平台的“我已登录，确认并保存”，再点击“保存并关闭浏览器”，释放登录配置。
4. 填写订单号，选择该订单的全部礼品。“是否改地址”保持空心圆点。
5. 点击“star 开始登记”。工具保存任务快照，按订单逐条串行执行；鼠标停在状态上可查看结果或失败原因。

工作台支持多行新增、Excel 多行粘贴、礼品多选与模糊匹配、自动保存、撤销和列宽调整。订单号、商品编码和电话按字符串处理，保留前导零及长编号。输入内容保存在本机草稿中，刷新后可继续编辑。

礼品目录位于 `config/gifts.json`，商品编码就是礼品 ID。支持在工作台点击“新增礼赠”，填写编码、完整名称和可选别名，追加保存后立即可用；冲突内容不会覆盖已有礼品。“赠品小背包”与“小米小背包”分别显示，礼品名称中的斜杠不会作为粘贴分隔符。粘贴后仍未匹配的礼品需人工选择。

工作台默认只监听 `127.0.0.1`；可配置带独立访问密码的[局域网共享模式](docs/lan.md)。启动程序、保存草稿及轮询状态不会自行登记。若登录助手仍占用配置，开始操作会提示先关闭助手，本次不创建业务任务；关闭后需再次明确点击开始。

启用共享后，主机通过 `127.0.0.1` 或 `localhost` 仍免登录；通过局域网 IP、域名访问继续使用独立访问密码。

局域网新密码使用六位数字，仍在本机加密保存；旧长密码可通过 `main.py --simplify-lan-password` 明确更换后重启。网络或 IP 变化时，工具在主机弹出提醒，并在本机工作台提示去 Cloudflare 核对 DNS 和同步本机共享配置；详见[局域网说明](docs/lan.md)。其他设备打不开时先看 `data/frontend_access.log`：只记录状态码、方法、无查询参数的路径、Host 与客户端网段，不记录 Cookie、密码或请求体。

多订单和后续批次复用同一组平台浏览器。订单页或礼赠列表已就绪时，直接更新订单号并查询，减少整页重复加载；详情页、其它入口或页面未就绪时才重新进入已核对的列表。每次查询仍须确认本次刷新，原订单基线、回执和成功结果不会跨订单复用。浏览器保留到正常关闭工作台，不按订单反复开关。

如果手动关闭了平台浏览器，下次获取业务页面时会先确认旧会话已关闭、清理旧驱动，再复用原登录配置打开新浏览器，并重建页面适配器。连接超时等无法确认关闭的情况不会另开浏览器；提交途中关闭窗口仍按失败处理，不重放提交。已失败订单不会因此自动恢复。

班牛同一带 `#` 的地址仍残留详情时，会刷新一次并等待列表恢复可交互；已有编辑表单不刷新。关键按钮与回执入口先检查实际点击位置，避免透明加载层拦截。等待失败不会再次提交，状态详情会区分提交前失败与点击提交后的不确定结果。

填写失败后，会关闭本次自动创建、未触发提交且未被人工修改的表单，让后续独立订单继续。人工表单、内容异常和提交结果不确定的表单保留供核查；失败任务仍禁止重试。同编码的平台名称差异只通过已核对的明确映射兼容，不使用相似名称替代不同商品编码。

班牛任务保存后，异步同步中的状态不再立即判失败：最多进行 8 轮只读回执核验，轮次间隔 3 秒（另计页面加载时间）。明确同步失败或业务信息不符立即停止；等待耗尽仍未确认成功则失败。筛选后表格仍未刷新时，最多刷新列表重查一次，已有编辑表单会阻止刷新。上述等待和重查均不会重新提交登记。

## 自动登录与本机数据

工具优先复用各平台独立的 Chrome 配置，以及加密保存的 Cookie、localStorage 和 sessionStorage。会话过期后，若已配置账号，则在识别到官方 HTTPS 登录表单时尝试一次账号密码登录。验证码、二次验证、密码错误或结果不确定时提示人工处理，不反复发送密码。

自动登录账号由 `CredentialStore` 保存到本机 `config/accounts.local.json`。可在项目目录通过以下交互命令配置，密码输入不回显，也不写入命令历史：

```powershell
.\.venv\Scripts\python.exe -c "from getpass import getpass; from gift_registry.config import SITES; from gift_registry.credentials import CredentialStore; store = CredentialStore(); [(print(site.name), store.save(site.key, input('Username: '), getpass('Password: '))) for site in SITES]; print('Saved locally.')"
```

用户名按文本保存，密码和会话快照使用当前 Windows 用户的 DPAPI 加密。账号配置及整个 `data/` 目录均排除在 Git 之外；GitHub 发布不包含账号、会话、用户草稿或任务记录。换电脑或 Windows 用户后需重新配置登录，不能直接解密旧快照。Chrome 配置由浏览器管理，其整个目录并非均由本项目 DPAPI 加密。

**升级前正常退出工作台和登录助手，备份并保留 `data/`、本机账号配置及自行维护的礼品目录。** 持久化防重记录位于 `data/registration_jobs/`；删除或遗漏它们会丢失既往登记依据。已有任务不能靠删除记录、复制新行或重启程序来重试。

## 业务结果与限制

- 聚水潭先保存原订单与已有记录作为基线；班牛将全部礼品加入同一个任务，最多提交一次；提交后读取回执，再回聚水潭核对新增记录。
- 只有本次新增记录、订单号、礼品编码、数量及回执关联信息均一致，才返回“成功”。已有相同礼品、按钮点击成功或平台提示成功都不足以判定成功。
- 失败、超时和结果不确定均不自动重提。重复点击、复制相同业务到新行、跨批次和重启服务继续复用持久化结果。
- 登录恢复只允许有界的只读重查，不重新填写失效的登记表单，也不重试礼赠提交。
- 页面适配针对已核对的聚水潭订单页和班牛礼赠工作表；平台页面、权限或字段变化时需要重新验证。失败后的人工核查不包含自动重试入口。

详细规则见 [业务流程](docs/business-workflow.md)、[自动登录](docs/automatic-login.md) 和 [v1.0.0 发布说明](docs/releases/v1.0.0.md)。

## 开发与验证

```powershell
.\.venv\Scripts\python.exe main.py
.\.venv\Scripts\python.exe main.py --login
.\.venv\Scripts\python.exe main.py --no-open --port 8765
.\.venv\Scripts\python.exe main.py --status
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe tests\smoke_frontend.py
.\.venv\Scripts\python.exe tests\smoke_browser.py
.\.venv\Scripts\python.exe tests\smoke_browser_reopen.py
.\.venv\Scripts\python.exe tests\smoke_login_shutdown.py
.\.venv\Scripts\python.exe tests\smoke_banniu_list.py
.\.venv\Scripts\python.exe tests\smoke_banniu_form_cleanup.py
.\.venv\Scripts\python.exe tests\smoke_page_reuse.py
.\.venv\Scripts\python.exe tests\smoke_lan.py
```

当前发布回归的 381 项单元测试通过。单元测试使用隔离目录和显式替身；Chrome 验收加载本地测试页面，验证工作台、会话恢复、关闭行为、浏览器重建、局域网共享及班牛列表解析。测试通过不等于真实平台登记通过。历史真实验证已分别覆盖自动登记后的新增记录核验，以及两站自动登录；本轮发布整理不再次提交真实订单，详见发布说明。

后续页面复用优化新增跨批次、提交超时、防重和刷新检查的回归测试。`smoke_page_reuse.py` 仅通过本机合成页面验证连续查询减少整页加载、拒绝未刷新的旧结果，并确认详情状态会重新进入列表；不访问真实平台或提交礼赠。

局域网模式保留一个共享草稿、一个串行任务队列和原有防重记录；其他设备不需要安装 Python 或 ChromeDriver。访问控制覆盖客户端子网、准确域名、独立密码及跨站写请求。平台登录与验证码由主机维护，远端页面不会打开主机登录助手。`smoke_lan.py` 使用两个隔离 Chrome 客户端和模拟业务，验证共享草稿、编辑冲突及重复任务复用。

```text
main.py                    工作台入口、登录助手、状态和版本查询
frontend/                  无构建依赖的表格前端
gift_registry/             本地服务、任务队列、业务核验与 Selenium 页面操作
config/gifts.json          礼品编码、名称和别名
example.md                 礼品清单来源
tests/                     单元测试与本地 Chrome 验收
docs/                      业务、登录和发布文档
data/                      本机运行数据，运行时创建，不上传
config/accounts.local.json 本机账号配置，不上传
```
