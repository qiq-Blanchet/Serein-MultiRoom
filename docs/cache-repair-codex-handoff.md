# cache-repair：Codex 审查、验证与迁移交接

更新日期：2026-11-26。本文是原 cache-repair 工作交接，不是测试通过或生产部署证明。

**续轮修复分支请先阅读 [cache-repair-followup 最新交接](cache-repair-followup-codex-handoff.md)。** 该文替换本篇旧的候选前缀门槛和目标分支描述；本篇完整备份 / 低内存构建 / 迁移回退指南仍适用。工作分支为 cache-repair-followup（等待 Codex 复核），不直接部署或合并。

## 0. 目标与执行边界

- 原作者：`Yinglianchun/Serein`，基线提交 `43dfa5b7efb78b59e69a40c2df2cb3139197d8d2`。
- 自有 Fork：`qiq-Blanchet/Serein-MultiRoom`；原定制分支 `cache-repair`，当前独立续轮修复与复核分支 `cache-repair-followup`。最终部署分支由用户确认。
- 本文编写前的实现 HEAD：`8b0a101301e3f6254add051abbb6db932940bc5c`（相对基线 8 个提交）。审查时重新核对 HEAD，不假定该 SHA 永远是最新。
- 只在当前复核分支继续修复，不直接改原 cache-repair、main、其他实验分支或原作者仓库，不未经同意合并/部署。
- 用户主要使用 Orbis / Operit 经 Serein 的 OpenAI Chat Completions 入口访问 Anthropic Messages 上游，使用 Claude 4.6 及之后的模型，经常用 high / xhigh；不要求维护 4.6 之前的 Thinking 特殊兼容。
- VPS 现有安装据用户描述为 `/opt/Serein`，运行原作者版本并含小玑在 VPS 上手动打的旧 Thinking 补丁。实际安装目录、版本、部署方式、挂载和补丁尚未现场核对。
- VPS 可用资源：用户报告内存仅 1 GB 多，硬盘剩余 20 GB 多。不要把该报告当成实时检测；生产构建必须分步、限并发，先检查磁盘、内存和 swap。
- **缓存/Thinking/用量/更新器的全部回归和前端构建仍需核实，真实 API A/B 未测；OAuth CSP 补丁已在本地空库执行接口及 Chromium 回归。未修改生产服务器、数据库或客户端。先审查并测试，再提出迁移方案。**
- 遵守根目录 AGENTS.md：开发审查只用空库和合成 fixture；不自行探测/连接私人部署。生产迁移、停止服务、调用付费 API、恢复数据须由用户明确授权并在已知的 VPS 上执行。避免将真实聊天、记忆、密钥、签名及原始上游请求写入测试、Git 或公开日志。

## 1. 已实现的变更

### 1.1 Anthropic 缓存布局

问题：Orbis 新版 Chat Completions 序列化将 request-only 宿主数据拆为带 `name: orbis_host_data_v1` 的 system，紧邻相应 user 前；独立的「最新用户消息前（系统级）」世界书（BEFORE_LATEST_USER_SYSTEM）也是 system，但客户端内部 `source: orbis_prompt_injection` / `diagnostic_sources` 不作为常规消息正文元数据发送。Serein 原转换器把所有 system 拉到顶层 system，造成不断变化的时间戳和世界书破坏历史前缀。

实现位置：

- `src/serein/chat_context.py`：`_anthropic_context_layout` 在 `_anthropic_payload_for_upstream` 之前的消息转换过程中处理副本；`_find_cache_breakpoint`、`_apply_explicit_anthropic_cache_control` 支持尾部预留配置。
- `src/serein/api/settings.py`：ModelConnection 新增 `anthropic_system_layout` 和 `anthropic_cache_tail_tokens`；非 Anthropic 连接不接受非默认值。
- `src/serein/deployment.py`：旧模型连接分组时区分这两个选项，避免合并不同策略。
- `web/src/components/UpstreamSettings.jsx`、`web/src/storage/settingsError.js`：网页选项、协议切换重置、字段错误名称。

三个模式（默认不改变既有实例）：

