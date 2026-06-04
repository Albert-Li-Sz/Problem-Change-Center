# Polygon Converter Web UI

本项目是题包格式的团队内 Web UI 转换平台。前端负责邮箱登录、上传题包、配置参数、查看日志和下载结果；后端负责认证、配额、任务队列，并在宿主机上为每个任务启动一次性受限 Docker runner 容器。

当前支持三种转换方向：

- Polygon contest zip -> HydroOJ：通过 `polygon2hydro` 转换。
- Polygon contest zip -> DOMjudge/Kattis problem package：通过 `cn-xcpc-tools/Polygon2DOMjudge` 的 `p2d` API 逐题转换。
- HydroOJ package zip -> DOMjudge/Kattis problem package：runner 内置轻量文件格式转换器。

## 安全模型

- 默认安全模式使用 `--no-run-doall`，不会执行 Polygon 包内脚本。
- 用户显式启用 `doall.sh` 时，脚本仍只在受限 Docker 容器内运行。
- runner 固定使用无网络、非 root、只读根文件系统、能力裁剪、进程数/CPU/内存限制。
- Docker 是风险降低措施，不是绝对沙箱。高安全场景应考虑 gVisor、Kata Containers 或 Firecracker。
- 后端建议运行在宿主机上。如果把后端也放进 Docker 并挂载 `/var/run/docker.sock`，会削弱隔离边界。

## 目录结构

```text
backend/   FastAPI API、任务状态、Docker runner 调度
frontend/  React + Vite + TypeScript 单页工具
runner/    p2h-runner Docker 镜像与转换入口
```

## 一键安装

推荐在 macOS、Linux 或 Windows WSL2 中使用：

```bash
./install.sh
```

安装脚本会执行：

- 检查 Python、Node/npm、Docker 和 Docker Compose。
- 创建 `backend/.venv` 并安装 FastAPI 后端依赖。
- 使用 `npm ci` 安装前端依赖，并执行一次前端生产构建检查。
- 构建 `p2h-runner` Docker 镜像。
- 生成本地 `.env`，用于启动脚本读取数据库、认证、默认管理员、队列并发、runner 镜像和资源限制。
- 启动 `postgres:16-alpine` 容器，用户、审核状态和每日用量都存储在 PostgreSQL 中。

交互安装时可以输入最大并发 runner 容器数，默认 `2`。非交互环境可直接传参：

```bash
./install.sh --max-containers 4
```

如果题包需要执行 Windows `.exe`，使用 Wine runner：

```bash
./install.sh --wine
```

如果 Docker Hub 或 GitHub 下载临时超时，可以先安装 Python/Node 依赖，稍后再构建 runner：

```bash
./install.sh --skip-runner
docker compose --profile runner build runner
```

如果只是 Docker Hub 的 `python:3.12-slim-bookworm` 元数据或 token 请求超时，可以指定一个你当前网络可访问的 Python 基础镜像源：

```bash
./install.sh --base-image <registry>/library/python:3.12-slim-bookworm
```

手动构建时也可以这样传：

```bash
P2H_PYTHON_BASE_IMAGE=<registry>/library/python:3.12-slim-bookworm docker compose --profile runner build runner
```

启动：

```bash
./scripts/start.sh
```

