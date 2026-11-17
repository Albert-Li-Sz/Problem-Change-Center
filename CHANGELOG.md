# 版本变更

## 0.7.4 — 2026-11-17（main 同步）

- 修正默认安装脚本、文档和页脚的仓库及镜像地址，统一使用 `Albert-Li-Sz/Problem-Change-Center` 与 `ghcr.io/albert-li-sz/`。
- 拉取带标签的 Wine 镜像时，同时准备非 Polygon 转换需要的普通 runner；digest 固定的 Wine 镜像不推导其他镜像。
- 同步已发布版本中的数字题名、嵌套 TeX 文本格式、交互题 Input/Output/Interaction、居中正文、章节回退及样例保留修复。
- 同步 FPS checker 协议、Hydro 零分点与子任务依赖、HOJ 平均分拒绝、DMOJ 依赖编号及畸形元数据报告等适配器修复。
- 同步转换进度超时、任务重启状态和 SSE 重连修复；避免未启动的上传在服务重启时被标记为中断。
- runner 默认内存调整为 3 GiB，并对退出码 137 增加内存耗尽提示；Docker CI 固定使用兼容 Wine 的 Engine 29.9.3。
- 增加安装向导镜像选择、Wine 配套镜像和转换内容保留回归。版本标签 `v0.7.4` 保持不变，`main` 为滚动维护分支。
- 修正隔离校验脚本的 GNU/BSD mktemp 兼容性，避免 Linux 夜间检查在创建临时目录时退出。