| 值 | 实现行为 |
| --- | --- |
| legacy（默认） | 沿用原版，system 合并至顶层 |
| host-data | 识别紧邻 user 前的具名 Orbis 宿主数据，合并至对应 user 的上下文 |
| late-system（本用户目标） | 上述处理 + 已出现对话后、紧邻 user 前的普通 system，按原有顺序放到 user 上下文 |

转换用 `<serein_client_host_data>` / `<serein_late_system_context>` 包装；历史时间戳应与其原始 user 对应；保留顶部真正的 system，不改 OpenAI 透传和输入原件。普通中途 system 转为 user 时指令权限降低，这是明确的 opt-in 取舍；不会凭空获得 Anthropic 原生 system 权限。已合并在顶部 system 字符串内的世界书无法可靠分离。

明确断点仍按原有估算和 assistant 候选选择；续轮修复分支将候选前缀估算门槛简化为统一 4096，不再按型号区分，并保留 system/tools 的前缀估算。默认 `anthropic_cache_tail_tokens=4000`，允许 0–100000 整数。**用户要求保持 4000，不自行改为 1000。** 4000 是候选断点尾部最低预留估算，不是世界书/输入长度上限，不截断世界书。当前没有重写整个缓存算法。

### 1.2 Thinking 透传与请求默认值

旧 VPS 补丁背景（2026-10-31 重打）：固定 extended thinking budget、缺省 max_tokens=16384、流/非流补 reasoning_content、HTTP 400 时关闭 thinking 或降低 max_tokens 重试；旧补丁据用户提供记录备份在 `/srv/backups/serein-pre-thinking-patch-20261031/`，尚未核实。

新实现：

- `src/serein/model_runtime.py`：`anthropic_chat_defaults` / `_claude_thinking_family` / `ClaudeThinkingOptionsError`，`request_for` 透传原生 output_config，`AnthropicStream.convert` 将供应商返回的可见 thinking 文本同步放入 reasoning_content 并保留 reasoning_details 与签名/加密块。
- `src/serein/chat_context.py`：非流式输出增加 reasoning_content，与 reasoning_details 共存；流式汇总优先 reasoning_content，避免同一个 delta 的 reasoning 别名被重复累加。
- `src/serein/api/chat.py`：仅聊天入口、在召回/原话归档准备之后应用默认值，流和非流统一；明确不支持的强度返回 400。Writer、Persona 等后台任务不强行套用高思考默认值。

现代 Claude 默认请求（未显式配置时）：

```json
{"max_tokens":64000,"thinking":{"type":"adaptive","display":"summarized"},"output_config":{"effort":"high"}}
```

- 识别 Claude 4.6+、编号 5+（包括 5.5 和未来编号，前提是协议仍兼容）；4.6 以前保持原传输，不补旧版 extended 逻辑。无版本的别名不自动猜测。
- 输出长度显式优先：max_tokens > max_completion_tokens > max_output_tokens > 64000。64000 为现代 Claude 聊天的默认输出上限，并不保证消耗或供应商支持这么多。
- 原生 thinking / output_config.effort 优先；通用 reasoning_effort、reasoning.effort 等翻译为原生字段；只标准化同义拼写。4.6 的 xhigh 明确拒绝（不映射 max）。新版 xhigh 原样发送；其他未知强度让真实供应商校验，不静默降档。
- 默认请求 summarized，但用户显式 omitted 保留。可见摘要由上游生成，无法保证原始内部思维、文风或每轮非空。中转不返回可见摘要时，网关不能伪造。
- active thinking 时去除不兼容采样字段。目前没有小玑旧补丁的自动关闭 thinking / 8192 / 4096 降级重试，用户要求报错后自己调整。

### 1.3 自定义更新源

