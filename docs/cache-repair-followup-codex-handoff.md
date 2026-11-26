# cache-repair-followup：续轮修复、Codex 复核与迁移交接

日期：2026-11-26（Asia/Shanghai）。这是独立修复分支的交接，不是已部署证明。

## 1. 分支与边界

- 仓库：`qiq-Blanchet/Serein-MultiRoom`。
- 原定制分支：`cache-repair`，本次从 Codex 最后提交 `e1010ec8e9a346c4b5e9a8cca0fcf8e49b2aa327` 派生 `cache-repair-followup`。请先核对实际 HEAD，再检查 `git diff e1010ec..HEAD`。
- 不修改原 cache-repair、main、codex-server 或上游，不直接合并或部署。尚未连接生产 VPS、真实数据库、付费模型或客户端。遵守 AGENTS.md，只用合成 fixture 或本地空库做开发验证。
- 继承完整的 [首次迁移 / 备份 / 更新器 / OAuth / 用量统计说明](cache-repair-codex-handoff.md)。后文明确替换旧文中的目标工作分支和缓存门槛描述；原备份/回退约束不变。
- 用户明确要求先把当前三个布局和续轮修好，不新增 Claude 原生 mid-conversation system 第四模式；留作以后独立评估。保持 Messages 上游、明确断点、默认 4000 尾部预留、4.6+ Thinking 默认值及显式参数优先、xhigh 不静默降级。不要引入 4.6 之前的型号特殊兼容。无需维护各新型号缓存门槛表。

## 2. 实现说明与取舍

所有实现集中在 `src/serein/chat_context.py` 和聊天 API 边界，不改持久化 schema、配置字段、构建栈或更新器逻辑。

### 2.1 缓存断点简化

旧 Serein 代码分别使用 2048（Sonnet）或 4096（其他）作为**候选前缀的估算最低长度**，另有 **4000（尾部预留）**。Codex 在 e1010ec 中添加多型号具体最低门槛表；用户要求不再维护该表。

- `_cache_min_tokens_for_model` 简化为固定 4096 的保守前缀估算，与模型名无关。该数值不是 Anthropic 实测 token 计数或所有型号的真正最低长度；较短的可缓存前缀可能不会设置历史断点，由供应商决定实际能否命中。用户单轮输入常达数千 token，此取舍经需求背景选择，避免追逐型号规则。
- `anthropic_cache_tail_tokens` 默认 4000 及既有配置兼容原样保留，不截断历史、不影响 max_tokens，不替用户改原有自定义值。
- 保留 e1010ec 的 system/tools 前缀计数、工具块断点能力、cache_control TTL、现有 assistant 候选选择。继续使用近似估算，不能承诺真实命中率。
- 三模式仍为 legacy（默认）、host-data、late-system。普通后置系统进入 user 属于明确选项，不是原生系统指令；世界书更新/消失可能影响此前的缓存前缀。

### 2.2 安全恢复 Thinking（`_restore_cached_reasoning_content`）

- 流式与非流式仍缓存 provider 原生有序 content blocks，包括 thinking/signature、redacted_thinking、text、tool_use。通过相同会话与上游/配置合同隔离；保留查找 exact tool ID 优先及语义唯一性、重复调用判定。
- 对同一助手消息，正文与缓存内容不一致时，绝不恢复旧正文或旧 reasoning；调用方显式提供的 reasoning/details 不覆盖、不伪造签名。对于任一半途编辑的工具轮，供应商可能拒绝旧签名，用户应重新生成该轮，而不是让网关悄悄恢复旧内容。客户端如果保留自己的旧签名，网关不会替其重写。
- 允许 native `role=user, content:[tool_result]` 触发恢复；OpenAI role=tool 同样支持。
- 当客户端重写 ID 时，只有缓存及传入批次语义唯一、无其他调用 ID 冲突、整批相邻结果能完整对应且不重复，才在**准备发送给上游的副本**把 assistant.tool_calls 和相邻结果 ID 同步回 provider 原始 ID。原生 content 和签名不改。重复同参并行工具、缺失 ID、缺结果、重用/歧义 ID 不猜；此时可能返回上游错误而非伪造续轮。已有非原生 OpenAI 兼容行为保留。
- 入口清除外部传入的私有字段 `_serein_anthropic_content`；仅由服务端匹配缓存生成。内部字段不向客户端暴露。
- 新增限制只作用于**进程内** `pending_tool_reasoning`：每条响应一小时 TTL（自创建起），最多 64 个会话、每会话 32 条、每会话序列化内容合计 2 MiB、全局序列化内容合计 16 MiB。读写时清理过期、按最旧记录及 LRU 会话清理；最终非工具回复仍立即清理本会话。部分记录被淘汰后仅允许精确 ID 恢复，不因只剩一个语义匹配就猜测；若被淘汰记录与保留记录的签名和 ID 完全重复，同时移除歧义条目。日志只记录调用数量，不记录工具参数。字节预算是序列化大小估算，并非进程 RSS 硬限；极长工具轮、服务重启或淘汰后客户端必须保留原签名/原生上下文，否则可能需要重新发起工具轮。没有持久缓存或新的数据库文件。

