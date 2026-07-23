# 生产部署手册

这套部署面向单机 Linux：Caddy 负责 HTTPS、账号认证、静态前端和请求体上限；
FastAPI 只监听 `127.0.0.1:8000`；转换任务由同一专用用户的 rootless Docker
执行。不要把 Uvicorn、Docker socket 或根 Docker daemon 暴露到公网。

## 安全边界

- Caddy `basic_auth` 是面向用户的第一层认证，只能运行在 HTTPS 上。
- Caddy 覆盖客户端传入的 `X-P2H-Proxy-Secret`，再向后端注入独立随机密钥。
- 后端生产模式校验 Host、代理密钥、Origin/Referer、`Sec-Fetch-Site`、请求速率
  和请求体大小，并为 API 返回 no-store、CSP、HSTS 等安全响应头。
- runner 继续使用无网络、只读根文件系统、最小 capability、PID/内存/CPU/tmpfs
  上限和 `no-new-privileges`。
- 后端固定一个 worker。任务状态和限流器是进程内协调模型，多 worker 会导致重复
  执行和限流不一致。

Docker 官方明确警告 `docker` 组等同 root 级权限；因此模板默认要求 rootless
Docker。若组织选择 rootful Docker，必须把整台主机视为该 Web 服务的安全边界，
并在独立 VM 中运行，不要和其他业务混部。

## 前置条件

- 支持 systemd 和 cgroup v2 的 Linux 主机。
- 一个不与其他业务共享的专用用户 `ojconverter`，固定 UID（示例为 `1001`）；
  禁止 SSH 密码登录，但保留 home 和 rootless Docker 所需的 systemd 用户会话。
- Python 3.12+、Node.js 22+、Caddy 2.10+、Docker Engine rootless。
- 域名已经解析到主机，防火墙仅向公网开放 TCP 80/443；8000 和 Docker socket
  不开放。
- `/opt/oj-package-converter` 是经过审核的发布目录，由 root 拥有且服务用户只读。

Caddy 2.10 是硬要求，因为 `request_body max_size` 从该版本开始提供。Caddy 的
`basic_auth` 只接受哈希密码，不能填写明文。

## 1. 安装 rootless Docker

以下命令以 `ojconverter` 用户执行：

```bash
dockerd-rootless-setuptool.sh install
systemctl --user enable --now docker.service
sudo loginctl enable-linger ojconverter
id -u
docker info
```

`docker info` 的 `Security Options` 必须包含 `rootless`，并确认 `Cgroup Driver`
为 `systemd`，否则容器 CPU、内存和 PID 限制可能不会全部生效。把实际 UID 写入
`DOCKER_HOST=unix:///run/user/<UID>/docker.sock`。

## 2. 准备发布目录

从一个干净、已验证的提交构建，不要把开发机的 `.env`、任务数据或虚拟环境复制
进发布包：

```bash
cd /opt/oj-package-converter
python3 -m venv backend/.venv
backend/.venv/bin/pip install --requirement backend/requirements.txt
npm --prefix frontend ci
npm --prefix frontend run build
sudo -u ojconverter env DOCKER_HOST=unix:///run/user/1001/docker.sock \
  docker compose --profile runner build runner
sudo chown -R root:root /opt/oj-package-converter
sudo chmod -R o-w /opt/oj-package-converter
```

发布前记录源码提交 SHA、runner 镜像 ID 和依赖审计结果。生产升级必须重新运行
后端测试、前端构建、隔离探针和镜像扫描。

## 3. 生成配置与密钥

```bash
sudo install -d -m 0750 -o root -g ojconverter /etc/oj-package-converter
sudo install -m 0640 -o root -g ojconverter \
  deploy/production.env.example \
  /etc/oj-package-converter/production.env
openssl rand -hex 32
caddy hash-password
```

编辑配置并完成这些替换：

- `P2H_SITE_ADDRESS`、`P2H_ALLOWED_HOSTS` 是实际域名。
- `P2H_ALLOWED_ORIGINS` 是带 `https://` 的准确 origin，不含路径。
- `P2H_TRUSTED_PROXY_SECRET` 使用 `openssl rand -hex 32` 的输出。
- `P2H_ADMIN_PASSWORD_HASH` 使用 `caddy hash-password` 的输出；原始密码不落盘。
- `DOCKER_HOST` 使用专用用户的实际 UID。
- `P2H_FRONTEND_ROOT` 指向只读的 `frontend/dist`。

代理密钥与登录密码必须不同。禁止提交 `production.env`、访问日志、上传包或备份
到 Git。轮换密码或代理密钥后，要先验证配置，再原子替换环境文件并同时重启
Caddy 与后端，避免两端短暂不一致。

