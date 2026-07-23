from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
RUNNER_DIR = ROOT / "runner"
sys.path.insert(0, str(RUNNER_DIR))

import package_converter  # noqa: E402
from package_adapters import _filter_problems  # noqa: E402
from package_converter import REPORT_FILENAME, convert_package  # noqa: E402
from package_ir import Problem  # noqa: E402
from package_security import load_json_file, load_yaml_file  # noqa: E402


def _hydro_package(path: Path) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "P1000/problem.yaml", "title: A + B\npid: P1000\ntag:\n  - math\n"
        )
        archive.writestr("P1000/problem_en.md", "# A + B\n\nAdd two numbers.\n")
        archive.writestr(
            "P1000/testdata/config.yaml",
            "type: default\ntime: 1500ms\nmemory: 256m\n"
            "cases:\n  - input: 1.in\n    output: 1.ans\n",
        )
        archive.writestr("P1000/testdata/1.in", "1 2\n")
        archive.writestr("P1000/testdata/1.ans", "3\n")
        archive.writestr(
            "P1000/testdata/solution.cpp",
            "#include <iostream>\nint main() { long long a, b; std::cin >> a >> b; std::cout << a + b << '\\n'; }\n",
        )
        archive.writestr("P1000/additional_file/samples/1.in", "2 3\n")
        archive.writestr("P1000/additional_file/samples/1.out", "5\n")


def _zip_directory(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source).as_posix())


