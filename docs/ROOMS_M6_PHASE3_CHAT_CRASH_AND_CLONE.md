# M6 第三阶段：真实 Chat 请求、异常恢复与动态第三房间

> 验收日期：2026-11-14；仓库 `qiq-Blanchet/Serein-MultiRoom`，仅开发分支 `feature/multi-room`。测试使用 GitHub-hosted Ubuntu runner 上的真实 Docker Engine、临时目录、合成数据和只监听该安装私有 Docker bridge 的本地模型模拟服务。**没有连接真实 VPS、没有读取私人聊天或密钥，也没有调用任何付费模型 API。**

## 自动化入口与成功记录

- 测试脚本：[`tests/integration/docker_multiroom_smoke.py`](../tests/integration/docker_multiroom_smoke.py)
- GitHub Actions：[`.github/workflows/multiroom-docker-smoke.yml`](../.github/workflows/multiroom-docker-smoke.yml)
- 真实 Docker 双房并发聊天 + 服务进程异常恢复：[Actions #43583060325](https://github.com/qiq-Blanchet/Serein-MultiRoom/actions/runs/43583060325)，通过。
- 运行中新增第三房间（通过原版 Dashboard 的房间创建接口）及复测：[Actions #43583792569](https://github.com/qiq-Blanchet/Serein-MultiRoom/actions/runs/43583792569)，通过。
- 同一提交的 Python/Node 回归、原版 Dashboard 构建和全套 152 项 JavaScript 测试：[Actions #43583792608](https://github.com/qiq-Blanchet/Serein-MultiRoom/actions/runs/43583792608)，通过。

## 已实际验证的行为

### 1. 两房并发走通真实 Chat API

测试在独立 Docker 网络的宿主 bridge 私有 IP 启动只返回合成响应的 OpenAI 兼容本地服务。A/B 两间 Serein 分别保存自己独立的上游模型设置，均经过真正 `/v1/chat/completions` Gateway 转发到各自运行中的 Serein 服务，再转给该私有本地服务。每间房间发起 5 次请求，共 10 次，使用独立窗口标识；返回 HTTP 200，模型标识和回答内容都对应正确的房间。

随后直接检查每房间自己的真实 SQLite `raw_events`：各有 5 条用户消息及 5 条助手消息；A 库不存在 B 的合成原话和回复，B 库亦不存在 A 的内容。检查本地假模型收到的原始请求中没有另一房间的用户标记。这不是单纯检查 `/v1/models` 静态列表，确实完成了真实 HTTP 模型请求与持久化归档链路。

### 2. 应用进程异常退出后的自动恢复

在 A 房间的容器**内部**，向 Serein Python 应用进程发送 SIGKILL，使该容器因应用异常退出；并非在外部执行 `docker kill`（后者属于管理员手动停止，会正确抑制 `unless-stopped` 自动重启）。验证 Docker 容器的 `RestartCount` 增加、健康检查恢复；全过程 B 的 API 仍然可用。A 恢复后再次成功调用本地假模型并写入一组新原话，先前的 A 原话持续存在；B 仍只拥有自己的 5 组消息。

这只证明**单个应用进程崩溃**的 Docker 自动恢复，不证明 VPS 整机断电或宿主机重启恢复。

### 3. 运行中新增第三个房间

A/B 仍在服务时，以原版网页登录凭据请求 `POST /__serein/rooms`，选择默认 `source=first` 配置模板建立 `third-main`。确认房间列表能看到 C 而不泄露 Gateway Key；C 拥有全新且与 A/B 不同的 Gateway Key，在启动前无法使用该 Key 访问 Gateway。停止的 C 不能被 Dashboard 选中。

检查 C 的初始数据库：`documents`、`raw_events`、`injection_debug` 全为空；C 不包含 A 的任何合成私人 Scene 和聊天原话。C 的用户/AI 显示身份重新初始化，不复制 A 的私人身份。可复用的模型上游、任务模型分配、原版功能开关、自动摘要开关和 MCP 工具白名单则按模板复制，未复制上游私密 Key。

启动 C 后，使用其专属 Gateway Key 查询到继承自 A 的模型目录；通过 Dashboard 选择 C，读取到 C 自己的模型/身份设置。停止/重启 C 不影响 A/B 的原有路由和配置；C 的 Gateway Key 保持不变，私人原话库仍为空。

### 4. 回归检查

第一、二阶段的 Dashboard 模型/摘要设置保存、旧标签页误写拒绝、MCP 允许列表变更、单房暂停隔离、整房备份恢复仍在相同 Docker 流程中运行，原版单房路径及网页全部 152 项 JavaScript 回归也继续通过。

## 尚未验证、不可宣称已上线

- 真正的第三方 API 模型（这次本地模拟的是 Chat Completions 协议，不消耗外部配额），多个房间持续执行真实自动 Event 摘要、梦境、心绪、索引和后台 Agent 工作数小时/数日的行为、性能及费用限流。
- VPS 的正式 HTTPS/反向代理、systemd 宿主机私有管理进程开机自动拉起、系统重启/断电复原、外部备份存储、大文件传输。
- 按房间绑定的 OAuth、跨版本数据库升级及安全滚动发布、全部 Dashboard 页面在真实浏览器内逐项交互验收。
- 用户现有原版 Serein 数据迁移或生产切换。未经用户授权，不得触碰或替换其现有部署。

**状态：第三阶段的 CI 环境真实容器验收通过；生产上线验收、后台真模型长期负载尚未完成。**
