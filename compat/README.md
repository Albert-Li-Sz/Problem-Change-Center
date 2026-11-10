# 平台兼容验证

`versions.lock.json` 固定夜间与发布验证使用的 Hydro、DOMjudge 和 HOJ
版本。升级版本必须单独提交，并同时更新转换语义快照。

快速流水线运行：

```bash
python3 compat/check_lock.py
backend/.venv/bin/python -m pytest -q \
  backend/tests/test_package_converter.py \
  -k 'core_format_matrix or semantic'
```

`.github/workflows/quality.yml` 在每次提交运行完整快速测试，并在夜间或手动
触发时构建 runner、执行容器隔离探针。真实平台验收使用独立、一次性实例，
依次验证导入、字段读取、AC/WA 提交、特殊 checker 和 OI 子任务得分；由于
这些实例包含管理员凭据，不在公共仓库中预置。凭据只能从受保护 CI
environment 注入，不得写入 fixture、日志或转换报告。平台启动失败与题包
导入失败必须分开报告。

兼容语料应为原创最小题或具有明确再分发许可的题包，不提交真实比赛私有数据。

本地语义回归位于 `backend/tests/test_adapter_semantics.py`，覆盖 FPS 特判协议、
Hydro 零分评测点与 `id: 0` 依赖，以及 HOJ 平均分模式的显式拒绝。依据为：

- [HUSTOJ FPS 导出实现](https://github.com/zhblue/hustoj/blob/11085130b1c0ee8a4e2dcf84c7a1e10023c2cf04/trunk/web/admin/problem_export_xml.php)：区分 `<spj>` 与 `<tpj>`。
- [Hydro checker 实现](https://github.com/hydro-dev/Hydro/blob/2267ac9984bf2a03311b765d1b1928f9f4cc2bd8/packages/hydrojudge/src/checkers.ts)：原生 `hustoj` 协议使用输入、标准答案、选手输出的参数顺序。
- [Hydro 配置读取实现](https://github.com/hydro-dev/Hydro/blob/2267ac9984bf2a03311b765d1b1928f9f4cc2bd8/packages/utils/lib/cases.ts)：`checker` 必须是文件路径字符串，不能写为 `file`/`lang` 对象。
- [HOJ 计分实现](https://github.com/HimitZH/HOJ/blob/9a65e3f24e81ed76cc1aa1c06da68262434eaadd/hoj-springboot/JudgeServer/src/main/java/top/hcode/hoj/judge/JudgeStrategy.java)：`subtask_average` 对每组测试点的得分求平均，再用 `Math.round` 取整。

以上是上游源码核对和本地转换测试，不等同于真实 OJ 实例的导入、提交验收。
