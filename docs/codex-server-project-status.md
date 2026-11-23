# Codex Server 项目状态与运维交接

> 状态：2026-11-24 已完成本轮独立审查、隔离 VPS 部署与真实 App Server 验收。本轮合并目标是 `codex-server`，不包含 `main` 发布。
>
> 本文记录“已完成、已验证、未完成和运维边界”。架构与配置方法见 [Codex 登录态聊天网关与 Serein 记忆](codex-chat-runner.md)，审查项见 [Codex Runner 独立复核交接单](codex-runner-review-handoff.md)。

## 1. 目标和数据路径

第三方客户端仍然连接 Serein 的 OpenAI 兼容网关，Serein 可选择把聊天请求转给一个使用真实 Codex 登录态的内部 Runner：

```text
第三方客户端
  -> Serein HTTPS /v1/chat/completions
  -> 记忆召回、上下文注入、成功轮次归档
  -> 回环/私网 Codex Runner
  -> codex app-server（ChatGPT 账号登录态）
```

MCP 仍由 Serein 提供；客户端工具仍由客户端执行。Runner 只负责 Chat Completions 与 Codex App Server 协议转换，不替客户端运行本地命令、文件或 MCP 工具。

## 2. 已完成

### 2.1 Codex App Server 适配

- 新增可选 `protocol=codex` 上游，不改变原有 OpenAI/Anthropic 上游。
- 实现临时 thread 创建、历史注入、`turn/start`、App Server 事件流及流式/非流式输出。
- 转换 App Server 真实 usage；当上游没有报告 token 数时不伪造数值。
- 支持 `reasoning_effort`、`reasoning.effort` 和兼容的 `thinking.type`。客户端的 `none/disabled` 映射到 Codex 最低兼容档 `low`。
- 统一 Gateway 与 Runner 的长超时边界，限制等待队列，正确处理断流、取消和上游错误。
- Runner 到 App Server 的流量不使用宿主机环境代理，避免回环请求被错送到公网代理。

### 2.2 客户端工具往返

- OpenAI function tools 映射为 App Server `dynamicTools`。
- 客户端 `mcp__...` 工具名会在 App Server 边界转成请求内安全别名，返回客户端时还原；转换后的重名别名会被明确拒绝。
- Codex 工具请求转为标准 `tool_calls`，保留调用 ID、函数名和 arguments。
- 客户端执行工具后用 `role: tool` 回传，Runner 根据完整历史继续生成。
- 文本与 Base64 内联图片的工具结果可继续传给模型。
- 限制每次响应只桥接一个工具调用，拒绝跨轮错配的工具请求。

### 2.3 图片、记忆和归档

- 当 `image_eyes=false` 且 `image_transcription_async=true` 时，用户原图直接交给主聊天模型，同时独立进入后台图片转录/Event 材料链。
- 当前消息、历史消息和客户端工具结果中的 Base64 内联 `image_url` 都能保留。
- 工具续轮不重复发送原始用户图片。
- 保留 Serein 原有记忆召回、上下文注入、成功轮次的用户原话/助手正文归档和自动 Event 入口。
- 召回块、系统消息、推理与工具返回值不写成用户原话。不完整或失败轮次不伪装成已成功归档。

### 2.4 MCP OAuth 浏览器授权

- 保留 Gateway Key 静态客户端路径，同时支持 ChatGPT 的 OAuth 动态客户端注册与授权码交换。
- 修复授权页 `Content-Security-Policy: form-action 'self'` 拦截外部回调的问题。CSP 只增加已注册并精确匹配的 `redirect_uri` 来源，不放开任意跳转。
- 在注册阶段严格校验回调 host 和 port；拒绝 IPv6 zone identifier、IPvFuture 以及可构造 CSP 指令注入的地址。
- 真实 ChatGPT MCP 授权已验证 `POST /authorize -> 303 -> POST /token -> 200`。

### 2.5 隔离与安全

- Runner 使用 `approvalPolicy: never`、`readOnly` 且关闭 App Server 网络访问，并使用空的隔离工作目录。
- 本地真实登录态 smoke 脚本仅允许回环 Runner URL，不跟随重定向，不使用环境代理，避免把内部 Token 送到外部地址。
- 代码不包含 ChatGPT/Codex 登录文件、Gateway Key、Runner Token、模型 Key 或页面密码。

## 3. 已验证状态

### 3.1 离线与 CI

- OAuth、MCP、HTTP Gateway 与 Codex 兼容层专项测试：66 项通过。
- 代码 SHA `1043d5740a67684c3266f265b74262e5bb9c1101` 的 `Codex runner compatibility` 通过。
- 同一 SHA 的 `Index embedding safety` 通过，包含离线 API 回归、Python 编译、前端构建/测试和发行包校验。

### 3.2 隔离 VPS 真实验收

- 真实 Codex 登录态能列出模型并完成流式/非流式聊天。
- 真实 App Server 已通过 109 个合成 `mcp__...` 工具的非调用请求，以及原名还原、调用 ID 保留和 `role: tool` 续轮。
- 实际使用的 Orbis 客户端已完成普通聊天、思考档位、客户端工具续轮和图片输入。
- Serein 记忆召回、原话归档与后台材料链保留在 Gateway 前后。
- ChatGPT 自定义 MCP 通过浏览器 OAuth 成功连接。

这些是本次隔离环境的验收证据，不代表任意账号、任意 Codex CLI 版本或任意第三方客户端都必然兼容。

## 4. 未完成与已知限制

