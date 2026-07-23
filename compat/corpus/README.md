# 最小兼容语料

这里的语料由 `build.py` 确定性生成，内容均为本项目原创并以 CC0
提供，可用于公开 CI、平台导入测试与问题复现。

```bash
python3 compat/corpus/build.py /tmp/oj-compat-corpus
```

生成文件：

- `hydro-core-rich.zip`：普通 ACM、样例、OI 子任务与依赖、文件 IO、
  testlib 风格 checker/validator、交互题、PDF/多语言题面、附件、模板和
  accepted solution。
- `icpc-minimal.zip`、`fps-minimal.zip`、`generic-multi.zip`：跨源格式的
  最小有效包，覆盖多语言题面、模板、样例、多题与 Unicode 路径。
- `hydro-missing-answer.zip`：配置引用 `1.ans`，包内只有 `1.out`，用于
  验证 `.ans/.out` 建议、显式确认、派生任务与原 ZIP 不变性。
- `hydro-case-only-answer.zip`、`hydro-missing-validator.zip`、
  `hydro-answer-conflict.zip`：分别验证大小写修复、缺失文件上传和冲突答案
  不可静默接受。
- `manifest.json`：列出每个 fixture 的输入格式、目标矩阵、期望问题数、
  能力标签或预期 fatal code，供 CI 数据驱动遍历。

生成器固定 ZIP 时间戳和文件顺序；测试比较语义快照，不依赖压缩后的字节完全
一致。
