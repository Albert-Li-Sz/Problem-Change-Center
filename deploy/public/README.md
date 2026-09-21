# 公益公众版：单机 Docker 部署

邀请码 + 邮箱验证的免费公众站，由 Caddy、API、PostgreSQL、Worker 和临时 Docker runner 组成。API 不挂载 Docker Socket，Worker 无法通过数据库账号读取密码、邮件和会话。使用专用 Linux x86_64 主机，建议 24 vCPU、64 GiB 内存和 500 GiB 磁盘。全局 10 并发，其中 Wine 最多 3 并发。

公众代码使用非 root、默认 seccomp/AppArmor、禁网、只读根目录、零 capability、内存/PID/CPU 限额的普通 Docker。Docker 与宿主共享内核，本方案按项目决定接受该隔离边界；主机不应存放其他业务或无关凭据。

## 首次安装

安装 Docker Engine、Compose v2、Git、Python 3。域名 A/AAAA 记录指向服务器，开放 80/443；不开放数据库、API 或 Docker TCP 端口。

```bash
git clone --branch public https://github.com/Albert-Li-Sz/Problem-Change-Center.git
cd Problem-Change-Center
python3 scripts/public-config.py
sudo install -d -o 10001 -g 10001 -m 0700 /srv/p2h/data
```

编辑 `.env.public`：填写 `PUBLIC_DOMAIN`（纯域名）、联系邮箱、SMTP、Cloudflare Turnstile site/secret key。生成器分别生成数据库 owner、API、Worker 密码，不覆盖已有文件、不输出密码。SMTP 支持 587 STARTTLS 或 465 TLS。Turnstile 配置的域名须与站点相同。

先构建转换基础镜像，再构建公众版包装镜像：

```bash
docker compose --profile wine build runner runner-wine
docker compose --env-file .env.public -f docker-compose.public.yml --profile build-runners build runner runner-wine
docker compose --env-file .env.public -f docker-compose.public.yml build api worker caddy
docker compose --env-file .env.public -f docker-compose.public.yml up -d
```

`PUBLIC_DATA_DIR` 容器内路径必须与宿主绝对路径相同，目录归 UID/GID 10001。`DOCKER_GID` 与 `/var/run/docker.sock` 所属组一致。若 `172.30.0.0/24` 网络冲突，应同时调整 Compose 网络和 API 的 `--forwarded-allow-ips`，仅信任 Caddy 地址。

迁移服务使用 owner 账号运行编号 SQL migration 并授权；日常服务不使用 owner 账号。只有 Caddy 发布宿主端口。

## 管理员与邀请码

```bash
docker compose --env-file .env.public -f docker-compose.public.yml run --rm api \
  python -m app.public.cli admin admin@example.org
```

交互输入至少 12 字符的密码，登录后在管理后台生成邀请码。邀请码单次使用、7 天有效，明文仅创建时展示，可导出。注册时消耗邀请码，邮箱验证后自动开通。未验证账号 48 小时后清理，已使用邀请码不恢复。无默认管理员密码，无支付或收费功能。

也可通过 CLI 生成邀请码：

```bash
docker compose --env-file .env.public -f docker-compose.public.yml run --rm api \
  python -m app.public.cli invite --count 10
```

管理员可调整每日分钟数、封禁用户、终止任务、暂停注册和任务领取。暂停队列不会终止运行中任务。

## 额度与数据

- 每人最多运行 2 个、排队 5 个、同时上传 2 个任务，最多保留 20 个任务。全站同时接收 2 个上传，解析请求体之前预占名额，避免临时文件挤满内存。
- 每日 120 分钟，UTC 00:00 重置；检查预留 1 分钟，转换预留 30 分钟。实际运行时间（含失败）向上取整，结束后退回未用额度；排队取消不消耗额度，跨午夜结算回提交日。这仅用于公平分配资源，不涉及收费。
- 上传最大 512 MiB；检查最长 60 秒，转换最长 30 分钟；输出最大 1 GiB，日志最大 10 MiB。
- 输入、补充文件、报告、日志、结果保留至任务结束后 24 小时；未开始任务自创建起保留 24 小时，未完成上传 1 小时后清理。
- Worker 每 15 秒清理到期/删除任务；删除运行任务先终止容器。注销立即撤销会话，文件删除后移除账号；安全审计保留 30 天。
- 磁盘达到 80% 拒绝上传，90% 停止领取任务；后台显示磁盘和 Worker 心跳。

