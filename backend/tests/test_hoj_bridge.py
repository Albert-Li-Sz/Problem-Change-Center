from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
RUNNER_DIR = ROOT / "runner"
sys.path.insert(0, str(RUNNER_DIR))

import package_converter  # noqa: E402
from format_bridge import ArchiveExtractionBudget  # noqa: E402
from hoj_bridge import (  # noqa: E402
    HojProblem,
    _find_hoj_problems,
    _filter_hoj_problems,
    convert_hoj_to_domjudge,
    convert_hoj_to_hydro,
    convert_hydro_to_hoj,
)
from package_converter import (  # noqa: E402
    REPORT_FILENAME,
    _expand_nested_packages_for_detection,
    convert_package,
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


def _minimal_hoj_document(problem_id: str, title: str) -> dict:
    return {
        "samples": [{"input": "1.in", "output": "1.out"}],
        "problem": {
            "problemId": problem_id,
            "title": title,
            "description": f"Statement for {title}.",
            "timeLimit": 1000,
            "memoryLimit": 256,
        },
    }


def _write_minimal_hoj_tree(
    root: Path,
    problem_id: str,
    title: str,
    *,
    output: str = "1\n",
) -> None:
    key = f"problem_{problem_id.casefold()}"
    (root / key).mkdir(parents=True, exist_ok=True)
    (root / f"{key}.json").write_text(
        json.dumps(_minimal_hoj_document(problem_id, title), ensure_ascii=False),
        encoding="utf-8",
    )
    (root / key / "1.in").write_text("1\n", encoding="utf-8")
    (root / key / "1.out").write_text(output, encoding="utf-8")


def _write_minimal_hoj_zip(
    path: Path,
    problem_id: str,
    title: str,
    *,
    prefix: str = "",
    metadata_suffix: str = ".json",
) -> None:
    key = f"problem_{problem_id.casefold()}"
    base = f"{prefix.rstrip('/')}/" if prefix else ""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            f"{base}{key}{metadata_suffix}",
            json.dumps(_minimal_hoj_document(problem_id, title), ensure_ascii=False),
        )
        archive.writestr(f"{base}{key}/1.in", "1\n")
        archive.writestr(f"{base}{key}/1.out", "1\n")


def test_mixed_expanded_and_nested_hoj_export_keeps_all_unique_problems(
    tmp_path: Path,
) -> None:
    direct = tmp_path / "direct"
    _write_minimal_hoj_tree(direct / "contest" / "A", "P1000", "Alpha")
    nested_a = tmp_path / "A.zip"
    nested_b = tmp_path / "B.zip"
    attachment = tmp_path / "attachment.zip"
    _write_minimal_hoj_zip(nested_a, "P1000", "Alpha", prefix="A")
    _write_minimal_hoj_zip(nested_b, "P1001", "Beta")
    with zipfile.ZipFile(attachment, "w") as archive:
        archive.writestr("readme.txt", "not a problem package\n")

    source = tmp_path / "mixed-hoj.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(direct.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(direct).as_posix())
        archive.write(nested_a, "contest/A.zip")
        archive.write(nested_b, "contest/B.zip")
        archive.write(attachment, "contest/attachment.zip")

    output = tmp_path / "output"
    report = convert_package(
        source, output, source_format="auto", target_format="hydro"
    )

    assert report["source_format"] == "hoj"
    assert report["problem_count"] == 2
    assert len(list(output.glob("*.zip"))) == 2


def test_explicit_hoj_expands_nested_packages_despite_other_strong_marker(
    tmp_path: Path,
) -> None:
    direct = tmp_path / "direct"
    _write_minimal_hoj_tree(direct, "P1000", "Alpha")
    (direct / "init.yml").write_text("test_cases: []\n", encoding="utf-8")
    nested = tmp_path / "B.zip"
    _write_minimal_hoj_zip(nested, "P1001", "Beta")
    source = tmp_path / "explicit-hoj.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(direct.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(direct).as_posix())
        archive.write(nested, "B.zip")

    output = tmp_path / "output"
    report = convert_package(source, output, source_format="hoj", target_format="hydro")

    assert report["problem_count"] == 2
    assert len(list(output.glob("*.zip"))) == 2


def test_nested_hoj_metadata_extension_is_case_insensitive(tmp_path: Path) -> None:
    direct = tmp_path / "direct"
    _write_minimal_hoj_tree(direct, "P1000", "Alpha")
    nested = tmp_path / "B.zip"
    _write_minimal_hoj_zip(nested, "P1001", "Beta", metadata_suffix=".JSON")
    source = tmp_path / "uppercase-json.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(direct.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(direct).as_posix())
        archive.write(nested, "B.zip")

    output = tmp_path / "output"
    report = convert_package(
        source, output, source_format="auto", target_format="hydro"
    )

    assert report["problem_count"] == 2
    assert len(list(output.glob("*.zip"))) == 2


