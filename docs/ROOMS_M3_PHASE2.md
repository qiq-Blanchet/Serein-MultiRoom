# Serein Multi-Room — M3 第二阶段：原版 Dashboard 房间切换

> 状态：2026-11-09，已在 `feature/multi-room` 提交代码和 GitHub 离线测试，**没有在用户的真实 VPS 上运行**。这是原版 Dashboard 的**可选**多房间扩展，不替换原版 `main`、不更改原有 `deploy/compose.yaml`、数据库结构、memory 容器挂载或记忆功能。

## 产品决定

- 原版 Serein Dashboard 的左侧导航在「使用说明／花园／设置」上方增加**一个小房间切换按钮**，间距、字体、颜色沿用原版，不另设复杂大屏。点击后才展开房间列表和新建房间小表单。
- 浏览器进入统一管理网站只使用**同一个管理员身份**进行一次 HTTPS 鉴权。之后切换 AI 房间不再重复输入密码。管理员凭据与每房间给 Operit/MCP 的 Gateway Key 完全不同。
- 新房间可从 Dashboard 创建、复制首房/指定房间的功能配置或空白初始化，并可在菜单中启动。**创建时只填一个房间 ID**（如 `xiaogi-main`），不额外要求房间名称。AI 对外显示名称继续使用原版「设置 → 外观」里的 `ai_name`；创建后立即返回随机 Gateway Key。创建房间不需要每次 SSH 到 VPS；首次安装、Docker 镜像构建与版本升级仍是运维工作。
- 必须完整保留每个房间原版的设置编辑、模型、自动摘要和各记忆功能；客户端的房间 ID、窗口 ID、模型名都不能绕过服务端验证的房间身份。
- **私人数据永不互相复制**：Scene、Event、原话、日记、梦境、心绪、备忘、暗房、窗影、模型私人提示、历史任务状态、索引和备份。复制的是配置白名单，不复制私人内容。

## 已写入的源码

| 位置 | 作用 |
|---|---|
| `web/src/components/RoomSwitcher.jsx` | 小按钮、切换列表、可选模板、创建房间和启动按钮 |
| `web/src/components/Sidebar.jsx` / `web/src/styles.css` | 在既有 footer 顶部按相同节奏插入、沿用 Serein 视觉样式 |
| `web/src/App.jsx` | 从同源管理 API 加载房间，切换后整页刷新以清空 React 组件状态 |
| `web/server/roomSession.mjs` | 管理员 Basic 凭据校验、已签名的 HttpOnly/SameSite 房间选择 cookie |
| `web/server/roomDashboardRoutes.mjs` | 同源列表、创建、启动、停止、切换 API；服务端持有管理员密钥向管理层请求，不将密钥发送给浏览器 |
| `web/server/gateway.mjs` | **只有启用多房间环境变量时**才切换到管理员单点认证、M2 Chat/MCP 入口和房间上下文；否则保留原版认证/转发逻辑 |
| `web/server/roomContext.mjs` / `web/server/sereinBackend.mjs` | 服务端验证房间签名及 ready 路由清单，把原版 Dashboard 的后端桥接绑定到具体房间 |
| `web/vite.config.mjs` / `web/server/backupDownload.mjs` | 现有设置编辑、模型、Dream、Diary、Event、Scene、备份等桥接按当前 room 访问；路由草稿和叙事临时预览分别隔离 |
| `web/src/storage/roomScopedStorage.js` | 为每房间前端 localStorage/sessionStorage 加独立命名空间；单房模式仍使用原有 key |
| `web/src/main.jsx` / `web/src/garden/entry.js` | 在加载有状态页面前验证当前房间，拒绝在身份不明时加载历史本地状态 |

### 单次鉴权与选房

多房间模式下，Serein Web Gateway **继续使用原版的 `webAuth.mjs` 登录方式**：HTTP Basic 用户名与密码可由用户自行设置（`scripts/rooms.py web-auth`），凭据按原版 scrypt 格式存入 `control/secrets/web-auth.json`，而不是固定用户名 `admin`，更不是把内部管理员 Key 当成网页登录密码。由 HTTPS 反向代理加密传输；浏览器在同一站点完成一次验证，切换房间时不再要求重新输入。**独立管理员 Key 只用于受信任服务之间的管理 API 调用**，浏览器永远不接收它。

