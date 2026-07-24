# Linux 部署手册

本项目使用两个独立服务：Node 提供已构建的前端，FastAPI 提供 API、SSE、下载
和指标。内部与外部模式使用完全相同的固定端口：

| 服务 | 监听地址 | 固定端口 |
|---|---|---:|
| 后端 | `0.0.0.0` | `11451` |
| 前端 | `0.0.0.0` | `11452` |

内部模式不校验访问密钥，只能部署在可信 LAN 或 VPN。外部模式要求页面进入密钥，
并对 API、SSE、下载、就绪探针和 `/metrics` 统一校验
`X-P2H-Access-Key`。`/api/health/live` 保持免密，供服务存活探测使用。

> [!WARNING]
> 项目不提供反向代理或内置 TLS。外部模式使用 HTTP，访问密钥、题包和下载内容
> 在传输中不会加密。不要在不可信网络直接使用；应通过可信 VPN、SSH 隧道或由
> 管理员自行维护的上游 TLS 网关接入。应用端口仍保持 `11451/11452`。

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

## 2. 创建服务用户与目录

```bash
sudo useradd --system --create-home --shell /bin/bash ojconverter
sudo usermod -aG docker ojconverter
sudo install -d -o ojconverter -g ojconverter -m 0755 /opt/oj-package-converter
sudo install -d -o ojconverter -g ojconverter -m 0700 /var/lib/oj-package-converter
sudo install -d -o root -g ojconverter -m 0750 /etc/oj-package-converter
```

重新登录或启动新的用户进程后，确认服务用户可以访问 Docker：

```bash
sudo -u ojconverter -H docker info
```

## 3. 获取源码并运行交互安装

```bash
sudo -u ojconverter -H git clone \
  https://github.com/LMTINSUZHOU/Problem-Change-Center.git \
  /opt/oj-package-converter
cd /opt/oj-package-converter
sudo -u ojconverter -H ./install.sh --interactive \
  --config /tmp/oj-package-converter.env
```

安装器依次询问：

1. 内部部署或外部部署。
2. 是否启用 Wine runner。
3. 拉取预构建镜像、本地构建或跳过 runner。
4. 外部部署的访问主机名/IP 与访问密钥。

推荐选择预构建镜像。只有必须运行题包内 binary-only Windows `.exe` 时才选择
Wine。外部密钥要求 16-256 个无空格可打印 ASCII 字符；安装器只保存
PBKDF2-SHA256 哈希，不把明文密钥写入配置。

国内网络构建 runner 时可使用清华 Debian 源：

```bash
sudo -u ojconverter -H ./install.sh --interactive \
  --config /tmp/oj-package-converter.env \
  --apt-mirror https://mirrors.tuna.tsinghua.edu.cn/debian \
  --apt-security https://mirrors.tuna.tsinghua.edu.cn/debian-security
```

安装完成后，把数据目录改为 systemd 可写的持久目录，再安装配置：

```bash
sudo sed -i \
  's|^P2H_DATA_DIR=.*|P2H_DATA_DIR=/var/lib/oj-package-converter|' \
  /tmp/oj-package-converter.env
sudo install -o root -g ojconverter -m 0640 \
  /tmp/oj-package-converter.env \
  /etc/oj-package-converter/production.env
sudo rm /tmp/oj-package-converter.env
```

检查关键配置：

```text
P2H_DEPLOYMENT_MODE=internal 或 external
P2H_BACKEND_HOST=0.0.0.0
P2H_BACKEND_PORT=11451
P2H_FRONTEND_HOST=0.0.0.0
P2H_FRONTEND_PORT=11452
```

外部模式还必须满足：

```text
P2H_ALLOWED_HOSTS=<访问主机名或 IP>,localhost,127.0.0.1,::1
P2H_ALLOWED_ORIGINS=http://<访问主机名或 IP>:11452
P2H_ACCESS_KEY_HASH='pbkdf2_sha256$...'
```

## 4. 安装并启动 systemd 服务

