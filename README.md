# SuperGrok Router

一个不限账号数量的本机 SuperGrok 账号路由器。每个账号通过官方 Grok Build 设备授权登录，并按 Agent 和账号组组织成稳定的 OpenAI-compatible Provider。

## 启动

前提：Windows 已安装官方 `grok` CLI，且 `grok --version` 可运行。桌面版使用系统 WebView2 Runtime，不再启动 Chrome 或 Edge App 模式。

直接运行打包产物：

```powershell
.\dist\SuperGrokRouter.exe
```

从源码启动桌面版：

```powershell
cd C:\path\to\supergrok-router
.\start.ps1
```

应用使用固定 1280×720 原生窗口。最小化窗口会隐藏到系统托盘；双击托盘图标或点击“打开”即可恢复，点击“退出”会同时关闭窗口和后台 Provider。

桌面宿主和后端各有独立的 Windows 单例锁；重复双击会恢复现有窗口，不会再创建第二个实例或端口监听进程。

应用窗口和系统托盘使用 `static/app-icon.png` / `static/app-icon.ico` 中的 SG 图标。

## 构建桌面版

```powershell
.\build.ps1
```

构建脚本使用隔离的 `.venv-build` 环境，生成无控制台、包含静态资源和应用图标的 `dist\SuperGrokRouter.exe`。

如果只想启动服务、不打开窗口：

```powershell
.\start.ps1 -NoBrowser
```

## 使用

1. 在管理页点击“添加账号”。
2. 输入本机显示名称并选择会员类型（Lite / Super / Heavy）。
3. 打开 UI 给出的 xAI 官方地址，登录并确认一次性代码。
4. 切到对应 Agent 分页，把管理页显示的 Base URL 和该 Agent 的 API Key 填入客户端。

“连接详情 → Zcode / Hermes / Grok Build 配置”提供三套可复制片段。它们按各客户端的真实字段分别发送推理强度，同时 Router 保持透明转发、不按客户端改写请求：

- Zcode Desktop：`kind: openai-compatible` 与 `reasoning.variants / defaultVariant`，恢复当前版本识别的推理按钮。
- Hermes：`api_mode: codex_responses` 与 `agent.reasoning_effort`。
- Grok Build：`api_backend = "responses"`、`supports_reasoning_effort` 与 `reasoning_effort`。

当前只为官方支持强度调节的 `grok-4.5` 声明这些能力，不会给 Composer 模型伪造推理开关。

## Agent 与账号组

- 顶部 ZCODE、GROK BUILD、HERMES 分页分别代表独立 Agent；每个 Agent 拥有一个 API Key、自己的串行路由锁和若干账号组。
- 同一 Agent 内，启用的账号组按界面顺序自动兜底：先在当前组内选择健康账号，额度或授权不可用时才进入下一组。
- 每个组都可独立隔离或重新启用。隔离只跳过路由，账号和授权数据仍保留；上移、下移可调整兜底顺序。
- 一个账号只属于一个组，但可以移动到任意 Agent 的任意组。不同 Agent 可以并行，同一 Agent 的请求串行，流式响应期间保持账号锁。
- 升级旧数据时，原 API Key、当前账号和所有既有账号迁入 ZCODE；现有 Zcode 客户端配置继续可用。
- 管理页支持新建、重命名、删除空账号组；Provider 连接信息随 Agent 分页切换，不随组切换。

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