压缩包检查在 runner 中完成，API 只保存原始字节和摘要。任务只读挂载自身输入，不持有宿主写挂载。输出存入有界 tmpfs；结束后只读导出程序终止残留子进程，通过 tar 流传回 Worker。Worker 拒绝链接、特殊文件、重复路径、路径穿越和超限数据，再生成下载包。

队列、配额、租约存入 PostgreSQL。API 重启不丢任务。Worker 超过 30 秒不续租的任务被终止并标记失败，不自动重跑用户程序。用户可重新上传。

Wine runner 额外挂载专属 `/run/user/10001` tmpfs，规避 Debian Wine 10 在缺失运行目录且设置 `TMPDIR` 时的初始化崩溃（[Debian #1110936](https://bugs.debian.org/1110936)）。Wine prefix 的 tmpfs 上限为 2 GiB，仍计入单个 Wine 任务的 4 GiB 总内存限制。保持默认 seccomp/AppArmor，无需增加 capability 或使用 `unconfined`。

## 升级与备份

升级前暂停队列，等待任务结束并备份数据库。更新代码、重建镜像，然后执行 `up -d --force-recreate migrate api worker caddy`。不要删除 PostgreSQL volume；先在测试数据库验证迁移和恢复。

安装 `age`，在离线电脑生成密钥对；将公钥配置到服务器 `/etc/p2h-backup.env`：`PUBLIC_BACKUP_RECIPIENT=age1...`，私钥不放服务器。默认项目安装目录为 `/opt/Problem-Change-Center`，否则修改 unit 路径。

```bash
sudo install -m 0644 deploy/public/p2h-backup.service deploy/public/p2h-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now p2h-backup.timer
sudo systemctl start p2h-backup.service
```

数据库每日加密备份至 `/srv/p2h/backups`，保留 7 天；配置与凭据另行加密备份，题包/结果不备份。云盘每日加密快照和 7 天保留需在云厂商控制台设置，代码不创建云资源。

先在离线电脑用 `age -d -i PRIVATE_KEY BACKUP.dump.age > recovered.dump` 解密，再在全新测试数据库用 `pg_restore --no-owner --no-acl` 验证，由 migrate 服务重建角色授权。正式恢复后清空 `sessions`、`auth_tokens` 并取消遗留任务，避免恢复旧登录状态。

## 本地验证

```bash
uv pip install --python backend/.venv/bin/python --require-hashes -r backend/requirements-public.lock
PUBLIC_TEST_DATABASE_URL=postgresql://USER:PASSWORD@127.0.0.1:5432/TEST_DB \
  backend/.venv/bin/python -m pytest backend/tests/test_public.py -q
```

测试在指定数据库中创建和销毁随机 schema。设置 `PUBLIC_TEST_DOCKER=1` 可运行真实 Docker 测试，需先构建公众版 runner。

本机调试可设置 `PUBLIC_DEVELOPMENT=1`，跳过 CAPTCHA，并将邮件链接写入开发目录 `dev-mail/`；生产 Compose 不传递该开关。前端构建使用 `VITE_PUBLIC_MODE=1`；API 入口是 `app.public.main:create_app --factory`，Worker 是 `python -m app.public.worker`。

## 运维

```bash
docker compose --env-file .env.public -f docker-compose.public.yml ps
docker compose --env-file .env.public -f docker-compose.public.yml logs --tail=100 api worker
curl --fail https://YOUR_DOMAIN/api/health/live
```

存活检查免登录；就绪检查和后台要求管理员登录。验证邮件、CAPTCHA、注册、找回密码、上传、SSE、转换、下载、取消和清理后，先发放 10 个邀请码运行 72 小时，再扩大规模。

参考：[Docker 安全边界](https://docs.docker.com/engine/security/)、[PostgreSQL 队列锁](https://www.postgresql.org/docs/current/sql-select.html)、[Turnstile 服务端验证](https://developers.cloudflare.com/turnstile/get-started/server-side-validation/)。