- `scripts/upstream_update.py`：新增 source 校验、配置、读取；默认仍为原作者 main。自定义公开 GitHub owner/repo 与 branch 持久化在 `deploy/installation.json` 的 update_source 字段。只接受 GitHub 仓库名与合法分支，不允许任意 URL、路径或注入命令。设置时查 ref、显示 commit 并确认，不停服。
- `scripts/manage.py`：新增菜单 13，菜单 1 按选中来源更新。仍先拉取固定 commit、校验 manifest/文件、保护本地修改、保存源码备份、停服并备份数据、替换源码、依次构建 memory 与 gateway、启动并检查。首次切换前的手改文件不可粗暴绕过 source_hashes 检查。
- `deploy/compose.yaml`、`deploy/Gateway.Dockerfile`、`web/src/components/VersionUpdate.jsx`：将构建时 repo/branch 传给网页版本检查，Docker 与 Python/Node 直跑共用构建信息。网页显示的是构建时来源，设置改完要等下次构建才更新网页。
- `docs/deployment-guide.md`：说明菜单 13 和更新来源。
- 只支持公开 GitHub 分支，不承诺私有仓库认证；任意第三方源码具有执行权限，必须用户信任并确认。切到不含该特性的原作者分支可能使菜单 13 消失。

### 1.4 MCP OAuth 浏览器授权回调 CSP 修复

- 用户截图：输入 Gateway Key 并确认后，`POST /authorize` 返回 303，但 Chromium 控制台报 `form-action 'self'`，阻止跨 origin 的 OAuth 回调。原限制由 `src/serein/api/oauth.py` 的 GET 授权页本身设置；`web/server/gateway.mjs` 原样转发该头。
- `src/serein/api/oauth.py`：仅在授权页面把已通过动态注册、完整 `redirect_uri` 精确匹配及 HTTPS/loopback 验证的当前回调 origin 加入 `form-action 'self'`（仅 scheme/host/port，不加入其他已注册地址、不开放 `*` 或整个 `https:`）。拒绝非法 host、通配符、CSP 分隔符、反斜杠、空白/控制字符和非法端口；国际域名使用 ASCII Punycode URI。POST 仍提交到本站 `/authorize`，重新验证完整回调、Gateway Key、PKCE、resource 等，正常成功仍返回 303，仅将 code/state 附加至原回调；不把 Gateway Key 发给外部客户端。
- Chromium 不接受 IPv6 字面地址的 CSP host-source；对已合法注册的 IPv6 回调保持授权页 `form-action 'self'`，成功 POST 后返回无脚本的 200 完成页自动 GET 跳转，附手动继续链接。回调不匹配或 Key 错误时仍拒绝，不产生成功页面。其他安全策略（default-src/base-uri/frame-ancestors、no-store、no-referrer、nosniff）、token 轮换、静态 Bearer、数据库 schema 不变。
- `tests/test_mcp_oauth.py`：新增实际响应头和非法 URI 回归，并提供可选真实 Chromium 测试，使用空库、合成 Key、两个不同 origin 的 loopback 端口（HTTP IPv4/IPv6、HTTPS 本地自签证书），检验真实提交、回调 GET、不泄露 Key、state 和 PKCE code 换 token。普通 pytest 不依赖浏览器；需要时另行安装 Playwright 并指定 Chromium 路径（见 2.4）。
- 2026-11-26 本地使用 Chromium 158 实测：旧 HEAD `c11e7b9` 的 HTTPS 回调复现原截图 `form-action 'self'` 浏览器拦截；新实现 `SEREIN_TEST_CHROMIUM=... python -m pytest -q tests/test_mcp_oauth.py tests/test_http_mcp.py tests/test_gateway_contract.py` 为 **44 passed**（含三个真实浏览器用例）。这不是实际生产 OAuth 已通过的证明。
- 生产反代若另外设置 CSP（含通过多个 CSP 头共同生效），仍可能拒绝跳转。升级后对照浏览器 GET `/authorize` 最终响应头及上游直连/反代响应，确认没有额外冲突的 `form-action 'self'`；只对授权路由消除重复/冲突策略，不删除全站 CSP、不禁用浏览器安全机制、不放宽 HTTPS 或回调精确匹配。用户实际 HTTPS 部署与客户端还需要授权后实测。

## 2. Codex 必做审查与验证

先读取 AGENTS.md 和本分支 diff，确认这次只改上述范围。对照当前 Anthropic 官方 Messages / adaptive thinking / effort / prompt caching 文档及实际供应商模型能力，尤其 4.6/5.5、display、xhigh、max_tokens、工具调用与 thinking 签名。不要把模型名推断当成能力保证。

