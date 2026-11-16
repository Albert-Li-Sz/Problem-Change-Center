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

`backend/tests/test_statement_markup.py` 覆盖 TeX 嵌套文本格式、数学公式与代码保护；
行内代码的反引号和空白处理依据 [CommonMark 0.31.2](https://spec.commonmark.org/0.31.2/#code-spans)。
公众版 Docker 验收还运行完整的 Polygon → Hydro 转换，核对数字题名、嵌套粗体与
行内代码，以及保留在数学公式内的 TeX 命令。

`backend/tests/test_statement_preservation.py` 对 Hydro、ICPC、HOJ、FPS、QDUOJ、
UOJ、DMOJ 的 42 条不同格式转换逐一核对 Description、Input、Output、Interaction、
Scoring、Notes 的正文标记。UOJ/DMOJ 的验收包含结果包附带的题面 sidecar，
只搬运评测数据 ZIP 无法保留这些题面。

本轮还覆盖交互题 Input/Output 与 Interaction 共存、Polygon 单边样例与空目录回退、
center 文字、Markdown 代码围栏内的标题、按编号配对的样例和 HOJ 样例分隔符。
HOJ 的 [样例读写协议](https://github.com/HimitZH/HOJ/blob/9a65e3f24e81ed76cc1aa1c06da68262434eaadd/hoj-vue/src/common/utils.js#L126-L145)
使用原始字符串分隔，不执行 HTML 实体反解；无法安全表示的样例继续留在正文，
并报告 `hoj-sample-delimiter`，不会自行转义成含义不同的样例。

边界：这套矩阵核对题面正文，不代表所有 TeX 宏、图片引用、PDF 或目标 OJ 的
渲染均已验收。Polygon 优化路径目前仍按中文/英文优先级选取一种题面语言，
未实现所有语种的全量导出；其余格式的多语言、PDF 和附件限制仍需查看转换报告。
