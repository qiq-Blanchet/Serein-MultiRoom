# M6 第一期：GitHub 托管 Runner 的真实 Docker 双房验收

> 2026-11-14。只对 Fork `qiq-Blanchet/Serein-MultiRoom` 的 `feature/multi-room` 开发分支进行测试；**原版 main、现有 VPS、已有数据/密钥都没有被触碰**。本阶段不是模拟 Docker：确实用 GitHub-hosted Ubuntu runner 的 Docker Engine 启动两个 Serein memory 容器、一个 Chat/MCP 网关、一个原版 Dashboard 和私有管理 API。但仍是**临时测试环境**，不是长期运行的生产 VPS。

## 自动 CI 入口

- Workflow：`.github/workflows/multiroom-docker-smoke.yml`
- 测试脚本：`tests/integration/docker_multiroom_smoke.py`
- 最终通过的双房 + 整房备份恢复运行：[GitHub Actions #43578019018](https://github.com/qiq-Blanchet/Serein-MultiRoom/actions/runs/43578019018)
- 同一提交的原 Python/Node 回归：[Actions #43578018973](https://github.com/qiq-Blanchet/Serein-MultiRoom/actions/runs/43578018973)
- GitHub-hosted runner 支持 Docker；ChatGPT 当前容器沙盒本身没有 Docker socket，不能在其中常驻部署 Serein。CI 运行完成后，脚本强制清理所有此安装专属容器和 Docker 网络，临时目录由 Python 自动删除。

工作流触发：开发分支中 Docker/核心/web/CI 集成脚本相关修改的 push，以及 Actions 工作流手动执行。每次从提交版本重建核心与网页镜像，不从正式 VPS 复制任何私有卷或真实 Key。

## 已经用真实容器验证

1. 以**全新隔离根目录**创建两间假房，示例自定义房间 ID 为 `xiaogi-main`、`other-main`，分别生成不同的内部 UUID 与 Gateway Key。每个容器仅挂载自己的 `config/`、`secrets/`、`data/`，没有任何原版单房数据目录参与。
2. A/B 独立添加合成 Scene 记忆及不同的假上游模型列表（`alpha/alpha-only-model`、`beta/beta-only-model`）。经真正 Gateway 的 `/v1/models` 按不同 Key 查询，结果各不相同，互不泄露。
3. 使用官方 MCP Streamable HTTP Python 客户端经过真实 Gateway 建立两个 MCP 会话，分别测试 `initialize`、`tools/list` 和 `tools/call`。A 仅允许 `read_memory`，B 仅允许 `recall_memory`；A 能读自己的 Scene，无法读到 B 的私人正文。**Chat/MCP 使用房间同一 Gateway Key**，不额外要求 Window ID 等于房间 ID。
4. 无 Key／错误 Key 返回 401，A Key 搭配明显伪造 B 的 `X-Serein-Room-ID` 返回 403；单独设置任意 `X-Serein-Window-ID` 不参与路由，继续保留原版窗口语义。
5. 真正停止 A 后，A Key 无法访问；B 的 `/v1/models` 仍能成功。重新启动 A 后原 Gateway Key 没有改变，各房模型仍各自隔离。
6. **整房备份与回滚恢复（真实 Docker）**：A 运行中制作只属于 A 的归档、完成预览校验；确认 B 备份清单为空。停止 A 后写入一条新的合成 Scene，再启动并按房间 ID + SHA-256 确认恢复旧归档；恢复后新增 Scene 被替换，原 Scene 恢复，回滚归档保留，A Gateway Key 仍是创建时那一把，B 数据和模型不变。
7. 通过宿主机私有 Docker bridge 运行内管服务、启动原版 Dashboard 容器。未登录访问网页被 401 拒绝；使用原版自定义用户名 + scrypt 密码登录后能加载 HTML 页面；同一登录态下查询房间列表，准确列出 A/B，未返回 Gateway Key。

## 实测发现并修复的部署问题

- **私有路由清单权限**：最初 Gateway 容器取消了 `CAP_DAC_OVERRIDE`，但服务进程仍用容器 root 身份读取宿主机普通用户拥有的 0700/0600 控制目录，实际返回 `PermissionError: /control/routes.json`。修复：Gateway 与 Dashboard 在 Docker 中匹配**宿主机创建文件时的 UID:GID**，保持私有权限，不改成宽松的 0644/0755。
- **只读 Dashboard 的 Vite 临时文件**：Vite 6 预览启动时要在 `/app/node_modules/.vite-temp` 写临时配置。修复：保留 `--read-only` 和权限收紧，只为这一个目录添加 16 MiB 的**非持久、noexec tmpfs**，不放开整份镜像目录写入。
- **HTML 请求测试**：网页首页应发送 `Accept: text/html`，不能用 API 的 `application/json` 去断言 HTML 响应；修正了测试代码，未改动原版前端路由。

所有上述修复已在真实 GitHub Runner 再次验证。原有 Python/Node 自动回归也照常运行。

## 尚未完成，不能提前宣布正式可用

- 还没有在用户自己的 VPS、正式 HTTPS 域名/反向代理或完整长期数据上验收；不要覆盖或迁移原版安装。
- 还没有对多个真实后台模型任务（自动摘要、梦境、心绪）、索引吞吐、MCP 长会话、完整操作数小时/数日的并行负载进行实测；本次只使用静态假模型列表，不调用第三方模型、不消耗用户 API Key。
- 宿主机管理服务目前仍需运维配置 systemd 等开机自动拉起；`--restart unless-stopped` 能恢复已运行的 Docker 容器，但不自动恢复宿主机 `admin-serve` 进程。
- 还没有在 GitHub runner 上做真正**操作系统重启/断电**、数据库 schema 跨版本迁移、反向代理大文件上传、外部备份存储、OAuth 按房绑定、全房间合并归档、生产可回滚发布。
- 已实现的真实 Docker 测试验证的是**双房基础链路、工具权限和同房数据备份恢复**，不等于所有原版 Dashboard 页面都完成交互端到端测试。

## 下一步

优先在**完全独立的测试 VPS 目录**做 HTTPS + systemd + 真正双房运行/停止/重启的可复现验收，再验证后台任务持久化及容量，并补全自动升级和整体多房归档。用户确认前**绝不接管或更换正在运行的原版单房服务**。
