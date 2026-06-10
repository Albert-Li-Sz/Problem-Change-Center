# Polygon Converter Web UI

本项目是题包格式的本地 Web UI 转换工具。前端负责上传题包、配置参数、查看日志和下载结果；后端不直接执行转换逻辑，而是为每个任务启动一次性 Docker runner 容器。

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
- 生成本地 `.env`，用于启动脚本读取端口、runner 镜像和资源限制。

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
--wine                 同时构建 p2h-runner-wine，并在新 .env 中使用它
--skip-runner          跳过 Docker runner 构建
--skip-backend         跳过后端依赖安装
--skip-frontend        跳过前端依赖安装
--no-frontend-build    跳过 npm run build
--python PATH          指定创建 backend/.venv 的 Python
--base-image IMAGE     指定 runner Docker 基础镜像
```

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

然后启动后端前指定：

```bash
export P2H_RUNNER_IMAGE=p2h-runner-wine
```

`p2h-runner-wine` 使用 `linux/amd64` 并安装 32/64 位 Wine，适合同时包含 `PE32` 和 `PE32+` 可执行文件的 Polygon 包。它明显更大，且在 Apple Silicon 上会通过 Docker 的 amd64 仿真运行，速度比普通 runner 慢。

## 手动启动后端

```bash
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
P2H_MAX_UPLOAD_BYTES=536870912
P2H_JOB_TIMEOUT_SECONDS=600
P2H_DOCKER_MEMORY=1g
P2H_DOCKER_CPUS=2
P2H_DOCKER_PIDS_LIMIT=1024
```

后端会为每个 job 创建独立的 `work/` 和 `output/` 目录并挂载到 runner。默认数据目录放在 `~/.p2h-web-ui/backend_data`，避免 macOS Docker Desktop 无法 bind mount 外接卷或 `/Volumes/...` 路径。`/tmp` 仍以 `noexec` tmpfs 挂载；`/work` 使用 job 专属目录，因为真实 Polygon `doall.sh` 可能生成超过 1GB 的测试数据，不能可靠地放在 tmpfs 里。

## 手动启动前端

```bash
cd frontend
npm install
npm run dev
```

访问 [http://127.0.0.1:5173](http://127.0.0.1:5173)。Vite 会把 `/api` 代理到 `http://127.0.0.1:8000`。

## API

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

## 测试

后端：

```bash
cd backend
pytest -q
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
