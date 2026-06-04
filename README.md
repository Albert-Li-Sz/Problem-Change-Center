# Polygon2Hydro Web UI

本项目是 `polygon2hydro` 的本地 Web UI。前端负责上传题包、配置参数、查看日志和下载结果；后端不直接执行转换逻辑，而是为每个任务启动一次性 Docker runner 容器。

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
runner/    p2h-runner Docker 镜像
```

## 准备 runner 镜像

```bash
docker compose --profile runner build runner
```

镜像名为 `p2h-runner`，默认安装 `polygon2hydro` 提交 `93aca21`。runner 基础镜像固定为 `python:3.12-slim-bookworm`，因为当前 `python:3.12-slim` 可能解析到 Debian trixie，而 trixie 不提供 `openjdk-17-jdk-headless`。镜像不安装 `wine`；如果题包依赖 Windows `.exe` 生成器，需要维护单独扩展镜像。

如果题包的 `doall.sh` 会运行 Windows `.exe`，构建 Wine runner：

```bash
docker compose --profile wine build runner-wine
```

然后启动后端前指定：

```bash
export P2H_RUNNER_IMAGE=p2h-runner-wine
```

`p2h-runner-wine` 使用 `linux/amd64` 并安装 32/64 位 Wine，适合同时包含 `PE32` 和 `PE32+` 可执行文件的 Polygon 包。它明显更大，且在 Apple Silicon 上会通过 Docker 的 amd64 仿真运行，速度比普通 runner 慢。

## 启动后端

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

可选环境变量：

```text
P2H_DATA_DIR=backend_data
P2H_RUNNER_IMAGE=p2h-runner
P2H_MAX_UPLOAD_BYTES=536870912
P2H_JOB_TIMEOUT_SECONDS=600
P2H_DOCKER_MEMORY=1g
P2H_DOCKER_CPUS=2
P2H_DOCKER_PIDS_LIMIT=256
```

后端会为每个 job 创建独立的 `work/` 和 `output/` 目录并挂载到 runner。`/tmp` 仍以 `noexec` tmpfs 挂载；`/work` 使用 job 专属目录，因为真实 Polygon `doall.sh` 可能生成超过 1GB 的测试数据，不能可靠地放在 tmpfs 里。

## 启动前端

```bash
cd frontend
npm install
npm run dev
```

访问 [http://127.0.0.1:5173](http://127.0.0.1:5173)。Vite 会把 `/api` 代理到 `http://127.0.0.1:8000`。

## API

- `POST /api/inspect`：上传并基础检查 zip，返回 `job_id`。
- `POST /api/jobs`：启动转换任务。
- `GET /api/jobs/{job_id}`：查询任务状态。
- `GET /api/jobs/{job_id}/logs`：读取纯文本日志。
- `GET /api/jobs/{job_id}/download`：下载转换结果。
- `DELETE /api/jobs/{job_id}`：取消运行中任务或清理已完成任务。

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
```