def test_copy_output_rejects_symlink_without_reading_target(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("must not be copied", encoding="utf-8")
    (source / "leak.txt").symlink_to(secret)

    with pytest.raises(ValueError, match="symbolic link"):
        package_converter._copy_output(source, destination)

    assert not (destination / "leak.txt").exists()


def test_only_selector_rejects_normalized_name_collision() -> None:
    problems = [
        Problem(id="one", slug="A+B", title="First"),
        Problem(id="two", slug="A B", title="Second"),
    ]

    with pytest.raises(ValueError, match="ambiguous problem selector"):
        _filter_problems(problems, ["a-b"])


def test_only_selector_deduplicates_repeated_selection() -> None:
    problem = Problem(id="one", slug="sum", title="Sum")

    assert _filter_problems([problem], ["sum", "SUM"]) == [problem]


def test_ambiguous_auto_detection_writes_failure_report(tmp_path: Path) -> None:
    source = tmp_path / "ambiguous.zip"
    output = tmp_path / "output"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("P1000/problem.yaml", "title: Ambiguous\n")
        archive.writestr("P1000/testdata/config.yaml", "cases: []\n")
        archive.writestr("init.yml", "test_cases: []\n")

    with pytest.raises(ValueError, match="ambiguous source format"):
        convert_package(source, output, source_format="auto", target_format="fps")

    report = json.loads((output / REPORT_FILENAME).read_text())
    assert report["source_format"] == "auto"
    assert report["counts"]["fatal"] == 1
    assert report["issues"][0]["code"] == "source-detection-error"


def test_explicit_source_mismatch_writes_failure_report(tmp_path: Path) -> None:
    source = tmp_path / "hydro.zip"
    output = tmp_path / "output"
    _hydro_package(source)

    with pytest.raises(ValueError, match="source format mismatch"):
        convert_package(source, output, source_format="dmoj", target_format="fps")

    report = json.loads((output / REPORT_FILENAME).read_text())
    assert report["source_format"] == "dmoj"
    assert report["issues"][0]["code"] == "source-format-mismatch"


def test_polygon_icpc_2025_reuses_icpc_route_before_ir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested_targets: list[str] = []

    def fake_command(*args: object, target: str, **kwargs: object) -> list[str]:
        requested_targets.append(target)
        return ["converter"]

    monkeypatch.setattr(package_converter, "_polygon_command", fake_command)
    monkeypatch.setattr(
        package_converter.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )

    with pytest.raises(ValueError, match="exited with code 1"):
        package_converter._run_polygon_route(
            tmp_path / "polygon.zip",
            tmp_path / "output",
            target_format="icpc",
            loss_policy="warn",
            only=(),
            options={"profile": "2025-09"},
            workspace=tmp_path,
        )

    assert requested_targets == ["icpc"]


@pytest.mark.parametrize("target", ["fps", "qduoj", "uoj", "dmoj"])
def test_new_format_roundtrip_through_hydro(tmp_path: Path, target: str) -> None:
    source = tmp_path / "hydro.zip"
    first_output = tmp_path / target
    second_output = tmp_path / "hydro-result"
    first_output.mkdir()
    second_output.mkdir()
    _hydro_package(source)

    first_report = convert_package(
        source, first_output, source_format="hydro", target_format=target
    )
    assert first_report["problem_count"] == 1
    assert (first_output / REPORT_FILENAME).is_file()

    intermediate = tmp_path / f"{target}.zip"
    _zip_directory(first_output, intermediate)
    second_report = convert_package(
        intermediate, second_output, source_format="auto", target_format="hydro"
    )

    assert second_report["source_format"] == target
    packages = list(second_output.glob("*.zip"))
    assert len(packages) == 1
    with zipfile.ZipFile(packages[0]) as archive:
        names = set(archive.namelist())
        assert "P1000/problem.yaml" in names
        assert "P1000/testdata/1.in" in names
        assert archive.read("P1000/testdata/1.ans") == b"3\n"


def test_generic_directory_to_hydro_records_defaults(tmp_path: Path) -> None:
    source = tmp_path / "generic.zip"
    output = tmp_path / "output"
    output.mkdir()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("sum/1.in", "1 2\n")
        archive.writestr("sum/1.out", "3\n")
        archive.writestr("sum/statement.md", "# Sum\n")

    report = convert_package(
        source, output, source_format="generic", target_format="hydro"
    )

    assert report["counts"]["warning"] == 1
    assert any(issue["code"] == "generic-default-limits" for issue in report["issues"])


def test_generic_multi_problem_directories_remain_separate(tmp_path: Path) -> None:
    source = tmp_path / "generic-multi.zip"
    output = tmp_path / "output"
    output.mkdir()
    with zipfile.ZipFile(source, "w") as archive:
        for slug in ("sum", "max"):
            archive.writestr(f"{slug}/statement.md", f"# {slug}\n")
            archive.writestr(f"{slug}/1.in", "1 2\n")
            archive.writestr(f"{slug}/1.out", "3\n")

    report = convert_package(
        source, output, source_format="generic", target_format="hydro"
    )

    assert report["problem_count"] == 2
    assert len(list(output.glob("*.zip"))) == 2


@pytest.mark.parametrize("target", ["icpc", "uoj", "dmoj"])
def test_colliding_problem_slugs_do_not_overwrite_artifacts(
    tmp_path: Path, target: str
) -> None:
    source = tmp_path / "generic-collisions.zip"
    output = tmp_path / target
    output.mkdir()
    with zipfile.ZipFile(source, "w") as archive:
        for directory in ("a+b", "a b"):
            archive.writestr(f"{directory}/statement.md", f"# {directory}\n")
            archive.writestr(f"{directory}/1.in", "1 2\n")
            archive.writestr(f"{directory}/1.out", "3\n")

    report = convert_package(
        source,
        output,
        source_format="generic",
        target_format=target,
    )

    assert report["problem_count"] == 2
    assert len(list(output.glob("*.zip"))) == 2


def test_generic_unpaired_test_is_fatal(tmp_path: Path) -> None:
    source = tmp_path / "generic-unpaired.zip"
    output = tmp_path / "output"
    output.mkdir()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("sum/statement.md", "# Sum\n")
        archive.writestr("sum/1.in", "1 2\n")
        archive.writestr("sum/2.in", "3 4\n")
        archive.writestr("sum/1.out", "3\n")

    with pytest.raises(ValueError, match="unpaired test data"):
        convert_package(source, output, source_format="generic", target_format="hydro")

    assert {path.name for path in output.iterdir()} == {REPORT_FILENAME}
    report = json.loads((output / REPORT_FILENAME).read_text())
    assert report["counts"]["fatal"] >= 1


@pytest.mark.parametrize(
    ("source_format", "target_format"),
    [
        ("hydro", "fps"),
        ("icpc", "hydro"),
        ("fps", "hydro"),
        ("qduoj", "hydro"),
        ("uoj", "hydro"),
        ("dmoj", "hydro"),
    ],
)
def test_problem_without_secret_tests_is_fatal_and_reported(
    tmp_path: Path, source_format: str, target_format: str
) -> None:
    source = tmp_path / f"empty-{source_format}.zip"
    output = tmp_path / "output"
    output.mkdir()
    with zipfile.ZipFile(source, "w") as archive:
        if source_format == "hydro":
            archive.writestr("P1000/problem.yaml", "title: Empty\npid: P1000\n")
            archive.writestr("P1000/problem_en.md", "# Empty\n")
            archive.writestr("P1000/testdata/config.yaml", "cases: []\n")
        elif source_format == "icpc":
            archive.writestr(
                "problem.yaml",
                "problem_format_version: legacy-icpc\nname: Empty\n",
            )
            archive.writestr("problem_statement/problem.en.md", "# Empty\n")
            archive.writestr("data/secret/.keep", "")
        elif source_format == "fps":
            archive.writestr(
                "problem.xml",
                '<fps version="1.6"><item><title>Empty</title>'
                "<description># Empty</description></item></fps>",
            )
        elif source_format == "qduoj":
            archive.writestr(
                "1/problem.json",
                json.dumps(
                    {
                        "display_id": "1",
                        "title": "Empty",
                        "description": {"format": "markdown", "value": "# Empty"},
                        "test_case_score": [],
                    }
                ),
            )
            archive.writestr("1/testcase/.keep", "")
        elif source_format == "uoj":
            archive.writestr(
                "problem.conf",
                "n_tests 0\nuse_builtin_judger on\nuse_builtin_checker wcmp\n",
            )
            archive.writestr("statement.md", "# Empty\n")
        else:
            archive.writestr("init.yml", "test_cases: []\n")
            archive.writestr("statement.md", "# Empty\n")

    with pytest.raises(ValueError, match="no non-sample test cases"):
        convert_package(
            source,
            output,
            source_format=source_format,
            target_format=target_format,
        )

    report = json.loads((output / REPORT_FILENAME).read_text())
    assert any(issue["code"] == "missing-secret-tests" for issue in report["issues"])


def test_hydro_missing_configured_checker_is_fatal_and_reported(
    tmp_path: Path,
) -> None:
    source = tmp_path / "missing-checker.zip"
    output = tmp_path / "output"
    output.mkdir()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("P1000/problem.yaml", "title: Sum\npid: P1000\n")
        archive.writestr("P1000/problem_en.md", "# Sum\n")
        archive.writestr(
            "P1000/testdata/config.yaml",
            "checker: missing.cpp\ncases:\n  - input: 1.in\n    output: 1.ans\n",
        )
        archive.writestr("P1000/testdata/1.in", "1 2\n")
        archive.writestr("P1000/testdata/1.ans", "3\n")

    with pytest.raises(ValueError, match="missing checker file"):
        convert_package(source, output, source_format="hydro", target_format="fps")

    report = json.loads((output / REPORT_FILENAME).read_text())
    assert any(issue["code"] == "hydro-missing-checker" for issue in report["issues"])


def test_hydro_missing_declared_case_file_is_not_silently_skipped(
    tmp_path: Path,
) -> None:
    source = tmp_path / "missing-case.zip"
    output = tmp_path / "output"
    output.mkdir()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("P1000/problem.yaml", "title: Sum\npid: P1000\n")
        archive.writestr("P1000/problem_en.md", "# Sum\n")
        archive.writestr(
            "P1000/testdata/config.yaml",
            "cases:\n"
            "  - input: 1.in\n    output: 1.ans\n"
            "  - input: 2.in\n    output: 2.ans\n",
        )
        archive.writestr("P1000/testdata/1.in", "1 2\n")
        archive.writestr("P1000/testdata/1.ans", "3\n")
        archive.writestr("P1000/testdata/2.in", "2 3\n")

    with pytest.raises(ValueError, match="missing output file"):
        convert_package(source, output, source_format="hydro", target_format="fps")

    report = json.loads((output / REPORT_FILENAME).read_text())
    assert any(
        issue["code"] == "hydro-missing-test-output" for issue in report["issues"]
    )


def test_hydro_malformed_case_entry_is_reported_instead_of_skipped(
    tmp_path: Path,
) -> None:
    source = tmp_path / "malformed-case.zip"
    output = tmp_path / "output"
    output.mkdir()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("P1000/problem.yaml", "title: Sum\npid: P1000\n")
        archive.writestr("P1000/problem_en.md", "# Sum\n")
        archive.writestr("P1000/testdata/config.yaml", "cases:\n  - 42\n")
        archive.writestr("P1000/testdata/1.in", "1 2\n")
        archive.writestr("P1000/testdata/1.ans", "3\n")

    with pytest.raises(ValueError, match="unsupported config entry"):
        convert_package(source, output, source_format="hydro", target_format="fps")

    report = json.loads((output / REPORT_FILENAME).read_text())
    assert any(
        issue["code"] == "hydro-invalid-test-entry" for issue in report["issues"]
    )


def test_icpc_unpaired_secret_test_is_fatal(tmp_path: Path) -> None:
    source = tmp_path / "icpc-unpaired.zip"
    output = tmp_path / "output"
    output.mkdir()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("problem.yaml", "name: Sum\nvalidation: default\n")
        archive.writestr("problem_statement/problem.en.md", "# Sum\n")
        archive.writestr("data/secret/1.in", "1 2\n")
        archive.writestr("data/secret/1.ans", "3\n")
        archive.writestr("data/secret/2.in", "2 3\n")

    with pytest.raises(ValueError, match="unpaired test file"):
        convert_package(source, output, source_format="icpc", target_format="hydro")

    report = json.loads((output / REPORT_FILENAME).read_text())
    assert any(issue["code"] == "icpc-unpaired-test" for issue in report["issues"])


def test_uoj_missing_declared_extra_test_is_fatal(tmp_path: Path) -> None:
    source = tmp_path / "uoj-extra.zip"
    output = tmp_path / "output"
    output.mkdir()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(
            "problem.conf",
            "n_tests 1\nn_ex_tests 1\nn_sample_tests 1\n"
            "input_pre input\ninput_suf in\noutput_pre output\noutput_suf out\n"
            "use_builtin_judger on\nuse_builtin_checker wcmp\n",
        )
        archive.writestr("statement.md", "# Sum\n")
        archive.writestr("input1.in", "1 2\n")
        archive.writestr("output1.out", "3\n")
        archive.writestr("ex_input1.in", "2 3\n")

    with pytest.raises(ValueError, match="missing UOJ extra test"):
        convert_package(source, output, source_format="uoj", target_format="hydro")

    report = json.loads((output / REPORT_FILENAME).read_text())
    assert any(issue["code"] == "uoj-missing-extra-test" for issue in report["issues"])


def test_dmoj_missing_bridged_checker_is_fatal(tmp_path: Path) -> None:
    source = tmp_path / "dmoj-checker.zip"
    output = tmp_path / "output"
    output.mkdir()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(
            "init.yml",
            "test_cases:\n"
            "  - in: 1.in\n    out: 1.out\n"
            "checker:\n"
            "  name: bridged\n"
            "  args:\n"
            "    files:\n"
            "      - missing.cpp\n",
        )
        archive.writestr("statement.md", "# Sum\n")
        archive.writestr("1.in", "1 2\n")
        archive.writestr("1.out", "3\n")

    with pytest.raises(ValueError, match="no existing source file"):
        convert_package(source, output, source_format="dmoj", target_format="hydro")

    report = json.loads((output / REPORT_FILENAME).read_text())
    assert any(issue["code"] == "dmoj-missing-checker" for issue in report["issues"])


def test_missing_statement_is_a_strict_mode_loss(tmp_path: Path) -> None:
    source = tmp_path / "missing-statement.zip"
    output = tmp_path / "output"
    output.mkdir()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("P1000/problem.yaml", "title: Sum\npid: P1000\n")
        archive.writestr(
            "P1000/testdata/config.yaml",
            "cases:\n  - input: 1.in\n    output: 1.ans\n",
        )
        archive.writestr("P1000/testdata/1.in", "1 2\n")
        archive.writestr("P1000/testdata/1.ans", "3\n")

    with pytest.raises(ValueError, match="loss_policy=error"):
        convert_package(
            source,
            output,
            source_format="hydro",
            target_format="fps",
            loss_policy="error",
        )

    report = json.loads((output / REPORT_FILENAME).read_text())
    assert any(issue["code"] == "missing-statement" for issue in report["issues"])


def test_source_reader_failure_produces_a_structured_report(tmp_path: Path) -> None:
    source = tmp_path / "broken-qduoj.zip"
    output = tmp_path / "output"
    output.mkdir()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("1/problem.json", "{not json")
        archive.writestr("1/testcase/.keep", "")

    with pytest.raises(ValueError, match="invalid JSON"):
        convert_package(source, output, source_format="qduoj", target_format="hydro")

    report = json.loads((output / REPORT_FILENAME).read_text())
    assert report["problem_count"] == 0
    assert report["counts"]["fatal"] == 1
    assert report["issues"][0]["code"] == "source-read-error"


def test_hoj_reader_rejects_duplicate_top_level_json_keys(tmp_path: Path) -> None:
    source = tmp_path / "duplicate-hoj.zip"
    output = tmp_path / "output"
    output.mkdir()
    document = '{"problem":{"title":"first"},"problem":{"title":"second"},"samples":[]}'
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("problem_1.json", document)
        archive.writestr("problem_1/.keep", "")

    with pytest.raises(ValueError, match="duplicate JSON"):
        convert_package(source, output, source_format="hoj", target_format="hydro")

    report = json.loads((output / REPORT_FILENAME).read_text())
    assert report["issues"][0]["code"] == "source-read-error"


def test_hoj_reader_rejects_invalid_embedded_extra_file_json(tmp_path: Path) -> None:
    source = tmp_path / "invalid-hoj-extra.zip"
    output = tmp_path / "output"
    output.mkdir()
    document = {
        "problem": {"title": "Sum"},
        "samples": [{"input": "1.in", "output": "1.out"}],
        "userExtraFile": "{not json",
    }
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("problem_1.json", json.dumps(document))
        archive.writestr("problem_1/1.in", "1\n")
        archive.writestr("problem_1/1.out", "1\n")

    with pytest.raises(ValueError, match="embedded HOJ userExtraFile"):
        convert_package(source, output, source_format="hoj", target_format="hydro")

    report = json.loads((output / REPORT_FILENAME).read_text())
    assert report["issues"][0]["code"] == "source-read-error"


def test_hydro_reader_rejects_duplicate_template_json_keys(tmp_path: Path) -> None:
    source = tmp_path / "duplicate-template.zip"
    output = tmp_path / "output"
    output.mkdir()
    _hydro_package(source)
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr(
            "P1000/additional_file/code_templates.json",
            '[{"language":"C++","code":"first","code":"second"}]',
        )

    with pytest.raises(ValueError, match="duplicate JSON"):
        convert_package(source, output, source_format="hydro", target_format="qduoj")

    report = json.loads((output / REPORT_FILENAME).read_text())
    assert report["issues"][0]["code"] == "source-read-error"


def test_empty_target_writer_output_is_fatal_and_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "hydro.zip"
    output = tmp_path / "output"
    output.mkdir()
    _hydro_package(source)
    original = package_converter.ADAPTERS["qduoj"]
    monkeypatch.setitem(
        package_converter.ADAPTERS,
        "qduoj",
        SimpleNamespace(
            read=original.read,
            write=lambda bundle, staged, options: [],
            validate_target=original.validate_target,
        ),
    )

    with pytest.raises(ValueError, match="without producing importable files"):
        convert_package(source, output, source_format="hydro", target_format="qduoj")

    report = json.loads((output / REPORT_FILENAME).read_text())
    assert any(
        issue["code"] == "missing-target-artifacts" for issue in report["issues"]
    )


def test_loss_policy_error_rejects_uoj_without_statement(tmp_path: Path) -> None:
    source = tmp_path / "uoj.zip"
    output = tmp_path / "output"
    output.mkdir()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(
            "problem.conf",
            "n_tests 1\ninput_pre input\ninput_suf in\noutput_pre output\noutput_suf out\n"
            "time_limit 1\nmemory_limit 256\nuse_builtin_judger on\nuse_builtin_checker wcmp\n",
        )
        archive.writestr("input1.in", "1\n")
        archive.writestr("output1.out", "1\n")

    with pytest.raises(ValueError, match="loss_policy=error"):
        convert_package(
            source,
            output,
            source_format="uoj",
            target_format="hydro",
            loss_policy="error",
        )
    assert {path.name for path in output.iterdir()} == {REPORT_FILENAME}


def test_fps_rejects_external_entity(tmp_path: Path) -> None:
    source = tmp_path / "fps.zip"
    output = tmp_path / "output"
    output.mkdir()
    xml = """<?xml version="1.0"?>
<!DOCTYPE fps [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<fps version="1.6"><item><title>&xxe;</title></item></fps>
"""
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("problem.xml", xml)

    with pytest.raises(ValueError, match="unsafe FPS XML"):
        convert_package(source, output, source_format="fps", target_format="hydro")


def test_yaml_alias_limit_is_enforced(tmp_path: Path) -> None:
    yaml_path = tmp_path / "aliases.yml"
    aliases = ", ".join("*base" for _ in range(40))
    yaml_path.write_text(f"base: &base [1]\nvalue: [{aliases}]\n", encoding="utf-8")

    with pytest.raises(ValueError, match="alias limit"):
        load_yaml_file(yaml_path)


def test_json_extreme_nesting_fails_cleanly(tmp_path: Path) -> None:
    json_path = tmp_path / "deep.json"
    json_path.write_text('{"value":' * 2_000 + "null" + "}" * 2_000, encoding="utf-8")

    with pytest.raises(ValueError, match="invalid JSON|nesting limit"):
        load_json_file(json_path)


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e9999"])
def test_json_non_finite_numbers_are_rejected(tmp_path: Path, number: str) -> None:
    json_path = tmp_path / "non-finite.json"
    json_path.write_text(f'{{"score": {number}}}', encoding="utf-8")

    with pytest.raises(ValueError, match="non-finite"):
        load_json_file(json_path)


def test_yaml_non_finite_numbers_are_rejected(tmp_path: Path) -> None:
    yaml_path = tmp_path / "non-finite.yml"
    yaml_path.write_text("score: .nan\n", encoding="utf-8")

    with pytest.raises(ValueError, match="non-finite"):
        load_yaml_file(yaml_path)


def test_duplicate_metadata_keys_are_rejected(tmp_path: Path) -> None:
    json_path = tmp_path / "duplicate.json"
    json_path.write_text('{"title": "first", "title": "second"}', encoding="utf-8")
    yaml_path = tmp_path / "duplicate.yml"
    yaml_path.write_text("title: first\ntitle: second\n", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate JSON"):
        load_json_file(json_path)
    with pytest.raises(ValueError, match="duplicate YAML"):
        load_yaml_file(yaml_path)


def test_dmoj_archive_shares_the_outer_zip_expansion_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = tmp_path / "data.zip"
    with zipfile.ZipFile(nested, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("1.in", "x" * 300)
        archive.writestr("1.out", "y" * 300)
    source = tmp_path / "dmoj.zip"
    init = "archive: data.zip\ntest_cases:\n  - in: 1.in\n    out: 1.out\n"
    statement = "# Budget\n"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("init.yml", init)
        archive.writestr("statement.md", statement)
        archive.write(nested, "data.zip")
    with zipfile.ZipFile(source) as archive:
        outer_size = sum(info.file_size for info in archive.infolist())
    monkeypatch.setenv("P2H_MAX_ARCHIVE_UNCOMPRESSED_BYTES", str(outer_size + 300))
    output = tmp_path / "output"
    output.mkdir()

    with pytest.raises(ValueError, match="uncompressed size limit"):
        convert_package(source, output, source_format="dmoj", target_format="hydro")

    report = json.loads((output / REPORT_FILENAME).read_text())
    assert report["issues"][0]["code"] == "source-read-error"


def test_dmoj_default_regex_preserves_inferred_batches(tmp_path: Path) -> None:
    data_archive = tmp_path / "cases.zip"
    with zipfile.ZipFile(data_archive, "w") as archive:
        for name in ("1", "2.1", "2.2"):
            archive.writestr(f"{name}.in", f"input {name}\n")
            archive.writestr(f"{name}.out", f"output {name}\n")
    source = tmp_path / "dmoj-regex.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("init.yml", "archive: cases.zip\npoints: 1\n")
        archive.writestr("statement.md", "# Regex cases\n")
        archive.write(data_archive, "cases.zip")
    output = tmp_path / "hydro"
    output.mkdir()

    report = convert_package(
        source, output, source_format="dmoj", target_format="hydro"
    )

    assert any(issue["code"] == "dmoj-regex-cases" for issue in report["issues"])
    with zipfile.ZipFile(next(output.glob("*.zip"))) as archive:
        config = archive.read("P1000/testdata/config.yaml").decode()
        assert "subtasks:" in config
        assert (
            len(
                [
                    name
                    for name in archive.namelist()
                    if name.startswith("P1000/testdata/") and name.endswith(".in")
                ]
            )
            == 3
        )


def test_dmoj_explicit_empty_test_list_does_not_infer_files(tmp_path: Path) -> None:
    data_archive = tmp_path / "cases.zip"
    with zipfile.ZipFile(data_archive, "w") as archive:
        archive.writestr("1.in", "1\n")
        archive.writestr("1.out", "1\n")
    source = tmp_path / "dmoj-empty-list.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("init.yml", "archive: cases.zip\ntest_cases: []\n")
        archive.writestr("statement.md", "# Empty list\n")
        archive.write(data_archive, "cases.zip")
    output = tmp_path / "hydro"
    output.mkdir()

    with pytest.raises(ValueError, match="no non-sample test cases"):
        convert_package(source, output, source_format="dmoj", target_format="hydro")

    report = json.loads((output / REPORT_FILENAME).read_text())
    assert any(issue["code"] == "missing-secret-tests" for issue in report["issues"])


def test_dmoj_bridged_interactor_maps_to_hydro_interactive_config(
    tmp_path: Path,
) -> None:
    source = tmp_path / "dmoj-interactive.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(
            "init.yml",
            "test_cases:\n"
            "  - in: 1.in\n    out: 1.out\n    points: 1\n"
            "interactive:\n"
            "  files:\n    - interactor.cpp\n"
            "  type: testlib\n",
        )
        archive.writestr("statement.md", "# Interactive\n")
        archive.writestr("1.in", "1\n")
        archive.writestr("1.out", "1\n")
        archive.writestr("interactor.cpp", "int main() { return 0; }\n")
    output = tmp_path / "hydro"
    output.mkdir()

    convert_package(source, output, source_format="dmoj", target_format="hydro")

    with zipfile.ZipFile(next(output.glob("*.zip"))) as archive:
        config = archive.read("P1000/testdata/config.yaml").decode()
        assert "type: interactive" in config
        assert "interactor: interactor.cpp" in config
        assert "P1000/testdata/interactor.cpp" in archive.namelist()


def test_dmoj_custom_judge_is_not_misclassified_as_an_interactor(
    tmp_path: Path,
) -> None:
    source = tmp_path / "dmoj-custom-judge.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(
            "init.yml",
            "test_cases:\n"
            "  - in: 1.in\n    out: 1.out\n    points: 1\n"
            "custom_judge: grader.py\n",
        )
        archive.writestr("statement.md", "# Custom grader\n")
        archive.writestr("1.in", "1\n")
        archive.writestr("1.out", "1\n")
        archive.writestr("grader.py", "class Grader: pass\n")
    output = tmp_path / "hydro"
    output.mkdir()

    with pytest.raises(ValueError, match="custom_judge Python graders"):
        convert_package(source, output, source_format="dmoj", target_format="hydro")

    report = json.loads((output / REPORT_FILENAME).read_text())
    assert any(issue["code"] == "dmoj-custom-judger" for issue in report["issues"])


@pytest.mark.parametrize("checker", ["floats", "identical", "standard-ab"])
def test_unsupported_dmoj_builtin_checker_is_not_silently_downgraded(
    tmp_path: Path, checker: str
) -> None:
    source = tmp_path / f"dmoj-{checker}.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(
            "init.yml",
            "test_cases:\n"
            "  - in: 1.in\n    out: 1.out\n    points: 1\n"
            f"checker: {checker}\n",
        )
        archive.writestr("statement.md", "# Floats\n")
        archive.writestr("1.in", "1\n")
        archive.writestr("1.out", "1.0\n")
    output = tmp_path / "hydro"
    output.mkdir()

    with pytest.raises(ValueError, match="builtin checker is not supported"):
        convert_package(source, output, source_format="dmoj", target_format="hydro")

    report = json.loads((output / REPORT_FILENAME).read_text())
    assert any(
        issue["code"] == "unsupported-dmoj-checker" for issue in report["issues"]
    )


