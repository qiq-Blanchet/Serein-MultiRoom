# M4 第一期：后台任务隔离与逐房间 MCP 工具权限

> 2026-11-09，`feature/multi-room` 开发分支。已实现源码与合成测试，**没有在用户真实 VPS 或私人记忆库上运行**。原版 `main`、原有 `deploy/compose.yaml`、Serein MCP 服务核心逻辑均不修改。

## 本轮检查结论

- **独立后台生命周期**：每个房间各有一套完整的 Serein Application 和数据库/索引挂载。原版 `src/serein/lifecycle.py` 为每个 Application 独立创建扩展任务、模型后台任务（包括自动摘要处理）和 requested-work；退出一个 Application 时只取消其自身任务。合成测试对两个 Application 使用不同的真实房间 SQLite，并检查启动/取消事件不交叉。
- **私人内容不复制**：Scene、Event、日记、梦境、心绪、备忘、暗房、窗影、原话及后台执行状态仍属于房间私有数据库；M1 复制只保留功能性配置白名单。原版功能开关保持房间内独立生效。
- **修复新建 MCP 默认权限**：原版 `[mcp].tools` **不设置**时会注册该实例的原版可用工具并由功能开关约束；`tools=[]` 才表示显式禁用所有 MCP 工具。M1 初始实现曾误把新房间默认写为 `tools=[]`，本轮已修为**不写这一字段**，以保留原版行为。复制配置时，若源房设置了自定义工具白名单才继承，否则继续使用原版默认。已经用旧代码生成的空白 `tools=[]` 不会擅自改写，可从 Dashboard 选「沿用原版」恢复。
- **工具权限与功能开关分离**：MCP 允许清单只控制对客户端公开的工具，不能越过原版功能开关（例如 dream_read、memos、window_shadows），不会删除既有梦境、备忘或 Scene 数据。
- **实际 MCP 拒绝验证**：`tests/test_room_services_isolation.py` 分别用两套真正的 Serein MCP 服务，给 A 仅开 `read_memory`、给 B 仅开 `recall_memory`，检查 `tools/list`、不允许的 `tools/call` 以及跨房间私有正文不泄露。

## Dashboard 使用体验

原版「设置 → 功能」页末尾，增加一个折叠的「MCP 工具权限」区域；**只有开启多房间时才出现**。仍然沿用 Dashboard 的自定义用户名/密码登录，不要求第二份管理员密码。

- **默认推荐**：沿用原版 MCP 工具行为，功能开关生效，MCP 工具权限不再加一层限制。
- **自定义**：展开工具清单，仅勾选允许该 AI 使用的工具；列表从该房间当前真实的 Serein 工具注册信息取得。部分功能关闭时，相关可选工具暂时不可见，需要先开启对应的原版「功能」开关再刷新。
- **保存时**：会弹出确认；当前房间若正在运行，管理层只停止该房间、更新其 `config.toml` 的 `[mcp].tools`、移除**旧容器壳**后重新创建启动并完成健康检查。**SQLite、索引、私有密钥和数据挂载都不会删除**，其余房间不受影响。停止状态的房间保存后下次启动生效。
- **失败处理**：校验未知工具在重启前完成。若重启发生故障，将恢复配置文件的旧值并报告管理操作失败；房间可能需要管理员排查后手动重新启动，不能把此阶段说成无停机热更新。

配置和 API 由原有房间管理层提供：
- `RoomManager.mcp_permissions` 查询原版当前工具目录、工具选择模式；
- `RoomManager.update_mcp_permissions` 应用配置并只重启目标房间；
- `GET/POST /api/rooms/{room_id}/mcp-tools` 为仅内部管理员令牌可访问的控制 API；
- `GET/POST /__serein/rooms/{room_id}/mcp-tools` 经原版 Dashboard Web 鉴权后转发，**仅当前网页选中的房间**可编辑，不能通过改 URL 影响另一间房。

## 完成与待验收

已添加 `tests/test_room_services_isolation.py` 和扩展 `tests/test_room_manager.py`、`tests/test_room_admin.py`、`web/tests/room-dashboard-routes.test.mjs`，接入 GitHub CI。覆盖默认 MCP 工具非空、特定房间白名单、更改后仅目标房间重启、服务端管理鉴权、网页当前房间校验、原版真实 MCP 工具执行路径、并行后台任务上下文与数据目录分离。

**仍需实机验收**：真正的 VPS Docker 双房长时间运行、自动摘要/梦境/心绪/备忘真实异步模型调用与持久化、原版全网页的读写互不影响、重启后的 MCP `tools/list` 和 `tools/call` 端到端联机、容器意外停止恢复、升级与回滚、OAuth、备份/恢复。离线测试不能代替以上实机验收。

**原则**：一 AI 一独立房间与 Gateway Key，Chat/MCP 共用此 Key；`X-Serein-Window-ID` 保留原版窗口语义，不额外强制等于房间 ID。原版所有功能、数据与挂载保持独立且完整。
