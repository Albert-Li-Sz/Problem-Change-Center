# OJ 题包转换器 Web UI

本项目是本地运行的多 OJ 题包转换工具。前端负责上传、自动识别、配置源/目标格式、查看日志与字段损失报告；后端为每个任务启动一次性 Docker runner。除 Polygon 的成熟优化链路外，所有转换都经过统一 `ProblemBundle` 中间模型，因此支持任意可读格式到任意可写格式，而不需要维护两两转换器。

| 格式 ID | 平台/格式 | 读取 | 输出 | 备注 |
|---|---|:---:|:---:|---|
| `polygon` | Polygon、Codeforces 出题链 | ✓ | — | 默认不运行 `doall.sh` |
| `hydro` | HydroOJ | ✓ | ✓ | 完整能力中心格式 |
| `icpc` | DOMjudge、Kattis、PC² | ✓ | ✓ | 兼容 legacy 与 `2025-09`，默认 `legacy-icpc` |
| `hoj` | HOJ | ✓ | ✓ | 原生 JSON + 同名数据目录 |
| `fps` | HUSTOJ、OpenJudger | ✓ | ✓ | HUSTOJ 1.6 / QDUOJ 1.2 profile |
| `qduoj` | QingdaoU OnlineJudge | ✓ | ✓ | 编号目录、`problem.json`、`testcase/` |
| `uoj` | UOJ Community | ✓ | ✓ | `problem.conf`，题面与元数据使用 sidecar |
| `dmoj` | DMOJ、LQDOJ | ✓ | ✓ | `init.yml` + data archive，题面使用 sidecar |
| `generic` | 洛谷测试数据、Lemon、普通目录 | ✓ | — | 推断 PDF/Markdown 与 `.in/.out/.ans` |

转换报告把问题分为 `warning`、`loss`、`fatal`。`loss_policy=warn` 会输出结果并列出无法表达的字段；`loss_policy=error` 会在写出文件前拒绝任何有损转换。UOJ/DMOJ 若没有题面会生成醒目占位题面并登记 `loss`；它们的输出包含可上传评测数据包，以及独立题面和 metadata sidecar。

## 缺失文件如何处理

转换器不会猜测或伪造会影响判题结果的文件。即使任务失败，Web UI 仍会展示结构化转换报告；API 客户端可读取 `GET /api/jobs/{job_id}/report`。失败容器只会导出报告，不会把 writer 留下的半成品作为可下载题包。

| 情况 | 处理结果 | 建议修复方式 |
|---|---|---|
| `.in` 缺少对应 `.out/.ans`，或配置声明的测试点文件不存在 | `fatal`，拒绝输出 | 从原 OJ 重新导出；检查文件名、扩展名和大小写是否与配置完全一致 |
| 配置引用的 checker、interactor、validator 不存在 | `fatal`，拒绝输出 | 补回原始源码并保持配置路径；不要用普通 diff checker 代替特殊 checker |
| 交互题缺 interactor | `fatal`，拒绝输出 | 从原题包恢复 interactor；若题目实际不是交互题，再修正源格式的题型配置 |
| PDF 引用或附件清单指向不存在的文件 | `fatal`，拒绝输出 | 补回文件，或删除错误引用后重新导出；路径按大小写敏感处理 |
| 整个题面缺失 | `loss`；`warn` 下生成醒目占位题面，`error` 下失败 | UOJ/DMOJ 可在数据包旁添加 `statement.md/html/pdf`；其他格式建议从原平台重新导出完整包 |
| `problem.json`、`problem.conf`、`init.yml` 等元数据损坏或包结构不完整 | `source-read-error` | 使用报告中的文件名定位问题，优先重新导出，不要手工猜测必需字段 |
| 自动识别没有达到 0.8 或出现多个强候选 | 不允许自动启动 | 在界面查看候选证据并明确选择输入格式；若仍失败，检查是否把多种题包错误地混在同一 ZIP 中 |
| 本地缺少配置的 runner 镜像 | 启动脚本在启动后端前报错 | 运行 `./install.sh`；Wine 镜像运行 `./install.sh --wine` |

路径错误最常见于 Windows 导出的包在 Linux runner 中运行：`Check.cpp` 和 `check.cpp` 是两个不同文件。修复后应重新打 ZIP，避免只在解压目录中改名却仍上传旧压缩包。

