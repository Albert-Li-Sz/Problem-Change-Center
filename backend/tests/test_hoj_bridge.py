from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
RUNNER_DIR = ROOT / "runner"
sys.path.insert(0, str(RUNNER_DIR))

from hoj_bridge import (  # noqa: E402
    convert_hoj_to_domjudge,
    convert_hoj_to_hydro,
    convert_hydro_to_hoj,
)


def _hoj_document() -> dict:
    return {
        "judgeMode": "spj",
        "languages": ["C++", "Python3"],
        "samples": [
            {"input": "case1.in", "output": "case1.out", "score": 40, "groupNum": 1},
            {"input": "case2.in", "output": "case2.out", "score": 60, "groupNum": 2},
        ],
        "tags": ["入门", "HOJ"],
        "problem": {
            "auth": 1,
            "author": "admin",
            "isRemote": False,
            "problemId": "HOJ-1000",
            "description": "计算两个整数之和。",
            "source": "School Contest",
            "title": "A + B",
            "type": 1,
            "judgeMode": "spj",
            "judgeCaseMode": "subtask_lowest",
            "timeLimit": 1500,
            "memoryLimit": 256,
            "input": "两个整数。",
            "output": "它们的和。",
            "difficulty": 1,
            "examples": "<input>1 2\n</input><output>3\n</output>",
            "ioScore": 100,
            "codeShare": True,
            "hint": "注意范围。",
            "isRemoveEndBlank": True,
            "openCaseResult": True,
            "spjLanguage": "C++",
            "spjCode": '#include "testlib.h"\nint main() {}\n',
            "isFileIO": True,
            "ioReadFileName": "sum.in",
            "ioWriteFileName": "sum.out",
        },
        "codeTemplates": [{"language": "C++", "code": "int main() {}"}],
        "userExtraFile": {"helper.txt": "helper\n"},
        "judgeExtraFile": {"testlib.h": "// testlib\n"},
    }


def _write_hoj_zip(path: Path) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "problem_1000.json", json.dumps(_hoj_document(), ensure_ascii=False)
        )
        archive.writestr("problem_1000/case1.in", "1 2\n")
        archive.writestr("problem_1000/case1.out", "3\n")
        archive.writestr("problem_1000/case2.in", "2 3\n")
        archive.writestr("problem_1000/case2.out", "5\n")


def _write_hydro_zip(path: Path) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "P1000/problem.yaml", "title: A + B\npid: P1000\ntag:\n  - 入门\n"
        )
        archive.writestr(
            "P1000/problem_zh.md",
            """\
## 题目描述

计算两个整数之和。

## 输入格式

两个整数。

## 输出格式

它们的和。

## 样例

```input1
1 2
```

```output1
3
```

## 提示

注意范围。

## 来源

School Contest
""",
        )
        archive.writestr(
            "P1000/testdata/config.yaml",
            """\
type: default
time: 1500ms
memory: 256m
checker_type: testlib
checker: checker.cc
langs:
  - cc
  - py.py3
user_extra_files:
  - helper.txt
subtasks:
  - score: 40
    cases:
      - input: 1.in
        output: 1.out
  - score: 60
    cases:
      - input: 2.in
        output: 2.out
""",
        )
        archive.writestr("P1000/testdata/1.in", "1 2\n")
        archive.writestr("P1000/testdata/1.out", "3\n")
        archive.writestr("P1000/testdata/2.in", "2 3\n")
        archive.writestr("P1000/testdata/2.out", "5\n")
        archive.writestr(
            "P1000/testdata/checker.cc", '#include "testlib.h"\nint main() {}\n'
        )
        archive.writestr("P1000/testdata/helper.txt", "helper\n")
        archive.writestr(
            "P1000/additional_file/code_templates.json",
            '[{"language":"C++","code":"int main() {}"}]\n',
        )


def test_hoj_to_hydro_preserves_statement_cases_and_judging_config(
    tmp_path: Path,
) -> None:
    source = tmp_path / "hoj.zip"
    output = tmp_path / "output"
    _write_hoj_zip(source)

    assert (
        convert_hoj_to_hydro(
            source,
            output,
            pid_start="H050",
            owner=8,
            tags=["2026"],
            verbose=True,
        )
        == 0
    )

    packages = list(output.glob("*.zip"))
    assert len(packages) == 1
    with zipfile.ZipFile(packages[0]) as archive:
        names = set(archive.namelist())
        assert "H050/problem.yaml" in names
        assert "H050/problem_zh.md" in names
        assert "H050/testdata/config.yaml" in names
        assert "H050/testdata/1.in" in names
        assert "H050/testdata/2.out" in names
        assert "H050/testdata/checker.cc" in names
        assert "H050/testdata/helper.txt" in names
        assert "H050/additional_file/samples/1.in" in names
        assert "H050/additional_file/code_templates.json" in names

        statement = archive.read("H050/problem_zh.md").decode()
        assert "## 输入格式" in statement
        assert "```input1\n1 2\n```" in statement
        config = archive.read("H050/testdata/config.yaml").decode()
        assert "1500ms" in config
        assert "subtasks" in config
        assert "checker.cc" in config
        meta = archive.read("H050/problem.yaml").decode()
        assert "owner: 8" in meta
        assert "2026" in meta