### 2.1 缓存与上下文

- 核对客户端实际 wire 格式（仅合成/脱敏样例）：leading system + 具名 host system + 历史 user/assistant + 独立 late worldbook system + 最新 user。确认 `name` 到适配层前不被吞；不要仅凭 name 认定可信身份。
- 三模式、默认兼容、时间戳与对应 user 的稳定顺序、连续 system、空内容、第一条用户、系统末尾无 user、system 夹在工具序列、图片及 text blocks、多模态输入、合成 user 等；不得插坏 tool_use/tool_result、丢内容或污染原话/召回/Writer。核查当前代码会把具名宿主数据整体搬到 user（包括可能混合的动态字段），而非只抽取时间戳；确认这是预期或收窄分类。
- 核查 Serein 自己的稳定/动态记忆、时钟和 tool continuation snapshot/replay，不应重复注入或改变旧时间。
- 核查动态世界书从第二轮到第三轮可能消失/变化，曾带世界书的上一 user 前缀不一定还能命中；不能声称整段历史永远稳定。确认 4000 对大量动态内容的效果，检查断点位置、20-block lookback、最小 token 阈值、最多 4 个断点、TTL、缓存块选择（尤其 thinking/tool_use），并验证同一工具续轮。
- 配置保存/读取/旧分组/模板导入/切换协议/网页持久化，以及 OpenAI 透传行为不变。
- 验证 usage 统计：Anthropic input_tokens 通常只包含未缓存输入，真实输入总量 = input_tokens + cache_creation_input_tokens + cache_read_input_tokens；实际命中率 = cache_read_input_tokens / 输入总量。后续补丁已在 model_runtime.py 增加 normalize_anthropic_usage：流式按累计字段合并 message_start/message_delta，非流式共用转换，prompt_tokens 为三类输入之和，prompt_tokens_details.cached_tokens 为缓存读取量；原始缓存写入字段保留。缺失缓存读数不可伪造为 0。若上游报告 output_tokens_details.thinking_tokens，则映射 completion_tokens_details.reasoning_tokens（不重复计入输出总量）。请核对 Orbis/Operit 实际显示、工具多轮统计、原有断言及第三方上游字段差异，不把 UI 显示当作真实命中。

### 2.2 Thinking 与 API

- 4.6 默认 adaptive + summarized + high；xhigh 结构化 HTTP 400；5.5 和现代模型 xhigh/high/max 原样；前 4.6、未知模型别名、非 Claude Anthropic 协议和 OpenAI 保持原行为。默认 64000 只在未传长度时应用。
- 测试显式 native thinking、output_config.effort、display omitted、通用 off/auto、冲突参数、null/无效 max_tokens，以及上游 400/422/429 不被静默降级；尤其核查当前代码对 disabled + 显式 effort、native budget 与 generic effort 冲突的优先级是否真正符合用户意图。无效长度不应被转换成出乎意料的默认值；如需修复应清楚报错。
- 模拟 SSE start/thinking_delta/signature_delta/redacted_thinking/tool_use/tool_result/message_stop，验证可见摘要不丢、不重叠、不泄露加密块，reasoning_details 索引/签名不被改写，工具续轮能原样恢复 provider 原生块；不能仅凭 reasoning_content 重造签名。
- 非流式与流式、后台任务、Orbis 与 Operit 显示、真实供应商是否过滤 thinking。仅在用户同意后用少量无隐私测试请求，不能用真实记忆测试供应商。
- 保留用户选择 high/xhigh、64000 默认与 4000 缓存尾部。不要自动回退关闭思考或减少输出限制。

### 2.3 更新器、前端和安全边界

