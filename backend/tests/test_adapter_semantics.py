from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "runner"))

import format_bridge as bridge  # noqa: E402
from package_converter import REPORT_FILENAME, convert_package  # noqa: E402


CHECKER = '#include "testlib.h"\nint main(int c, char** v) { registerTestlibCmd(c, v); quitf(_ok, "ok"); }\n'


def _zip(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return path


def _hydro(tmp_path: Path, config: str, **extra: str) -> Path:
    return _zip(
        tmp_path / "hydro.zip",
        {
            "P1/problem.yaml": "title: Sum\npid: P1\n",
            "P1/problem_zh.md": "statement\n",
            "P1/testdata/config.yaml": config,
            "P1/testdata/a.in": "1\n",
            "P1/testdata/a.out": "1\n",
            "P1/testdata/b.in": "2\n",
            "P1/testdata/b.out": "2\n",
            **extra,
        },
    )


def _icpc(tmp_path: Path, **extra: str) -> Path:
    return _zip(
        tmp_path / "icpc.zip",
        {
            "sum/problem.yaml": "name: Sum\n",
            "sum/problem_statement/problem.en.md": "statement\n",
            "sum/data/secret/1.in": "1\n",
            "sum/data/secret/1.ans": "1\n",
            **extra,
        },
    )


def _hydro_config(archive_path: Path) -> dict:
    with zipfile.ZipFile(archive_path) as archive:
        name = next(n for n in archive.namelist() if n.endswith("testdata/config.yaml"))
        return yaml.safe_load(archive.read(name))


def _issue_codes(report: dict) -> set[str]:
    return {issue["code"] for issue in report["issues"]}


def test_hydro_reader_accepts_native_subtask_id_and_if(tmp_path: Path) -> None:
    source = _hydro(
        tmp_path,
        "subtasks:\n"
        "  - id: 1\n    score: 40\n    cases: [{input: a.in, output: a.out}]\n"
        "  - id: 2\n    score: 60\n    if: [1]\n    cases: [{input: b.in, output: b.out}]\n",
    )
    output = tmp_path / "out"

    convert_package(source, output, source_format="hydro", target_format="dmoj")

    with zipfile.ZipFile(next(output.glob("*.zip"))) as archive:
        init = yaml.safe_load(archive.read("init.yml"))
    assert init["test_cases"][1]["dependencies"] == [1]


def test_hydro_writer_emits_id_and_if_for_dependencies(tmp_path: Path) -> None:
    source = _zip(
        tmp_path / "dmoj.zip",
        {
            "p/init.yml": (
                "test_cases:\n"
                "  - {points: 40, batched: [{in: 1.in, out: 1.out}]}\n"
                "  - {points: 60, dependencies: [1], batched: [{in: 2.in, out: 2.out}]}\n"
            ),
            "p/1.in": "1\n",
            "p/1.out": "1\n",
            "p/2.in": "2\n",
            "p/2.out": "2\n",
        },
    )
    output = tmp_path / "out"

    convert_package(source, output, source_format="dmoj", target_format="hydro")

    subtasks = _hydro_config(next(output.glob("*.zip")))["subtasks"]
    assert [subtask["id"] for subtask in subtasks] == [1, 2]
    assert "if" not in subtasks[0]
    assert subtasks[1]["if"] == [1]
    assert all("dependencies" not in subtask for subtask in subtasks)


@pytest.mark.parametrize("value", ["[1, 2]", "{a: 1}", "1e400"])
def test_malformed_metadata_ends_in_a_report_not_a_traceback(
    tmp_path: Path, value: str
) -> None:
    source = _zip(
        tmp_path / "dmoj.zip",
        {
            "p/init.yml": f"time_limit: {value}\ntest_cases:\n  - {{in: 1.in, out: 1.out}}\n",
            "p/1.in": "1\n",
            "p/1.out": "1\n",
        },
    )
    output = tmp_path / "out"

    with pytest.raises(ValueError, match="unexpected structure"):
        convert_package(source, output, source_format="dmoj", target_format="hydro")

    report = json.loads((output / REPORT_FILENAME).read_text(encoding="utf-8"))
    assert _issue_codes(report) == {"source-metadata-error"}


def test_uoj_reports_time_limit_rounded_to_whole_seconds(tmp_path: Path) -> None:
    source = _hydro(tmp_path, "time: 1500ms\n")
    output = tmp_path / "out"

    report = convert_package(source, output, source_format="hydro", target_format="uoj")

    assert "uoj-time-limit-rounded" in _issue_codes(report)
    with zipfile.ZipFile(next(output.glob("*.zip"))) as archive:
        assert "time_limit 2\n" in archive.read("problem.conf").decode()


def test_whole_second_limit_is_not_reported_for_uoj(tmp_path: Path) -> None:
    report = convert_package(
        _hydro(tmp_path, "time: 2s\n"),
        tmp_path / "out",
        source_format="hydro",
        target_format="uoj",
    )

    assert "uoj-time-limit-rounded" not in _issue_codes(report)


def _hydro_with_testlib_checker(tmp_path: Path) -> Path:
    return _hydro(
        tmp_path,
        "checker_type: testlib\nchecker: checker.cc\n",
        **{"P1/testdata/checker.cc": CHECKER},
    )


def test_fps_stores_testlib_checker_as_tpj_for_hustoj(tmp_path: Path) -> None:
    output = tmp_path / "out"

    convert_package(
        _hydro_with_testlib_checker(tmp_path),
        output,
        source_format="hydro",
        target_format="fps",
        options={"profile": "hustoj-1.6"},
    )

    xml = (output / "problem.xml").read_text(encoding="utf-8")
    assert "<tpj" in xml and "<spj" not in xml


@pytest.mark.parametrize(
    ("target", "options"),
    [("qduoj", {}), ("fps", {"profile": "qduoj-1.2"})],
)
def test_qduoj_targets_report_testlib_checker_protocol_mismatch(
    tmp_path: Path, target: str, options: dict[str, str]
) -> None:
    report = convert_package(
        _hydro_with_testlib_checker(tmp_path),
        tmp_path / "out",
        source_format="hydro",
        target_format=target,
        options=options,
    )

    assert "qduoj-checker-protocol" in _issue_codes(report)


def test_fps_rejects_test_data_xml_cannot_represent(tmp_path: Path) -> None:
    source = _hydro(tmp_path, "time: 1s\n", **{"P1/testdata/a.in": "1\x0c2\n"})
    output = tmp_path / "out"

    with pytest.raises(ValueError, match="control characters"):
        convert_package(source, output, source_format="hydro", target_format="fps")

    report = json.loads((output / REPORT_FILENAME).read_text(encoding="utf-8"))
    assert "target-write-error" in _issue_codes(report)


def test_icpc_reads_time_limit_from_timelimit_file(tmp_path: Path) -> None:
    output = tmp_path / "out"

    convert_package(
        _icpc(tmp_path, **{"sum/.timelimit": "3\n"}),
        output,
        source_format="icpc",
        target_format="hydro",
    )

    assert _hydro_config(next(output.glob("*.zip")))["time"] == "3000ms"


def test_output_lookup_prefers_ans_over_other_suffixes(tmp_path: Path) -> None:
    for suffix in (".output", ".out", ".ans"):
        (tmp_path / f"1{suffix}").write_text(suffix, encoding="utf-8")
    (tmp_path / "1.in").write_text("1", encoding="utf-8")

    assert bridge._find_matching_output_path(tmp_path / "1.in") == tmp_path / "1.ans"


@pytest.mark.parametrize("tag, mode", [("spj", "hustoj"), ("tpj", "testlib")])
def test_fps_checker_protocol_is_preserved_in_hydro(
    tmp_path: Path, tag: str, mode: str
) -> None:
    source = _zip(
        tmp_path / "fps.zip",
        {
            "problem.xml": (
                '<fps version="1.6"><item><title>Sum</title>'
                "<test_input>1</test_input><test_output>1</test_output>"
                f'<{tag} language="C++"><![CDATA[int main() {{ return 0; }}]]></{tag}>'
                "</item></fps>"
            )
        },
    )
    output = tmp_path / "out"

    convert_package(source, output, source_format="fps", target_format="hydro")

    config = _hydro_config(next(output.glob("*.zip")))
    assert config["checker_type"] == mode
    assert isinstance(config["checker"], str)
    with zipfile.ZipFile(next(output.glob("*.zip"))) as archive:
        assert any(
            name.endswith(f"testdata/{config['checker']}")
            for name in archive.namelist()
        )


@pytest.mark.parametrize("mode, tag", [("hustoj", "spj"), ("testlib", "tpj")])
def test_hydro_checker_protocol_selects_the_correct_hustoj_tag(
    tmp_path: Path, mode: str, tag: str
) -> None:
    output = tmp_path / "out"
    source = _hydro(
        tmp_path,
        f"checker_type: {mode}\nchecker: checker.cc\n",
        **{"P1/testdata/checker.cc": "int main() { return 0; }\n"},
    )

    convert_package(source, output, source_format="hydro", target_format="fps")

    xml = (output / "problem.xml").read_text(encoding="utf-8")
    assert f"<{tag} " in xml
    assert ("<tpj " if tag == "spj" else "<spj ") not in xml


@pytest.mark.parametrize(
    "target, options",
    [
        ("icpc", {}),
        ("hoj", {}),
        ("uoj", {}),
        ("dmoj", {}),
        ("qduoj", {}),
        ("fps", {"profile": "qduoj-1.2"}),
    ],
)
def test_native_hustoj_checker_is_rejected_for_incompatible_targets(
    tmp_path: Path, target: str, options: dict[str, str]
) -> None:
    source = _hydro(
        tmp_path,
        "checker_type: hustoj\nchecker: checker.cc\n",
        **{"P1/testdata/checker.cc": "int main() { return 0; }\n"},
    )
    output = tmp_path / "out"

    with pytest.raises(ValueError, match="HUSTOJ native checker"):
        convert_package(
            source, output, source_format="hydro", target_format=target, options=options
        )

    report = json.loads((output / REPORT_FILENAME).read_text(encoding="utf-8"))
    assert any(
        issue["code"] == "hustoj-checker-protocol" and issue["severity"] == "fatal"
        for issue in report["issues"]
    )
    assert not list(output.glob("*.zip"))


@pytest.mark.parametrize(
    "config",
    [
        "cases: [{input: a.in, output: a.out, score: 0}, {input: b.in, output: b.out, score: 100}]\n",
        "subtasks:\n  - {id: 1, score: 0, cases: [{input: a.in, output: a.out}]}\n"
        "  - {id: 2, score: 100, if: [1], cases: [{input: b.in, output: b.out}]}\n",
    ],
)
def test_hydro_zero_score_cases_still_participate_in_judging(
    tmp_path: Path, config: str
) -> None:
    output = tmp_path / "out"

    convert_package(
        _hydro(tmp_path, config), output, source_format="hydro", target_format="icpc"
    )

    with zipfile.ZipFile(next(output.glob("*.zip"))) as archive:
        assert (
            len(
                [
                    name
                    for name in archive.namelist()
                    if name.startswith("data/secret/") and name.endswith(".in")
                ]
            )
            == 2
        )
        assert not any(name.startswith("data/sample/") for name in archive.namelist())


@pytest.mark.parametrize("legacy_names", [False, True])
def test_hydro_zero_subtask_id_is_a_valid_dependency(
    tmp_path: Path, legacy_names: bool
) -> None:
    names = ("name: first, ", "name: second, ") if legacy_names else ("", "")
    source = _hydro(
        tmp_path,
        "subtasks:\n"
        f"  - {{{names[0]}id: 0, score: 40, cases: [{{input: a.in, output: a.out}}]}}\n"
        f"  - {{{names[1]}id: 1, score: 60, if: [0], cases: [{{input: b.in, output: b.out}}]}}\n",
    )
    output = tmp_path / "out"

    convert_package(source, output, source_format="hydro", target_format="dmoj")

    with zipfile.ZipFile(next(output.glob("*.zip"))) as archive:
        config = yaml.safe_load(archive.read("init.yml"))
    assert config["test_cases"][1]["dependencies"] == [1]


def test_hydro_explicit_samples_remain_samples(tmp_path: Path) -> None:
    source = _hydro(
        tmp_path,
        "cases: [{input: a.in, output: a.out, sample: true}, {input: b.in, output: b.out}]\n",
    )
    output = tmp_path / "out"

    convert_package(source, output, source_format="hydro", target_format="icpc")

    with zipfile.ZipFile(next(output.glob("*.zip"))) as archive:
        sample_inputs = [
            name
            for name in archive.namelist()
            if name.startswith("data/sample/") and name.endswith(".in")
        ]
        assert len(sample_inputs) == 1
        assert archive.read(sample_inputs[0]) == b"1\n"
        assert "data/secret/b.in" in archive.namelist()


def test_hydro_configured_sample_filename_is_still_a_judged_case(
    tmp_path: Path,
) -> None:
    source = _hydro(
        tmp_path,
        "cases: [sample.in]\n",
        **{"P1/testdata/sample.in": "3\n", "P1/testdata/sample.out": "3\n"},
    )
    output = tmp_path / "out"

    convert_package(source, output, source_format="hydro", target_format="icpc")

    with zipfile.ZipFile(next(output.glob("*.zip"))) as archive:
        assert "data/secret/sample.in" in archive.namelist()
        assert not any(name.startswith("data/sample/") for name in archive.namelist())


@pytest.mark.parametrize("target", ["hydro", "icpc", "dmoj"])
def test_hoj_average_scoring_is_not_silently_changed_to_minimum(
    tmp_path: Path, target: str
) -> None:
    source = _zip(
        tmp_path / "hoj.zip",
        {
            "problem_1.json": json.dumps(
                {
                    "problem": {
                        "title": "Sum",
                        "type": 1,
                        "judgeCaseMode": "subtask_average",
                    },
                    "samples": [
                        {"input": "1.in", "output": "1.out", "score": 5, "groupNum": 1},
                        {"input": "2.in", "output": "2.out", "score": 5, "groupNum": 1},
                    ],
                }
            ),
            "problem_1/1.in": "1\n",
            "problem_1/1.out": "1\n",
            "problem_1/2.in": "2\n",
            "problem_1/2.out": "2\n",
        },
    )
    output = tmp_path / "out"

    with pytest.raises(ValueError, match="subtask_average"):
        convert_package(source, output, source_format="hoj", target_format=target)

    report = json.loads((output / REPORT_FILENAME).read_text(encoding="utf-8"))
    assert any(
        issue["severity"] == "fatal" and "subtask_average" in issue["message"]
        for issue in report["issues"]
    )
    assert not list(output.glob("*.zip"))
