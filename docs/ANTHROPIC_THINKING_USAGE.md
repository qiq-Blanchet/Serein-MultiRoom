# Anthropic Thinking 与 Token Usage 兼容补丁

状态：已提交到 `feature/multi-room`，**尚待 CI / 真实客户端验收**。本补丁先于多房间改造实现，只影响现有 OpenAI 兼容聊天网关到 Anthropic Messages 上游的适配；不修改数据库、记忆、日记、梦境和 MCP 权限。

## 背景

用户在 VPS 上曾使用 2026-10-31 的手工补丁修复 Operit 无法显示 Claude Thinking 的问题。原版 Serein 将 Anthropic `thinking_delta` 映射为 `delta.reasoning_details`，而 Operit 使用 `delta.reasoning_content`。另一方面，原版直接透传 Anthropic 流式 `usage`，非流式也没有把缓存 token 纳入 OpenAI `prompt_tokens`，导致输入、输出、缓存统计不一致。

## 当前补丁行为

- **本 Fork 仅针对用户实际使用的 Claude Opus 4.6 及更新型号**，不提供 4.5 及更早型号的 thinking 参数兼容：Anthropic 上游未显式指定 `thinking` 时，默认发送 `{"type":"adaptive","display":"summarized"}`，让 Operit 等客户端收到可见思考摘要；不再自动发送旧式 `budget_tokens`。Adaptive thinking 对简单问题可能直接回答，不保证每一轮出现思考摘要。
- Anthropic Messages API 必须带 `max_tokens`；如果客户端省略，则 Serein 默认填 `65536`。客户端提供 `max_tokens` 或 `max_completion_tokens` 时优先使用其值，不强制限制为 65536；应遵守上游真实上限（Claude Opus 4.6+ 的同步 Messages API 为 128K）。
- Serein 将客户端自定义请求里的顶层 `output_config`（包含 `effort`）、`top_k`、`max_tokens` 等转发到 Anthropic 上游，未提供 `output_config.effort` 时保持模型供应商自己的默认 effort。
- 使用 `thinking.type=adaptive` 时移除不兼容的 `temperature` / `top_p`。客户端**显式设置** `thinking` 时按原样传递；例如显式设置 `display=omitted` 不会被网关改回 `summarized`。但 Opus 5.5 不支持旧式 `enabled + budget_tokens` 或 `disabled`，此类显式设置可能得到上游 400。
- 流式 `thinking_delta` 同时提供 `reasoning_content` 与原 `reasoning_details`；签名和加密思考材料仍留在 `reasoning_details`，不会作为可见文本输出。非流式回复同样提供 `message.reasoning_content`。
- 对**自动启用 thinking** 的请求，只有收到上游 HTTP 400 时才有限重试：先移除网关自动 thinking；如果客户端未指定输出上限，可再试默认上限降为 16384、8192、4096。**客户端明确设置的 max_tokens/max_completion_tokens 不允许被偷偷调低**。显式 thinking 请求不自动降级；其他 HTTP 状态不重试。错误信息仍遵循安全分类，不回显上游原始错误正文。
- 自动后台 Persona 任务保持不启用默认 thinking，避免改变现有该任务的非思考约定。


## Operit 自定义请求示例

通过 Serein 的 OpenAI 兼容 `POST /v1/chat/completions` 发送**顶层 JSON 字段**（具体模型名以 Serein 实际模型列表为准）：

```json
{
  "model": "provider/claude-opus-5-5",
  "stream": true,
  "stream_options": {"include_usage": true},
  "max_tokens": 120000,
  "output_config": {"effort": "high"},
  "messages": [{"role": "user", "content": "你好"}]
}
```

省略 `thinking` 时 Serein 自动加 `{"type":"adaptive","display":"summarized"}`；也可在自定义请求中明确填写该对象或覆盖 display。没有填 `max_tokens` 时网关使用默认 65536。缓存输入统计会归并到 `prompt_tokens`，同时以 `prompt_tokens_details.cached_tokens` 单独报告命中数，客户端不要再次累加缓存计数。

## Token 与缓存口径

Anthropic 官方用量的三部分输入计数彼此独立，统一映射为：

```text
prompt_tokens      = input_tokens + cache_creation_input_tokens + cache_read_input_tokens
completion_tokens  = output_tokens
total_tokens       = prompt_tokens + completion_tokens
prompt_tokens_details.cached_tokens = cache_read_input_tokens
cache_read_input_tokens             = 原生 cache_read_input_tokens
cache_creation_input_tokens         = 原生 cache_creation_input_tokens
```

缓存命中属于输入 token 的一部分，**不要在客户端再次加到 `prompt_tokens` 上**。缓存写入与命中成本可能不同；本补丁仅传递准确计数，**不计算费用**，也不根据字符数估算缺失计数。

流式 Anthropic 的 `message_start.usage` 常包含输入与缓存统计，`message_delta.usage` 常包含最终输出 token。转换器先合并两者，再发送完整 OpenAI usage：

- 客户端传 `stream_options: {include_usage: true}`：在 `[DONE]` 前发送一条 `choices: []` 的最终 usage chunk，其余 chunk 不重复放 usage。
- 客户端未传该选项：为了兼容既有 Serein 行为，将归一化 usage 放在最后的 `message_delta` 转换 chunk 中。
- 非流式：`chat.completion.usage` 直接提供归一化计数。

## 验证方式

在**独立工作目录或 CI** 运行，不连接正在使用的 VPS 数据库：

```bash
python -m pip install -e '.[test,http,background]'
python -m compileall -q src
python -m pytest -q tests/test_anthropic_thinking_usage.py tests/test_chat_image_transport.py tests/test_chat_upstream_errors.py tests/test_upstream_catalog.py
```

人工验收需要：Operit 流式 Thinking 显示，正常回复，非流式 Thinking，输入/输出/cache-read/cache-create 计数，显式关闭 thinking，400 降级，不泄露错误正文。使用**合成数据或测试密钥**，先验证后再评估更新正在运行的 VPS 服务。

## 说明与限制

Adaptive thinking（尤其 `effort=max`）可能大幅增加输出 token 费用。Opus 5.5 官方默认 `effort=medium`；不应该由网关强制设为 `max`。第三方 Anthropic 兼容代理未必支持 `display=summarized` 或 `output_config.effort`；有限 400 降级只保障尝试，**不代表最终响应一定有思考摘要**。不要将这些行为误认为已经在生产 VPS 验收；当前提交仅存在于 Fork 的开发分支。GitHub Actions 工作流位于 `.github/workflows/anthropic-compat.yml`，如果 Fork 尚未启用 Actions，需要先在 GitHub 启用后才能看到运行结果。
