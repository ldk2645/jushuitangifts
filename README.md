# 礼赠自动登记工具

Version 1（`v1.0.0`）。面向 Windows 的 Python + Selenium 工具，通过本地表格工作台管理订单与礼品，在聚水潭查询订单、向班牛登记，再回聚水潭核对新增记录。

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

工作台只监听 `127.0.0.1`。启动程序、保存草稿及轮询状态不会自行登记。若登录助手仍占用配置，开始操作会提示先关闭助手，本次不创建业务任务；关闭后需再次明确点击开始。

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
.\.venv\Scripts\python.exe tests\smoke_login_shutdown.py
.\.venv\Scripts\python.exe tests\smoke_banniu_list.py
```

发布回归的 290 项单元测试通过。单元测试使用隔离目录和显式替身；Chrome 验收加载本地测试页面，验证工作台、会话恢复、关闭行为及班牛列表解析。测试通过不等于真实平台登记通过。历史真实验证已分别覆盖自动登记后的新增记录核验，以及两站自动登录；本轮发布整理不再次提交真实订单，详见发布说明。

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