def test_non_equivalent_uoj_builtin_checker_is_not_approximated(
    tmp_path: Path,
) -> None:
    source = tmp_path / "uoj-lcmp.zip"
    output = tmp_path / "output"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(
            "problem.conf",
            "n_tests 1\ninput_pre input\ninput_suf in\noutput_pre output\n"
            "output_suf out\ntime_limit 1\nmemory_limit 256\n"
            "use_builtin_judger on\nuse_builtin_checker lcmp\n",
        )
        archive.writestr("input1.in", "1\n")
        archive.writestr("output1.out", "1\n")
        archive.writestr("statement.md", "# Lines\n")

    with pytest.raises(ValueError, match="UOJ builtin checker is not supported"):
        convert_package(source, output, source_format="uoj", target_format="hydro")

    report = json.loads((output / REPORT_FILENAME).read_text())
    assert any(issue["code"] == "unsupported-uoj-checker" for issue in report["issues"])


def test_multifile_dmoj_bridged_checker_is_not_partially_copied(
    tmp_path: Path,
) -> None:
    source = tmp_path / "dmoj-multifile-checker.zip"
    output = tmp_path / "output"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(
            "init.yml",
            "test_cases:\n  - in: 1.in\n    out: 1.out\n"
            "checker:\n  name: bridged\n  args:\n"
            "    files: [checker.cpp, helper.cpp]\n"
            "    lang: CPP20\n    type: testlib\n",
        )
        archive.writestr("1.in", "1\n")
        archive.writestr("1.out", "1\n")
        archive.writestr("checker.cpp", "int check();\n")
        archive.writestr("helper.cpp", "int check() { return 0; }\n")
        archive.writestr("statement.md", "# Checker\n")

    with pytest.raises(ValueError, match="multi-file DMOJ bridged checker"):
        convert_package(source, output, source_format="dmoj", target_format="hydro")

    report = json.loads((output / REPORT_FILENAME).read_text())
    assert any(issue["code"] == "dmoj-multifile-checker" for issue in report["issues"])


