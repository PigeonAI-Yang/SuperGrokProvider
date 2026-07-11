# SuperGrok Router

一个不限账号数量的本机 SuperGrok 账号池。每个账号通过官方 Grok Build 设备授权登录，对外提供一个稳定的 OpenAI-compatible Provider。

## 启动

前提：Windows 已安装官方 `grok` CLI，且 `grok --version` 可运行。

```powershell
cd C:\path\to\supergrok-router
.\start.ps1
```

启动脚本会用 Chrome 或 Edge 的 App 模式打开一个 1280×720 本地工具窗口。最小化窗口会隐藏到系统托盘；双击托盘图标或点击“打开”即可恢复，点击“退出”会同时关闭窗口和后台 Provider。

启动器和后端各有独立的 Windows 单例锁；重复双击不会再创建第二个窗口、托盘宿主或端口监听进程。

应用窗口和系统托盘使用 `static/app-icon.png` / `static/app-icon.ico` 中的 SG 图标。
启动器会通过 Windows 原生窗口消息同步设置任务栏的大、小图标，不依赖 Chrome 的 favicon 缓存。

如果只想启动服务、不打开窗口：

```powershell
.\start.ps1 -NoBrowser
```

## 使用

1. 在管理页点击“添加账号”。
2. 输入本机显示名称并选择会员类型（Lite / Super / Heavy）。
3. 打开 UI 给出的 xAI 官方地址，登录并确认一次性代码。
4. 在 Agent 中填写管理页显示的 Base URL 和 API Key。

“连接详情 → Zcode / Hermes / Grok Build 配置”提供三套可复制片段。它们按各客户端的真实字段分别发送推理强度，同时 Router 保持透明转发、不按客户端改写请求：

- Zcode Desktop：`levels / defaultLevel / providerOptionsByLevel`，恢复低 / 中 / 高推理图标。
- Hermes：`api_mode: codex_responses` 与 `agent.reasoning_effort`。
- Grok Build：`api_backend = "responses"`、`supports_reasoning_effort` 与 `reasoning_effort`。

当前只为官方支持强度调节的 `grok-4.5` 声明这些能力，不会给 Composer 模型伪造推理开关。

## 账号分组

- 分组名称可自定义，例如 Zcode、Codex；每个账号只属于一个分组。
- 所有分组使用同一个 Base URL，但各自拥有独立 API Key；Key 决定请求进入哪个账号池。
- 请求只在目标组内轮换。空组或整组额度耗尽时直接报错，不会跨组借用账号。
- 组内请求串行，组间可以并行；账号移动期间仍由账号锁防止同号并发。
- 默认组保留旧版 API Key，升级后现有客户端配置继续可用。
- 管理页支持新建、重命名、删除空组，以及在组间移动账号；Provider 和客户端配置随当前组切换。

支持的透明代理路径：

- `GET /v1/models`
- `POST /v1/responses`
- `POST /v1/chat/completions`
- 其他 `/v1/*` 路径也会按原请求转发。

## 轮换规则

- 默认优先当前账号。
- 401 时先调用官方 CLI 刷新登录，再重试一次。
- 402、429 或 xAI 明确返回的 `spending-limit` 等额度耗尽错误会隔离账号并切换。
- 普通 429 进入 60 秒冷却，然后切换。
- 其他请求校验错误原样返回，不误切账号。

## 额度监控

- 软件启动后立即读取每个账号的官方 Grok Build billing 数据。
- 软件运行期间每隔 30 分钟再加 0–5 分钟随机延迟，全量串行刷新一次，不发送模型提示，不消耗推理额度。
- SSL、代理、超时或响应截断等临时错误会随机退避后重试两次；HTTP 4xx 不会盲目重试。
- 账号行显示已用百分比和官方重置时间，也可以点击“刷新额度”。
- 账号行同时显示会员类型和本地添加时间；会员类型以添加时选择的值为准。
- 达到 100% 自动标记为额度耗尽；新周期额度恢复后自动重新启用。
- 临时网络或代理错误只显示检查失败，不会误停账号。
- 软件关闭后停止轮询，但账号登录态和最后一次额度数据仍会保留。

## 数据与安全

- 默认数据目录：`%LOCALAPPDATA%\SuperGrokRouter`。
- OAuth 文件保存在每个账号自己的 `GROK_HOME`。
- 服务只允许监听 `127.0.0.1` 或 `localhost`。
- 管理接口不会返回 OAuth access token 或 refresh token。
- `/v1/*` 必须使用管理页生成的本地 API Key。

## 系统代理

每次上游请求都会读取 Windows“Internet 选项”中的当前系统代理：

- 系统代理开启：只通过该代理访问 xAI，代理失败时直接报错，不回落直连。
- 系统代理关闭：不使用代理，也不继承进程中残留的 `HTTP_PROXY`、`HTTPS_PROXY` 或 `ALL_PROXY`。

## 验证

```powershell
python -m unittest discover -s tests -v
```
