# Codex 登录态聊天网关与 Serein 记忆

这条接法让第三方客户端继续使用 Serein 的 OpenAI 兼容地址，同时把其中一个聊天上游改为已登录 ChatGPT 账号的 Codex CLI。Serein 仍负责召回、当前轮原话归档、自动 Event 整理和 MCP；独立 Runner 只把 Chat Completions 请求翻译给 Codex App Server。

它不是 OpenAI API Key 的替代协议，也不把 Codex 登录凭据存入 Serein。使用资格、限额和可用模型由 Runner 所登录的 ChatGPT 账号及当时的 Codex 客户端决定。

当前已完成事项、已验证范围、未完成限制与运维注意事项见 [Codex Server 项目状态与运维交接](codex-server-project-status.md)。

交给 Codex 做独立代码审查时，先读 [复核交接单](codex-runner-review-handoff.md)。该清单明确了需要审查的 App Server 协议、前端工具往返、图片/异步转录以及上线前未验证事项。

## 拓扑与边界

```text
第三方客户端 ── HTTPS ──> Serein /v1/chat/completions
     │                         │
     │                         ├─ 召回与成功轮次归档 ──> Serein SQLite ──> 自动 Event
     │                         │
     └─ MCP ─────────────────> /serein/mcp
                               │
                               └─ 私网 HTTP ──> serein-codex-runner ── stdio ──> codex app-server
```

Runner 必须与 Serein 同机或位于受保护的私网。不要把 Runner 的 4501 端口暴露到公网；它的 Bearer Token 只是 Serein 到 Runner 的内部凭据。若以后使用 `gptmemory.你的域名`，域名、HTTPS 证书和 Cloudflare 只反代 Serein 网关，Runner 保持回环地址或容器私网地址。

每个 Chat Completions 请求创建临时 Codex 线程，并把客户端提供的历史重新注入。Runner 使用 `approvalPolicy: never`、`readOnly` 且关闭网络访问的 sandbox，并给模型加入“只作为聊天模型”的 developer 约束。当前 App Server 的 `readOnly` 协议没有“只允许读取某个根目录”的字段；它限制写入，但不是主机文件读取隔离边界。因此必须给 Runner 使用单独的低权限系统用户（或独立容器）和空的隔离工作目录，并通过操作系统权限确保该身份无法读取源码、密钥或私人文件。developer 约束只用于引导模型，不能替代权限隔离。

## 安装 Runner

在实际运行 Runner 的系统用户下安装并登录 Codex。登录步骤由 Codex 自己完成；不要把登录令牌、Cookie 或本地认证文件复制到 Serein 配置、聊天消息或仓库。

在 Serein 仓库发行目录安装 HTTP 依赖：

```sh
python -m pip install '.[http]'
```

创建一个空的隔离目录，然后只在 Runner 进程环境中配置：

```sh
export SEREIN_CODEX_WORKDIR=/var/lib/serein-codex/empty-workdir
export SEREIN_CODEX_RUNNER_TOKEN=GENERATE_A_LONG_RANDOM_VALUE
export SEREIN_CODEX_BIND=127.0.0.1
export SEREIN_CODEX_PORT=4501
serein-codex-runner
```

Windows PowerShell 使用同名环境变量，然后运行 `serein-codex-runner`。若 `codex` 不在 PATH，可设置 `SEREIN_CODEX_EXECUTABLE` 为可执行文件的绝对路径。`SEREIN_CODEX_TIMEOUT` 可在 30–1800 秒内调整，默认 240 秒。

`GET /health` 不需要凭据，只表示 Runner HTTP 进程活着。`GET /v1/models` 与 `/v1/chat/completions` 都要求内部 Runner Token；模型列表还会实际访问 Codex App Server，可用于检查登录态。

## 在 Serein 中添加 Codex 上游

进入“设置 → 模型”，添加上游并选择 **Codex App Server Runner**：

- Runner Base URL：同机进程填 `http://127.0.0.1:4501/v1`；分容器部署改用只在内部网络可达的服务名。
- Runner Token：填 `SEREIN_CODEX_RUNNER_TOKEN` 的值。
- 点击“拉取模型”，选择当前登录账号实际返回的模型并保存。

