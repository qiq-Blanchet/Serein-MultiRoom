# Codex Runner 独立复核交接单（部署前）

> 状态：2026-11-23 已完成本轮独立代码审查、隔离部署与真实 Codex/客户端验收。本文保留为后续升级的复核清单；当前结论与证据边界见 [Codex Server 项目状态与运维交接](codex-server-project-status.md)。
>
> 日期：2026-11-21。基线 `codex-server`；工作分支 `fix/codex-client-parity-20261121`；Draft PR [#1](https://github.com/qiq-Blanchet/Serein-MultiRoom/pull/1)。请对比 `codex-server...fix/codex-client-parity-20261121`，以实际分支最新代码为准。

## 0. Codex 审查任务

先独立审查，再决定是否修改。不要把本交接单当作代码正确性的证据。

1. 阅读本文件、`docs/codex-chat-runner.md`、PR diff 和相关测试。自行核对**当前安装的 Codex App Server 协议/生成的 schema 或官方文档**，不要假定旧示例就是现行协议。
2. 优先列出有证据的 P0 / P1 / P2 问题（文件/行号、触发条件、影响、修复建议）；优先处理会导致实际工具失效、错误归档、泄露数据、流式假成功、旧上游回归的问题。
3. 在**现有工作分支**修复确认的阻断问题并补回归测试，说明改动和验证结果。不要未经用户同意合并、发布、部署、覆盖数据库或修改生产配置。
4. 如无真实 Codex 登录态、无实际前端或无测试凭据，只执行模拟/离线测试，明确列出**未验证**项目，不要凭绿灯宣布生产可用。

## 1. 目标和非目标

目标是让 Operit、RikkaHub 等支持 OpenAI Chat Completions 的第三方客户端继续连接 Serein，而 Serein 的一个聊天上游可选用**已登录账户的 Codex App Server**。要保留：

- Serein 的记忆召回/注入、当前用户原话与最终助手回复的归档、事件/摘要处理、原有 MCP 接口与 OpenAI/Anthropic 上游；
- 客户端自有 tools/MCP 的 `tool_calls` 传回客户端执行，以及客户端回送 `role: tool` 后的完整续轮；不允许 Runner 替客户端执行本地工具；
- 主聊天模型直接看用户原图。用户实际配置是 `image_eyes=false`、`image_transcription_async=true`：异步转录仅供后台 Event/摘要，不应剥夺主模型图片或强行开启 Eyes；
- 正常流式与非流式回复、按会话窗口区分的上下文、错误处理与尽量安全的隔离。

非目标：使用 OpenAI API Key 替代 Codex 登录；自动获取或迁移 OAuth/ChatGPT 凭据；替客户端安装 MCP；在 Runner 里获得文件系统/命令执行权限；此阶段自动合并/发布/生产部署。

## 2. 实际数据路径

```
Operit / RikkaHub / 其他前端
    -> Serein /v1/chat/completions (OpenAI 兼容)
    -> Serein 记忆召回/上下文注入、窗口快照和原话归档
    -> HTTP /v1/chat/completions (protocol=codex, 内网 Bearer token)
    -> Serein Codex Runner
    -> codex app-server (stdio JSON-RPC, 账户登录态)
    -> 最终文本或客户端 function tool_calls
    -> 若调用工具，由前端执行，再以 role: tool 回传相同 call_id 继续请求
```

Runner 不应公开到公网；只暴露 Serein 公共入口。每次 Chat Completion 使用临时 Codex 线程并注入客户端历史；服务端工具执行/审批需受限。真实账号授权、模型目录和订阅额度不能通过 FakeAppServer 测试证明。

## 3. 关键改动位置

| 路径 | 关注点 |
| --- | --- |
| `src/serein/codex_runner.py` | Chat Completions -> Codex App Server 适配；stdio JSON-RPC 会话、历史注入、`dynamicTools`、客户端工具调用、图像、SSE、参数映射及隔离 |
| `src/serein/model_runtime.py` | `protocol=codex` 请求转发、Runner 的超时和标准响应传递 |
| `src/serein/api/chat.py` | Gateway SSE 超时/客户端生命周期，记忆召回、快照续轮、原话/助手结果归档；对 Eyes 与异步图片转录的原有语义保持不变 |
| `src/serein/api/host.py` | 主机完成轮次归档失败不得错误记为已交付 |
| `tests/test_codex_runner.py`, `tests/test_codex_runner_compat.py` | FakeAppServer 工具调用、图片历史、续轮、事件流、协议选项、失败处理 |
| `tests/test_chat_image_transport.py`, `tests/test_chat_eyes_fallback.py`, `tests/test_public_settings.py`, `tests/test_host_turn_archive.py` | Gateway 与旧功能回归；特别关注图片和工具往返 |
| `scripts/smoke_codex_runner.py` | 只在本机真实登录态运行的验收脚本；不得泄露 Runner Token 或接受重定向 |
| `docs/codex-chat-runner.md` | 架构、部署前置条件、局限、手工验收步骤 |
| `.github/workflows/codex-runner-compat.yml` | 兼容性检查 |
| `release-files.json` | 打包时 Runner 与文档的清单 |

## 4. 必须主动挑刺的协议风险（不要假设已解决）

**A. Codex App Server 版本/JSON-RPC**：
确认 `thread/start`、`thread/inject_items`、`turn/start`、`item/tool/call`、`item/agentMessage/delta`、`turn/completed`、token usage 事件、`dynamicTools` / `inputSchema` 的当前字段与调用时序；确认客户端工具的 tool-call 应答/中断语义、`function_call_output`（尤其多模态数组）的数据结构、权限限制、模型与推理强度选项，以及阶段标记 `final_answer`/`partial_answer` 等。实验性接口尤其需要**真实 Codex 版本**端到端验证。

**B. 客户端工具往返**：
验证 `tool_calls` 的 ID、函数名、arguments、`finish_reason=tool_calls`、SSE 分块拼装、`role:tool` 续轮；多次连续工具调用、不同窗口/轮次污染、客户端取消/重试、流未完整结束的状态。Runner 目前按**每次返回一个工具调用、串行续轮**设计，不提供并行多工具支持；需确认对 `parallel_tool_calls=true` 的语义处理是否足够明确。

**C. 图片/历史**：
用户图像用 Base64 inline `image_url`；主模型直接获取原图，异步图片转录仅供后台处理，Eyes 关闭；工具结果内联图片及工具续轮历史也应保留。检查 input_image 在历史注入和实际 turn 输入的角色和重复发送问题。HTTP(S) 远程图片目前不支持，不能偷偷由 Runner 下载任意 URL（SSRF 风险）。确认实际第三方客户端支持哪种图片格式。

**D. Chat Completions 参数差异**：
`max_tokens` / `max_completion_tokens` **当前仅校验格式，尚未强制生成长度**（已知语义缺口）；`reasoning_effort` / `reasoning.effort` / 简写 `thinking.type` 映射到 `turn/start.effort`，要按实际 App Server schema 验证；`tool_choice`、`response_format` / `outputSchema`、usage、未知字段要检查。不得默默忽略会改变行为的选项。

**E. 原有 Serein 功能与隔离**：
防止回忆块、系统注入、工具结果、推理内容误写成用户原话；工具续轮不重复触发召回或错误投递；助手只在完整成功时归档；流式断开和错误不能被记为成功。检查 Codex Runner 的只读 sandbox、单独低权限用户、敏感信息与日志、不允许额外文件/网络/命令工具。保证旧 OpenAI/Anthropic 上游继续可用。

**F. 部署与运维**：
确认 Docker/非 Docker 部署下 Runner 位置、loopback/私网、网络与认证、Serein Base URL 拼接、端口、防外网暴露、长超时、进程终止清理、并发与死锁；原有数据库/索引、备份与回滚不应被修改。不要把用户订阅凭据或 Runner Token 贴进 CI、issue、PR 或聊天。

## 5. 可执行测试（离线优先）

```sh
python -m pip install -e '.[http]' pytest
python -m py_compile src/serein/codex_runner.py src/serein/api/chat.py src/serein/api/host.py src/serein/model_runtime.py scripts/smoke_codex_runner.py
python -m pytest -q tests/test_codex_runner.py tests/test_codex_runner_compat.py tests/test_host_turn_archive.py tests/test_chat_eyes_fallback.py tests/test_chat_image_transport.py tests/test_public_settings.py
```

同时检查仓库现有的 Index embedding safety 全仓库回归工作流。此前一个已提交代码版本（`6d2e04fc76dcfd7e4bca7a1abe85801612056df2`）对应的 Codex runner compatibility 与 Index embedding safety 两个 GitHub Actions 已完成且通过；**后续代码或审查修复需重新检查最新 SHA 的 CI**。

真实登录态只在用户准备好的测试机/隔离环境进行，按 `docs/codex-chat-runner.md` 手工运行 `scripts/smoke_codex_runner.py`；不得跳过 Gateway 全链路及客户端实际 MCP 工具测试。使用无副作用的合成工具，不要接入生产敏感工具执行验证。

## 6. Codex 复核最终交付格式

请给出：
1. 一句话结论：**可进入隔离测试部署 / 仍有阻断项 / 暂不确定**，并明确证据边界。
2. 按 P0/P1/P2 排序的问题列表（文件和行号、复现/证据、影响、建议）。
3. 已修复的分支提交、补充的测试与精确测试结果；未修复问题及原因。
4. **未完成的真实验收**清单：Codex 当前版本和账号登录、真实动态工具、实际客户端图像、上下文记忆与归档、断流和安全边界。
5. 部署前必要的备份、回滚和环境参数；保留 Draft PR，未经用户同意不要合并或部署。

**重要**：即使所有离线测试通过，也不能宣称已验证真实 ChatGPT 订阅额度、真实 Codex App Server 工具协议或 Operit/RikkaHub 实际兼容性。