## 4. 安装服务

```bash
sudo install -m 0644 deploy/systemd/oj-package-converter.service \
  /etc/systemd/system/oj-package-converter.service
sudo install -d -m 0755 /etc/systemd/system/caddy.service.d
sudo install -m 0644 deploy/systemd/caddy-oj-package-converter.conf \
  /etc/systemd/system/caddy.service.d/oj-package-converter.conf
sudo install -m 0644 deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl daemon-reload
```

先以服务用户执行预检：

```bash
sudo -u ojconverter \
  /opt/oj-package-converter/scripts/production-check.sh \
  --backend-only /etc/oj-package-converter/production.env
sudo env --chdir=/opt/oj-package-converter \
  /opt/oj-package-converter/scripts/production-check.sh \
  /etc/oj-package-converter/production.env
```

第二条命令还验证 Caddy 和前端构建。通过后启用服务：

后端预检会实际启动一次隔离探针容器，并要求 rootless Docker 使用 systemd
cgroup driver；这不是只检查镜像标签。任何 capability、namespace、挂载或资源
限制不兼容都会在服务启动前失败。

```bash
sudo systemctl enable --now oj-package-converter.service
sudo systemctl enable --now caddy.service
```

## 5. 验证

后端存活探针不需要代理密钥，但只应从本机访问：

```bash
curl --fail http://127.0.0.1:8000/api/health/live
```

生产就绪探针必须经过 Caddy 认证，它会检查数据目录、Docker daemon 和 runner
镜像：

```bash
curl --fail --user admin https://convert.example.com/api/health/ready
```

还要验证以下负向场景：

```bash
# 绕过 Caddy 必须为 403
curl -i -H 'Host: convert.example.com' http://127.0.0.1:8000/api/health

# 错误 Host 必须为 400
curl -i -H 'Host: attacker.example' http://127.0.0.1:8000/api/health/live

# 公网 8000 必须不可达
nc -vz convert.example.com 8000
```

最后上传一个最小题包，完成转换、下载、取消和删除测试；检查失败任务没有残留
容器或半成品：

```bash
sudo -u ojconverter env DOCKER_HOST=unix:///run/user/1001/docker.sock \
  docker ps --filter label=app=p2h-web-ui
```

## 监控与告警

- 每 30 秒检查 `/api/health/live`，每 60 秒通过认证检查 `/api/health/ready`。
- 告警条件：连续 3 次 not_ready、HTTP 5xx、磁盘可用空间低于上传上限的两倍、
  任务超时率突增、runner 容器超过配置并发数、服务频繁重启。
- 后端日志使用 journald：`journalctl -u oj-package-converter -f`。
- Caddy JSON 访问日志默认写入 `/var/log/oj-package-converter/access.json`，
  100 MiB 轮转，保留 10 个或 30 天。
- 不记录上传内容、代理密钥或登录密码。向外部日志平台发送前，按组织政策处理
  IP、任务 ID 和文件名。

## 备份、升级与回滚

任务目录包含用户上传的题包，可能涉及隐私或竞赛保密内容。备份必须加密，限制
访问并设置明确的销毁周期。为了获得一致备份：

```bash
sudo systemctl stop oj-package-converter
sudo tar --xattrs --acls -C /var/lib \
  -czf /secure-backup/oj-package-converter-$(date +%F).tar.gz \
  oj-package-converter
sudo systemctl start oj-package-converter
```

每次升级保留上一版只读发布目录与 runner 镜像 ID。升级顺序是：构建新版本、
完整测试、停止后端、备份、切换 `/opt/oj-package-converter`、运行生产预检、启动
后端、验证真实转换。回滚时停止后端，切回上一目录和 runner 镜像，再启动并验证。
不要在两个版本之间同时运行后端。

## 事件处理

怀疑凭据或主机失陷时：

1. 从负载均衡或防火墙摘除主机并停止 Caddy/后端。
2. 保留 journald、Caddy 日志、任务元数据和容器事件作为只读证据。
3. 在干净主机轮换登录密码、代理密钥和 TLS/备份凭据。
4. 从已验证提交和镜像重建，不在可疑主机上“原地清理”后恢复服务。
5. 按适用规则通知受影响题包所有者，并复盘上传内容和下载访问范围。

官方参考：

- [Docker Rootless mode](https://docs.docker.com/engine/security/rootless/)
- [Docker rootless systemd 与资源限制](https://docs.docker.com/engine/security/rootless/tips/)
- [Caddy basic_auth](https://caddyserver.com/docs/caddyfile/directives/basic_auth)
- [Caddy request_body](https://caddyserver.com/docs/caddyfile/directives/request_body)