Codex 上游不使用 Serein 的 prompt cache 选项；线程与账户侧缓存由 Codex App Server 管理。其他 OpenAI／Anthropic 上游可同时保留，客户端会继续看到“上游名/模型名”，随时可以切回 API 上游。

## 主模型看图与异步转录

当 Serein 开启 **异步图片转录**（`image_transcription_async=true`），且未开启 Eyes（`image_eyes=false`）时，客户端提供的原图保留在主模型请求里，交给 Codex 原生视觉处理；Serein 同时把用户原话和图片引用归档并排入后台转录队列，供后续 Event／摘要使用。异步转录不是主模型的图像输入替身，转录失败也不应伪装成主模型看图失败。

这与 Eyes 同步转录不同：Eyes 是另一种模式，适用于希望让 Serein 先转录、再给主模型提供文本材料的配置。两种模式互斥；不要为了使用 Codex 原生视觉而开启 Eyes。Runner 目前只支持 Base64 内联 `image_url`，不支持直接获取远程图片 URL。

## 第三方客户端与 MCP

第三方客户端仍填写 Serein，而不是 Runner：

- Base URL：`https://gptmemory.你的域名/v1`
- API Key：Serein Gateway Key，不加 `Bearer` 前缀
- Window Header：`X-Serein-Window-ID`，同一会话固定，新会话更换
- MCP：`https://gptmemory.你的域名/serein/mcp`

客户端发送的 OpenAI function tools 会映射为 Codex App Server 的实验性 `dynamicTools`。App Server 保留了 `mcp__` 工具名前缀，因此 Runner 会在单次请求内把这类客户端名称确定性地转为安全别名，并在返回 `tool_calls` 前还原原名；调用 ID、arguments 和后续 `role: tool` 历史不变，转换后别名若重复则拒绝请求而不静默覆盖。Codex 要调用工具时，Runner 返回标准 Chat Completions `tool_calls`；**客户端原有的 MCP／本地工具仍由客户端执行**，客户端再把 `role: tool` 结果续发，Runner 按原有对话历史继续生成。这里不需要把客户端的工具安装进 Codex 工作目录，也不允许 Codex 替客户端执行文件、命令或其他本地操作。

当前每次模型响应只桥接一个工具调用，并要求 Codex 串行请求客户端工具；工具结果中的文字及 Base64 内联图片会继续传给模型。支持文字与 OpenAI Chat Completions 的 Base64 内联 `image_url`（当前消息、历史消息和客户端工具结果）；为避免 Runner 代客户端访问内部网络，暂不支持 HTTP(S) 图片 URL，也不支持音频／视频、多工具并行、旧 `functions` 字段以及所有的自定义响应选项。`max_tokens` 与 `max_completion_tokens` 当前仅校验格式，App Server 没有等价的逐请求输出长度限制映射，不能依赖它们强制截断生成。对不支持且会改变行为的选项明确返回 400，不默默吞掉参数。支持 `tool_choice=auto/none`；`response_format=json_schema` 严格映射到 App Server 的 `outputSchema`，`json_object` 则使用只输出 JSON 的 developer 约束并由调用方继续解析校验，因为 App Server 的严格 schema 不能表达任意 JSON 对象。Runner 还会将 `reasoning_effort`、`reasoning.effort` 或 `thinking.type` 的合法思考档位映射到当前 Codex turn 的 `effort`。Codex 模型不能真正关闭推理，因此第三方客户端的 `none` / `disabled` 会映射到最低兼容档 `low`；其余模型可用档位不同，以 `/v1/models` 及 Codex 实际模型目录为准，不支持的预算型 `thinking` 参数会明确报错。`stream_options.include_usage` 仅在 App Server 实际报告 token 数量时返回用量。带图片的真实模型验收仍是上线前置条件；如果前端只发远程图片 URL，应先在客户端转为内联图片，或者临时切回已支持远程图片的上游。

动态工具是 Codex App Server 实验接口。升级 Codex CLI 后应先用无副作用的合成工具测试普通回复、流式回复、工具调用和工具结果续轮，再更新生产 Runner。