## 安全模型

- 默认安全模式使用 `--no-run-doall`，不会执行 Polygon 包内脚本。
- 用户显式启用 `doall.sh` 时，脚本仍只在受限 Docker 容器内运行。
- runner 无网络且根文件系统只读；受信任入口仅保留建立隔离 namespace 所需的窄权限，实际转换器固定以非 root、空 capability 集合运行，并受进程数/CPU/内存限制。
- ZIP 在上传和解压阶段都会检查路径穿越、符号链接、重复/大小写冲突文件名、条目数、展开大小、单文件大小和压缩比。
- FPS 使用 `defusedxml`，允许合法 DOCTYPE 声明但禁止实体和外部实体；XML、JSON、YAML 均限制大小、节点数和嵌套深度，JSON/YAML 拒绝重复键和非有限数字，YAML 还限制 alias 数量。
- 转换期间不会执行上传包中的 checker、validator、generator、interactor 或其他脚本；只有用户显式启用的 Polygon `doall.sh` 例外。
- Docker 是风险降低措施，不是绝对沙箱。高安全场景应考虑 gVisor、Kata Containers 或 Firecracker。
- 后端建议运行在宿主机上。如果把后端也放进 Docker 并挂载 `/var/run/docker.sock`，会削弱隔离边界。

## 目录结构

```text
backend/   FastAPI API、任务状态、Docker runner 调度
frontend/  React + Vite + TypeScript 单页工具
runner/    p2h-runner Docker 镜像与转换入口
```

## 一键安装

推荐在 macOS、Linux 或 Windows WSL2 中使用。安装脚本使用 Bash，macOS 自带的 `/bin/bash` 和常见 Linux 发行版的 Bash 都可运行。

前端依赖要求 Node.js 20.19+、22.12+ 或 24+；安装脚本会在执行 `npm ci` 前检查版本。

```bash
./install.sh
```

安装脚本会执行：

- 检查 Python、Node/npm、Docker 和 Docker Compose。
- 创建 `backend/.venv` 并安装 FastAPI 后端依赖。
- 使用 `npm ci` 安装前端依赖，并执行一次前端生产构建检查。
- 构建 `p2h-runner` Docker 镜像。
- 生成本地 `.env`，用于启动脚本读取端口、runner 镜像和资源限制。

### macOS Docker Desktop

macOS 上请先安装并启动 Docker Desktop，再确认终端可以访问 Docker：

```bash
docker info
```

不要用 `sudo ./install.sh` 或 `sudo ./scripts/start.sh` 启动本项目；Docker Desktop 应该能从普通用户 shell 访问。Apple Silicon 上构建或运行 Wine runner 时会使用 `linux/amd64` 仿真，速度会慢一些。

### Linux Docker 权限

Linux 上后端需要能以当前用户直接执行 `docker run`，因为每个转换任务都会启动一个受限 runner 容器。安装或启动前先确认：

```bash
docker info
```

如果提示没有权限访问 Docker daemon，推荐把当前用户加入 `docker` 组，然后重新登录：

```bash
sudo usermod -aG docker "$USER"
newgrp docker
docker info
```

不要只用 `sudo ./scripts/start.sh` 临时绕过权限；这容易在项目目录、`backend/.venv` 或数据目录里留下 root 拥有的文件，之后普通用户运行会遇到写入失败。生产部署时也可以让后端运行在一个有权访问 `/var/run/docker.sock` 的专用服务用户下。

如果题包需要执行 Windows `.exe`，使用 Wine runner：

```bash
./install.sh --wine
```

如果 Docker Hub 或 GitHub 下载临时超时，可以先安装 Python/Node 依赖，稍后再构建 runner：

```bash
./install.sh --skip-runner
docker compose --profile runner build runner
```

安装器默认会自动跳过 `localhost`、`127.*` 或 `::1` 这类构建容器访问不到的宿主机回环代理。如果 `apt-get update` 日志里仍出现类似 `Could not connect to 192.168.x.x:10808`，说明 Docker build 继承了宿主机代理，但构建容器访问不到这个代理。没有必要走代理时可以重试：

```bash
./install.sh --no-build-proxy
```