def test_corrupt_nested_zip_fails_with_structured_extraction_report(
    tmp_path: Path,
) -> None:
    direct = tmp_path / "direct"
    _write_minimal_hoj_tree(direct, "P1000", "Alpha")
    (direct / "B.zip").write_bytes(b"not a zip archive")
    source = tmp_path / "corrupt-nested.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(direct.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(direct).as_posix())

    output = tmp_path / "output"
    with pytest.raises(ValueError, match="invalid nested ZIP archive: B.zip"):
        convert_package(source, output, source_format="auto", target_format="hydro")

    report = json.loads((output / REPORT_FILENAME).read_text(encoding="utf-8"))
    assert report["issues"][0]["code"] == "source-extraction-error"


def test_nested_hoj_packages_share_existing_extraction_budget(tmp_path: Path) -> None:
    _write_minimal_hoj_tree(tmp_path / "expanded", "P1000", "Alpha")
    _write_minimal_hoj_zip(tmp_path / "A.zip", "P1000", "Alpha")
    _write_minimal_hoj_zip(tmp_path / "B.zip", "P1001", "Beta")
    budget = ArchiveExtractionBudget(
        max_entries=5,
        max_uncompressed_bytes=1024 * 1024,
        max_member_bytes=1024 * 1024,
        max_compression_ratio=200,
        min_compression_ratio_bytes=1024 * 1024,
        used_entries=3,
    )

    with pytest.raises(ValueError, match="zip archives exceed entry limit"):
        _expand_nested_packages_for_detection(tmp_path, budget)

    assert budget.used_entries == 5


def test_find_hoj_problems_deduplicates_identical_problem_id(tmp_path: Path) -> None:
    _write_minimal_hoj_tree(tmp_path / "expanded", "P1000", "Alpha")
    _write_minimal_hoj_tree(tmp_path / "nested", "P1000", "Alpha")

    problems = _find_hoj_problems(tmp_path)

    assert [problem.problem["problemId"] for problem in problems] == ["P1000"]


@pytest.mark.parametrize(
    ("conflict", "message"),
    [
        ("metadata", "conflicting HOJ metadata"),
        ("data", "conflicting HOJ problem data"),
    ],
)
def test_find_hoj_problems_rejects_conflicting_duplicate_problem_id(
    tmp_path: Path, conflict: str, message: str
) -> None:
    _write_minimal_hoj_tree(tmp_path / "expanded", "P1000", "Alpha")
    _write_minimal_hoj_tree(
        tmp_path / "nested",
        "P1000",
        "Changed" if conflict == "metadata" else "Alpha",
        output="2\n" if conflict == "data" else "1\n",
    )

    with pytest.raises(ValueError, match=message):
        _find_hoj_problems(tmp_path)


def test_filter_hoj_problems_deduplicates_repeated_selectors(tmp_path: Path) -> None:
    _write_minimal_hoj_tree(tmp_path, "P1000", "Alpha")
    problem = _find_hoj_problems(tmp_path)[0]

    assert _filter_hoj_problems([problem], ["P1000", "p1000"]) == [problem]


def test_filter_hoj_problems_rejects_ambiguous_normalized_selector(
    tmp_path: Path,
) -> None:
    first = HojProblem(
        "problem_p1000",
        tmp_path / "problem_p1000.json",
        tmp_path / "problem_p1000",
        _minimal_hoj_document("P1000", "A+B"),
    )
    second = HojProblem(
        "problem_p1001",
        tmp_path / "problem_p1001.json",
        tmp_path / "problem_p1001",
        _minimal_hoj_document("P1001", "A B"),
    )

    with pytest.raises(ValueError, match="ambiguous problem selector.*a-b"):
        _filter_hoj_problems([first, second], ["a-b"])


def test_zip_runtime_error_produces_structured_extraction_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.zip"
    output = tmp_path / "output"
    _write_minimal_hoj_zip(source, "P1000", "Alpha")

    def fail_detection(*_args: object, **_kwargs: object) -> list[object]:
        raise RuntimeError("encrypted ZIP")

    monkeypatch.setattr(
        package_converter, "_expand_nested_packages_for_detection", fail_detection
    )

    with pytest.raises(RuntimeError, match="encrypted ZIP"):
        convert_package(source, output, source_format="auto", target_format="hydro")

    report = json.loads((output / REPORT_FILENAME).read_text(encoding="utf-8"))
    assert report["issues"][0]["code"] == "source-extraction-error"


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
