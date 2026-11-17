# Linux 部署手册

本项目使用两个独立服务：Node 提供已构建的前端，FastAPI 提供 API、SSE、下载
和指标。端口固定，监听地址二选一：

| 服务 | 默认监听地址 | 内网共享 | 固定端口 |
|---|---|---|---:|
| 后端 | `127.0.0.1` | `0.0.0.0` | `11451` |
| 前端 | `127.0.0.1` | `0.0.0.0` | `11452` |

默认 `127.0.0.1` 只允许本机访问（可配合 SSH 隧道使用）；需要内网团队协作时选择
`0.0.0.0`。应用没有登录或访问密钥，所有 API、SSE、下载、就绪探针和
`/metrics` 对能连上端口的人都开放。

> [!WARNING]
> 项目不提供访问控制、反向代理或内置 TLS，题包和下载内容以 HTTP 明文传输。
> 选择 `0.0.0.0` 时只能把端口开放给可信 LAN 或 VPN，不要暴露到公网。

## 1. 系统要求

- Debian 12+/Ubuntu 22.04+ 或同类 systemd Linux。
- Python 3.10+ 与 `venv`。
- Node.js 20.19+、22.12+ 或 24+。
- 系统 Docker Engine 与 Docker Compose 插件。
- 建议使用独立主机或 VM，并固定 runner 镜像 digest。

后端通过系统 Docker daemon 创建隔离 runner。加入 `docker` 组等价于获得主机
root 权限，因此不要在承载其他敏感业务的共享主机上部署。

Debian/Ubuntu 基础依赖示例：

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv docker.io docker-compose-plugin
sudo systemctl enable --now docker.service
```

Node.js 版本应通过发行版仓库、NodeSource 或组织内部软件源安装。安装后检查：

```bash
python3 --version
node --version
npm --version
docker info
docker compose version
```

## 2. 准备部署用户

服务以执行安装器的用户身份运行。建议使用一个专门的普通部署用户：它需要能访问
Docker，并能通过 `sudo` 写入 systemd 单元（安装器只在这一步使用 `sudo`）。

```bash
sudo useradd --create-home --shell /bin/bash deploy
sudo usermod -aG docker,sudo deploy   # RHEL 系发行版使用 wheel 组
sudo -iu deploy
docker info                            # 重新登录后应能直接访问 Docker
```

不要用 `sudo ./install.sh`：那会让虚拟环境、前端产物和服务都归 root 所有。

## 3. 安装并启用 systemd 服务

```bash
git clone https://github.com/Albert-Li-Sz/Problem-Change-Center.git
cd Problem-Change-Center
./install.sh --systemd
```

交互安装依次询问：

1. 监听地址：`127.0.0.1`（仅本机）或 `0.0.0.0`（内网共享）。
2. 是否启用 Wine runner。
3. 拉取预构建镜像、本地构建或跳过 runner。

非交互安装示例：

```bash
./install.sh --non-interactive --lan --systemd --runner-source prebuilt
```

推荐选择预构建镜像。只有必须运行题包内 binary-only Windows `.exe` 时才选择
Wine。国内网络构建 runner 时可使用清华 Debian 源：

```bash
./install.sh --systemd \
  --apt-mirror https://mirrors.tuna.tsinghua.edu.cn/debian \
  --apt-security https://mirrors.tuna.tsinghua.edu.cn/debian-security
