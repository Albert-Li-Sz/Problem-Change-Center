# 公益公众版：单机 Docker 部署

邀请码 + 邮箱验证的免费公众站，由 Caddy（含 cerberus 工作量证明）、API、PostgreSQL、Worker 和临时 Docker runner 组成。API 不挂载 Docker Socket，Worker 无法通过数据库账号读取密码、邮件和会话。

公众代码使用非 root、默认 seccomp/AppArmor、禁网、只读根目录、零 capability、内存/PID/CPU 限额的普通 Docker。Docker 与宿主共享内核，本方案按项目决定接受该隔离边界；主机不应存放其他业务或无关凭据。Worker 挂载 Docker Socket，等同宿主 root，请使用专用主机。

## 一条命令

准备一台专用 Linux x86_64 主机，安装 Docker Engine（29.4.3 或更新）、Compose v2、Git 和 Python 3。域名 A/AAAA 记录指向服务器，只开放 80/443。

```bash
git clone --branch public https://github.com/Albert-Li-Sz/Problem-Change-Center.git
cd Problem-Change-Center
./public.sh
```

不带参数会显示运维菜单；首次运行进入安装向导。向导依次询问资源档位、域名、发信、人机验证、安全选项、镜像来源和备份公钥，写入权限为 0600 的 `.env.public`，检查环境，构建或拉取镜像，启动服务，并可顺带创建管理员和每日备份定时器。数据库密码自动生成且不显示。

| 命令 | 作用 |
|---|---|
| `./public.sh install` | 安装，或重新运行以修改配置（回车保留现值） |
| `./public.sh upgrade` | 备份 → `git pull` → 重建镜像 → 暂停队列并等待任务结束 → 重启 → 恢复队列 |
| `./public.sh backup` | 加密备份数据库与 `.env.public` |
| `./public.sh timer` | 安装每日自动备份的 systemd 定时器 |
| `./public.sh status` / `doctor` | 容器与健康状态 / 主机环境检查 |
| `./public.sh admin EMAIL` | 创建或重置管理员（交互输入 12 位以上密码） |
| `./public.sh invite [N]` | 生成邀请码 |
| `./public.sh logs [服务]` | 查看日志 |

`install --yes` 与 `upgrade --yes` 不提问，直接使用现有 `.env.public`，可用于自动化。`upgrade --no-pull` 只重建当前代码，`--no-backup` 跳过升级前备份。

## 资源档位与 8 GiB 方案

单个任务的内存上限包含它的 tmpfs（工作目录、输出目录和 HOME），所以内存是主要成本。向导按主机内存给出三档，也可以选“自定义”后手工修改 `.env.public`。

| 设置 | 8 GiB（small） | 16 GiB（medium） | 64 GiB（large） |
|---|---:|---:|---:|
| 并行任务 / 其中 Wine | 2 / 1 | 3 / 1 | 10 / 3 |
| 普通任务 / Wine 任务内存 | 2g / 3g | 3g / 4g | 4g / 6g |
| 任务可用交换空间 | 2g | 0 | 0 |
| 工作 / 输出 / HOME tmpfs | 1g / 512m / 512m | 2g / 1g / 1g | 2g / 1g / 1g |
| 上传上限 | 256 MiB | 512 MiB | 512 MiB |
| 解压后上限 | 512 MiB | 1 GiB | 1 GiB |
| API / Worker / PostgreSQL / Caddy 上限 | 1g / 768m / 384m / 192m | 2g / 1g / 1g / 256m | 3g / 4g / 2g / 512m |
| 各项上限合计 | 约 7.3 GiB | 约 14.3 GiB | 约 55.5 GiB |

8 GiB 档的要点：

