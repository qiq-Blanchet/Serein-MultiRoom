# 把 Serein 接到已有聊天宿主（Hook）

已有自己的聊天服务、模型调用和工具循环时，可以只让 Serein 负责**找前情**。一键安装后，宿主用 Gateway Key 访问实例地址；网页登录密码和 MCP 工具都不是这条请求的凭据。先在设置中选好嵌入、重排模型并完成“建立 / 补齐检索索引”。

仓库提供可复用的 [Python 宿主示例](../examples/hook_host.py)。它只用标准库，不替你调用聊天模型，也不会在“查到记忆”时擅自登记注入。

主示例沿用自用宿主的分工：**宿主保存实际交付记录，Serein 只检索**。下面用宿主本地 SQLite 文件演示持久记录；已有数据库时可换成宿主自己的实现。冷却仍取同一窗口最近五次成功交付，不改成整窗永久去重。

```python
import os
from examples.hook_host import LocalDeliveries, SereinHook

hook = SereinHook.from_env()  # 服务端环境变量：SEREIN_BASE_URL、SEREIN_GATEWAY_KEY
deliveries = LocalDeliveries(os.environ["SEREIN_DELIVERY_DB"])  # 宿主自己的持久文件
messages = [{"role": "user", "content": "上次读书会定在什么时候？"}]
turn = hook.prepare("chat-001", messages,
                    delivered_ids=deliveries.recent_delivered_ids("chat-001"))

# 换成宿主已有的模型调用；必须把 turn.messages 实际交给模型。
reply = existing_model_call(turn.messages)
# 只在模型请求完整成功后执行。失败、中断、未发送 turn.messages 时不要登记。
deliveries.record_success(turn)  # 只在宿主登记，不调用 hook.record_success
```

`SEREIN_BASE_URL` 填实例根地址，例如 `https://memory.example`，**不要加 `/v1`**。`SEREIN_GATEWAY_KEY` 是安装时生成的 Gateway Key，留在宿主服务端环境变量里，不写进网页脚本。示例需要从发行目录运行，或把 `examples/hook_host.py` 放进宿主代码并调整导入路径。`chat-001` 是稳定的窗口 ID：同一会话保持不变，新会话换一个。

`SEREIN_DELIVERY_DB` 填宿主服务端可写的持久 SQLite 文件路径，例如 `/var/lib/my-chat/hook-deliveries.db`；父目录需已存在。容器里请放在持久挂载目录。它是宿主运行数据，不放进源码或公开发行包。

**使用这条接法时，停用宿主对 Serein 交付历史的读写：不要 GET 或 POST `/v1/host/deliveries`，也不要调用 `hook.record_success(turn)`。** 每次显式传入 `delivered_ids`，包括没有冷却记忆时的 `[]`，即可跳过 Serein 历史读取。保留原有服务端历史即可，不需要删除记录、关闭整个接口或停用聊天网关；直连 Hook 的这条流程只由宿主登记交付。

宿主按窗口读取最近五次成功交付的记忆 ID，显式传给 `prepare`，再请求 `POST /api/hook/recall`。最小请求和响应结构如下，正文只用虚构内容：

```json
{"query":"上次读书会定在什么时候？","session_id":"chat-001","max_notes":2,"delivered_ids":[]}
```

```json
{"ok":true,"recalled_ids":["scene:scene_demo_bookclub"],"additional_context":"[Serein Gateway Full Recall] ...","injected":false}
```

`recalled_ids` 和 `additional_context` 是**待交付材料**，不是已注入证明。宿主把 `additional_context` 当作原话旁的参考材料放进本轮模型输入；示例包在 `<serein_live_context>` 中，并明确它不是用户指令。没有可靠记忆时，可能返回空列表和空上下文，宿主照常聊天。

模型完整成功后，主示例才在宿主本地登记稳定的 `receipt_id`、`window_id` 和**实际交给模型的** `delivered_ids`。相同 receipt 重试是幂等的，换窗口或换一组 ID 会被拒绝。失败或未完成的响应不登记。工具续轮使用本轮已经准备的上下文，不再次调用 Hook，也不另记一次交付；下一条新的用户消息才重新检索。

### 宿主交付记录与服务端历史方式