def test_dmoj_writer_uses_interactive_field_not_custom_judge(tmp_path: Path) -> None:
    source = tmp_path / "hydro-interactive.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("P1000/problem.yaml", "title: Interactive\npid: P1000\n")
        archive.writestr("P1000/problem_en.md", "# Interactive\n")
        archive.writestr(
            "P1000/testdata/config.yaml",
            "type: interactive\n"
            "interactor: interactor.cpp\n"
            "cases:\n  - input: 1.in\n    output: 1.ans\n",
        )
        archive.writestr("P1000/testdata/1.in", "1\n")
        archive.writestr("P1000/testdata/1.ans", "1\n")
        archive.writestr("P1000/testdata/interactor.cpp", "int main() { return 0; }\n")
    output = tmp_path / "dmoj"
    output.mkdir()

    convert_package(source, output, source_format="hydro", target_format="dmoj")

    with zipfile.ZipFile(next(output.glob("*.zip"))) as archive:
        config = archive.read("init.yml").decode()
        assert "interactive:" in config
        assert "custom_judge" not in config
        assert "interactor.cpp" in archive.namelist()


def test_dmoj_writer_maps_python_interactor_language(tmp_path: Path) -> None:
    source = tmp_path / "hydro-python-interactor.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("P1000/problem.yaml", "title: Interactive\npid: P1000\n")
        archive.writestr("P1000/problem_en.md", "# Interactive\n")
        archive.writestr(
            "P1000/testdata/config.yaml",
            "type: interactive\ninteractor: interactor.py\n"
            "cases:\n  - input: 1.in\n    output: 1.ans\n",
        )
        archive.writestr("P1000/testdata/1.in", "1\n")
        archive.writestr("P1000/testdata/1.ans", "1\n")
        archive.writestr("P1000/testdata/interactor.py", "print('ok')\n")
    output = tmp_path / "dmoj"

    convert_package(source, output, source_format="hydro", target_format="dmoj")

    package = next(output.glob("*.zip"))
    with zipfile.ZipFile(package) as archive:
        config = archive.read("init.yml").decode()
        assert "lang: PY3" in config
        assert "interactor.py" in archive.namelist()