如果重试后仍然连接同一个代理，检查 `docker info` 里是否配置了 Docker daemon 级别的代理；这种代理需要在 Docker 服务配置里修正或移除。

如果 Docker build 确实需要代理，请使用构建容器可以访问的代理地址，再显式启用代理继承：

```bash
./install.sh --build-proxy
```

如果 Debian apt 源访问慢或被阻断，可以给 runner 构建指定 apt 镜像：

```bash
./install.sh --apt-mirror https://mirrors.tuna.tsinghua.edu.cn/debian
```

指定 apt 镜像且没有手动指定 `--base-image` 时，安装器会优先尝试已知的 Python 基础镜像镜像源，避免先等待 Docker Hub 超时。

也可以用环境变量保留给手动构建：

```bash
P2H_APT_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian docker compose --profile runner build runner
```

如果只是 Docker Hub 的 `python:3.14-slim-trixie` 元数据或 token 请求超时，可以指定一个你当前网络可访问的 Python 基础镜像源：

```bash
./install.sh --base-image <registry>/library/python:3.14-slim-trixie
```

手动构建时也可以这样传：

```bash
P2H_PYTHON_BASE_IMAGE=<registry>/library/python:3.14-slim-trixie docker compose --profile runner build runner
```

启动：

```bash
./scripts/start.sh
```