- **并行多工具**：未支持。每次响应桥接一个客户端工具调用，连续工具调用必须串行续轮。
- **硬性输出长度**：`max_tokens` / `max_completion_tokens` 只校验格式，App Server 没有等价的逐请求强制截断映射。
- **输入类型**：不支持旧 `functions`、音频、视频和 HTTP(S) 远程图片 URL。Runner 故意不下载远程图片，以避免 SSRF。
- **JSON 约束**：`json_schema` 会映射到 `outputSchema`；`json_object` 只能通过 developer 约束请求 JSON，不等价于严格 schema。
- **实验协议**：`dynamicTools` 与部分 App Server 事件仍是实验性接口。升级 Codex CLI 后必须重跑真实 smoke。
- **Hook 范围**：ChatGPT Work、网页和移动端不能运行本地 Codex Hook。它们能通过 MCP 主动读写 Serein，但没有本地 Hook 的自动召回与反向归档环路。
- **帐号状态**：Codex 登录、模型目录、订阅额度及到期行为由当前 Codex/ChatGPT 账号决定；Serein 不托管、迁移或保证自动刷新这些凭据。
- **客户端范围**：已真实验证 Orbis，但不声称已覆盖所有 Operit/RikkaHub 二改版本。
- **发布范围**：本轮只合入 `codex-server`，没有合入 `main` 或发布新版本。

## 5. 当前部署策略（不是仓库默认值）

当前隔离 VPS 把 Memory、Gateway、Codex Runner 拆分为独立服务；Runner 只监听回环地址，公网只提供 Serein HTTPS 网关。不应把实例 IP、域名、密钥或登录文件写入本文。

自动摘要 Agent 在该 VPS 上采用一次性 systemd 任务：

- 每日 `03:05 Asia/Shanghai` 启动，而不是全天轮询。
- 每次最多领取 4 个任务；没有足够原话或没有待处理批次时会正常结束。
- Agent 使用独立低权限用户、严格 systemd 隔离和回环网络白名单。
- 这是当前部署的运维选择，不是 Serein 仓库的默认调度方式。

## 6. 使用与升级注意事项

### 6.1 凭据不能混用

以下凭据属于不同安全边界：

1. Serein 网页用户名/密码；
2. Gateway Key；
3. 模型厂商 API Key；
4. Serein 到 Runner 的内部 Token；
5. Codex/ChatGPT 登录文件。

它们不得互相代替，不得进入仓库、CI 日志、Issue、PR 或聊天消息。Gateway Key 轮换后，静态客户端和已签发的 MCP OAuth token 都需要重新授权。

### 6.2 网络与权限

- Runner 端口只能位于回环或受保护的容器私网，不得公网暴露。
- `readOnly` 只是写入限制，不是主机文件读取隔离边界。Runner 必须使用低权限用户或容器、空工作目录和操作系统权限隔离。
- 公网入口应使用 HTTPS。Cloudflare 是否代理与 OAuth 无必然关系，但反向代理必须保留正确的外部 scheme/host。

### 6.3 会话窗口

同一客户端聊天要保持稳定的 `X-Serein-Window-ID`，新聊天应更换 ID。如果客户端不发送动态窗口 ID，多个聊天可能共用召回冷却、提醒轮次与续接状态。

### 6.4 Codex 或客户端升级

升级 Codex CLI、更换账号或更换第三方客户端后，至少重测：

1. `/v1/models`；
2. 普通与流式回复；
3. `tool_calls -> role: tool -> 最终回复`；
4. Base64 内联图片；
5. Serein 召回与成功轮次归档；
6. MCP OAuth 重新授权。

## 7. 备份与回滚

- 升级前备份记忆数据库、索引、运行配置、密钥文件和当前代码版本。备份可能包含模型凭据，不得公开分享。
- 本轮 Codex/OAuth 代码不需要数据库 schema 迁移。代码回滚与数据恢复应当分开，不要为了回滚代码而覆盖用户数据。
- OAuth CSP 修复可回滚为上一版 `oauth.py`，但回滚后 ChatGPT 浏览器授权将再次被 `form-action 'self'` 拦截。
- 若需更换 Gateway Key，应预期静态客户端与 OAuth 客户端一起重新授权，不要通过日志或文档传递新 Key。

## 8. 已知测试异常

VPS 上运行全量 `pytest -x` 时，在 `502 passed` 后停在：

```text
tests/test_event_handoff.py::
test_three_stage_image_chain_preserves_bytes_transcription_and_raw_sources[agent]
```

诊断结论是上游测试配置缺陷，不是图片丢失或生产流水线故障：

- 测试只给 `track_router/event_curator/event_writer` 分配了合成模型，没有配置 `image_transcription`。
- 实现的既定行为是：没有独立图片模型时，图片转录回退给 `track_router`。
- 测试却硬编码断言第二个任务必须名为 `image_transcription`，因此失败。
- 该测试与相关回退逻辑均来自上游 `main`，不在本次 Codex/OAuth PR 的改动范围。

实际影响是全量 pytest 不能作为全绿证据，但不代表线上图片转录失效。没有独立 `image_transcription` 模型时会按设计回退到 `track_router`；配置后才使用独立图片模型。后续最小修复应只在该测试的 assignments 中增加 `image_transcription: local`，不应为迎合测试改动生产回退逻辑。

## 9. 合并后建议

1. 先在 `codex-server` 保持一段时间的真实使用观察，再决定是否进入 `main`。
2. 单独修复或向上游反馈第 8 节的测试配置缺陷，不与 Codex 功能修复混为一个提交。
3. 记录 Codex CLI 升级日期与验收结果；实验性 App Server 协议变化时优先停止升级并回滚 Runner。
4. 如果要增加并行工具、远程图片或强制输出长度，必须分别设计协议、安全与回归测试，不要在当前适配层中默默开启。