def test_dmoj_writer_preserves_named_group_dependencies(tmp_path: Path) -> None:
    source = tmp_path / "hydro-dependencies.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("P1000/problem.yaml", "title: Groups\npid: P1000\n")
        archive.writestr("P1000/problem_en.md", "# Groups\n")
        archive.writestr(
            "P1000/testdata/config.yaml",
            "subtasks:\n"
            "  - name: base\n    score: 40\n"
            "    cases:\n      - input: 1.in\n        output: 1.ans\n"
            "  - name: full\n    score: 60\n    dependencies: [base]\n"
            "    cases:\n      - input: 2.in\n        output: 2.ans\n",
        )
        archive.writestr("P1000/testdata/1.in", "1\n")
        archive.writestr("P1000/testdata/1.ans", "1\n")
        archive.writestr("P1000/testdata/2.in", "2\n")
        archive.writestr("P1000/testdata/2.ans", "2\n")
    output = tmp_path / "dmoj"

    convert_package(source, output, source_format="hydro", target_format="dmoj")

    with zipfile.ZipFile(next(output.glob("*.zip"))) as archive:
        config = archive.read("init.yml").decode()
        assert "dependencies:\n  - 1" in config


def test_report_is_structured_json(tmp_path: Path) -> None:
    source = tmp_path / "hydro.zip"
    output = tmp_path / "fps"
    output.mkdir()
    _hydro_package(source)

    convert_package(source, output, source_format="hydro", target_format="fps")
    report = json.loads((output / REPORT_FILENAME).read_text())

    assert report["schema_version"] == 1
    assert report["source_format"] == "hydro"
    assert report["target_format"] == "fps"


