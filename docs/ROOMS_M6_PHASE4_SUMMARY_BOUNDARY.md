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

## 已通过的真实 Docker 自动调度与独立重启验收

- 代码用例：[`tests/integration/docker_multiroom_smoke.py`](../tests/integration/docker_multiroom_smoke.py) 中的 `verify_background_scheduler`；与之前的双房 Chat/MCP、网页登录、备份、动态第三房间验收一同运行。
- **真实 Docker 后台三阶段调度**：[Actions #43588639084](https://github.com/qiq-Blanchet/Serein-MultiRoom/actions/runs/43588639084)，通过。先在 B 的数据库内对先前原话写入安全 `auto_boundary`，再写入两条新的合成用户/助手原话。以原版 `pipeline.execution_mode='api'` 为 Router、Curator、Writer 配置独立的合成模型 ID，连接仅绑定临时 Docker bridge 私有 IP 的本地 OpenAI Chat Completions 兼容假模型服务器。**不是手动调用 `pipeline.advance`**，而是让容器内真正的 `model_jobs.run` 发现任务并启动 `scheduled_advance`。
- CI 验证三个角色分别实际发送 HTTP 请求到假模型服务器，返回符合原版校验规则的合成 JSON，B 的真实 SQLite 中成功生成一个 Event，并记录新的两条原话已完成处理。Event 内容含 B 的合成标记，没有 A 的标记。A 也配置了模型角色，但 `pipeline.auto_enabled=False`，因此没有向模型服务发出请求，也没有生成 Event；两个房间的 Gateway API 保持正常。
- **摘要完成后独立重启房间**：[Actions #43589039825](https://github.com/qiq-Blanchet/Serein-MultiRoom/actions/runs/43589039825)，通过。仅停止 B，确认 B Gateway Key 暂时不可访问、A 继续可用；随后独立启动 B。后台调度再次运行，`pipeline_schedule` 把已结算的当天任务标记完成，B 原有 Event ID、原话处理记录不变；假模型调用计数也没有增加，说明**这次有序重启未重复运行完成的模型阶段**。A 继续不生成 Event。这属于房间有序停止/启动，不能代替主机断电验收。
- 同一代码提交的 Python 回归和完整原版 Dashboard 测试：[Actions #43589039906](https://github.com/qiq-Blanchet/Serein-MultiRoom/actions/runs/43589039906)，通过。

## 验证边界

本阶段分别覆盖了**Python 摘要引擎与开启保护回归**，以及**真实 Docker 双房后台定时调度、三角色私有 HTTP 假模型调用、Event 持久化、单房有序重启后的去重**。虽然已证明容器内调度会真实运行，但本次测试是在 CI 临时容器内完成的一次合成摘要，不是多房间连续数日运行，不是现实第三方模型的错误重试或费用压力实测，也不是正式生产部署。

仍待验证：多房间自动摘要/梦境/索引/心绪的长时间持续负载，现实第三方模型 API 的超时/费用/失败处理，正式 HTTPS 与 systemd 宿主机恢复、VPS 整机断电、用户现有数据迁移与受控升级。以上通过不等于可迁移或覆盖现有正式部署。
