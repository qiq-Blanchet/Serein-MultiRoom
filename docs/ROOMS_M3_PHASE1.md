# M3 / Phase 1：独立房间管理控制台（开发预览）

> 2026-11-09 · `feature/multi-room`。此阶段仅增加新的管理员操作入口和页面，不改写原版 `deploy/compose.yaml`、`web/server/gateway.mjs`、Serein 的各功能/记忆数据库逻辑。使用合成数据进行自动化验证，**尚未在真实 VPS 或现有用户记忆库测试**。

完整需求与隐私边界以 [MULTI_ROOM_SPEC.md](MULTI_ROOM_SPEC.md) 为准，M1/M2 说明分别在 [ROOMS_M1.md](ROOMS_M1.md)、[ROOMS_M2.md](ROOMS_M2.md)。

## 本阶段已经加入的功能

- 独立的 FastAPI 管理层：`src/serein/room_admin.py`。使用 `RoomManager` 业务逻辑，支持查询房间及 Docker 健康信息、创建房间、启动和停止房间。
- 独立响应式网页：`src/serein/rooms_ui.html` 与 `src/serein/rooms_ui.js`。可选择「复制首房配置」「指定来源房间」「空白初始化」，并单独选择是否复用模型供应商密钥。展示房间状态，支持启动和停止；刚创建的房间 Gateway Key 只在新建响应里展示一次。
- 新的管理员身份：`control/secrets/admin-key`，与每个 AI 用于 Chat/MCP 的 Gateway Key **完全分离**。浏览器只在当前页面内存中持有管理员密钥，不用 cookie、localStorage、sessionStorage；刷新后需重新验证。除了静态页面，所有管理 API 都需要管理员密钥。
- 默认只绑定 `127.0.0.1:18220`，不会占用原版网页或 M2 Chat/MCP 网关的端口。远程查看请使用 **SSH 本地端口转发** 或先构建明确的 HTTPS 鉴权反向代理，**不可直接暴露 HTTP 控制接口到公网**。
- 不通过管理员网页直接复制、展示或访问房间私有 Scene、Event、原话、日记、梦境、心绪、备忘等。配置复制继续复用 M1 字段级白名单，功能「开启」状态可继承，功能产生的内容不会被复制。
- 管理员身份不能由房间 Key 冒充；网页房间列表和状态不包含内部 token、任何 Gateway Key 或模型提供商密钥。创建返回的 Key 需要管理员自行保管，关闭提示后不再显示。

## 独立开发测试方法

以下命令仅针对专门检出的开发分支和空白测试目录。**不要在正在运行的旧版 Serein VPS 安装目录执行。**

```bash
# 在开发环境中，先按 M1 建立空房间及 Docker 镜像
python3 -m pip install -e '.[test,http,background]'
python3 scripts/rooms.py --root /srv/serein-multiroom-test admin-init

# 查看刚刚生成的管理员密钥（不要发送给别人）
# cat /srv/serein-multiroom-test/control/secrets/admin-key

# 在 VPS 本机回环接口启动管理员控制台
python3 scripts/rooms.py --root /srv/serein-multiroom-test admin-serve --port 18220
```

如果你在自己的电脑上访问远程 VPS，可以在本地电脑新开一个终端，通过 SSH 安全转发：

```bash
ssh -L 18220:127.0.0.1:18220 your-user@your-vps
```

随后在本地浏览器打开 `http://127.0.0.1:18220`，输入 VPS 上 `control/secrets/admin-key` 的内容。若先前的房间已按 M1 正常创建、且 Docker 具备启动权限，便可以使用列表、创建、启动、停止。

**注意：** 管理网页服务当前运行于拥有 Docker 权限的 VPS 主机 Python 环境，并且仅监听 loopback。不要把 Docker socket 挂载到不受信任的公网容器，也不要放行 `18220` 的公网防火墙端口。

## 当前不提供

- 还没将原版 Serein 完整管理页接入统一房间切换，现有功能页面不会被替换。
- 功能开关 / MCP 白名单的网页内编辑、Gateway Key 轮换、删除、备份、恢复、OAuth 和滚动升级尚未接入。
- 原版 `web/server/gateway.mjs` 没变；这是一套单独的管理员网页。将来与原版界面统一之前，需先保证同源路径及房间上下文授权，不会因为页面切换导致跨房间数据误操作。
- 真实 VPS 同时运行两个房间、后台任务隔离、MCP `tools/list`/`tools/call` 的端到端认证以及生产安全审计尚待验收。

## 测试范围

`tests/test_room_admin.py` 在不启动 Docker 的情况下覆盖管理员鉴权、不能以房间 Key 越权、跨源管理请求拒绝、密钥文件访问权限、创建房间及首房配置复制、私人梦境/身份不跟随、启动与停止只修改目标房间状态、静态网页不泄露秘密以及 CLI 管理员密钥初始化。

与 M2 一样，当前成果属于开发预览版；**不要直接替换用户现有的单房 Serein 服务，避免产生无法回退的数据状态。**
