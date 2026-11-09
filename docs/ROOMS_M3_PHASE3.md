# M3 第三阶段：原版 Dashboard 与多房间 Docker 内网连接（开发验收版）

> 状态：2026-11-09，开发分支 `feature/multi-room`。独立的 Docker 启动命令、可自定义的 Dashboard 登录用户名和密码、隔离/模拟部署测试已写入；**尚未在真实 VPS 的 Docker 网络中实测**，**不要直接覆盖旧版单房服务**。

## 本阶段的最小改动原则

1. **不修改原版 `deploy/compose.yaml`**，不改变原始 memory 服务挂载、不影响既有 `main` 分支与 VPS 的旧服务。
2. 仍然只有**一份原版 React/Vite Dashboard 镜像**供所有房间使用。房间切换按钮放在原版 Sidebar 的导航底部，紧贴「使用说明／花园／设置」上方，颜色与间距保持一致。
3. 网页鉴权仍然采用原版 **`web/server/webAuth.mjs` + `web-auth.json`（scrypt 加盐哈希）**，登录用户名与密码**由用户自己设置**，不再固定为 `admin`。用户登录一次，切换房间不需要第二次输入密码；签名房间 cookie 只决定当前房间，不赋予管理权限。
4. 独立管理员密钥仅留在服务器/控制层，用于 Node → Python 内部管理 API 通信；浏览器无需填写管理员密钥。
5. 管理员 API 继续在 **VPS 宿主机上运行**，而不是在用户可访问的网页容器内暴露 Docker socket。它仅绑定到该安装专属私有 Docker bridge 地址；网页容器通过网桥 IP 访问它。
6. 各房间内的 Scene、Event、日记、梦境、心绪、备忘、原话、索引、备份等独立保存，不因为模型功能配置被复制。网页容器只挂载控制层的路由清单与必要密钥，不挂载任何房间数据库。

## 独立 VPS 开发验证流程

以下操作在**完全独立的 Fork 工作目录和空数据目录**进行。不会导入或改动你 VPS 已经使用的原版记忆库。

```bash
git clone --branch feature/multi-room https://github.com/qiq-Blanchet/Serein-MultiRoom.git
cd Serein-MultiRoom

python3 -m pip install -e '.[test,http,background]'
export ROOM_ROOT=/srv/serein-multiroom-test

# 1. 一次性建立空管理目录、独立管理员内部密钥，以及自定义网页账号
python3 scripts/rooms.py --root "$ROOM_ROOT" init
python3 scripts/rooms.py --root "$ROOM_ROOT" admin-init
python3 scripts/rooms.py --root "$ROOM_ROOT" web-auth
# 上一条会交互询问你想要的 Dashboard 用户名和密码，不显示密码明文。

# 2. 构建共享的 Serein 核心镜像和原版 Dashboard 网页镜像
python3 scripts/rooms.py --root "$ROOM_ROOT" build
python3 scripts/rooms.py --root "$ROOM_ROOT" dashboard-build

# 3. 创建并启动第一间真正空白的测试房间
python3 scripts/rooms.py --root "$ROOM_ROOT" create "第一间测试房" --blank
python3 scripts/rooms.py --root "$ROOM_ROOT" list
python3 scripts/rooms.py --root "$ROOM_ROOT" start <上一步的ROOM_ID>

# 4. 启动统一 Chat/MCP 网关（仅回环端口，不对公网开放）
python3 scripts/rooms.py --root "$ROOM_ROOT" gateway-start

# 5. 读取该安装的私有 Docker bridge IP
python3 scripts/rooms.py --root "$ROOM_ROOT" bridge-address
```

然后在**单独终端**保持管理员服务运行：

```bash
python3 scripts/rooms.py --root "$ROOM_ROOT" admin-serve --docker-network --port 18220
```

最后再在 VPS 的另一终端运行：

```bash
# 开发期预览：只在宿主机回环地址 127.0.0.1:18217 暴露网页
python3 scripts/rooms.py --root "$ROOM_ROOT" dashboard-start --port 18217
python3 scripts/rooms.py --root "$ROOM_ROOT" dashboard-status
```

远程电脑的浏览器可通过 SSH 端口转发访问**开发预览**：

```bash
ssh -L 18217:127.0.0.1:18217 your-user@your-vps
```

在自己的电脑打开 `http://127.0.0.1:18217`，输入 `web-auth` 阶段设置的用户名和密码。进入 Dashboard 后，左侧靠下有「切换房间」按钮；从中创建新房间并启动即可，无需再进入 VPS 修改配置。创建功能允许从首房、指定房间或空白房间建立，复用模型供应商密钥须显式勾选。

正式公网 HTTPS 入口需要**现有或单独配置的 TLS 反向代理**，终止 HTTPS 后回源宿主机 `127.0.0.1:18217`：

```bash
python3 scripts/rooms.py --root "$ROOM_ROOT" dashboard-start \
  --port 18217 --public-origin https://your-serein-domain.example/
```

此命令本身**不申请域名、不配置 Caddy/Nginx、不开放防火墙，也不处理证书**；不能将 18217、18219 或管理员端口 18220 直接公开到互联网。

## 各进程网络关系

```text
浏览器（一个自定义用户名 + 密码）
    |
  HTTPS 反向代理（由部署者配置）
    |
  127.0.0.1:18217
    |
  原版 Node/Vite Dashboard 容器
    |-- React 原版页面 + Sidebar 小按钮
    |-- 直接经 Docker 内网到 room-<id>:8011（内部 Serein Token）
    |-- 经本安装 Docker bridge IP:18220 到宿主机 Python 管理层
    |-- 经 Docker 内网到统一 M2 Chat/MCP 网关
    |
  管理层（VPS 宿主机，私有网桥 IP:18220，独立密钥）
    |-- 创建 / 启停房间的 Docker 编排
    |-- 控制层房间注册表
    |
  各独立 room-<id> memory 容器
    |-- 仅挂载各自的数据库、索引、配置、密钥
    |-- 不向宿主机发布 HTTP 端口
```

## 已自动验证与尚待验证

新增 `tests/test_dashboard_deployment.py` 核验：
- 自定义中英文用户名与原版 scrypt 密码存储兼容，不打印明文密码；
- Dashboard 仅向 `127.0.0.1` 发布宿主机端口；
- 内部管理层实际使用该安装独立 Docker bridge 的 RFC1918 IP，不依赖 `host.docker.internal` 的不稳定默认映射；
- Dashboard 容器只读取控制层路由和内部密钥，不直接挂载私人房间目录；
- 公网来源地址不允许明文 HTTP，开发预览可使用本机回环 HTTP；
- 网页镜像仍由原版 `deploy/Gateway.Dockerfile` 编译，而非复制一套新界面。

当前自动化属于**模拟 Docker 行为+实际原版 Dashboard 前端构建**，还需要真实 VPS 双房独立数据写入/读取、界面切换、设置编辑、后台任务、MCP 请求和长时间重启测试，才能进入生产环境。MCP OAuth、备份/恢复、受控更新仍需后续 M4–M6。

**用户变更控制**：未取得用户明确同意之前，不把原版部署迁移、删除、覆盖或修改既有挂载；不会为了多房间功能缩减原本已有的记忆服务能力。
