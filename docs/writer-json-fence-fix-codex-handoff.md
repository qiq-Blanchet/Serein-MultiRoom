# 自动摘要 Writer JSON 围栏与待办跳过修复 — Codex 复核交接

日期：2026-11-27（Asia/Shanghai）。本文件描述独立修复分支，不是生产部署或真实数据验证报告。

## 1. 分支、继承与范围

- 仓库：`qiq-Blanchet/Serein-MultiRoom`。
- 修复分支：`writer-json-fence-fix`，从 `cache-repair-followup` 的 `4cfb8a5ae05d9f1d0cd9239389a3bd179d5d747b` 派生。2026-11-27 核对远端：`cache-repair` 与 `cache-repair-followup` 均指向该提交；无需再次把旧 cache 分支合入。
- 本轮功能提交依次为 `ff3bf1d`（Writer JSON 围栏）、`9f0f2c5`（待办按钮反馈）、`b4d16d2`（已结算/部分结算待办的实际跳过与两个 UI 入口）。后续交接文档提交也在同分支；复查时核对实际 HEAD，并审查 `git diff 4cfb8a5..HEAD`。
- 继承 [cache-repair-followup 交接](cache-repair-followup-codex-handoff.md) 与 [首次迁移、OAuth、用量及更新源交接](cache-repair-codex-handoff.md)。原有 Messages、Thinking、缓存布局、4000 尾部预留、usage、OAuth、更新器、工具及图片路径不在本次改动范围。不要为本次小修复重做缓存/Thinking 或切换回旧版本。
- 遵守 AGENTS.md：只使用合成 fixtures / 空测试库；不连接私有部署，不上传诊断原文件、真实聊天、记忆、凭据或生产库。未合并 main/cache/codex-server，未部署生产。无新增依赖、持久化 schema 或配置字段。

## 2. 问题一：API Event Writer 返回完整 Markdown JSON 围栏

现场表现：自动摘要 Writer 的返回本体是合法 JSON，但外层为完整的三个反引号、json 标签、正文及结束围栏，故原先直接 `json.loads(raw)` 报「模型返回了无法解析的 JSON，请重试当前阶段」。诊断文件仅用于本地确认语法；没有提交真实内容，也未验证真实 Event 的证据是否合法。

改动：`src/serein/extensions/pipeline.py` 的 API job 解析入口，仅对 `request['role']=='event_writer'` 且字符串输出进行整串匹配。允许完整的 json 围栏（大小写、外层空白、LF/CRLF），脱掉外壳后仍由原 `json.loads` 解码。

- 纯 JSON 继续正常解析；不自动修补截断 JSON、不补括号/证据、不从任意文字中抽取 JSON、不接受未闭合围栏或多块输出。
- Router / Curator、Agent 提交以及其他 Writer 路径保持原行为。本次没有修改模型提示词、重试策略、schema 或证据判定。
- `prepare_stage_output`、`validate`、旧的结算/保存路径照常执行；日志 `record_attempt` 保存未经脱壳的原始输出，失败仍可下载核查。
- `tests/test_latest_pipeline.py` 新增的 12 个参数化用例包含纯 JSON、完整围栏、CRLF、截断、前后解释文字、多围栏、假证据、Router/Curator 不受影响。

## 3. 问题二：已整理原话仍挂在待办，两个跳过入口会无反馈

用户描述：下方待办中，点击「跳过本批，保留原话」有时没有确认预览；先「继续整理」或「重新切分」可能在上方整理状态块看到原话已处理的失败原因及同名跳过按钮，后者也常无反应。期望：两个入口均能出现确认预览；明确确认后该批退出待办，不再自动重复整理，原话及已有 Event 保留。

已在合成库中复现两个核心路径：旧 `_recovery_scope` 用于普通未结算批次和拆批，同时拒绝已处理 raw ID、已有结算收据或 `done` 批次；因此合法旧待办无法拿到跳过预览。前端又需等待预览请求，错误常显示在很长列表底部。以下修复针对这些已复现条件；实际线上每次无响应的原因还需复核。

### 3.1 前后端使用专门的跳过预览