访问 [http://127.0.0.1:5173](http://127.0.0.1:5173)。停止时按 `Ctrl+C`。

常用安装选项：

```text
 --wine                 同时构建 p2h-runner-wine，并配置 .env 使用它
--skip-runner          跳过 Docker runner 构建
--skip-backend         跳过后端依赖安装
--skip-frontend        跳过前端依赖安装
--no-frontend-build    跳过 npm run build
--skip-db              安装时不启动 PostgreSQL 容器
--max-containers N     最大同时运行的 runner 容器数
--python PATH          指定创建 backend/.venv 的 Python
--base-image IMAGE     指定 runner Docker 基础镜像
```

安装脚本会在新 `.env` 中写入 `P2H_BOOTSTRAP_ADMIN_EMAIL=admin@p2h.local` 和随机生成的 `P2H_BOOTSTRAP_ADMIN_PASSWORD`。后端启动时会创建或提升该账号为已通过管理员；账号已存在时不会覆盖密码，管理员登录后可在界面中修改。

## 手动准备 runner 镜像

```bash
docker compose --profile runner build runner
```

镜像名为 `p2h-runner`，默认安装：

- `polygon2hydro` 提交 `93aca21`
- `Polygon2DOMjudge` 提交 `8b0919a2a3e0946faaf677ec5cb2cad65fee7e30`

runner 基础镜像固定为 `python:3.12-slim-bookworm`，因为当前 `python:3.12-slim` 可能解析到 Debian trixie，而 trixie 不提供 `openjdk-17-jdk-headless`。普通镜像不安装 `wine`；如果题包依赖 Windows `.exe` 生成器，请使用 Wine runner。

如果题包的 `doall.sh` 会运行 Windows `.exe`，构建 Wine runner：

```bash
docker compose --profile wine build runner-wine
```

然后启动后端前指定，或在 `.env` 中设置同名变量：

```bash
export P2H_RUNNER_IMAGE=p2h-runner-wine
```

`p2h-runner-wine` 使用 `linux/amd64` 并安装 32/64 位 Wine，适合同时包含 `PE32` 和 `PE32+` 可执行文件的 Polygon 包。它明显更大，且在 Apple Silicon 上会通过 Docker 的 amd64 仿真运行，速度比普通 runner 慢。

## 手动启动后端

```bash
docker compose up -d postgres
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

可选环境变量：

```text
P2H_DATA_DIR=~/.p2h-web-ui/backend_data
P2H_RUNNER_IMAGE=p2h-runner
P2H_PYTHON_BASE_IMAGE=python:3.12-slim-bookworm
P2H_DATABASE_URL=postgresql://p2h:p2h_dev_password@127.0.0.1:5432/p2h
P2H_AUTH_SECRET_KEY=change-this-to-a-long-random-secret
P2H_AUTH_TOKEN_TTL_SECONDS=604800
P2H_BOOTSTRAP_ADMIN_EMAIL=admin@p2h.local
P2H_BOOTSTRAP_ADMIN_PASSWORD=change-this-to-a-random-password
P2H_DEFAULT_DAILY_QUOTA=10
P2H_MAX_UPLOAD_BYTES=134217728
P2H_MAX_CONCURRENT_JOBS=2
P2H_JOB_TIMEOUT_SECONDS=600
P2H_JOB_TTL_SECONDS=86400
P2H_DOCKER_MEMORY=1g
P2H_DOCKER_CPUS=2
P2H_DOCKER_PIDS_LIMIT=256
```

后端会为每个 job 创建独立的 `input/`、`work/` 和 `output/` 目录并挂载到 runner。默认数据目录放在 `~/.p2h-web-ui/backend_data`，避免 macOS Docker Desktop 无法 bind mount 外接卷或 `/Volumes/...` 路径。`/tmp` 仍以 `noexec` tmpfs 挂载；`/work` 使用 job 专属目录，因为真实 Polygon `doall.sh` 可能生成超过 1GB 的测试数据，不能可靠地放在 tmpfs 里。

## 团队版功能

- 邮箱注册后账号进入待审核状态；管理员通过后才能登录。密码使用 PBKDF2-HMAC-SHA256 加盐哈希，登录返回 7 天有效的 HMAC Bearer token。
- 管理员可以在前端用户管理区审核、拒绝用户，调整每日额度、今日已用次数、管理员权限，并重置用户密码。系统会阻止移除最后一个已通过管理员。
- 所有已登录用户都可以在界面中使用当前密码修改自己的密码。
- 普通用户默认每天 10 次成功转换额度。提交任务时先占用 pending，成功后转为 used；失败、取消、超时会释放 pending。管理员不受额度限制。
- 后端使用进程内 FIFO 队列，最多同时启动 `P2H_MAX_CONCURRENT_JOBS` 个 runner 容器。后端重启后会把遗留的 queued/running 任务标记为取消并释放 pending。
- 单次上传默认限制为 128MB，可通过 `P2H_MAX_UPLOAD_BYTES` 调整。超过限制会返回 413 并删除临时 job 目录。
- 每个任务结束后会删除 `input/`、`work/`、`output/`，保留 `metadata.json`、`logs.txt` 和成功任务的 `result.zip`。启动和任务结束时还会按 `P2H_JOB_TTL_SECONDS` 清理旧 job。

## 手动启动前端

```bash
cd frontend
npm install
npm run dev
```

访问 [http://127.0.0.1:5173](http://127.0.0.1:5173)。Vite 会把 `/api` 代理到 `http://127.0.0.1:8000`。

## API

认证接口：

- `POST /api/auth/register`：邮箱和密码注册，账号进入待审核状态。
- `POST /api/auth/login`：邮箱登录，要求账号已通过管理员审核。
- `GET /api/auth/me`：读取当前用户、角色、审核状态和今日额度。
- `POST /api/auth/password`：登录用户使用当前密码修改自己的密码。

管理员接口：

- `GET /api/admin/users`：列出用户、审核状态和今日用量。
- `PATCH /api/admin/users/{user_id}`：调整审核状态、每日额度、今日已用次数或管理员权限。
- `POST /api/admin/users/{user_id}/password`：管理员重置指定用户密码。

转换接口都需要 `Authorization: Bearer <token>`：

- `POST /api/inspect`：上传并基础检查 zip，返回 `job_id`。
- `POST /api/jobs`：启动转换任务，`target` 可为 `hydro`、`domjudge` 或 `hydro_to_domjudge`，默认 `hydro`。
- `GET /api/jobs/{job_id}`：查询任务状态。
- `GET /api/jobs/{job_id}/logs`：读取纯文本日志。
- `GET /api/jobs/{job_id}/download`：下载转换结果。
- `DELETE /api/jobs/{job_id}`：取消运行中任务或清理已完成任务。

Polygon -> DOMjudge 转换说明：

- 上传入口仍是 Polygon contest zip。
- runner 会安全解压 contest zip，按 `problems/<slug>` 找到题目。
- 不指定 `only slugs` 时会按 slug 排序逐题转换。
- 输出目录里会生成 `A-slug.zip`、`B-slug.zip` 这类 DOMjudge/Kattis problem package，后端再统一打包成一个下载文件。
- `doall.sh` 默认不执行。若启用，仍在同一个受限 Docker runner 内执行。
- P2D 的 contest 辅助入口目前不能直接批量转换 contest zip，本项目在 runner 里补了一层批量包装逻辑。

HydroOJ -> DOMjudge 转换说明：

- 上传入口是 HydroOJ package zip，可以是单题包，也可以是包含多个 HydroOJ 题包目录的 zip。
- 转换只做文件格式重排，不执行 `doall.sh`，也不调用 Polygon 专用转换链路。
- `problem.yaml`、`problem_*.md` 和 `testdata/` 会转换成 DOMjudge 的 `problem.yaml`、`domjudge-problem.ini`、`problem_statement/`、`data/sample`、`data/secret`。
- `testdata/std.cpp` 等标准程序会进入 `submissions/accepted`；`check.cpp`、`val.cpp`、`gen.cpp` 会分别进入 `output_validators/`、`input_validators/`、`generators/`。
- checker/validator、交互题、特殊 judging 脚本等复杂语义只能尽量搬运文件，转换后仍建议在目标 OJ 上复核。
- 当前没有提供 DOMjudge -> HydroOJ 转换入口。

## 测试

后端：

```bash
cd backend
pytest -q
```

认证的 PostgreSQL 集成测试默认跳过。要运行它：

```bash
docker compose up -d postgres
cd backend
P2H_TEST_DATABASE_URL=postgresql://p2h:p2h_dev_password@127.0.0.1:5432/p2h pytest -q
```

前端：

```bash
cd frontend
npm run build
```

runner：

```bash
docker compose --profile runner build runner
docker run --rm p2h-runner --help
docker run --rm p2h-runner domjudge-convert --help
docker run --rm p2h-runner hydro-to-domjudge --help
```