- 默认 upstream 与自定义 repo/branch、分支带斜杠、取消选择、不存在/非法 ref、缺 Git、断网、配置损坏、无版本记录、相同 SHA 但来源切换、拉取固定 SHA 时分支前进、文件清单校验、源码手改检测、源/数据备份失败、构建或健康检查失败及重试。
- 验证 update-state.json 的实际提交/仓库/分支、installation.json 中原 compose_project 等字段、密钥/数据库保持不变；确认切换来源后网页版本检查与下次构建一致。
- 重点检查低内存构建路径仍是 `compose('build','memory')` 完成才 `compose('build','gateway')`，没有并行构建、隐式全量 --build 或不必要重建。
- 原作者发布清单机制必须继续保护 runtime、secrets、配置和 .git；此次新增测试和本文也列入 release-files.json。确认旧版升级引导和所有原有测试没有因新字段/菜单改变而回归。
- MCP OAuth：核验授权页只允许当前验证过的回调 origin，其他安全头不变；检查不匹配的回调、错误 Key、PKCE、state、原回调查询参数、非法 host/端口/策略注入、IPv6 完成页、静态 Bearer。浏览器应完成回调并换取 token，而不只是 HTTP 303；检查真实代理是否追加第二份 CSP。

### 2.4 测试命令（开发机器/独立工作树，不在生产小内存 VPS 跑全套）

先建独立 Python 3.11+ 虚拟环境，按项目 constraints-runtime.txt 安装依赖，示例：

```sh
python -m pip install -c constraints-runtime.txt -e '.[test,http,embedding,mcp,background]'
python -m pytest -q tests/test_anthropic_cache_layout.py tests/test_anthropic_thinking_compat.py tests/test_anthropic_usage_compat.py
python -m pytest -q tests/test_mcp_oauth.py tests/test_http_mcp.py tests/test_gateway_contract.py
python -m pytest -q tests/test_upstream_update.py tests/test_installer_menu.py tests/test_installer_doctor.py tests/test_upstream_catalog.py tests/test_public_settings.py
python -m pytest -q
```

OAuth 浏览器回归（仅独立开发机，需可用的 Chromium，不在低内存 VPS 安装浏览器）：

```sh
python -m pip install playwright
SEREIN_TEST_CHROMIUM=/实际/chromium/路径 python -m pytest -q tests/test_mcp_oauth.py
```

前端 Node 22.12+：

```sh
cd web
npm ci
npm test
npm run build
```

按需补查其他测试文件、静态检查与实际 API mock；记录命令、通过/失败数和失败原因。若环境限制不能运行，报告缺口而不是声称通过。特别核对已有测试中对无 thinking、默认 max_tokens=1024、原始安装元数据和网页版本链接的断言。审查发现问题后，提交小范围修复并复测，再给用户部署建议。

### 2.5 本地全量回归记录（2026-11-26）

在 OAuth 修复过程中（IPv4 CSP 修复已生效、最终 IPv6/HTTPS 浏览器用例尚未齐备）执行 `python -m pytest -q`，依赖按 constraints-runtime.txt 安装：**2573 passed、13 skipped、24 failed**。随后最终版含浏览器的 focused suite **44 passed**。这 24 个失败用例在修复前的独立工作树 `c11e7b9836364d294ad47aa34a87edc4e291b925` 逐一对照复跑，也全部失败。它们在本次 OAuth 修改前就存在（不等于都是原作者缺陷；例如其中有本地 SOCKS 代理缺少 socksio 的环境问题）。尚未修复或定位每一项，不要据此宣布整仓库通过、不要直接迁移。请 Codex 清理测试环境后定位剩余失败：