- `src/serein/api/imports.py`：`/v1/pipeline/recovery-preview` 接收可选 `action:'skip'`。两处跳过入口均发送该标记。普通拆批/旧调用继续走原有预览及严格重建守卫。
- `src/serein/extensions/pipeline_recovery.py`：跳过预览只核对跳过范围，不再做不必要的拆批计算。普通未处理批次沿用原恢复校验；特殊情况由 `_pending_skip_scope` 核对真实 open/running 待办对应的冻结批次、source_ids、scope、raw_processing、原文身份和完整问答边界，返回已经处理与待跳过数量。
- 特殊路径仅作用于存在待办的 `done` 批或已有处理记录的可恢复批次，不允许任意选择已完成 Event 或未完成对话尾部。已处理记录原样保留；损坏/改变的原话、未处理半截问答、其他待办占用、不能安全关闭整个批次的情况继续拒绝。对仍有未处理原话的活动批次，保留先处理同聊天更早批次的顺序限制。
- 最终请求仍走原 `/v1/pipeline/skip-batch` 且必须有 `SKIP_PIPELINE_BATCH` 明确确认、已有 pipeline 工作租约及 immediate transaction；服务端在提交时重复检查，不只依赖前端预览。
- `_close_pending_skip`：仅未处理的完整问答新增 `raw_processing.outcome='manual_skip'` 收据；已存在 raw_processing / Event settlement 收据绝不覆盖或重复插入。待办设置 `resolved_skipped`。原来已完成的 producer 和其结果保留；可关闭的未完成旧批次转为 done，记载原有结果、手动跳过收据和被退休的旧路由缓存。对于旧未完成计划的未处理路由（包括 parked tail 的缓存）以及有明确失效证据的旧计划缓存，清理本计划的缓存并保留审计；保留其他 producer、真实完成的结算及其有效路由。原话和已有 Event 不删除，不创建新 Event，不删除任务/模型返回。
- `src/serein/extensions/pipeline_pending.py`：`can_skip` 包含仍有待办的 done 批次，使其拥有合法的确认入口；其余抢占/处理中状态仍遵守原保护逻辑。

### 3.2 两个 UI 入口（不增加新的操作按钮）

- `web/src/components/PendingMaterials.jsx`：按下立即显示本卡片的预览/提交状态，成功自动定位确认区，错误就地可见；确认支持已处理/未处理计数。服务端成功后立即移入已处理记录，状态刷新失败或旧轮询结果不得把已确认跳过的同一批重新放回待办；其他待办不受影响。
- `web/src/components/PipelineSettings.jsx`：上方整理状态块中的同名按钮立即打开原恢复确认 dialog，等预览后方可提交，在 dialog 内展示失败原因。跳过成功先更新所选待办，再刷新 status / 延续已有后续整理流程，保留其他待办信息。后台正在执行时继续阻止竞争操作；没有取消显式确认。
- 用户意图是关闭本批待办，不是删除历史聊天、已有 Event 或新加自动跳过规则；未得到成功响应不得假装已经跳过。

## 4. Codex 复查清单（重点，不做无关重构）

- [ ] 确认分支基线、上述三次功能提交及工作树实际 HEAD；核对 diff 只涉及此处列出的自动摘要、前端与测试/文档。
- [ ] Writer 在实际 API 执行环境的纯 JSON、完整围栏、空白/CRLF、大小写；截断（含闭合围栏）、多围栏、夹带文字、错误类型/schema、伪造证据照旧拒绝。确保纯 JSON 与原始返回下载正常；Router/Curator 非本次兼容对象。
- [ ] 用空库和 synthetic fixtures 复现用户两个入口；分别测试普通失败批次、done 有保留待办、本批全部已处理、混合已处理/剩余完整问答、活动旧批/最近失败的 running 待办。预览准确、确认后真实 DB 里该待办 resolved_skipped，而非只改 UI；刷新、重新打开页面、服务重启后不再出现。
- [ ] 确认原话字节/ID、既有 Event 及其 source refs、既有 raw_processing、Event 结算收据、任务输出和审计不被重复写/删除；只有剩余已核对原话可标 manual_skip。不影响另一个待办、其他聊天、未完成问答尾部、原有重新切分/归线能力；确认/取消、租约忙、并发和重复提交均安全。
- [ ] 特别复核 `_pending_skip_scope` / `_close_pending_skip` 的 scope/ownership、防重复结算、完整单位、done vs 尚未完成计划、其他 producer 的 route provenance、真实跨批 Event 承接的旧路由仍可用及后续同一聊天能继续整理。检查跨预览和提交状态变化时是否需要再次预览，不能把未确认范围静默扩大；检查失败事务不落半份收据。
- [ ] 复核代码变化导致 `runtime_revision()` 变化对旧未结算任务、已完成/压缩的历史路由 producer 的影响（包括旧计划续轮、原话已结算的状态恢复及 parked tail）。新版可能将冻结旧任务标记 needs_repair；不要移除版本保护或通过伪造 revision/路由绕过证据检查。若历史路由兼容性仍存在回归，先用合成旧版本数据复现再做窄修复。
- [ ] 真实手机/浏览器上两处按钮、慢/失败预览、确认取消、提交 busy/errors、已确认成功后的轮询和状态刷新失败；确认已结算的待办计数不再堆积。API/前端同时升级；旧前端仍调用旧预览，不能只更新一个服务。
- [ ] 保留缓存/Thinking/用量/工具/图片/OAuth/更新源原合同；不把生产数据用于测试，不加新数据库 migration，不擅自改变记忆或部署目标。