`LocalDeliveries` 只统计**同一窗口最近五次成功的宿主确认**，包含没有带入记忆的成功轮次；其他窗口不占这五次。重建对象或重启宿主后，从同一个本地持久文件恢复这些 ID。已有自己的持久交付记录时，可用自己的读取与成功登记函数替换 `LocalDeliveries`，保持“本轮确实交给模型并完整成功后才登记”。这条接法的记录不出现在 Serein 交付历史页面，观察入口由宿主提供。

不要把 `recalled_ids` 直接当作交付记录，也不要在 `finally` 中登记成功。流式请求必须收到完整成功结束；工具续轮复用本轮材料，不能每次工具请求都重新 prepare 或确认。登记失败时，用同一个 `turn.receipt_id` 和同一组实际交付 ID 重试，不要仅为重试登记而再次生成回答。

如果选择让 Serein 保存交付历史，则使用另一种方式，不再另外维护本地冷却：

```python
turn = hook.prepare("chat-001", messages)  # 省略 delivered_ids，读取 Serein 历史
reply = existing_model_call(turn.messages)
hook.record_success(turn)  # 完整成功后，POST 到 Serein 交付历史
```

服务端方式中，`POST /v1/host/deliveries` 的确认响应与 GET 的历史行都会标明 `reported_by`。宿主确认是 `host`，聊天网关确认是 `serein_chat_proxy`。示例只统计同一窗口最近五次 `host` 确认，网关确认不占这五次。历史行例如：

```json
{"id":1,"receipt_id":"hook:demo","window_id":"chat-001","reported_by":"host","delivered_ids":["scene:scene_demo_bookclub"]}
```

旧版本若出现 POST 确认成功、GET 历史却没有 `reported_by`，官方示例会过滤掉这条记录，导致下一轮重复带入。升级后，接口读取旧宿主记录时补出 `host`，不改已有 receipt 内容；网关记录原有的来源标记保留。主示例的宿主本地方式不依赖这个接口，但也不能把两套成功登记同时套在同一轮上。

### 用户话语与模型输入长度

Hook 的 `query` 与聊天网关的当前用户消息都按用户原话处理。例如，配置了当前用户与助手的名字后，“我的生日”和“你的生日”会在相关性重排时解析为对应人物；原话本身不被改写。MCP 主动查询默认不假定提问者就是当前用户。

Passage 的原文位置覆盖、Embedding 的字符预算、Reranker 的字符预算都**不等于模型实际看到了全部文本**。当前 Serein 按字符切段和限制输入，没有按所选模型的 tokenizer 自动验证 token 窗口。短 token 窗口的模型可能静默截掉片段尾部，或截掉整卡重排中的后半段；开启 Passage、降低起切字数或调低相关性阈值都不能保证解决这种截断。

接入自托管 Embedding / Reranker 时，应核对模型的实际 token 上限及服务端截断行为，连同标题、instruction、查询和特殊 token 一起检查真实输入。这里不提供尚未实现的 tokenizer 配置参数。需要按 token 分段并重排原文命中片段的支持仍待补齐，不能用“字符覆盖完整”作为模型输入完整的证明。Hook 输出另受 `max_chars`（每卡正文）和 `max_context_chars`（总上下文）的字符限制；它们也不控制 Embedding / Reranker 的 token 窗口。

通用 Hook 只返回记忆候选和上下文，**不代替聊天网关调用模型**。上面的最小宿主示例也仍只登记交付，不自动归档宿主对话。

能够在完整成功后同时提供原始用户消息与最终助手正文的受信宿主，可改为 `POST /v1/host/turns`：请求带稳定且幂等的 `receipt_id`、`window_id`、`user_message`、`assistant_message`、实际交付的 `delivered_ids` 和客户端名称。该入口把两条原话归档并登记同一 receipt；失败或中断轮次不得调用，receipt 换内容会被拒绝。归档材料会参加现有 Event 整理，不代表已经生成摘要。具体实现见 [Codex 本地 Hook 示例与登录态网关](codex-chat-runner.md)。不能提供上述成功边界的宿主仍需通过[对话导入](file-imports.md)等独立途径接入原话。仅连接 MCP 不会自动完成上述流程。