- `tests/test_event_handoff.py::test_three_stage_image_chain_preserves_bytes_transcription_and_raw_sources[agent]`
- `tests/test_event_handoff.py::test_three_stage_image_chain_preserves_bytes_transcription_and_raw_sources[api]`
- `tests/test_file_imports.py::test_sillytavern_preview_batch_resume_and_explicit_summary`
- `tests/test_file_imports.py::test_import_history_uses_compact_boundary_without_consuming_live_chat`
- `tests/test_file_imports.py::test_completed_import_can_be_released_into_event_pipeline`
- `tests/test_file_imports.py::test_import_summary_choice_runs_without_automatic_switch`
- `tests/test_file_imports.py::test_skip_only_affects_selected_import_and_not_live_originals`
- `tests/test_legacy_migration_e2e.py::test_full_synthetic_conversion`
- `tests/test_live_jobs.py::test_promoted_event_leaves_revision_inbox_and_future_scout_materials`
- `tests/test_live_publication.py::test_upgrade_chat_uses_prepared_routes_without_publishing_or_reembedding[False]`
- `tests/test_live_publication.py::test_upgrade_chat_uses_prepared_routes_without_publishing_or_reembedding[True]`
- `tests/test_narrative_theme.py::test_discover_mixed_sources_excludes_locked_and_does_not_write`
- `tests/test_narrative_theme.py::test_discovery_rejects_invented_material`
- `tests/test_narrative_theme.py::test_discovery_caps_material_input_and_selection`
- `tests/test_personal_recall.py::test_gateway_and_hook_resolve_the_same_speaker_and_deliver_body[user_birthday]`
- `tests/test_personal_recall.py::test_gateway_and_hook_resolve_the_same_speaker_and_deliver_body[assistant_birthday]`
- `tests/test_revision_candidates.py::test_promoted_live_event_keeps_originals_and_retires_hint_without_losing_draft`
- `tests/test_transcription_first.py::test_saved_transcript_survives_expired_external_original[False-False]`
- `tests/test_transcription_first.py::test_saved_transcript_survives_expired_external_original[False-True]`
- `tests/test_transcription_first.py::test_saved_transcript_survives_expired_external_original[True-False]`
- `tests/test_transcription_first.py::test_saved_transcript_survives_expired_external_original[True-True]`
- `tests/test_upstream_catalog.py::test_index_status_distinguishes_missing_selection_and_provider_response[403-index_configuration_or_contract_error]`
- `tests/test_upstream_catalog.py::test_index_status_distinguishes_missing_selection_and_provider_response[200-embedding_invalid_response]`
- `tests/test_write_context.py::test_prepared_vectors_can_find_nonlexical_front_context`

## 3. 生产迁移：先完整备份，后换源码

**迁移是同一 Serein 实例的程序切换，不是从 Ombre 导入，不是重新初始化。不需要重导记忆、重新打标、重建向量、重新设置密钥或改域名。当前 VPS 未检查，以下为待核实的操作顺序，不能整段无脑执行。**

### 3.1 只读盘点（经用户授权，在指定服务器）

```sh
cd /opt/Serein
pwd
git remote -v
git status --short
git rev-parse HEAD
df -h /opt /srv
du -sh deploy/runtime deploy
free -h
swapon --show
docker compose version
docker ps --format '{{.Names}} {{.Image}} {{.Status}}'
```

核对实际部署 backend、Compose 项目名（installation.json 中 compose_project，旧默认可能 serein-public）、容器挂载真实源、端口、反向代理/HTTPS/Tunnel 配置及数据库实际路径。只输出必要非敏感字段，不 cat .env、secrets、全量数据库/配置给聊天。若是直跑，使用对应 service_action/local_runtime，不假定 Docker。检查旧补丁、原始备份位置、update-state.json 与源码哈希，并比较用户之前其他自定义分支/实际 VPS 本地补丁；不能假定本分支包含所有旧定制。确认当前 VPS 版本不比目标分支更新且数据库 schema 可升级；不支持的降级停止操作。

### 3.2 完整、可恢复的备份（必须做）

先确认磁盘实际剩余足够容纳完整副本、旧/新镜像及构建层，并保留安全余量。准备并验证新分支代码、测试结果、目标 commit 后再安排短暂停机；暂停 Orbis/Operit 等写入入口和后台任务。使用原安装的 `se → 4` 停止所有服务，或经核对后的项目专用停止命令。确认 memory 与 gateway 均已停，且无其他进程写 SQLite，再备份。

至少保留以下完整数据，含所有隐藏文件：

| 路径 | 说明 |
| --- | --- |
| deploy/runtime/ | 主库 serein.db、SQLite 相关文件、图片、派生索引、原话、迁移/任务状态等，整个目录保留 |
| deploy/secrets/ | Gateway Key、网页登录认证、其他实例密钥 |
| deploy/config.toml、deploy/.env、deploy/installation.json | 基础配置、端口、原项目名/部署方式、自定义环境 |
| deploy/connection-guide.txt、deploy/update-state.json | 原连接说明与实际更新记录 |
| 原完整源码树及 .git（如果有）、手工补丁 | 回退必须能恢复与数据库匹配的旧程序 |
| 额外配置/挂载及外部反代、systemd/定时任务、Tunnel 配置（如实际存在） | 核对路径后单独备份，不能只备份 repo |