```

安装器根据 `deploy/systemd/*.service.in` 模板生成并启用：

| 服务 | 作用 |
|---|---|
| `oj-package-converter.service` | FastAPI 后端，单 worker，监听 `P2H_BACKEND_HOST:11451` |
| `oj-package-converter-frontend.service` | Node 静态前端，监听 `P2H_FRONTEND_HOST:11452` |

两个服务都以安装用户身份运行，工作目录是当前源码目录，`EnvironmentFile` 指向
安装生成的 `.env`（权限 `0600`），开机自启并在失败时自动重启。后端只对
`P2H_DATA_DIR`（默认 `~/.p2h-web-ui/backend_data`）有写权限。源码目录路径不能包含
空格、引号、`%` 或 `$`。

检查关键配置：

```text
P2H_BACKEND_HOST=127.0.0.1 或 0.0.0.0
P2H_BACKEND_PORT=11451
P2H_FRONTEND_HOST=127.0.0.1 或 0.0.0.0
P2H_FRONTEND_PORT=11452
```

修改 `.env` 后重启两个服务即可生效。也可以运行 `./scripts/production-check.sh`
对配置权限、数据目录、Docker、runner 镜像和隔离探针做一次完整自检。

## 4. 防火墙与访问

浏览器需要同时访问前端 `11452` 和后端 `11451`。监听 `0.0.0.0` 时应把两个端口
限制在可信网段，例如 UFW：

```bash
sudo ufw allow from 192.168.0.0/16 to any port 11451 proto tcp
sudo ufw allow from 192.168.0.0/16 to any port 11452 proto tcp
```

监听 `127.0.0.1` 时无需放行端口，远程访问可使用 SSH 隧道：

```bash
ssh -L 11451:127.0.0.1:11451 -L 11452:127.0.0.1:11452 deploy@server
```

打开：

```text
http://<服务器地址>:11452
```

## 5. 健康检查与指标

```bash
curl --fail http://127.0.0.1:11451/api/health/live
curl --fail http://127.0.0.1:11451/api/health/ready
curl --fail http://127.0.0.1:11451/metrics
```

## 6. 日志、停止与重启

```bash
sudo systemctl status oj-package-converter oj-package-converter-frontend
sudo journalctl -u oj-package-converter -u oj-package-converter-frontend -f
sudo systemctl restart oj-package-converter oj-package-converter-frontend
sudo systemctl stop oj-package-converter oj-package-converter-frontend
```

任务数据库、上传、日志、报告和产物位于 `P2H_DATA_DIR`。不要在服务运行时直接
修改 `jobs.sqlite3`。

## 7. 更新

更新前确认没有运行中的转换，必要时备份 `.env` 和数据目录：

```bash
cd Problem-Change-Center
./install.sh update
```

更新会 `git pull --ff-only`（工作区有未提交修改时拒绝），用新版安装器重装后端
依赖、重新构建前端、按 `P2H_RUNNER_IMAGE` 重新拉取或构建 runner，然后重新生成
systemd 单元并重启服务。`.env` 不会被改写。只想刷新依赖时用
`./install.sh update --no-pull`，不想动 runner 时加 `--skip-runner`。

生产环境推荐检出明确版本标签，并把 `P2H_RUNNER_IMAGE` 固定为镜像 digest。
更新后重新执行健康检查并完成一次上传、SSE 进度与下载冒烟测试。

## 8. 卸载

```bash
./install.sh uninstall                               # 停用并删除服务，删除依赖和前端产物
./install.sh uninstall --purge --remove-images       # 另外删除 .env、全部任务数据和 runner 镜像
```

卸载前会列出将删除的内容并要求确认，非交互环境需加 `--yes`。安装器只会处理
指向当前源码目录的 systemd 服务；源码目录本身不会被删除。

## 9. 故障排查

- 服务反复重启：查看 `sudo journalctl -u oj-package-converter -e`；最常见的是
  Docker 未启动、运行用户不在 `docker` 组，或移动了源码目录（移动后重新执行
  `./install.sh --systemd`）。
- `production check failed: Docker daemon is unreachable`：确认 Docker 服务已启动，
  且运行服务的用户已加入 `docker` 组。
- `runner image is missing`：重新拉取配置中的 `P2H_RUNNER_IMAGE`，或重新运行安装器。
- 队友无法打开页面：确认 `P2H_BACKEND_HOST`/`P2H_FRONTEND_HOST` 为 `0.0.0.0`，
  并且防火墙对内网放行了 `11451` 与 `11452`。
- 页面能打开但 API 失败：确认浏览器可以访问 `11451`，并检查
  `P2H_ALLOWED_HOSTS` 与 `P2H_ALLOWED_ORIGINS`。
- Wine 在 ARM 主机异常：Wine runner 是 `linux/amd64`，优先使用普通 runner 和
  题包内源码，只在确有 Windows 二进制需求时启用 Wine。
