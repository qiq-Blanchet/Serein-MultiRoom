# M5 第三阶段：同房间受控恢复与 Gateway Key 稳定

> 2026-11-10，开发分支 `feature/multi-room`。**仅合成数据/模拟 Docker 自动测试，尚未在真实 VPS、真实数据库或正式 HTTPS 双房部署中验收**。原版 `main`、原版单房 Compose、真实 VPS 数据均未改动。

## 你关心的三种情况

1. **同一 VPS 重启**：系统不重新创建房间和 Gateway Key。Key 在每房 `secrets/gateway-key`，与 SQLite 等数据一起保留在宿主机；房间、统一网关和 Dashboard 新 Docker 容器均配置 `--restart unless-stopped`，因此已经启动过而非手动停止的容器通常会在 Docker daemon 恢复后自动启动。重启并不代表永远健康：Docker、磁盘、代理或服务进程异常需要检查。**宿主机独立 `admin-serve` 仍需一次性配置 systemd/其他 supervisor 才可随系统恢复**，否则网页管理操作会离线，但 Key 不变。
2. **同一 VPS 将备份恢复到现有房间**：同一 `xiaogi-main`，仅恢复该房间的 `config/`、`data/`；`secrets/`（Gateway Key / 内部 API Token）完全保留。若本房原本运行，先停本房 → 生成当前状态的回滚归档 → 同步恢复文件 → 重建**本房**容器并做健康检查 → 再开本房。其他 AI 不停止、不迁移、不换 Key。
3. **VPS 重装／换服务器**：新建同样的自定义房间 ID（`xiaogi-main`），此时内部 UUID 和 Gateway Key 可以是新值。上传旧房间 v2 归档后以自定义房间 ID 匹配，不要求旧 UUID、旧 Key。恢复后继续使用**新服务器那把已生成的 Gateway Key**，不会每次恢复又随机生成一把。注意：只有重新创建了房间才会生成新 Key；如果你还希望 Operit 完全不改 Key，必须单独保存和安全迁移原连接凭据，这与普通记忆备份分开管理。

## 原版 Dashboard 操作

进入原版「设置 → 旧库迁移 → 当前房间完整归档」：

- 「创建当前房间完整备份」及下载仍按 M5 第二阶段工作；
- 「导入已有的整房备份」选择 `.tar.gz`：**只上传到当前房间私人 `backups/` 并验证房间 ID、路径、逐文件哈希；不会立即恢复数据库，也不接纳不同房间 ID 的归档**。文件名由服务器随机生成，浏览器文件名不当作磁盘路径；
- 「校验／预览」给出可读房间 ID、来源旧内部 UUID、SHA-256、SQLite 结构兼容性；
- 只有兼容时出现「恢复到当前房间」按钮。操作前要求输入准确的自定义房间 ID 和再次确认；服务端重新检查**当前已登录选中的房间**、提交的 SHA-256、归档内容、当前 SQLite 结构一致性。新旧 Gateway Key 不参与校验；
- 恢复成功后显示本次自动创建的**恢复前回滚备份**文件名。新生成的 Gateway Key 和管理员登录凭据保持不变；
- 若当前备份与现有数据库结构不兼容，**禁止自动覆盖**，须走跨版本迁移/升级流程。老 v1 密钥归档可预览，但不会写入其旧 `secrets/`。

## 安全／故障边界

- 恢复前预检不改数据，SHA-256 防传输损坏，不被误称为密码学“来源证明”；同名房间 ID 本身也不是鉴权凭据——网页仍需管理员登录。
- 恢复期间持有**本房独占锁**，阻止同房重复备份/恢复；不锁其他 AI。
- 源归档逐项只允许恢复 `config/` 和 `data/`，不按 tar 的成员路径直接展开到活动目录；先解到本房私有临时目录，并检查安全路径、SHA-256、SQLite 完整性和总大小。上传只接受合法 gzip，压缩内容验证通过才进入本房备份列表。
- 执行前暂停本房并创建**恢复前归档**。通过当前房间目录内的旧目录交换和恢复日志记录，失败可回到旧版本。即使 VPS 在交换文件时掉电，之后的普通 `room start` 会被日志拦住，不会用半恢复的数据启动；先用 `restore-recover` 恢复旧数据，再显式启动。
- 备份/恢复不会删除其他房间、修改全局管理凭据、Gateway Key 或 Chat/MCP 静态 Bearer 身份。
- **本阶段的断电回滚、Docker 重建和上传仍为自动化／合成测试，没有完成真实 VPS 破坏性恢复演练**。正式用户数据迁移前必须独立验证路径权限、磁盘空间、反向代理上传限制、Docker 健康检查及回滚演练。

## 仅供故障时的 VPS 维护命令

```bash
# 在本安装的私有目录中运行，不指向正在使用的原版服务
ROOT=/srv/serein-multiroom-test
python3 scripts/rooms.py --root "$ROOT" list
python3 scripts/rooms.py --root "$ROOT" backups <本安装内部UUID>
python3 scripts/rooms.py --root "$ROOT" restore-plan <本安装内部UUID> <备份文件名>
python3 scripts/rooms.py --root "$ROOT" restore <本安装内部UUID> <备份文件名> \
    --confirm-room-id xiaogi-main --archive-sha256 <restore-plan返回的SHA256>
# 只在发现恢复中断日志、房间已停机时使用
python3 scripts/rooms.py --root "$ROOT" restore-recover <本安装内部UUID>
```

这几条命令是备选故障方案，平常只用 Dashboard。

## 仍未完成

真实 VPS 双房并行的恢复演练、所有原版页面/后台任务恢复后的逐项核验、跨 schema 的受控升级迁移、批量房间统一归档和加密备份存放。**不应把 CI 通过误解为可以无风险地直接迁移唯一一份私人记忆**。