建议在安装目录外使用权限 0700 的备份目录（例如 /srv/backups 内的日期命名目录，先确认磁盘与权限），保存停止写入后的完整旧实例归档或完整目录副本，外部数据另备；备份文件权限 0600，保存 SHA256 校验、目录清单、原版本/镜像 ID。检查归档可读且上述文件都存在，SQLite 在安全副本上执行 PRAGMA quick_check（必要时 integrity_check），最好另存可信离机副本。不要把包含密钥的归档上传 GitHub 或第三方。仅网页下载的 SQLite 备份不算完整备份；运行中直接 cp SQLite 主库也不算一致性备份。

如果保留整个旧目录作回退副本也应有独立备份，禁止把备份放在即将重命名/替换的目录中。备份有任何错误、空间不足或验证不通过立即停止迁移。

### 3.3 切换到自有分支（用户确认后）

- 在同一服务器、独立 staging 目录先取得 qiq-Blanchet/Serein-MultiRoom 的已审查、经用户确认的部署分支固定 commit。核对来源、提交和清单，不把生产数据提交 Git。避免用 git reset --hard、git clean -fdx 或覆盖整个运行目录来消除手改保护。
- 推荐保留原完整安装目录为回退副本，按实际磁盘空间选择安全地复制/迁移 runtime 和 secrets、部署配置至新源码目录；新实例使用原路径 /opt/Serein（以实际路径为准）、原项目名、端口、域名和密钥。不得同时启动两套访问同一数据库的服务；旧目录不得被新实例继续写入。复制时保留权限、所有者、隐藏文件及已核实的挂载；外部绝对路径/符号链接需单独核查。
- 不要复制旧源码覆盖新源码，也不要原封不动带入旧 update-state.json 的 source_hashes 作为新代码的状态。保留旧文件在备份中；新目录使用与实际已部署 commit 匹配的状态记录（由可信迁移/更新流程生成，先验证，不伪造完成状态）。确认首次安装后菜单 13 可用。
- 原作者的 se 菜单 1 写死原作者 main；仅改 git origin 不够。首次接管可用已审查的新目录/引导或可验证的更新流程，不能直接运行旧菜单 1 期待得到 fork。旧源码有本地修改时原更新器会拒绝覆盖，这是保护，不是应绕过的障碍。
- 在新代码生效前，保存新的 update_source（菜单 13，或经审查的迁移工具原子写入 installation.json、保留其他字段）。选择 qiq-Blanchet/Serein-MultiRoom / 经用户确认的实际部署分支（若直接用续轮修复分支则为 cache-repair-followup）。核对构建元数据是真正部署的 commit 和来源。旧 se 快捷方式若指向原路径，恢复原安装路径后应核验其指向。
- 不运行全新安装向导重新生成凭据；不使用旧 Ombre 迁移入口；不重建/清理向量、删除记忆、清空 volume、docker compose down -v 或 docker system prune -a。不要为了省空间先删除旧镜像与唯一备份。

### 3.4 低内存构建（硬性要求）

用户报告 VPS 仅 1 GB 多内存，磁盘还有 20 GB 多；以现场 free/df/swapon 为准。**禁止一次性并行构建 memory 和 gateway；禁止 docker compose up --build、无服务名的 docker compose build、并行 npm/pip、同时跑全套 pytest。** 新版管理器 build_runtime 已按 memory 后 gateway 顺序实现，优先复用，并确认实际项目与挂载、构建参数。构建期间两服务停止，使用现有镜像缓存，不无故 --no-cache。

如果必须手工构建，以下仅是命令顺序示例；应使用 manage.compose 相同的 -p/--project-directory/-f、.env、SEREIN_INSTALL_ROOT、legacy source 环境和 VITE_SEREIN_COMMIT / REPOSITORY / BRANCH，不能在任意目录直接跑默认项目：

