# 版本变更

## 0.8.1 — 2026-11-17

- 修复启用 AppArmor 的 Linux 主机（如 Ubuntu）上转换失败、报 `unshare: cannot change root filesystem propagation: Permission denied` 的问题：runner 容器改用 `--security-opt apparmor=unconfined`，让入口脚本能在独立 mount 命名空间里隐藏结果挂载；转换器仍在丢弃全部 capability 后运行，seccomp、只读根文件系统和断网限制不变。

## 0.8.0 — 2026-11-17

- 移除访问控制：删除外部部署模式、`X-P2H-Access-Key` 访问密钥、页面访问验证、外部模式限流与来源校验，以及 `P2H_DEPLOYMENT_MODE`、`P2H_ACCESS_KEY_HASH`、`P2H_RATE_LIMIT_*` 配置；旧 `.env` 中残留的这些变量会被忽略。
- 前后端默认改为监听 `127.0.0.1`（仅本机）。安装器新增 `--bind 127.0.0.1|0.0.0.0`、`--local`、`--lan` 及交互选项，`./scripts/start.sh --lan` 可临时监听 `0.0.0.0` 供内网团队协作；systemd 后端服务改为读取 `P2H_BACKEND_HOST`。
- 安装脚本重做为 `./install.sh [install|update|uninstall]`：`update` 拉取代码后用新版安装器刷新后端依赖、前端构建和已配置的 runner 镜像并重启服务；`uninstall` 删除服务、依赖和构建产物，`--purge` 额外删除配置与数据目录，`--remove-images` 删除 runner 镜像。重复安装时默认保留已有 `.env`。
- 新增可选 systemd 后台服务（`--systemd` 或交互安装时选择）：由 `deploy/systemd/*.service.in` 模板生成，以安装用户身份从当前目录运行并读取 `.env`，开机自启；安装器只刷新或删除指向当前目录的服务。移除不再使用的 `deploy/production.env.example`，`production-check.sh` 默认检查当前目录的 `.env`。

## 0.7.4 — 2026-11-17（main 同步）

- 修正默认安装脚本、文档和页脚的仓库及镜像地址，统一使用 `Albert-Li-Sz/Problem-Change-Center` 与 `ghcr.io/albert-li-sz/`。
- 拉取带标签的 Wine 镜像时，同时准备非 Polygon 转换需要的普通 runner；digest 固定的 Wine 镜像不推导其他镜像。
- 同步已发布版本中的数字题名、嵌套 TeX 文本格式、交互题 Input/Output/Interaction、居中正文、章节回退及样例保留修复。
- 同步 FPS checker 协议、Hydro 零分点与子任务依赖、HOJ 平均分拒绝、DMOJ 依赖编号及畸形元数据报告等适配器修复。
- 同步转换进度超时、任务重启状态和 SSE 重连修复；避免未启动的上传在服务重启时被标记为中断。
- runner 默认内存调整为 3 GiB，并对退出码 137 增加内存耗尽提示；Docker CI 固定使用兼容 Wine 的 Engine 29.9.3。
- 增加安装向导镜像选择、Wine 配套镜像和转换内容保留回归。版本标签 `v0.7.4` 保持不变，`main` 为滚动维护分支。
- 修正隔离校验脚本的 GNU/BSD mktemp 兼容性，避免 Linux 夜间检查在创建临时目录时退出。
- Wine 镜像拉取时明确指定 `linux/amd64`，修复 ARM Docker 主机按原生架构查找 Wine manifest 而失败的问题。