### 2.3 Orbis host data 不再静默丢失

- 两个优化布局均支持把紧邻 role=tool / 原生 tool_result 的具名 host data 放入转换后的 user 消息，置于全部并行工具结果之后，不进入工具结果正文；同一并行批次的多个连续 user/tool 结果统一合并成一个合法 user content，结果块始终先于附加文本（legacy 的特殊并行格式也规范化）。
- 没有有效相邻 user/工具结果（如时戳在消息列表末尾、下条为 assistant 或非法 carrier）时，不猜它属于哪一轮；保留原 system 路径作为兼容回退，可能影响该请求缓存，不静默删除数据。
- 不改源请求或原话归档，不把未确认的宿主标签当作可信授权。真实世界书、宿主信息可各自不同，需检查 Orbis 实际 wire 格式。

## 3. Codex 复核清单

重点检查以下边界，而不是重写架构：

- [ ] 实际网关 SSE/non-stream 两条路径：保留 provider 块顺序、thinking 签名、redacted_thinking；客户端收到可见 reasoning_content/usage，无内部原生字段；完整工具续轮本地合成 测试。
- [ ] 精确、重写、重复、跨会话、同参并行、不同参并行、乱序/缺失/多余/冲突 result；原生 user result 与 OpenAI role=tool，以及混合格式。只改请求副本，不猜 ID，不覆盖新编辑正文、不篡改调用方显式签名。
- [ ] 时间戳在并行结果前/中、没有 carrier、空/无效 carrier、普通 user 前；未标记 system 的权限和三个布局的兼容性。确认工具结果优先且不在结果正文内注入 host 文本。
- [ ] 缓存过期/大小/会话数/单会话条数/淘汰后不误匹配/最终回复清理；确认进程内空间预算适合 1 GB 多内存环境，不将明文签名/内容输出日志或测试。
- [ ] 固定前缀 4096、尾部默认 4000，两者互不混淆；system/tools 计数、实际上游 cache_read/cache_creation 和流式累计 usage，三种布局及设置 roundtrip；用户可传参，不静默改 64000、高/超高、summarized。
- [ ] OAuth 继续只放宽经验证的 callback origin；菜单 13 自定义更新源（同 SHA 不同来源也重建）、发布清单及迁移/低内存要求不变。
- [ ] 上游 main/原定制已有 24 项失败曾被独立确认；本次不追修这些与需求无关的失败，对比失败 ID 而非只看总数；若有新失败应先定位。实际 Claude API/客户端命中率、真实 OAuth 仍需用户授权后测试。

重点测试：

```sh
python -m pytest -q tests/test_cache_repair_followup.py tests/test_anthropic_cache_layout.py \
  tests/test_anthropic_thinking_compat.py tests/test_anthropic_usage_compat.py \
  tests/test_public_settings.py tests/test_chat_archive.py tests/test_chat_upstream_errors.py \
  tests/test_upstream_update.py tests/test_installer_menu.py tests/test_installer_doctor.py \
  tests/test_gateway_contract.py tests/test_mcp_oauth.py tests/test_http_mcp.py
# 可选：配置 PLAYWRIGHT 和 SEREIN_TEST_CHROMIUM 为合成 loopback 实测浏览器
SEREIN_TEST_CHROMIUM=/path/to/chromium python -m pytest -q tests/test_mcp_oauth.py -k chromium
```