def test_uoj_pdf_and_metadata_sidecars_roundtrip_to_hydro(tmp_path: Path) -> None:
    source = tmp_path / "hydro-pdf.zip"
    uoj_output = tmp_path / "uoj-output"
    hydro_output = tmp_path / "hydro-output"
    uoj_output.mkdir()
    hydro_output.mkdir()
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "P1000/problem.yaml",
            "title: PDF Sum\npid: P1000\ntag:\n  - pdf\nsource: contest\n",
        )
        archive.writestr("P1000/problem_zh.md", "@[pdf](file://statement.pdf)\n")
        archive.writestr("P1000/additional_file/statement.pdf", b"%PDF-1.4\nsidecar\n")
        archive.writestr(
            "P1000/testdata/config.yaml",
            "cases:\n  - input: 1.in\n    output: 1.ans\n",
        )
        archive.writestr("P1000/testdata/1.in", "1 2\n")
        archive.writestr("P1000/testdata/1.ans", "3\n")

    convert_package(source, uoj_output, source_format="hydro", target_format="uoj")
    wrapper = tmp_path / "uoj-wrapper.zip"
    _zip_directory(uoj_output, wrapper)
    report = convert_package(
        wrapper, hydro_output, source_format="auto", target_format="hydro"
    )

    assert report["source_format"] == "uoj"
    with zipfile.ZipFile(next(hydro_output.glob("*.zip"))) as archive:
        assert archive.read("P1000/problem.yaml").decode().find("PDF Sum") >= 0
        assert archive.read("P1000/problem_und.md") == b"@[pdf](file://statement.pdf)\n"
        assert (
            archive.read("P1000/additional_file/statement.pdf")
            == b"%PDF-1.4\nsidecar\n"
        )


