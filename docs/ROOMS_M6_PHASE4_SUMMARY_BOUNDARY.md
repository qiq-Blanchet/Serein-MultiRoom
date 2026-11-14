# M6 第四阶段（部分）：自动摘要重新开启与跨房间 Event 隔离

> 2026-11-14。仅在 `feature/multi-room` 的 GitHub Runner 使用临时数据库和合成原话。未使用真实第三方模型、私人 API Key、用户 VPS 或实际聊天数据。

## 已通过的回归

- 代码用例：`tests/test_room_services_isolation.py::test_real_summary_pipeline_keeps_auto_toggle_and_settled_events_in_their_room`
- 成功 Actions：[Python + 原版 Dashboard 回归 #43587005178](https://github.com/qiq-Blanchet/Serein-MultiRoom/actions/runs/43587005178)
- 在真实的 `RoomManager` 创建 A/B 两个独立房间和 SQLite 库，用原版原话归档接口分别写入合成用户/助手消息。直接执行原版 Event 摘要引擎 `pipeline.advance`，用测试中的 `synthetic_runner` 代替外部模型生成，不会产生真实 API 请求。
- **A 自动摘要关闭**：`scheduled_advance(A)` 返回 `auto_paused`。不生成摘要批次、不消费该房间的原话；刚创建的房间甚至可以尚未初始化摘要处理表。
- **B 独立生成 Event**：原版三角色流水线处理 B 的完整一组对话，在 B 自己的数据库创建 Event 并登记两条已处理原话；Event 只能包含 B 的合成正文，不含 A 的内容。A 依然未处理。
- **原版开启保护保留**：将 A 的 `pipeline.auto_enabled` 从 `false` 改为 `true` 时，`save_settings` 故意把关闭期间两条旧原话记为 `raw_processing.outcome='auto_boundary'`，防止突然补跑历史聊天、意外消耗模型额度。这些旧原话仍保存在 `raw_events`，不是删除或丢失。
- **重开后的新对话正常摘要**：给 A 再写入一组全新的合成对话，摘要引擎成功在 A 独立创建 Event，只引用重开后的内容；没有吸收被 `auto_boundary` 保护的旧原话，也没有 B 的私人内容。A/B 的数据库状态彼此保持原值。
- **可重复运行**：B 再次运行返回 `current`，不重复结算已经完成的 Event；A/B Event 快照和处理进度仍然独立。

## 验证边界

这里是使用**真实 Serein 摘要引擎、真实 SQLite 持久化和合成模型输出**的 Python 端到端回归；不是在多房间 Docker 容器中等待定时 `model_jobs` 触发的长时间完整后台实测。前面 M6 第三阶段在 Docker 环境验证过真实 Chat 请求、两房归档及容器异常恢复，但**不能用那次成功替代本阶段尚未执行的定时任务联机验收**。

仍待验证：多房间真实定时自动摘要/梦境/索引/心绪长负载、真实模型 API 的失败和费用保护、正式 HTTPS 与 systemd 宿主机恢复、VPS 整机断电与受控升级。以上通过不等于可迁移或覆盖现有正式部署。