- 同时最多 1 个 Wine 转换加 1 个普通任务（检查或非 Polygon 转换），其余排队。队列、额度和公平调度不变。
- 上限合计接近物理内存，所以**请为主机创建 4 GiB 以上的 swap**。任务的 `--memory-swap` 比内存上限多 2g，tmpfs 中的大文件可以换出，而不是直接触发 OOM。没有 swap 时站点仍能运行，但接近上限的题包会失败并提示“任务超出内存上限”。
- 3g 的 Wine 上限对大型 Polygon 题包是否足够尚未用真实大题包验证；如果常见任务被 OOM 终止，优先把 `PUBLIC_MAX_CONCURRENT` 降到 1，再把 `PUBLIC_WINE_MEMORY` 提到 4g。
- 首次构建 Wine 镜像需要较多内存和时间；也可以选择拉取 CI 发布的预构建镜像。

创建 swap 的示例：

```bash
sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

可调项都在 `.env.public` 中：`PUBLIC_MAX_CONCURRENT`、`PUBLIC_MAX_WINE`、`PUBLIC_JOB_MEMORY`、`PUBLIC_WINE_MEMORY`、`PUBLIC_JOB_SWAP`、`PUBLIC_JOB_CPUS`、`PUBLIC_WORK_SIZE`、`PUBLIC_OUTPUT_SIZE`、`PUBLIC_HOME_SIZE`、`PUBLIC_WINE_HOME_SIZE`、`PUBLIC_MAX_UPLOAD_MB`（需同步调整代理的 `PUBLIC_MAX_BODY`，比上传上限多 16MiB）、`PUBLIC_API_WORKERS` 以及各服务的 `PUBLIC_*_MEMORY`。修改后执行 `./public.sh upgrade --no-pull` 生效。手工修改过的数值在升级时保留；重新运行 `install` 并选择某一档位会恢复该档默认值。

## 入口防护：cerberus

Caddy 镜像内置 [cerberus](https://github.com/sjtug/cerberus) 插件（版本固定，构建时由 Go 校验和数据库验证）：

- 访问首页，以及匿名的登录、注册、重发邮件、找回和重置密码接口之前，浏览器需完成一次工作量证明。默认难度 10，约一百万次哈希，通常不到一秒。一次通过在 1 小时内覆盖 32 次此类请求；用完后前端自动刷新页面重新验证。
- 同一地址累计 128 次未完成的挑战会被封禁 1 小时，封禁对全站生效。IPv4 按单个地址、IPv6 按 /64 计。
- 已登录的上传、下载、事件流等接口不做挑战，只拒绝已封禁地址。
- `CERBERUS_DIFFICULTY` 每加 1，计算量变为 4 倍；`CERBERUS_BLOCK_TTL` 调整封禁时长。

因为登录接口受工作量证明保护，脚本无法直接调用登录接口；监控请使用下文的健康检查地址。

Cloudflare Turnstile 改为可选：`PUBLIC_CAPTCHA=off`（向导默认）时只依靠工作量证明和既有的 IP/邮箱限流，不依赖任何外部服务；`PUBLIC_CAPTCHA=turnstile` 时在其上叠加 Turnstile，需填写 site/secret key，且域名与站点一致。

## 安全选项

- **管理接口白名单**：`PUBLIC_ADMIN_ALLOW` 填写以空格分隔的 CIDR 后，`/api/admin/*` 只接受这些来源，其他地址返回 403。默认不限制。
- **任务运行时**：`PUBLIC_RUNNER_RUNTIME` 默认 `runc`。主机安装并注册 [gVisor](https://gvisor.dev/docs/user_guide/install/) 后可设为 `runsc`，任务改由用户态内核承接系统调用。Wine 在 gVisor 下的兼容性未经验证，启用前请先用 Polygon 题包测试。
- **容器加固**：Caddy 与 PostgreSQL 也使用只读根文件系统、`no-new-privileges` 和最小 capability，所有服务的日志按 10 MiB × 3 轮转。
- **访问日志**：Caddy 以 JSON 输出访问日志到容器日志（`./public.sh logs caddy`），Cookie 与 Authorization 头自动脱敏，一次性令牌只出现在 URL 片段中，不会进入日志。
- **镜像固定**：基础镜像按 digest 固定，Dependabot 每周检查更新。
- **网络**：只有 Caddy 发布宿主端口。若 `172.30.0.0/24` 与现有网络冲突，应同时调整 Compose 网络和 API 的 `--forwarded-allow-ips`，仅信任 Caddy 地址。

不要给站点套 Cloudflare 代理（橙云）：免费版单请求上限 100 MB，且所有请求会来自 Cloudflare 的地址，按 IP 的限流和 cerberus 封禁都会失效。

## 镜像来源

- **本机构建**（默认）：依次构建转换基础镜像、公众版任务镜像和站点镜像。向导可选国内镜像源（清华 apt/PyPI、npmmirror、goproxy.cn）。
- **拉取预构建镜像**：`public` 分支每次推送通过验收后，CI 向 `ghcr.io/albert-li-sz/p2h-public-*` 发布 `:public` 标签。该流程在首次推送后才会产生镜像，使用前请确认包已发布且可见。

`PUBLIC_DATA_DIR` 容器内路径必须与宿主绝对路径相同，目录归 UID/GID 10001。`DOCKER_GID` 由脚本按 `/var/run/docker.sock` 的属组填写。

## 管理员与邀请码

```bash
./public.sh admin admin@example.org
./public.sh invite 10
```

登录后也可在管理后台生成邀请码。邀请码单次使用、7 天有效，明文仅创建时展示，可导出。注册时消耗邀请码，邮箱验证后自动开通。未验证账号 48 小时后清理，已使用邀请码不恢复。无默认管理员密码，无支付或收费功能。

管理员可调整每日分钟数、封禁用户、终止任务、暂停注册和任务领取。暂停队列不会终止运行中任务。

## 额度与数据

- 每人最多运行 2 个、排队 5 个、同时上传 1 个任务，最多保留 20 个任务。全站同时接收 2 个上传，解析请求体之前预占名额，避免临时文件挤满内存。
- 每日 120 分钟，UTC 00:00 重置；检查预留 1 分钟，转换预留 30 分钟。实际运行时间（含失败）向上取整，结束后退回未用额度；排队取消不消耗额度，跨午夜结算回提交日。这仅用于公平分配资源，不涉及收费。
- 上传上限随档位为 256 或 512 MiB，单次上传须在 30 分钟内完成（512 MiB 约需 2.3 Mbps 上行）；检查最长 60 秒，转换最长 30 分钟；日志最大 10 MiB。
- 输入、补充文件、报告、日志、结果保留至任务结束后 24 小时；未开始任务自创建起保留 24 小时，未完成上传 1 小时后清理。
- Worker 每 15 秒清理到期/删除任务；删除运行任务先终止容器。注销立即撤销会话，文件删除后移除账号；安全审计保留 30 天。
- 磁盘达到 80% 拒绝上传，90% 停止领取任务；后台显示磁盘和 Worker 心跳。

压缩包检查在 runner 中完成，API 只保存原始字节和摘要。任务只读挂载自身输入，不持有宿主写挂载。输出存入有界 tmpfs；结束后只读导出程序终止残留子进程，通过 tar 流传回 Worker。Worker 拒绝链接、特殊文件、重复路径、路径穿越和超限数据，再生成下载包。

队列、配额、租约存入 PostgreSQL。API 重启不丢任务。Worker 超过 30 秒不续租的任务被终止并标记失败，不自动重跑用户程序。用户可重新上传。

Wine runner 额外挂载专属 `/run/user/10001` tmpfs，规避 Debian Wine 10 在缺失运行目录且设置 `TMPDIR` 时的初始化崩溃（[Debian #1110936](https://bugs.debian.org/1110936)）。保持默认 seccomp/AppArmor，无需增加 capability 或使用 `unconfined`。

Wine 部署请使用 Docker Engine 29.4.3 或更新版本。29.4.2 的默认 seccomp 配置会阻断 32 位程序使用的 `socketcall`，导致 Wine 报 `socket: Function not implemented`；`./public.sh doctor` 会检查这一点。参见 [Docker 29.4.3 修复记录](https://docs.docker.com/engine/release-notes/29/#2943)。

## 升级

```bash
./public.sh upgrade
```

流程：配置了备份公钥时先备份数据库；`git pull --ff-only`（工作区有未提交修改时拒绝）；用新代码重建镜像；暂停任务领取并最多等待 35 分钟让运行中的任务结束；重建 migrate、api、worker、caddy（PostgreSQL 不重启）；健康检查通过后恢复队列。升级中断或失败时队列同样会恢复。升级不会删除 PostgreSQL volume。

升级后请登录站点做一次上传、转换、下载的冒烟测试。跨版本升级前建议先在测试数据库验证迁移和恢复。

## 备份与恢复

在离线电脑安装 [age](https://github.com/FiloSottile/age) 并运行 `age-keygen` 生成密钥对。服务器只保存公钥（向导中填写，或写入 `.env.public` 的 `PUBLIC_BACKUP_RECIPIENT=age1...`），私钥不放服务器。服务器同样需要安装 `age`。

```bash
./public.sh backup   # 立即备份
./public.sh timer    # 每日 03:20 UTC 自动备份
```

每次生成 `p2h-时间.dump.age`（数据库）和 `p2h-时间.env.age`（配置与凭据），默认保存在 `/srv/p2h/backups`，保留 7 天（`PUBLIC_BACKUP_DIR`、`PUBLIC_BACKUP_KEEP_DAYS` 可调）。题包与结果不备份。备份文件应再同步到其他机器；云盘快照需在云厂商控制台设置。`backup --plain` 生成未加密备份，其中包含邮箱与密码哈希，仅用于临时迁移。

恢复：先在离线电脑用 `age -d -i PRIVATE_KEY BACKUP.dump.age > recovered.dump` 解密，再在全新测试数据库用 `pg_restore --no-owner --no-acl` 验证，由 migrate 服务重建角色授权。正式恢复后清空 `sessions`、`auth_tokens` 并取消遗留任务，避免恢复旧登录状态。

## 监控

```bash
curl --fail https://YOUR_DOMAIN/api/health/live     # 进程存活，免登录
curl --fail https://YOUR_DOMAIN/api/health/status   # 站点可用，免登录
```

`/api/health/status` 在 Worker 心跳超过 30 秒、磁盘达到 90% 或数据库不可达时返回 503，正常时返回 200；响应只有 `ok` 或 `degraded`，不暴露内部细节，适合接入外部可用性监控。带详情的 `/api/health/ready` 和后台仍要求管理员登录。

验证邮件、注册、找回密码、上传、SSE、转换、下载、取消和清理后，先发放 10 个邀请码运行 72 小时，再扩大规模。

## 本地验证

公众版的本地 Python 环境要求 3.11 或更新版本。生产 Docker 镜像已使用 Python 3.14；内网工具仍支持 Python 3.10+。

```bash
uv pip install --python backend/.venv/bin/python --require-hashes -r backend/requirements-public.lock
PUBLIC_TEST_DATABASE_URL=postgresql://USER:PASSWORD@127.0.0.1:5432/TEST_DB \
  backend/.venv/bin/python -m pytest backend/tests/test_public.py backend/tests/test_public_deploy.py -q
```

测试在指定数据库中创建和销毁随机 schema。设置 `PUBLIC_TEST_DOCKER=1` 可运行真实 Docker 测试，需先构建公众版 runner。`test_public_deploy.py` 不需要数据库，校验三个档位、生成的配置和 Compose 文件。

本机调试可设置 `PUBLIC_DEVELOPMENT=1`，跳过人机验证，并将邮件链接写入开发目录 `dev-mail/`；生产 Compose 不传递该开关。前端构建使用 `VITE_PUBLIC_MODE=1`；API 入口是 `app.public.main:create_app --factory`，Worker 是 `python -m app.public.worker`。

参考：[Docker 安全边界](https://docs.docker.com/engine/security/)、[PostgreSQL 队列锁](https://www.postgresql.org/docs/current/sql-select.html)、[cerberus](https://github.com/sjtug/cerberus)、[Turnstile 服务端验证](https://developers.cloudflare.com/turnstile/get-started/server-side-validation/)。