def test_icpc_pdf_statements_with_the_same_basename_do_not_overwrite(
    tmp_path: Path,
) -> None:
    source = tmp_path / "icpc-pdfs.zip"
    output = tmp_path / "hydro"
    output.mkdir()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("problem.yaml", "name: PDF problem\nvalidation: default\n")
        archive.writestr("problem_statement/en/problem.pdf", b"%PDF English")
        archive.writestr("problem_statement/zh/problem.pdf", b"%PDF Chinese")
        archive.writestr("data/secret/1.in", "1\n")
        archive.writestr("data/secret/1.ans", "1\n")

    convert_package(source, output, source_format="icpc", target_format="hydro")

    with zipfile.ZipFile(next(output.glob("*.zip"))) as archive:
        assert archive.read("P1000/additional_file/problem.pdf") == b"%PDF English"
        assert archive.read("P1000/additional_file/problem-2.pdf") == b"%PDF Chinese"
        references = {
            archive.read("P1000/problem_und.md"),
            archive.read("P1000/problem_und_2.md"),
        }
        assert references == {
            b"@[pdf](file://problem.pdf)\n",
            b"@[pdf](file://problem-2.pdf)\n",
        }


def test_target_validation_participates_in_strict_loss_policy(tmp_path: Path) -> None:
    source = tmp_path / "hydro-file-io.zip"
    output = tmp_path / "output"
    output.mkdir()
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("P1000/problem.yaml", "title: Sum\npid: P1000\n")
        archive.writestr("P1000/problem_en.md", "# Sum\n")
        archive.writestr(
            "P1000/testdata/config.yaml",
            "filename: sum\ncases:\n  - input: 1.in\n    output: 1.ans\n",
        )
        archive.writestr("P1000/testdata/1.in", "1 2\n")
        archive.writestr("P1000/testdata/1.ans", "3\n")

    with pytest.raises(ValueError, match="loss_policy=error"):
        convert_package(
            source,
            output,
            source_format="hydro",
            target_format="fps",
            loss_policy="error",
        )

    assert {path.name for path in output.iterdir()} == {REPORT_FILENAME}


def test_icpc_2025_profile_uses_current_directory_layout(tmp_path: Path) -> None:
    source = tmp_path / "hydro.zip"
    output = tmp_path / "icpc-2025"
    output.mkdir()
    _hydro_package(source)

    report = convert_package(
        source,
        output,
        source_format="hydro",
        target_format="icpc",
        options={"profile": "2025-09"},
    )

    assert report["counts"]["fatal"] == 0
    package = next(output.glob("*.zip"))
    assert package.name == "p1000.zip"
    with zipfile.ZipFile(package) as archive:
        names = set(archive.namelist())
        assert "statement/problem.en.tex" in names
        assert "statement/problem.en.md" not in names
        assert "problem_statement/problem.html" not in names
        assert "domjudge-problem.ini" not in names
        assert "submissions/accepted/solution_1.cpp" in names
        problem_yaml = archive.read("problem.yaml").decode()
        assert "problem_format_version: 2025-09" in problem_yaml
        assert "name:" in problem_yaml and "en: A + B" in problem_yaml
        assert "type: pass-fail" in problem_yaml
        assert "uuid:" in problem_yaml