Serein 网关在完整成功后归档当前用户原话和最终助手正文，召回材料、系统消息、推理、工具内容不作为原话。归档消息自动成为现有原话流水线的待处理材料；是否立即生成 Event 仍由“自动摘要”开关、批次门槛和已配置的 Agent／模型决定。MCP 路由和工具定义没有因 Codex 上游而改变。

## 本地 Codex 自动召回插件（附带）

[`examples/serein-codex-hook`](../examples/serein-codex-hook/) 是一个手动安装的本地插件。安装后先审核并信任 hooks，在某个本地 Codex 会话发送 `serein:on`，该会话后续会自动召回；每次完整回复后，原始用户消息和最终助手消息通过 `/v1/host/turns` 反向归档，并进入同一自动 Event 流水线。`serein:status` 查看状态，`serein:off` 关闭。

插件把启用状态、待重试轮次和最近五轮交付 ID 放在 `PLUGIN_DATA`，不保存 Gateway Key。中断轮次不会归档；服务暂时不可用时保留已完成轮次，下一条提示前重试。

脚本 Hook 只在本地 Codex CLI／桌面 Codex 执行。云端编排的 ChatGPT Work、普通网页和移动端不会运行这类本地脚本；这些界面仍可通过 MCP 主动读写 Serein，但当前不能使用此自动召回与反向归档环路。

## 真实登录态验收（只在 Runner 所在电脑／VPS 执行）

单元测试的模拟响应无法证明 ChatGPT 订阅登录态与模型权限。请在已经通过 `codex login` 登录、已启动 `serein-codex-runner` 的机器上运行下面的只读验收。**不要把 ChatGPT 的登录文件、OAuth 令牌或 Runner Token 粘贴到聊天／GitHub Issue／Actions 日志中。** 脚本只通过进程环境读取 Runner 内部 Token，不会执行客户端的真实工具。

Linux/macOS（沿用启动 Runner 时设定的 `SEREIN_CODEX_RUNNER_TOKEN`）：

```sh
python scripts/smoke_codex_runner.py --model <实际可用的模型ID>
# 可选：验证 Base64 内联图片
python scripts/smoke_codex_runner.py --model <实际可用的模型ID> --image /path/to/test.png
```

Windows PowerShell：

```powershell
py -3 scripts/smoke_codex_runner.py --model <实际可用的模型ID>
# 可选：附加图片文件
py -3 scripts/smoke_codex_runner.py --model <实际可用的模型ID> --image "C:\\path\\to\\test.png"
```

脚本依次测试：模型列表、真实订阅推理、流式响应、客户端自定义 `lookup` 工具调用与 `role: tool` 续轮，以及可选图片识别。它仅连接 `http://127.0.0.1:4501/v1` 等本机地址，绝不会把 Runner Token 发往外部网址。图片请求只验证服务端是否完成回答，回答是否准确仍需人工判断。

上述脚本直接测试 Runner；Serein 的记忆注入、工具回复重放、原话归档与自动 Event 还必须用完整的 `客户端 → Serein Gateway → Runner` 路径再次验证。此分支不能在没有真实登录态时声称这些端到端验收已通过。

## 验收顺序

1. Runner 的 `/health` 正常，带内部 Token 的 `/v1/models` 能列出模型。
2. Serein 模型页能拉取并保存 Codex 模型。
3. 第三方客户端用独立 `X-Serein-Window-ID` 完成普通和流式回复。
4. 用无副作用的客户端 MCP 查询验证 `tool_calls → role: tool → 最终回答` 续轮，确认调用仍发生在客户端。
5. 保持 Serein Eyes 关闭、异步图片转录开启，使用 Base64 内联 PNG/JPEG 图片验证主模型原生视觉；同时确认原话档案保留图片引用，转录任务进入后台队列。
6. 检查 Serein 原话中只有当前用户与最终助手正文，没有召回块和工具返回值。随后开启自动摘要的测试实例，确认原话进入待整理批次；再按现有流程检查 Agent／Writer 结果。

域名和证书不是 Runner 代码的前置条件。先用回环／私网跑通上述六项，再把 Serein 的公开入口切到 `gptmemory.你的域名`，可以把网络变量与应用问题分开验证。
