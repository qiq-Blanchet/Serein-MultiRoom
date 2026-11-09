# Serein Multi-Room：M2 分房间 Chat/MCP 静态鉴权网关

> **当前状态（2026-11-09）**：M2 基础代码已写入 `feature/multi-room`，合成数据与双实际 Serein 应用的离线 CI 已通过；**尚未在真实 VPS 或 Operit 上测试**。此处不包含统一管理网页和 OAuth。架构约束以 [MULTI_ROOM_SPEC.md](MULTI_ROOM_SPEC.md) 为准。

## M2 的边界

统一对外入口：同一个地址的 `/v1/models`、`/v1/chat/completions` 和 `/serein/mcp`（`/mcp` 别名）通过每房间**独立 Gateway Key** 分流；网关从已验证的密钥查找唯一的房间 ID，再转发给该房间 Docker 容器。

- 认证使用服务端生成的高熵 Key，控制层 SQLite 仅保存其 SHA256 摘要；**不根据模型名、`window_id`、房间名称或用户提供的 room ID 选路**。
- 只有处于 `ready` 的房间进入动态路由清单，停止房间立即撤销对外 Key 的使用资格。列表、日志和网页不显示内部 token。
- 网关仅可读取私有的 `control/gateway/routes.json` 机密路由清单，不挂载任何房间的数据库、索引、备份、梦境、日记或备忘文件。这个机密清单包含房间 ID、外部 Key 摘要和房间内部 Serein Bearer Token，必须严格保护，永远不要提交到 Git。
- 每房间核心服务仍使用自己单独的内部 token，且不发布宿主机端口。网关以 `http://room-<id>:8011` 为**服务端生成**的内部目标，不接受用户指定上游 URL。
- 客户端伪造 `X-Serein-Room-ID` 或请求体顶层/`serein.room_id` 时，若与 Key 所属房间不一致则拒绝。各 AI 使用独一无二的创建时房间 ID 作为自定义请求 `X-Serein-Window-ID`（如 `xiaogi-main`）；若这个请求头与已验证 Gateway Key 的房间 ID 不一致，网关直接拒绝 403，不能只靠修改请求头选房。旧客户端不传该头时保持按 Gateway Key 鉴权路由。
- MCP 使用静态 Bearer；保持会话 ID 和事件流，并对浏览器 Origin 做同源校验。MCP OAuth 授权、注册、token 等入口**故意不对外开放**，待 M5 完成独立 audience/resource 绑定后接通。不能用本版本声称 OAuth 多房间已经可用。
- M2 公共网关禁止访问 `/v1/settings` 等管理接口，也不提供管理网站。M3 才增加有管理员身份验证的网页与房间切换入口。

## VPS 独立测试（不要覆盖原版部署）

仅限本 Fork 单独检出、测试用目录；Python 3.11+ 和 Docker 已安装。先按 [ROOMS_M1.md](ROOMS_M1.md) 建好第一间、第二间并通过 `start ROOM_ID` 启动。一个镜像可以运行多间房，**不需要复制源码**：

```bash
# 在 Fork 开发目录；此前已运行 rooms.py build、create、start
python3 scripts/rooms.py --root /srv/serein-multiroom-test list

# 在独立的本机端口启动唯一的 M2 公共 Chat/MCP 网关
python3 scripts/rooms.py --root /srv/serein-multiroom-test gateway-start --port 18219
python3 scripts/rooms.py --root /srv/serein-multiroom-test gateway-status

# 用第一间的 room_id 查找它自己的 Gateway Key（请勿分享）
# cat /srv/serein-multiroom-test/rooms/<ROOM_ID>/secrets/gateway-key

# 验证自己的模型列表，Bearer 使用上面的 Key
# curl -H "Authorization: Bearer <ROOM_GATEWAY_KEY>" \
#   http://127.0.0.1:18219/v1/models

# 可停止公共网关，但不会自动删除房间数据
python3 scripts/rooms.py --root /srv/serein-multiroom-test gateway-stop
```

Operit / OpenAI 兼容客户端的 Base URL 可指向 `http://127.0.0.1:18219/v1`（仅在**客户端与 VPS 为同一主机**时可用），API Key 填该房间的 Gateway Key。不同 AI 使用相同 Base URL、不同 Key，便路由到不同房间。远程客户端需要额外的 **HTTPS 反向代理或安全隧道**连接本机回环端口；M2 自身强制只绑定 `127.0.0.1`，不会默认在公网暴露明文 HTTP。

MCP 静态 Key 的完整资源 URL 是服务端的 `/serein/mcp`；OAuth 模式不可用。Chat/MCP 接入必须分别在客户端配置（不会因为配好 Chat API 就自动启用 MCP）。如代理改变 Host/Origin，应确保同源配置及转发正确。

## 测试与当前限制

`tests/test_room_gateway.py` 采用虚拟 Docker、合成 room 数据和模拟上游，检查：
- A Key 只到 A 的 Chat/模型/MCP、B Key 只到 B；相同窗口 ID 不影响隔离。
- A Key 携带 B 房间的 URL/header/body 提示不会越权；未知 Key、无 Key、OAuth/管理路径全部拒绝。
- MCP session 与事件流原样透传，浏览器的跨来源 Origin 请求拒绝。
- 停止 A 只撤销 A 的入口、B 继续可用；机密清单没有原文 Key 和私有数据路径。
- 网关 Docker 不挂载 `rooms/<id>/data`、`backups`、`secrets` 整目录，只读挂载最小必要机密清单；宿主机发布只允许 loopback。

**尚需**：真实 VPS 双房容器的网络及健康实测、网关认证错误/大请求的压力测试、MCP 真实工具 `tools/list`/`tools/call` 的权限双重验证、Key 轮换/吊销管理界面、M3 原版网页复用与管理鉴权、M5 OAuth、备份与升级流程。不要将此 M2 开发版本用于保护重要的真实私人记忆，直至完整安全验收。

## 不可混淆的私人数据边界

Scene、Event、日记、梦境、心绪、备忘、暗房、窗影、原话、向量索引及各后台任务状态都属于私人数据。创建新房时仅复制**已审查配置字段**：模型／上游连接、任务角色选择、功能开关、自动摘要策略、MCP 工具白名单等；不会复制旧房间数据库或私人 Prompt、身份与这些数据正文。继承功能的「开启」状态不代表继承该功能先前产生的数据。