### 复测命令（独立开发工作树，从完整 Git 仓库运行）

使用已按 constraints-runtime.txt 安装所需项目依赖的虚拟环境。前端必须先构建再跑全量 npm test（两个 gateway 测试依赖已构建静态资源；缺 build 时会产生 503，并非本次代码错）。

```sh
python -m pytest -q tests/test_pipeline_batch_recovery.py tests/test_pipeline_material_locator.py \
  tests/test_pipeline_pending.py tests/test_latest_pipeline.py tests/test_pipeline_upgrade_backlog.py \
  tests/test_pipeline_route_history.py tests/test_event_append_capacity.py tests/test_pipeline_scenes.py

cd web
npm ci
npm run build
npm test
node --test tests/pending-materials.dom.mjs tests/pipeline-recovery.dom.mjs
cd ..
```

必要时单独执行完整 Python 测试和旧 cache 集成测试（见前两份交接文档）；与上次失败测试 ID 比对，不修与本需求无关的既有失败。测试用例在完整 Git checkout 中，发布包不一定包含全部开发测试及 fixtures。确认 release-files.json 仅列合规文件、包含运行与本交接文件，无数据或凭据。

### 实际验证记录

- Writer 新增用例完成 red/green；该阶段的自动摘要相关测试 210 passed。
- 最终功能 HEAD `b4d16d2`：上述 Python 相关 8 文件 **250 passed**、DOM 两文件 **25 passed**、前端全量 npm test **137 passed**、Vite build 成功。共 412 项最终相关验证；早期集合与最终集合重叠，不另外累加。构建有既有大 chunk 警告，不是失败。
- 文档提交后补验 release boundaries：12 passed；发布清单 633 项均为存在的唯一合规文件，合成源码打包并核验 ZIP 文件列表及完整性成功。
- 已复现并修复未结算尾部后续新回复的合成续轮失败；相关测试最终全绿。代码与已经推送的远端树一致。未重新执行整仓 Python 全量、未访问真实客户端/API/生产 DB/生产 VPS。
- 旧缓存阶段全量的 **24 个已知基线失败** 见 cache-repair-followup 文档：对比分支之前失败 ID 完全相同，尚未全库全绿。此轮不宣称它们已修复，也不能预先保证最终 HEAD 不存在其他未覆盖失败。

## 5. 迁移、备份、更新与低内存约束

**先复查，不擅自部署。迁移/更新前必须做完整、可恢复的备份。** 用户确认更新后才操作实际服务器。继承两份旧交接的详细说明：

1. 核对实际部署分支/commit、安装目录、compose project、每个挂载/volume 和服务状态。确保旧源码/配置、运行库和一致性 SQLite 快照（停止写入或使用一致性备份，处理 WAL/SHM）、全部记忆、原话、索引、图片/附件、未完成任务和日志、安装元数据、环境变量、凭据、OAuth 配置与反向代理配置均有完整备份。备份校验值并验证可恢复；备份与真实诊断不得提交 Git，不覆盖唯一旧备份。
2. 核验发布清单与拟部署的固定 SHA。分支包含 cache 基线和本次全部小修复；不擅自合并到 cache/main、切回上游或覆盖现有部署。先在独立环境按 §4 核验。
3. VPS 内存约 1 GB 多、磁盘剩余约 20 GB 多（现场核对）。必须 **memory 构建成功后再构建 gateway**，两者串行、逐项检查内存/磁盘；禁止并行全量构建、无服务名的 build、`docker compose up --build` 等隐式同时构建。保留旧镜像/旧数据；重测试和浏览器构建验证在开发机完成。
4. 通过复查和用户批准部署后，检查原始数据、记忆、配置、未完成批次、服务健康，再在受控测试数据下验证自动摘要的 Writer 和两处跳过预览/确认及重新打开页面后状态。已有 Event、原话、结算保持；用户自行确认真实待办的跳过范围，不能自行批量清理。
5. 如果最终直接部署此分支，使用原自定义更新源菜单（se → 13）设置仓库 `qiq-Blanchet/Serein-MultiRoom`、分支 `writer-json-fence-fix`；若 Codex 复查后用户选择其他合并分支，则设置实际确认的那个分支。菜单 1 更新前仍备份并按固定目标 SHA、顺序构建，网页版本与实际记录一致。
6. 更新失败时保留失败现场/新数据备份，停新实例写入，按旧已知可工作 commit/镜像及匹配旧数据恢复。严禁拿未知兼容的旧代码直接打开新版生产库，或因待办卡住就删除聊天、Event、任务记录、数据卷。

交接到此；请先向用户报告复查结论、未解决风险、实际测试结果和建议部署目标，再按用户授权继续。