def test_icpc_2025_names_exactly_match_multilingual_statement_files(
    tmp_path: Path,
) -> None:
    source = tmp_path / "hydro-multilingual.zip"
    output = tmp_path / "icpc-2025"
    output.mkdir()
    _hydro_package(source)
    with zipfile.ZipFile(source, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("P1000/problem_zh_CN.md", "# A + B\n\n计算两数之和。\n")

    report = convert_package(
        source,
        output,
        source_format="hydro",
        target_format="icpc",
        options={"profile": "2025-09"},
    )

    assert not any(
        issue["code"] == "icpc-legacy-multilingual" for issue in report["issues"]
    )
    with zipfile.ZipFile(next(output.glob("*.zip"))) as archive:
        names = set(archive.namelist())
        assert "statement/problem.en.tex" in names
        assert "statement/problem.zh-cn.tex" in names
        problem_yaml = archive.read("problem.yaml").decode()
        assert "  en: A + B" in problem_yaml
        assert "  zh-cn: A + B" in problem_yaml


def test_icpc_legacy_profile_emits_required_icpc_subset_files(tmp_path: Path) -> None:
    source = tmp_path / "hydro.zip"
    output = tmp_path / "icpc-legacy"
    output.mkdir()
    _hydro_package(source)

    convert_package(
        source,
        output,
        source_format="hydro",
        target_format="icpc",
        options={"profile": "legacy-icpc", "license": "public domain"},
    )

    package = next(output.glob("*.zip"))
    assert package.name == "p1000.zip"
    with zipfile.ZipFile(package) as archive:
        names = set(archive.namelist())
        assert "problem_statement/problem.en.tex" in names
        assert "submissions/accepted/solution_1.cpp" in names
        assert "input_validators/validator.py" in names
        problem_yaml = archive.read("problem.yaml").decode()
        assert "problem_format_version: legacy" in problem_yaml
        assert "problem_format_version: legacy-icpc" not in problem_yaml
        assert "license: public domain" in problem_yaml
        assert "uuid:" in problem_yaml


def test_generated_icpc_tex_roundtrips_but_external_tex_stays_an_attachment(
    tmp_path: Path,
) -> None:
    hydro_source = tmp_path / "hydro.zip"
    icpc_output = tmp_path / "icpc"
    hydro_output = tmp_path / "hydro-output"
    icpc_output.mkdir()
    hydro_output.mkdir()
    _hydro_package(hydro_source)
    convert_package(
        hydro_source,
        icpc_output,
        source_format="hydro",
        target_format="icpc",
    )
    icpc_source = next(icpc_output.glob("*.zip"))
    with zipfile.ZipFile(icpc_source, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "problem_statement/problem.fr.tex",
            "\\problemname{Somme}\nTex source with custom commands.\n",
        )
        archive.writestr(
            "submissions/wrong_answer/wrong.cpp", "int main() { return 1; }\n"
        )

    report = convert_package(
        icpc_source,
        hydro_output,
        source_format="icpc",
        target_format="hydro",
    )

    assert {issue["code"] for issue in report["issues"]} >= {
        "icpc-tex-statement",
        "icpc-nonaccepted-submission",
    }
    package = next(hydro_output.glob("*.zip"))
    with zipfile.ZipFile(package) as archive:
        names = set(archive.namelist())
        assert (
            "P1000/additional_file/attachments/problem_statement/problem.fr.tex"
            in names
        )
        assert (
            "P1000/additional_file/attachments/problem_statement/problem.en.tex"
            not in names
        )
        assert "# A + B" in archive.read("P1000/problem_en.md").decode()
        assert (
            "P1000/additional_file/attachments/submissions/wrong_answer/wrong.cpp"
            in names
        )
        assert not any(name.endswith("solution_2.cpp") for name in names)


@pytest.mark.parametrize("target", ["icpc", "hoj", "fps", "qduoj", "uoj", "dmoj"])
def test_multilingual_statement_loss_is_reported(tmp_path: Path, target: str) -> None:
    source = tmp_path / "hydro-multilingual.zip"
    output = tmp_path / target
    output.mkdir()
    _hydro_package(source)
    with zipfile.ZipFile(source, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("P1000/problem_zh.md", "# A + B\n\n计算两数之和。\n")

    report = convert_package(
        source,
        output,
        source_format="hydro",
        target_format=target,
    )

    assert any(
        issue["severity"] == "loss" and "multilingual" in issue["code"]
        for issue in report["issues"]
    )


def test_all_non_polygon_source_target_combinations_smoke(tmp_path: Path) -> None:
    writable = ("hydro", "icpc", "hoj", "fps", "qduoj", "uoj", "dmoj")
    source_archives: dict[str, Path] = {}

    hydro_source = tmp_path / "source-hydro.zip"
    _hydro_package(hydro_source)
    source_archives["hydro"] = hydro_source

    for source_format in writable[1:]:
        generated = tmp_path / f"generated-{source_format}"
        generated.mkdir()
        convert_package(
            hydro_source,
            generated,
            source_format="hydro",
            target_format=source_format,
        )
        archive = tmp_path / f"source-{source_format}.zip"
        _zip_directory(generated, archive)
        source_archives[source_format] = archive

    generic = tmp_path / "source-generic.zip"
    with zipfile.ZipFile(generic, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("sum/statement.md", "# Sum\n\nAdd two integers.\n")
        archive.writestr("sum/1.in", "1 2\n")
        archive.writestr("sum/1.ans", "3\n")
    source_archives["generic"] = generic

    completed: set[tuple[str, str]] = set()
    for source_format, source_archive in source_archives.items():
        for target_format in writable:
            if source_format == target_format:
                continue
            output = tmp_path / f"matrix-{source_format}-to-{target_format}"
            output.mkdir()
            report = convert_package(
                source_archive,
                output,
                source_format=source_format,
                target_format=target_format,
            )
            assert report["source_format"] == source_format
            assert report["target_format"] == target_format
            assert report["problem_count"] == 1
            assert (output / REPORT_FILENAME).is_file()
            assert any(path.name != REPORT_FILENAME for path in output.iterdir())
            completed.add((source_format, target_format))

    assert len(completed) == 49