访问 [http://127.0.0.1:5173](http://127.0.0.1:5173)。停止时按 `Ctrl+C`。

启动脚本只允许后端和前端监听 loopback 地址，避免把无认证的转换接口意外暴露到局域网或公网。远程使用时应保持应用监听 `127.0.0.1`，并在前面部署带身份认证、TLS 和请求限速的反向代理。

常用安装选项：

```text
--wine                 同时构建 p2h-runner-wine，并在新 .env 中使用它
--skip-runner          跳过 Docker runner 构建
--skip-backend         跳过后端依赖安装
--skip-frontend        跳过前端依赖安装
--no-frontend-build    跳过 npm run build
--python PATH          指定创建 backend/.venv 的 Python
--base-image IMAGE     指定 runner Docker 基础镜像
--apt-mirror URL       指定 runner Docker 构建使用的 Debian apt 镜像
--apt-security URL     指定 runner Docker 构建使用的 Debian security 镜像
--build-proxy          强制构建 runner 时继承宿主机代理环境变量
--no-build-proxy       构建 runner 时不继承宿主机代理环境变量
```

## 手动准备 runner 镜像

```bash
docker compose --profile runner build runner
```

镜像名为 `p2h-runner`，默认安装：

- `polygon2hydro` 提交 `93aca21`
- `Polygon2DOMjudge` 提交 `8b0919a2a3e0946faaf677ec5cb2cad65fee7e30`

runner 基础镜像使用 `python:3.14-slim-trixie` 和 OpenJDK 21，避免使用安全扫描中出现多项不再回补 CVE 的旧版 Debian 组件。构建完成后会移除运行时不需要的 pip。普通镜像不安装 `wine`。对于同时包含 Windows `.exe` 和对应 C/C++ 源码的 Polygon 包，runner 会先按 `problem.xml` 在容器内重编译为原生 Linux 可执行文件，再运行 `doall.sh`；这种包不需要 Wine runner。

安全扫描记录（2026-07-23）：Python 3.14.6 的版本清单仍会命中 `CVE-2026-15308`，因为扫描器只根据解释器版本判断；两个 Runner 镜像已固定应用 CPython 3.14 官方回补提交 `07efb081`，并校验文件 SHA-256，构建时还会验证补丁新增状态。Python 3.14 发布包含该修复的稳定维护版本后，应移除临时回补并直接升级基础镜像。

可运行 `scripts/security-scan.sh` 对普通与 Wine Runner 镜像执行 Grype 高危漏洞门禁；脚本会加载 `security/openvex.json`，仅豁免上述已实际回补但仍被版本匹配命中的 CVE。

只有题包缺少对应源码、必须直接运行 Windows `.exe` 时，才构建 Wine runner：

```bash
docker compose --profile wine build runner-wine
```

然后启动后端前指定：

```bash
export P2H_RUNNER_IMAGE=p2h-runner-wine
```

`p2h-runner-wine` 使用 `linux/amd64` 并安装 32/64 位 Wine，适合同时包含 `PE32` 和 `PE32+` 可执行文件的 Polygon 包。它明显更大，且在 Apple Silicon 上会通过 Docker 的 amd64 仿真运行，速度比普通 runner 慢。

Apple Silicon 上不要把 Wine runner 当作首选。Docker 官方把 QEMU 下运行 amd64 容器定义为 best effort，Wine 的二次兼容层可能表现为 `free(): invalid pointer`、`qemu: uncaught target signal 6` 等崩溃。runner 会在执行题包脚本前探测 Wine；探测失败会立即终止并给出单一根因，不再继续生成几百条连锁错误。必须运行 binary-only Windows 包时，在 Docker Desktop 中选择 Apple Virtualization framework 并启用 Rosetta，然后重启 Docker Desktop；否则请在 Polygon 导出时保留 generator、validator、checker 和主标程源码。

`doall.sh` 以 fail-fast 模式运行。生成器、validator、checker 或标程首次失败后任务就会停止，避免后续空测试点和缺答案错误掩盖真正的失败位置。

如果日志里出现 `qemu: qemu_thread_create: Resource temporarily unavailable`，通常是 Apple Silicon 上 Wine/QEMU 创建线程时触达容器 pid 限制。后端会对 Wine runner 默认使用 `P2H_DOCKER_WINE_PIDS_LIMIT=4096`；如果题包测试很多或 Wine 进程仍然失败，可以继续调高这个值，或临时设为 `-1` 取消 Docker 的 pid 限制。

Wine runner 会把 Wine 的 `HOME`、`TMPDIR` 和 `WINEPREFIX` 放到独立的 `/home/app` tmpfs，而不是通用 `/work` tmpfs。Wine prefix 初始化可能占用 1GB 以上；如果该 tmpfs 过小，可能报 `could not load kernel32.dll` 或 `No space left on device`，可调大 `P2H_DOCKER_WINE_HOME_SIZE`。

Wine runner 的 `/home/app` tmpfs 会显式开启 `exec`，用于执行 Polygon `doall.sh` 内部的 `scripts/*.sh`。如果这里保持 Docker 默认 `noexec`，即使脚本已经 chmod 为可执行，也会报 `scripts/xxx.sh: Permission denied`。

Wine 生成器可能以 Windows 文本模式写出 CRLF 换行。runner 会在 `doall.sh` 成功后、打包前，将所选题目的 `tests/` 中的 CRLF 统一转换为 LF；其他源码和附件不会被改写。

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
P2H_PYTHON_BASE_IMAGE=python:3.14-slim-trixie
P2H_APT_MIRROR=
P2H_APT_SECURITY_MIRROR=
P2H_MAX_UPLOAD_BYTES=536870912
P2H_JOB_TIMEOUT_SECONDS=600
P2H_JOB_TTL_SECONDS=86400
P2H_MAX_CONCURRENT_JOBS=2
P2H_MAX_STORED_JOBS=100
P2H_MAX_STORAGE_BYTES=10737418240
P2H_MAX_LOG_BYTES=10485760
P2H_DOCKER_MEMORY=1g
P2H_DOCKER_CPUS=2
P2H_DOCKER_PIDS_LIMIT=1024
P2H_DOCKER_WINE_PIDS_LIMIT=4096
P2H_DOCKER_WINE_HOME_SIZE=4g
P2H_DOCKER_TMP_SIZE=512m
P2H_DOCKER_WORK_SIZE=1g
P2H_DOCKER_OUTPUT_SIZE=1g
```

后端会为每个 job 创建独立目录。上传包以只读方式挂载；`/work` 和 `/output` 都是有容量上限的容器 tmpfs。转换成功后，入口脚本会拒绝符号链接，再把 `/output` 复制到宿主机的 job 输出目录供后端打包。转换失败时只复制常规文件形式的 `.p2h-report.json`，不会复制半成品。这样生成脚本不能绕过输出容量限制，也不能通过输出符号链接读取宿主文件。

任务在创建或完成超过 `P2H_JOB_TTL_SECONDS` 后会在下一次 API 请求时清理；运行中的任务不会被清理。系统同时限制并发转换数、保存任务数、总存储量和单任务日志大小。达到限制时应先删除旧任务或调整对应环境变量。

runner 的受信任入口进程负责建立独立的 mount/PID namespace，并在其中卸载宿主 `/result` 挂载点；随后才以非 root 用户 `10001:10001`、空 capability 集合和 `no-new-privileges` 运行转换器。这样即使显式启用了题包内的 `doall.sh`，脚本也无法直接写宿主输出目录，后台子进程也会在转换器退出时一并终止。容器根文件系统仍为只读。Linux bind mount 会保留宿主机文件所有权，所以后端会显式设置：

- job 根目录和 `input/` 为 `0755`，让容器可以遍历并读取上传包。
- 上传的 `contest.zip` 为 `0644`。
- 宿主 job 输出目录为 `0777`，仅供受信任入口进程在转换结束后写入；转换程序所在 namespace 看不到 `/result`，实际产物写入独立 `/output` tmpfs。

如果你把 `P2H_DATA_DIR` 改到自定义路径，确保启动后端的用户能创建和修改该目录；不要把它放在 root-only 目录下。

## 手动启动前端

```bash
cd frontend
npm install
npm run dev
```

访问 [http://127.0.0.1:5173](http://127.0.0.1:5173)。Vite 会把 `/api` 代理到 `http://127.0.0.1:8000`。

## API

- `POST /api/inspect`：上传并安全检查 ZIP，返回 `job_id`、`detected_format` 和带证据的 `format_candidates`。只有唯一候选置信度达到 0.8 才会自动采用。
- `POST /api/jobs`：使用 `source_format`、`target_format`、`loss_policy`、`only` 和 `options.<format>` 启动通用转换。旧 `target` 七个枚举仍作为一个小版本周期内的兼容入口；旧字段和新字段不能混用。
- `GET /api/jobs/{job_id}`：查询任务状态。
- `GET /api/jobs/{job_id}/logs`：读取纯文本日志。
- `GET /api/jobs/{job_id}/report`：读取独立的结构化转换报告。报告不会混入可导入的目标题包。
- `GET /api/jobs/{job_id}/download`：下载转换结果。
- `DELETE /api/jobs/{job_id}`：取消运行中任务或清理已完成任务。

统一 runner 命令：

```bash
package-convert INPUT.zip \
  --source-format auto \
  --target-format hydro \
  --loss-policy warn \
  -o OUTPUT
```

`convert`、`domjudge-convert`、`hydro-to-domjudge`、`domjudge-to-hydro`、`hoj-to-hydro`、`hydro-to-hoj` 和 `hoj-to-domjudge` 继续作为兼容包装命令。

ICPC 输出会生成确定性 UUID；缺少输入 validator 时会基于题包测试输入的 SHA-256 集合生成严格 validator，而不会执行上传包中的生成器。`2025-09` profile 要求至少一个 accepted solution。许可证默认为规范允许的 `unknown`；选择 CC、educational 或 permission 时必须填写 rights owner。

默认的 `legacy-icpc` profile 只写出 ICPC 子集内容，但在 `problem.yaml` 中声明兼容的 `problem_format_version: legacy`，以兼容当前 reference `problemtools`（其尚不识别规范允许的 `legacy-icpc` 字符串）。

`2025-09` profile 的文本题面写为 `statement/problem.<语言>.tex`（正文使用 verbatim），PDF 则原样写入同目录；这样既符合 2025-09 规范，也兼容当前 BAPCtools 的题面语言一致性检查。`problem.yaml.name` 会严格使用实际写出的题面语言集合。

旧兼容命令 Polygon -> DOMjudge 转换说明：

- 上传入口仍是 Polygon contest zip。
- runner 会安全解压 contest zip，按 `problems/<slug>` 找到题目。
- 不指定 `only slugs` 时会按 slug 排序逐题转换。
- 输出目录里会生成 `A-slug.zip`、`B-slug.zip` 这类 DOMjudge/Kattis problem package，后端再统一打包成一个下载文件。
- `doall.sh` 默认不执行。若启用，仍在同一个受限 Docker runner 内执行。
- P2D 的 contest 辅助入口目前不能直接批量转换 contest zip，本项目在 runner 里补了一层批量包装逻辑。

旧兼容命令 HydroOJ -> DOMjudge 转换说明：

- 上传入口是 HydroOJ package zip，可以是单题包，也可以是包含多个 HydroOJ 题包目录的 zip。
- 转换只做文件格式重排，不执行 `doall.sh`，也不调用 Polygon 专用转换链路。
- `problem.yaml`、`problem_*.md` 和 `testdata/` 会转换成 DOMjudge 的 `problem.yaml`、`domjudge-problem.ini`、`problem_statement/`、`data/sample`、`data/secret`。
- `testdata/std.cpp` 等标准程序会进入 `submissions/accepted`；`check.cpp`、`val.cpp`、`gen.cpp` 会分别进入 `output_validators/`、`input_validators/`、`generators/`。
- checker/validator、交互题、特殊 judging 脚本等复杂语义只能尽量搬运文件，转换后仍建议在目标 OJ 上复核。

旧兼容命令 DOMjudge -> HydroOJ 转换说明：

- 上传入口可以是单个 DOMjudge/Kattis problem package，也可以是包含多个题包目录或多个内嵌题包 zip 的外层 zip。
- `problem_statement/*.pdf` 会复制到 Hydro 的 `additional_file/`；每种识别到的语言会生成对应的 `problem_<语言>.md`，内容使用 `@[pdf](file://文件名)` 嵌入 PDF。
- `data/secret` 会转换为 Hydro `testdata/`；`data/sample` 会保存在 `additional_file/samples/`。统一 `package-convert` 路由要求至少一对 secret 测试，只有旧 `domjudge-to-hydro` 兼容命令保留 sample 兜底行为。
- `domjudge-problem.ini` 和 `problem.yaml` 中的标题、时限和内存限制会写入 Hydro 元数据与 `testdata/config.yaml`。
- 能识别为 testlib 的 `output_validators` 源文件会作为 Hydro checker 搬运；accepted submissions、input/output validators 和 generators 也会保存在 `additional_file/sources/` 供人工复核。
- 每题必须包含至少一个 PDF 题面和一对输入/答案文件，否则转换会失败并在任务日志中给出原因。
- checker/validator、交互题、特殊 judging 脚本等复杂语义只能尽量搬运文件，转换后仍建议在目标 OJ 上复核。

旧兼容命令 HOJ 相关转换说明：

- HOJ 输入使用官方导出结构：zip 顶层包含成对的 `problem_x.json` 与 `problem_x/` 测试数据目录；支持单题和批量导出包。
- HOJ -> HydroOJ 会转换 Markdown 题面、题面样例、标签、时空限制、语言限制、文件 IO、OI 分组、SPJ/交互器以及 user/judge extra files。
- HydroOJ -> HOJ 会在结果 zip 顶层生成 HOJ 要求的 JSON 与同名目录，可直接在 HOJ 后台导入；Hydro 的 `subtasks` 会映射为 HOJ 的 `subtask_lowest` 分组。
- HOJ -> DOMjudge 会输出 `A-slug.zip`、`B-slug.zip` 这类独立 problem package；HOJ 题面样例进入 `data/sample`，正式测试点进入 `data/secret`。
- HOJ 的 `subtask_average` 与 Hydro/DOMjudge 的计分语义不能完全等价，目前按最低分子任务进行保守映射。
- HOJ 原生题包没有与 Hydro `additional_file` 完全等价的题面附件清单；Hydro -> HOJ 时 Markdown 内的附件引用会保留，但图片/PDF 等附件仍需在 HOJ 中人工复核。

## 测试

后端：

```bash
cd backend
pytest -q
```

前端：

```bash
cd frontend
npm test
npm run build
```

runner：

```bash
docker compose --profile runner build runner
docker run --rm p2h-runner --help
docker run --rm p2h-runner package-convert --help
docker run --rm p2h-runner domjudge-convert --help
docker run --rm p2h-runner hydro-to-domjudge --help
docker run --rm p2h-runner domjudge-to-hydro --help
docker run --rm p2h-runner hoj-to-hydro --help
docker run --rm p2h-runner hydro-to-hoj --help
docker run --rm p2h-runner hoj-to-domjudge --help
```

ICPC 题包可再用上游工具交叉验证：`legacy-icpc` profile 使用 reference `problemtools` 的 `verifyproblem`；`2025-09` 使用当前 BAPCtools 的 `bt validate`。后者目前只识别 TeX/PDF 题面，因此本项目的 2025 文本题面默认写为 TeX。