def test_hydro_to_hoj_writes_native_json_and_paired_testdata_directory(
    tmp_path: Path,
) -> None:
    source = tmp_path / "hydro.zip"
    output = tmp_path / "output"
    _write_hydro_zip(source)

    assert convert_hydro_to_hoj(source, output, verbose=True) == 0

    json_files = list(output.glob("problem_*.json"))
    assert len(json_files) == 1
    key = json_files[0].stem
    assert (output / key / "1.in").read_text() == "1 2\n"
    assert (output / key / "2.out").read_text() == "5\n"

    document = json.loads(json_files[0].read_text())
    assert document["judgeMode"] == "spj"
    assert document["languages"] == ["C++", "Python3"]
    assert document["tags"] == ["入门"]
    assert document["samples"][0]["score"] == 40
    assert document["samples"][0]["groupNum"] == 1
    assert document["problem"]["problemId"] == "P1000"
    assert document["problem"]["description"] == "计算两个整数之和。"
    assert document["problem"]["input"] == "两个整数。"
    assert document["problem"]["output"] == "它们的和。"
    assert document["problem"]["examples"] == "<input>1 2</input><output>3</output>"
    assert "testlib.h" in document["problem"]["spjCode"]
    assert document["userExtraFile"] == {"helper.txt": "helper\n"}
    assert document["codeTemplates"] == [{"language": "C++", "code": "int main() {}"}]


def test_hydro_to_hoj_rejects_invalid_code_template_json(tmp_path: Path) -> None:
    source = tmp_path / "hydro-invalid-template.zip"
    output = tmp_path / "output"
    _write_hydro_zip(source)
    replacement = tmp_path / "replacement.zip"
    with (
        zipfile.ZipFile(source) as original,
        zipfile.ZipFile(replacement, "w") as target,
    ):
        for info in original.infolist():
            if info.filename != "P1000/additional_file/code_templates.json":
                target.writestr(info, original.read(info))
        target.writestr(
            "P1000/additional_file/code_templates.json",
            '[{"language":"C++","code":"first","code":"second"}]',
        )
    replacement.replace(source)

    with pytest.raises(ValueError, match="duplicate JSON"):
        convert_hydro_to_hoj(source, output)

    assert not list(output.glob("problem_*.json"))


def test_hoj_to_domjudge_reuses_statement_samples_and_checker(tmp_path: Path) -> None:
    source = tmp_path / "hoj.zip"
    output = tmp_path / "output"
    _write_hoj_zip(source)

    assert (
        convert_hoj_to_domjudge(
            source, output, code_start="C", color="#112233", verbose=True
        )
        == 0
    )

    packages = list(output.glob("*.zip"))
    assert len(packages) == 1
    assert packages[0].name.startswith("C-")
    with zipfile.ZipFile(packages[0]) as archive:
        names = set(archive.namelist())
        assert "problem.yaml" in names
        assert "domjudge-problem.ini" in names
        assert "problem_statement/problem.html" in names
        assert (
            "计算两个整数之和。"
            in archive.read("problem_statement/problem.html").decode()
        )
        assert "data/sample/001.in" in names
        assert "data/sample/001.ans" in names
        assert "data/secret/001.in" in names
        assert "data/secret/002.ans" in names
        assert "output_validators/checker/checker.cc" in names
        ini = archive.read("domjudge-problem.ini").decode()
        assert "short-name = C" in ini
        assert "color = #112233" in ini


def test_hydro_groups_to_hoj_preserves_group_scoring_without_double_counting(
    tmp_path: Path,
) -> None:
    source = tmp_path / "hydro-groups.zip"
    output = tmp_path / "output"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("P2000/problem.yaml", "title: Grouped\npid: P2000\n")
        archive.writestr("P2000/problem_en.md", "# Grouped\n")
        archive.writestr(
            "P2000/testdata/config.yaml",
            """\
type: default
groups:
  - score: 40
    cases:
      - input: one/a.in
        output: one/a.ans
      - input: two/b.in
        output: two/b.ans
  - score: 60
    cases:
      - input: three/c.in
        output: three/c.ans
""",
        )
        for directory, name, value in (
            ("one", "a", "1"),
            ("two", "b", "2"),
            ("three", "c", "3"),
        ):
            archive.writestr(f"P2000/testdata/{directory}/{name}.in", f"{value}\n")
            archive.writestr(f"P2000/testdata/{directory}/{name}.ans", f"{value}\n")

    assert convert_hydro_to_hoj(source, output) == 0

    document = json.loads(next(output.glob("problem_*.json")).read_text())
    assert document["problem"]["ioScore"] == 100
    assert document["problem"]["type"] == 1
    assert document["problem"]["judgeCaseMode"] == "subtask_lowest"
    assert [case["groupNum"] for case in document["samples"]] == [1, 1, 2]
    assert [case["score"] for case in document["samples"]] == [40, 40, 60]