```sh
# 使用已核对的同一 Compose 项目及参数，逐条执行：
docker compose ... stop gateway memory
docker compose ... build memory
# 上一步成功、检查内存与磁盘后再继续
docker compose ... build gateway
# 两个镜像成功后，先启动 memory 并等待健康，再启动 gateway
docker compose ... up -d --wait memory
docker compose ... up -d --wait gateway
```

这里的 ... 是占位符，不是可直接执行的参数。若用原 build_runtime，应确认顺序仍成立，必要时对启动过程逐服务操作。若单独 gateway 的 npm ci/Vite 也 OOM，先查日志与 OOM 记录，停止反复重建；评估已有 swap，取得用户同意并确认磁盘后再考虑新增 swap 或限额、降低构建并发，亦可在更大机器构建并安全传入同平台镜像。不要默默创建 swap、启用无限内存或盲目提升 Node heap；不要未经同意清理 Docker 全局资源。

### 3.5 上线核验与回退

- 服务启动和健康检查通过后，先核对网页登录、原 API Key、原域名及端口、数据库/记忆/原话/图片/配置/模型/索引状态、任务积压，检查日志是否有 schema、权限、OOM 错误。用合成数据进行只读查询或小范围测试，避免污染真实记忆。
- 在用户明确授权的实际客户端中重新发起 MCP OAuth，用无痕窗口或正常刷新重新加载授权页；核对真正的回调 GET、code/state、token 交换和 MCP 初始化而非仅看 303。若仍报 CSP，检查应用与代理各自的 CSP 响应头（勿记录 Key、code、state、token），不要关闭浏览器安全设置。
- 再设置 Anthropic 上游：Messages、Anthropic 明确断点、动态上下文优化 late-system、尾部 4000、缓存时长保持原设置。Orbis 世界书仍为「最新用户消息前（系统级）」；时间戳保持原配置。用经用户同意的少量 API 请求验证高/超高、摘要显示、MCP/tool 续轮、两轮以上真实缓存读写。记录脱敏统计而不是原始上下文。
- 若失败，立即停止新实例及写入，不删除新库；保留失败现场副本以便诊断。根据备份恢复与旧版本匹配的完整源码、runtime、secrets、部署配置及原项目名/路径，避免把新 schema DB 直接交给旧代码。使用原镜像或旧源码分步构建，健康后恢复入口。若新实例已写入新记忆，回退到旧快照会丢失这段新增内容，需先单独备份新库并由用户决定如何保留，不擅自覆盖。
- 明确提交用户可读的迁移报告：旧/新 SHA、备份位置与校验（无秘密）、验证通过项、停服/回退状态、仍未确认事项。没有验证通过不要宣布上线完成。

## 4. 后续维护

首次切换成功后，运行 se → 13，自定义公开仓库 `qiq-Blanchet/Serein-MultiRoom`，分支为用户确认的实际部署分支（若直接用续轮修复分支则为 `cache-repair-followup`），确认所显示的目标 commit。以后 se → 1 → 选择默认的先备份再更新。网页显示构建时来源，重建后核对版本。

Fork 的 main 用于同步原作者；cache-repair 保存原定制，cache-repair-followup 保存本轮独立修复。同步 main 不自动改变这两个分支。需要吸收原作者更新时先检查差异，在开发分支合并/rebase（共享部署分支不擅自 force-push），重跑回归，确认数据库升级与更新器/发行清单仍兼容，再用菜单 1 更新 VPS。不要在运行源码目录直接叠加小玑旧 Thinking 补丁，否则可能覆盖新策略并触发本地修改保护。不要为了同步原作者 main 而把生产更新来源改回 main。

## 5. 交付门槛

请 Codex 先报告审查与测试，而非直接部署。发现 bug 可以在 cache-repair-followup 分支修复并提交，保持用户选择的行为；涉及数据库、网络入口、密钥、付费测试、停服、删除、强推及生产迁移须另行征得明确同意。推荐顺序：审查 → 本地合成测试 → 修复/复测 → 用户确认完整备份与资源 → 迁移 → 功能/缓存验证 → 设置自有更新源 → 保留回退副本。