新增 synthetic tests 在修复前确认失败；初轮核心联动测试 64 项通过，最终增加边界用例后的相关测试 90 项通过，含 Chromium、更新器和发布清单的最终集成测试 262 项通过（测试集合有重叠，不相加）。完整测试结果见下方（在开发环境跑，不在低内存 VPS 上并行跑）。

## 4. 发布、迁移和更新（硬性要求）

**未经用户授权绝对不要迁移、停服务、改真实配置或连接私有部署。迁移之前必须先完整备份并核验可恢复性。**

1. 查阅旧交接文档 §3，确认实际部署路径、compose project、挂载、数据库及索引、图片/附件、日志需要保留的部分、任务状态、凭据/环境变量、OAuth、配置、反代、当前 commit/镜像；先评估磁盘与可用内存。备份完整旧源码、运行数据、SQLite 一致性快照/相关 WAL（停止写入后）、所有独立 volume/配置/密钥，权限与属主，并生成校验值，做独立恢复演练。不将任何生产数据提交 Git。
2. 先在 staging 核对拟部署的**固定 SHA**与 release manifest，复核后由用户决定将 followup 合并到 cache-repair，还是直接部署通过复核的 followup。不要擅自合并、强推、覆盖现有安装或删除唯一备份。来源配置保存最终用户确认的实际部署分支；不能默认切换原 cache-repair、也不能更新回作者 main。
3. VPS 只有 1 GB 多内存、20 GB 多磁盘（以现场检查为准）。严格顺序构建 memory，再构建 gateway；逐个检查是否 OOM/空间不足；禁止一次并行构建、`docker compose up --build`、不带服务名的 build 或同时跑重测试。用现有缓存、保留旧镜像；若单组件内存仍不足，先停下诊断，不能擅自开 swap、删除 Docker volume 或重试无限次数。
4. 先核验记忆库原数据/配置/原话/索引/应用身份/凭证完整和服务健康，再完成真正浏览器授权与 token exchange、缓存连续两到三轮实测（明确断点、尾部 4000、late-system 可选）、Thinking 摘要、MCP 工具续轮、图片及 usage 测量。实际供应商请求需用户允许，使用合成数据。
5. 通过后，se → 13 设置自有公开仓库 `qiq-Blanchet/Serein-MultiRoom` 和最终确认的部署分支（如果直接部署本分支，选择 `cache-repair-followup`）；菜单 1 后续按固定目标 commit 更新，仍先备份再顺序构建。网页版本/来源要在重建后核对。
6. 失败先停新实例写入，保留失败现场与新数据备份，按旧 commit/镜像及匹配旧数据恢复；不要用旧代码直接打开可能已升级的生产库。参考旧交接完整回退步骤，报告变更与尚未确认项。

## 5. 后续方向（本次不实施）

Claude 新型号支持 mid-conversation system，有望让动态世界书保留系统角色且不破坏在其之前的稳定缓存。仅作为未来 opt-in 方向；需要按真实供应商能力/缓存前缀/Thinking 的约束做独立设计，不在本分支加入第四模式，也不移除现有三个模式。

## 6. 本次验证记录

- 全量回归（启用合成 loopback Chromium）在最后两个小边界用例的修改前开始，结束结果：**2617 passed, 24 failed, 11 skipped**，耗时 307.54 秒。逐一对比原有全量失败 ID，完全一致（新增失败 ID = 0；消失失败 ID = 0）。这 24 项保留为已知基线失败，不宣称全库全绿。
- 最后追加淘汰歧义和日志脱敏两个用例/修复后，重新执行最终相关测试过滤集，**90 passed**；重新执行包含全部本轮 26 个新增测试、缓存布局/Thinking/usage、OAuth（3 真实 Chromium）、更新器、安装器、网关、归档及 release boundaries 的最终针对性集成集，**262 passed，0 failed**。两组有重叠，不相加。
- `git diff --check` 通过；发布清单 632 个条目均存在且无重复；未运行生产构建、真实客户端/供应商/生产迁移。Codex 如需验证最终 HEAD 全量，请在开发环境独立重新跑并比较失败 ID。
