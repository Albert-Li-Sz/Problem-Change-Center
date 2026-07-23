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
