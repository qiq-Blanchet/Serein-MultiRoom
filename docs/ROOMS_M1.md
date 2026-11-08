# Serein Multi-Room：M1 房间管理内核（开发阶段）

> 状态：2026-11-09 首轮实现，已通过 **CI 合成数据测试**。没有在用户的真实 VPS 上启动或迁移任何服务。
> 总体目标及不可变约束见 [MULTI_ROOM_SPEC.md](MULTI_ROOM_SPEC.md)。

## 现阶段已实现

- `src/serein/room_manager.py`：控制层 SQLite 房间注册表，房间 ID 不可变，状态、配置来源、容器镜像和 Gateway Key 摘要；私有数据不进入控制层注册表。
- `src/serein/room_docker.py`：每房间独立的 Docker Serein 核心服务，同一镜像、不同私有数据挂载；独立 CPU/内存限额，**不开放任何房间端口**；由安装专属的内部 Docker network 为未来统一网关提供服务发现。
- `scripts/rooms.py`：VPS CLI，支持 `init`、`build`、`create`、`list`、`show`、`status`、`start`、`stop`。
- 使用原版 `bootstrap.initialize` 创建空库、空索引；一个 room 的数据库、索引、内部 token、外部候选 gateway key 和 Docker 容器均独立。
- 第二间及后续房间默认从第一间配置克隆；可以 `--source ROOM_ID` 或 `--blank`。配置复制遵循白名单，不复制 SQLite 文件。新房间身份、原话、日记、梦境、叙事和私有查询状态全部留空。MCP tools allowlist 可继承显式配置；新空房间默认空列表。
- 模型供应商密钥默认**不复制**，CLI 只有 `--reuse-provider-keys` 时才复制。Gateway Key 和内部 Serein Token 永远为新房间重新生成。
- 创建完成时状态为 `stopped`，须通过 Docker 启动并通过健康检查后才标记 `ready`；创建失败为 `error`，不会自动被路由。密钥不会在列表、状态输出中显示。

## 本地 VPS 开发体验（只针对测试环境）

以下示例假定从**本 Fork 的 feature/multi-room 分支**检出源码，且已安装 Python 3.11+ 和 Docker。**不要在已有的真实 Serein 安装目录内直接执行；不要将旧实例 runtime、config、secrets 指向本测试目录。**

```bash
# 在 Fork 的独立开发目录
python3 -m pip install -e '.[test,http,background]'
python3 scripts/rooms.py --root /srv/serein-multiroom-test init

# Docker 镜像只需构建一次，多个房间共用这个镜像
python3 scripts/rooms.py --root /srv/serein-multiroom-test build

# 首个完全空白的房间
python3 scripts/rooms.py --root /srv/serein-multiroom-test create "AI-A" --blank

# 查看创建结果中的 room_id；不暴露 Key 内容
python3 scripts/rooms.py --root /srv/serein-multiroom-test list

# 第二个房间：默认只复制首房配置，不复制模型密钥
python3 scripts/rooms.py --root /srv/serein-multiroom-test create "AI-B"

# 若你明确希望复用第一间的模型供应商密钥，可改用：
# python3 scripts/rooms.py --root /srv/serein-multiroom-test create "AI-B" --reuse-provider-keys

# 启动、查看和停止，把 <ROOM_ID> 换成 list 实际输出
python3 scripts/rooms.py --root /srv/serein-multiroom-test start <ROOM_ID>
python3 scripts/rooms.py --root /srv/serein-multiroom-test status <ROOM_ID>
python3 scripts/rooms.py --root /srv/serein-multiroom-test stop <ROOM_ID>
```

房间目录示例：

```text
/srv/serein-multiroom-test/
  control/rooms.sqlite             # 仅房间管理元数据和 gateway key SHA256 摘要
  rooms/<uuid>/
    config/config.toml              # 容器内 DB/index 路径 /data/...
    secrets/api-token               # 房间内部 Bearer Token
    secrets/gateway-key             # M2 网关将使用的房间 Key
    data/serein.db                  # 各房间独立 SQLite
    data/recall-index.sqlite        # 各房间独立索引
    backups/                        # 预留；备份功能还未实现
```

默认容器限额为 2 GiB 内存、1 CPU；可以在 `DockerRuntime` 运行配置中调整，正式版会提供控制层配置。镜像默认名为 `serein-multiroom:dev`；生产升级流程、版本固定与滚动重启尚未实现，不能把 `build` 当成升级功能。

## 操作与安全边界

1. 当前独立容器只运行单房间完整 Serein **核心服务**；统一网页 / Chat API / MCP 公共入口仍待 M2-M3。**尚不能直接把房间 gateway key 填进 Operit 连接多房间**，因为公开路由还没有接上。
2. 当前仅生成并保存 Gateway Key，`RoomManager.resolve_gateway_key()` 提供服务端查找基础；没有实现生产级 Key 轮换、OAuth、控制层 HTTP 权限管理与审计。
3. Docker room network 不对宿主机发布端口，只有后续受信任统一网关应加入该网络。容器之间可处于同一内部 network，但必须由未来网关验证密钥并明确路由；每个核心服务还分别有内部 Bearer Token。
4. `runtime`、`secrets`、`*.sqlite` 等文件不可提交到 Git；此阶段没有自动备份和恢复，**不能保存私有重要记忆用于生产使用**。
5. 不支持将原有 VPS 单实例直接迁入房间；真实迁移必须在备份/恢复机制和身份核对完成后单独实施。
6. 上游软件升级不能直接调用原作者的 updater 覆盖本 Fork；最终必须使用 `MULTI_ROOM_SPEC.md` 要求的受控滚动升级方案。

## 已完成测试与待办

自动化位于 `tests/test_room_manager.py`，并在 `.github/workflows/anthropic-compat.yml` 运行。覆盖新建房间路径与密钥不重复、配置只复制白名单、新库无私人 canary、模型密钥须 opt-in、MCP 权限模板、路径穿越拒绝、虚拟 Docker 无端口和健康状态、CLI 创建及列表。

**仍需实机验证**：用隔离的 Docker 测试 VPS 同时启动两间房间、验证两个核心服务健康、数据及后台任务运行互不干扰。

**下一步按设计规范推进：** M2 统一 Key 鉴权与 Chat/MCP 静态路由；M3 统一管理网页、房间切换、配置与工具开关；随后性能、OAuth、备份、更新和迁移。不要因为已有 M1 CLI 就将项目标记为可生产使用。
