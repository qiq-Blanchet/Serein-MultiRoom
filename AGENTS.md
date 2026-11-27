# Serein public edition
Use local empty databases or synthetic fixtures for ordinary development and tests. Do not discover or connect to a private deployment unless the user explicitly requests server maintenance under the rules below.
Preserve current mixed recall, evidence, lifecycle, cooldown and writer preview/save contracts.
Public release files are explicitly enumerated by the release manifest. Do not package runtime data or Git history.
Run the focused behavior tests after changing their implementations. Keep optional features opt-in.
Tagging extracts grounded entities alongside the primary domain for Event and Scene. Preserve authored domains/body/cues; validate exact source quotes, label body-only provenance, and invalidate derived entities on content or source changes. Alias suggestions never merge identities. Association, when implemented, may add at most one related memory as a candidate, never directly inject it.

## 阿里云部署维护

### 作用范围与授权

- 当前 Codex 项目对应仓库 `qiq-Blanchet/Serein-MultiRoom`。阿里云生产维护以 `cache-repair` 分支为准；功能修复可以先在临时分支完成，但部署前必须合并或快进到 `cache-repair`，并核对服务器实际 commit。
- 本仓库同时用于维护用户在阿里云运行的 Serein 实例。正式域名是 `memory.serein.bid`。不要把它与其他 Serein 工作区、测试服务器或旧域名混淆。
- 默认仍按公开版规则在本地使用空数据库或合成数据。只有用户明确要求检查、更新或维护服务器时，才连接私有部署；不要因为文档中存在地址或路径就自行访问。
- 服务器操作前先只读确认实际安装目录、部署方式、Compose 项目、仓库、分支和 commit。常见路径是 `/opt/Serein`，备份位于 `/srv/backups`，但不得在未核实时硬编码假设。
- 不在聊天、日志、提交、文档或命令输出中暴露密钥、Cookie、令牌、完整 `.env` 或其他凭据。

### 以正常使用路径为准

- 复查功能时优先验证真实网页和客户端会经过的正常路径、历史任务兼容性、路由缓存及合理并发；不要为了无法由正常客户端产生的内部状态钻牛角尖。
- 验证强度与风险相称：先运行最相关的行为测试，再按改动范围补充构建、类型检查、健康检查和日志检查。不要仅为追求数量重复运行无关的全套检查。
- 理论问题只有在能由真实输入触发、会破坏数据或有明确回归证据时才作为阻塞项。数据一致性、鉴权、备份可恢复性和数据库迁移不能以“避免过度检查”为由跳过。
- 用户只要求诊断或复查时，先报告结果；只有同时要求修复或部署时才修改代码或生产环境。

### 更新前备份

- 任何会替换代码、重建服务、迁移数据库或改变运行数据的生产更新，都必须先建立并验证恢复点。
- 常规更新优先备份不可轻易重建的内容：`deploy/runtime`、全部记忆和原话、SQLite 数据库及其一致性所需文件、索引、附件、用户上传内容、`deploy/secrets`、`config.toml`、`.env`、`installation.json`，以及本次变更涉及的反向代理或系统配置。
- 备份 SQLite 时先停止写入或使用项目提供的一致性快照流程；在备份副本上执行 `PRAGMA quick_check`，不要直接用运行中主库的普通文件复制冒充有效备份。
- 备份目录权限使用 `0700`，敏感归档和清单使用 `0600`。保存目录清单、校验和、备份时间、当前仓库、分支、commit、部署方式、Compose 项目和必要的镜像标识；验证归档可读且关键文件确实存在。
- 常规更新不重复归档完整 Git 历史、整个源码目录或 Docker 镜像。记录精确仓库、分支和 commit 即可重建代码；只有大规模迁移、上游不可得、存在未提交服务器补丁或用户明确要求时，才额外保存源码、Git bundle 或镜像。
- 已验证并标记为永久保留的备份不得参与自动清理。其他同类有效恢复点默认保留最近 3 份；`.incomplete`、手工专项备份和用途不明的目录不能自动计入或删除。
- 删除旧恢复点前确认较新的 3 份均有效。涉及唯一恢复点、旧补丁、历史配置或用途不明内容时，先列出占用和判断依据并询问用户。

### 低内存服务器更新

- 服务器只有约 1 GiB 内存，禁止同时构建 Memory 和 Gateway，也不要启动并行的全量构建。
- 顺序固定为：完成并验证备份；构建 Memory；启动并确认 Memory 健康；再构建 Gateway；启动并确认 Gateway 健康。
- 保留现有运行数据、Compose 项目名、端口、凭据、自定义更新来源和其他部署配置。不要用新克隆的空 `deploy` 目录替换现有实例状态。
- 更新后核对实际部署的仓库、分支和 commit，检查容器重启次数、数据库完整性及必要的数据计数，并通过当前配置中的正式域名验证服务；本项目的正式域名是 `memory.serein.bid`。
- 构建或启动失败时保留备份和现场，先恢复可用服务。除非用户明确授权并已确认目标恢复点，不要自动用旧数据库覆盖当前数据。

### 磁盘与临时文件清理

- 维护前后检查根盘、备份目录、临时解压目录和 Docker 占用。清理前先向用户列出各类实际占用、拟清理目标和预计影响。
- 可以清理已被成功完整备份替代的 `.incomplete`、确认不再使用的临时解压目录、此次诊断产生的临时文件，以及不被运行容器依赖的旧构建缓存。
- 禁止执行无差别的 `docker system prune`。不得删除正式运行数据、Docker 数据卷、正在使用或仍被保留容器引用的镜像、有效凭据、永久备份及用户要求保留的旧实例或 Ombre 资产。
- 删除必须使用经过核验的精确绝对路径，不使用宽泛通配符。若无法进入回收站且需要永久删除，先明确说明并取得用户授权。
- 清理后报告文件系统实际释放空间，而不只报告工具的逻辑回收量；再次确认保留备份、镜像和数据卷数量，并复查 Memory、Gateway、Nginx 配置及正式 HTTPS 健康状态。