房间选择由服务端签名后存放在 `HttpOnly; SameSite=Strict` cookie 中，并不是外部 Gateway Key，不能单独用 cookie 获得管理员权限。实际路由是通过已经由服务端验证的 ready 房间清单和内部 Serein token 完成；没有通过校验时**绝不回退到原版第一间 memory 库**。**一 AI 一房间 ID 一 Gateway Key**：创建小玑房间时填 `xiaogi-main`，其 Operit 自定义请求就使用 `X-Serein-Window-ID: xiaogi-main` 和小玑自己的 Gateway Key。另一 AI 必须使用自己的不同房间 ID/请求头/Key。网关先验证 Key，再核对请求头；不匹配直接拒绝 403，不能串用。为兼容原版，未传 Window ID 的旧客户端仍可仅凭自己的 Key 访问自己的房间。

多房间公网管理入口必须使用有效的 HTTPS origin。MCP OAuth 仍按 M2 设计保持关闭，M5 将单独设计 audience/resource 的房间绑定。

### 可选环境变量（开发参考，**尚非正式部署脚本**）

```text
SEREIN_MULTIROOM_ROUTES_FILE=/path/to/control/gateway/routes.json
SEREIN_MULTIROOM_ADMIN_KEY_FILE=/path/to/control/secrets/admin-key
SEREIN_MULTIROOM_ADMIN_URL=http://127.0.0.1:18220
SEREIN_MULTIROOM_CHAT_URL=http://127.0.0.1:18219
SEREIN_PUBLIC_ORIGIN=https://your-own-host.example/
SEREIN_GATEWAY_BIND=127.0.0.1
```

这些只是 Node 网关的**可选配置接口**，启用前还需要根据部署方式保证 Node 进程能够访问 M2 网关、管理员服务，并能解析私人 Docker network 中的 `room-<id>` 主机名。**本阶段之后已实现独立 Dashboard Docker 启动命令及宿主机私有网桥管理层连接，详见 [ROOMS_M3_PHASE3.md](ROOMS_M3_PHASE3.md)。** 这仍不等于完成真实 VPS 验收或生产级一键部署。 不要单凭上述环境变量就在正在使用的 VPS 上替换原版部署。特别是，旧单房容器的 `SEREIN_WEB_AUTH_FILE` 和 `SEREIN_MEMORY_URL` 仍保持原样使用。

如需从网页创建**第一间**房，当前要求先通过 M1 或独立 M3 Phase1 管理器创建并启动至少一间 ready 房间；没有 ready 房间时统一原版 Dashboard 仍会故障关闭，不会把空站点强行接上不明数据库。之后可以在左侧菜单添加其他房间并启动。

### 当前测试

- `web/tests/room-session.test.mjs`：管理员凭据、cookie 篡改、停止房间失效、签名服务端上下文、真实 `callSereinBackend` 路由与浏览器 local/session Storage 隔离。
- `web/tests/room-dashboard-routes.test.mjs`：同源管理 API、配置源、新建房间、启动、选择、密钥不泄露。
- GitHub Actions 新增 Node 22 测试与原版 React Dashboard 生产构建；原有 Python M1/M2/M3 首期和 Thinking 回归继续运行。

## 尚待 M3 第三阶段及后续

1. **真实多容器联机 / 部署编排**：可控地连接原版 React/Vite Web 服务、独立管理员 API、M2 Chat/MCP 网关和每个房间的 Docker 网络；验证不开放房间公网端口。
2. 完整的浏览器场景测试：A 改模型设置、日记、Scene、梦境、备忘后切 B，确认 B 看不到 A；再切回 A 数据仍在。
3. UI 内 MCP 工具名单编辑、功能开关，已存在的原版各功能配置页必须继续有效。
4. Key 轮换与禁用、备份/恢复、OAuth、受控升级、监控、操作审计及真实 VPS 迁移流程。

**原则：不因为多房间而删除原版功能，也不因“安全改进”擅自变更原版 memory 容器和挂载。** 必要的结构变化要单独向用户解释并获得同意。