```bash
sudo install -m 0644 deploy/systemd/oj-package-converter.service \
  /etc/systemd/system/oj-package-converter.service
sudo install -m 0644 deploy/systemd/oj-package-converter-frontend.service \
  /etc/systemd/system/oj-package-converter-frontend.service

sudo systemctl daemon-reload
sudo systemctl enable --now \
  oj-package-converter.service \
  oj-package-converter-frontend.service
```

服务启动前会校验生产配置、数据目录、Docker daemon、runner 镜像和隔离探针。
后端固定单 worker，以保证内存限流器与任务调度状态一致。

## 5. 防火墙与访问

浏览器需要同时访问前端 `11452` 和后端 `11451`。内部部署应把两个端口限制在
可信网段；外部部署则按实际入口网络放行这两个 TCP 端口。例如 UFW：

```bash
# 内部模式示例，只允许可信网段
sudo ufw allow from 192.168.0.0/16 to any port 11451 proto tcp
sudo ufw allow from 192.168.0.0/16 to any port 11452 proto tcp

# 外部模式确需公网直连时
sudo ufw allow 11451/tcp
sudo ufw allow 11452/tcp
```

打开：

```text
http://<服务器地址>:11452
```

外部模式首次进入会要求密钥。密钥只保存在该浏览器标签页的 `sessionStorage`，
不会进入 URL 或 `localStorage`；关闭标签页或点击“锁定”后需要重新输入。

## 6. 健康检查与指标

```bash
# 免密存活探针
curl --fail http://127.0.0.1:11451/api/health/live

# 内部模式
curl --fail http://127.0.0.1:11451/api/health/ready
curl --fail http://127.0.0.1:11451/metrics

# 外部模式
curl --fail -H 'X-P2H-Access-Key: YOUR_ACCESS_KEY' \
  http://127.0.0.1:11451/api/health/ready
curl --fail -H 'X-P2H-Access-Key: YOUR_ACCESS_KEY' \
  http://127.0.0.1:11451/metrics
```

外部模式不会开放 `/docs`、`/redoc` 和 OpenAPI JSON。

## 7. 日志、停止与重启

```bash
sudo systemctl status oj-package-converter.service
sudo systemctl status oj-package-converter-frontend.service
sudo journalctl -u oj-package-converter.service -f
sudo journalctl -u oj-package-converter-frontend.service -f

sudo systemctl restart \
  oj-package-converter.service \
  oj-package-converter-frontend.service
```

任务数据库、上传、日志、报告和产物位于
`/var/lib/oj-package-converter`。不要在服务运行时直接修改 `jobs.sqlite3`。

## 8. 更新

更新前备份生产配置和数据目录，并确认没有运行中的转换：

```bash
sudo systemctl stop \
  oj-package-converter-frontend.service \
  oj-package-converter.service
sudo -u ojconverter -H git -C /opt/oj-package-converter pull --ff-only
cd /opt/oj-package-converter
sudo -u ojconverter -H ./install.sh --non-interactive \
  --skip-runner --config /opt/oj-package-converter/.env
sudo systemctl start \
  oj-package-converter.service \
  oj-package-converter-frontend.service
```

生产环境推荐检出明确版本标签，并把 `P2H_RUNNER_IMAGE` 固定为镜像 digest。
更新后重新执行健康检查并完成一次上传、SSE 进度与下载冒烟测试。

## 9. 故障排查

- `production check failed: Docker daemon is unreachable`：确认 Docker 服务已启动，
  且 `ojconverter` 已加入 `docker` 组。
- `runner image is missing`：重新拉取配置中的 `P2H_RUNNER_IMAGE`，或重新运行安装器。
- 页面能打开但 API 失败：确认浏览器可以访问 `11451`，并检查
  `P2H_ALLOWED_HOSTS` 与 `P2H_ALLOWED_ORIGINS`。
- 密钥始终错误：不要把 PBKDF2 哈希当作登录密钥；用户输入的是安装时设置的明文。
- Wine 在 ARM 主机异常：Wine runner 是 `linux/amd64`，优先使用普通 runner 和
  题包内源码，只在确有 Windows 二进制需求时启用 Wine。
