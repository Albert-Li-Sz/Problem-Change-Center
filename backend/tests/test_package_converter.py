from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import zipfile
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
RUNNER_DIR = ROOT / "runner"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(RUNNER_DIR))

import package_converter  # noqa: E402
import format_bridge as bridge  # noqa: E402
from compat.corpus.build import build as build_compat_corpus  # noqa: E402
from adapters import ADAPTERS  # noqa: E402
from package_adapters import _filter_problems  # noqa: E402
from package_converter import REPORT_FILENAME, convert_package  # noqa: E402
from package_ir import (  # noqa: E402
    ConversionIssue,
    Problem,
    ProblemBundle,
    Program,
    RepairCandidate,
    Statement,
    TestCase as IRTestCase,
    compare_semantic_snapshots,
)
from package_repair import (  # noqa: E402
    populate_repair_suggestions,
    repair_source_archive,
)
from package_security import load_json_file, load_yaml_file  # noqa: E402
from progress import ProgressDeadlineExceeded, ProgressReporter  # noqa: E402


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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _read_bundle(archive: Path, source_format: str, workspace: Path) -> ProblemBundle:
    extracted = workspace / f"read-{source_format}"
    budget = bridge.ArchiveExtractionBudget.from_env()
    bridge._safe_extract_zip(archive, extracted, budget=budget)
    bundle = ADAPTERS[source_format].read(
        extracted, workspace / f"work-{source_format}", (), budget
    )
    bundle.validate_integrity()
    return bundle


def test_redistributable_core_corpus_converts_to_icpc_and_hoj(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus"
    build_compat_corpus(corpus)
    source = corpus / "hydro-core-rich.zip"

    for target in ("icpc", "hoj"):
        output = tmp_path / target
        output.mkdir()
        report = convert_package(
            source,
            output,
            source_format="hydro",
            target_format=target,
        )
        assert report["problem_count"] == 5
        assert report["counts"]["fatal"] == 0

    missing_output = tmp_path / "missing"
    with pytest.raises(ValueError, match="missing output"):
        convert_package(
            corpus / "hydro-missing-answer.zip",
            missing_output,
            source_format="hydro",
            target_format="hoj",
        )
    missing_report = json.loads(
        (missing_output / REPORT_FILENAME).read_text(encoding="utf-8")
    )
    assert missing_report["repair_suggestions"][0]["candidates"][0] == {
        "path": "P2000/testdata/1.out",
        "strategy": "extension-alias",
        "confidence": 0.95,
    }


def test_compatibility_manifest_drives_conversion_and_failure_matrix(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus"
    build_compat_corpus(corpus)
    manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 2
    assert manifest["license"] == "CC0-1.0"
    assert len(manifest["fixtures"]) == 11
    for fixture in manifest["fixtures"]:
        source = corpus / fixture["path"]
        assert source.is_file()
        for target in fixture.get("targets", []):
            output = tmp_path / f"{source.stem}-to-{target}"
            report = convert_package(
                source,
                output,
                source_format=fixture["source_format"],
                target_format=target,
            )
            assert report["problem_count"] == fixture["problem_count"]
            assert report["counts"]["fatal"] == 0

        semantic = fixture.get("semantic")
        if semantic:
            bundle = _read_bundle(
                source,
                fixture["source_format"],
                tmp_path / f"{source.stem}-semantic",
            )
            actual = {
                "case_count": sum(len(problem.cases) for problem in bundle.problems),
                "sample_count": sum(
                    case.sample for problem in bundle.problems for case in problem.cases
                ),
                "checker_count": sum(
                    problem.checker is not None for problem in bundle.problems
                ),
                "validator_count": sum(
                    problem.validator is not None for problem in bundle.problems
                ),
                "interactor_count": sum(
                    problem.interactor is not None for problem in bundle.problems
                ),
                "attachment_count": sum(
                    len(problem.attachments) for problem in bundle.problems
                ),
                "template_count": sum(
                    len(problem.templates) for problem in bundle.problems
                ),
                "solution_count": sum(
                    len(problem.solutions) for problem in bundle.problems
                ),
            }
            assert {key: actual[key] for key in semantic} == semantic

        invalid_target = fixture.get("invalid_target")
        if invalid_target is None:
            continue
        if fixture.get("requires_case_sensitive_fs"):
            probe = tmp_path / "CaseSensitiveProbe"
            probe.write_text("probe", encoding="utf-8")
            if probe.with_name("casesensitiveprobe").exists():
                continue
        output = tmp_path / f"{source.stem}-invalid"
        with pytest.raises(ValueError):
            convert_package(
                source,
                output,
                source_format=fixture["source_format"],
                target_format=invalid_target,
            )
        report = json.loads((output / REPORT_FILENAME).read_text(encoding="utf-8"))
        issue_codes = {issue["code"] for issue in report["issues"]}
        assert set(fixture["fatal_codes"]) <= issue_codes
        strategy = fixture.get("repair_strategy")
        if strategy == "upload":
            assert any(
                suggestion["requires_upload"]
                for suggestion in report["repair_suggestions"]
            )
        elif strategy:
            assert any(
                candidate["strategy"] == strategy
                for suggestion in report["repair_suggestions"]
                for candidate in suggestion["candidates"]
            )


def test_probhub_workspace_single_export_and_legacy_preserve_judge_semantics(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus"
    build_compat_corpus(corpus)

    workspace = _read_bundle(
        corpus / "probhub-workspace.zip",
        "probhub",
        tmp_path / "workspace-read",
    )
    assert [problem.id for problem in workspace.problems] == ["L01", "L02"]
    assert workspace.problems[0].title == "工作区求和"
    assert workspace.problems[0].time_ms == 1000
    assert workspace.problems[0].memory_mb == 256
    assert len(workspace.problems[0].solutions) == 1
    assert workspace.problems[0].validator is not None
    assert workspace.problems[1].checker is not None
    assert workspace.problems[1].checker.mode == "testlib"
    assert sum(case.sample for case in workspace.problems[0].cases) == 1
    assert sum(not case.sample for case in workspace.problems[0].cases) == 1
    first_statement = workspace.problems[0].statements[0].content or ""
    assert "## 样例" in first_statement
    assert "```input1\n1 2\n```" in first_statement
    assert "```output1\n3\n```" in first_statement
    second_statement = workspace.problems[1].statements[0].content or ""
    assert second_statement.count("```input1\n7\n```") == 1
    assert second_statement.count("```output1\n7\n```") == 1
    assert any(issue.code == "probhub-authoring-metadata" for issue in workspace.issues)

    single = _read_bundle(
        corpus / "probhub-single.zip",
        "probhub",
        tmp_path / "single-read",
    )
    assert len(single.problems) == 1
    assert single.problems[0].statements[0].format == "pdf"
    assert single.problems[0].checker is not None
    assert single.problems[0].checker.path is not None
    assert "output_validators/validate" in single.problems[0].checker.path.as_posix()

    legacy = _read_bundle(
        corpus / "probhub-legacy.zip",
        "probhub",
        tmp_path / "legacy-read",
    )
    assert len(legacy.problems) == 1
    assert legacy.problems[0].id == "legacy-sum"
    assert legacy.problems[0].title == "Legacy Sum"
    assert legacy.problems[0].time_ms == 2000
    assert legacy.problems[0].memory_mb == 512
    assert legacy.problems[0].statements[0].language == "zh"
    legacy_statement = legacy.problems[0].statements[0].content or ""
    assert "## 样例" in legacy_statement
    assert "```input1\n1 2\n```" in legacy_statement
    assert "```output1\n3\n```" in legacy_statement
    assert legacy.problems[0].checker is not None
    assert legacy.problems[0].validator is not None
    assert len(legacy.problems[0].solutions) == 1
    assert "probhub/brute.cpp" in legacy.problems[0].attachments
    assert "probhub/gen.py" in legacy.problems[0].attachments
    assert all(
        not name.endswith(".exe") and "/tmp/" not in f"/{name}"
        for name in legacy.problems[0].attachments
    )
    assert any(
        issue.code == "probhub-legacy-authoring-artifacts" for issue in legacy.issues
    )


@pytest.mark.parametrize(
    "archive_name",
    ["probhub-workspace.zip", "probhub-single.zip", "probhub-legacy.zip"],
)
def test_probhub_auto_detection_converts_to_hydro(
    tmp_path: Path, archive_name: str
) -> None:
    corpus = tmp_path / "corpus"
    build_compat_corpus(corpus)
    output = tmp_path / f"{Path(archive_name).stem}-hydro"

    report = convert_package(
        corpus / archive_name,
        output,
        source_format="auto",
        target_format="hydro",
    )

    assert report["source_format"] == "probhub"
    assert report["counts"]["fatal"] == 0
    if archive_name == "probhub-workspace.zip":
        with zipfile.ZipFile(output / "P1000-l01.zip") as archive:
            statement = archive.read("P1000/problem_und.md").decode("utf-8")
        assert "```input1\n1 2\n```" in statement
        assert "```output1\n3\n```" in statement


def test_direct_cli_progress_reporter_enforces_stage_timeout() -> None:
    with pytest.raises(ProgressDeadlineExceeded) as timeout:
        with ProgressReporter(
            output_format="none",
            total_timeout_seconds=10,
            idle_timeout_seconds=10,
            stage_timeout_seconds=1,
            problem_timeout_seconds=10,
        ) as reporter:
            reporter.phase("read", detail="simulating a stuck reader")
            time.sleep(2)

    assert timeout.value.kind == "stage"
    assert timeout.value.phase == "read"


def test_direct_cli_heartbeats_do_not_prevent_idle_timeout() -> None:
    stream = StringIO()
    with pytest.raises(ProgressDeadlineExceeded) as timeout:
        with ProgressReporter(
            output_format="jsonl",
            stream=stream,
            heartbeat_seconds=0.05,
            total_timeout_seconds=10,
            idle_timeout_seconds=1,
            stage_timeout_seconds=10,
            problem_timeout_seconds=10,
        ) as reporter:
            reporter.phase("read", detail="simulating a stuck reader")
            time.sleep(2)

    assert timeout.value.kind == "idle"
    assert '"event":"heartbeat"' in stream.getvalue()


def test_repair_candidate_confidence_ambiguity_and_role_separation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    (root / "Data").mkdir(parents=True)
    (root / "Data" / "Answer.ANS").write_text("3\n", encoding="utf-8")
    (root / "elsewhere").mkdir()
    classified_interactor = root / "elsewhere" / "checker.cpp"
    classified_interactor.write_text("int main(){}\n", encoding="utf-8")
    (root / "one").mkdir()
    (root / "two").mkdir()
    (root / "one" / "manual.pdf").write_bytes(b"%PDF-one")
    (root / "two" / "manual.pdf").write_bytes(b"%PDF-two")
    (root / "elsewhere" / "readme.txt").write_text("notes\n", encoding="utf-8")

    problem = Problem(
        id="p",
        slug="p",
        title="Repair candidates",
        interactor=Program("interactor", path=classified_interactor),
    )
    bundle = ProblemBundle("hydro", [problem])
    bundle.add_issue(
        "fatal",
        "case-only",
        "case mismatch",
        problem="p",
        field="cases",
        context={"expected_path": "data/answer.ans", "role": "output"},
    )
    bundle.add_issue(
        "fatal",
        "program-role",
        "missing checker",
        problem="p",
        field="checker",
        context={"expected_path": "wanted/checker.cpp", "role": "checker"},
    )
    bundle.add_issue(
        "fatal",
        "ambiguous-attachment",
        "missing attachment",
        problem="p",
        field="attachments",
        context={"expected_path": "docs/manual.pdf", "role": "attachment"},
    )
    bundle.add_issue(
        "fatal",
        "unique-attachment",
        "missing attachment",
        problem="p",
        field="attachments",
        context={"expected_path": "docs/readme.txt", "role": "attachment"},
    )

    populate_repair_suggestions(bundle, root)

    by_code = {
        suggestion.issue_code: suggestion for suggestion in bundle.repair_suggestions
    }
    assert by_code["case-only"].candidates == (
        RepairCandidate("Data/Answer.ANS", "case-only", 1.0),
    )
    assert by_code["program-role"].candidates == ()
    assert by_code["program-role"].requires_upload is True
    assert by_code["ambiguous-attachment"].candidates == ()
    assert by_code["unique-attachment"].candidates == (
        RepairCandidate("elsewhere/readme.txt", "unique-basename", 0.85),
    )


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


def test_explicit_compatible_source_override_writes_warning_report(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus"
    build_compat_corpus(corpus)
    output = tmp_path / "output"

    report = convert_package(
        corpus / "probhub-single.zip",
        output,
        source_format="icpc",
        target_format="hydro",
    )

    assert report["source_format"] == "icpc"
    assert report["target_format"] == "hydro"
    assert report["problem_count"] == 1
    assert report["counts"]["fatal"] == 0
    override = next(
        issue for issue in report["issues"] if issue["code"] == "source-format-override"
    )
    assert override["severity"] == "warning"
    assert override["context"] == {"requested": "icpc", "detected": "probhub"}


@pytest.mark.parametrize("source_format", ["icpc", "probhub"])
def test_domjudge_compatible_pdf_statement_to_hoj_is_rejected_without_partial_artifacts(
    tmp_path: Path, source_format: str
) -> None:
    source = tmp_path / "domjudge-pdf.zip"
    output = tmp_path / "output"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("problem.yaml", "name: PDF only\n")
        archive.writestr("domjudge-problem.ini", "short-name = A\n")
        archive.writestr("problem.pdf", b"%PDF-1.4\n%%EOF\n")
        archive.writestr("data/secret/1.in", "1\n")
        archive.writestr("data/secret/1.ans", "1\n")

    with pytest.raises(ValueError, match="cannot carry PDF statements"):
        convert_package(
            source, output, source_format=source_format, target_format="hoj"
        )

    assert {path.name for path in output.iterdir()} == {REPORT_FILENAME}
    report = json.loads((output / REPORT_FILENAME).read_text(encoding="utf-8"))
    assert report["artifacts"] == []
    assert any(
        issue["code"] == "hoj-pdf-statement-unsupported"
        and issue["severity"] == "fatal"
        for issue in report["issues"]
    )


def test_hydro_pdf_statement_to_hoj_uses_importable_placeholder(
    tmp_path: Path,
) -> None:
    source = tmp_path / "hydro-pdf.zip"
    output = tmp_path / "output"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("P1000/problem.yaml", "title: PDF only\npid: P1000\n")
        archive.writestr("P1000/problem_zh.md", "@[pdf](file://statement.pdf)\n")
        archive.writestr("P1000/additional_file/statement.pdf", b"%PDF-1.4\n%%EOF\n")
        archive.writestr(
            "P1000/testdata/config.yaml",
            "cases:\n  - input: 1.in\n    output: 1.ans\n",
        )
        archive.writestr("P1000/testdata/1.in", "1\n")
        archive.writestr("P1000/testdata/1.ans", "1\n")

    report = convert_package(source, output, source_format="hydro", target_format="hoj")

    assert report["counts"]["fatal"] == 0
    assert any(issue["code"] == "hoj-pdf-statement" for issue in report["issues"])
    document_path = next(output.glob("problem_*.json"))
    document = json.loads(document_path.read_text(encoding="utf-8"))
    description = document["problem"]["description"]
    assert "原题面仅提供 PDF" in description
    assert "file://" not in description

    wrapper = tmp_path / "hoj-wrapper.zip"
    _zip_directory(output, wrapper)
    roundtrip = _read_bundle(wrapper, "hoj", tmp_path / "roundtrip")
    assert len(roundtrip.problems) == 1
    assert "原题面仅提供 PDF" in (roundtrip.problems[0].statements[0].content or "")


def test_root_polygon_problem_is_wrapped_before_optimized_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "single-polygon.zip"
    output = tmp_path / "output"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("problem.xml", "<problem package='test'/>")
        archive.writestr("tests/01", "1 2\n")
        archive.writestr("tests/01.a", "3\n")

    captured_members: list[str] = []

    def fake_polygon_route(
        normalized_source: Path,
        route_output: Path,
        **kwargs: object,
    ) -> dict[str, object]:
        with zipfile.ZipFile(normalized_source) as archive:
            captured_members.extend(sorted(archive.namelist()))
        route_output.mkdir(parents=True, exist_ok=True)
        return {
            "schema_version": 2,
            "source_format": "polygon",
            "target_format": "hydro",
            "problem_count": 1,
            "counts": {"warning": 0, "loss": 0, "fatal": 0},
            "issues": [],
            "artifacts": ["single-polygon.zip"],
        }

    monkeypatch.setattr(package_converter, "_run_polygon_route", fake_polygon_route)

    report = convert_package(
        source,
        output,
        source_format="auto",
        target_format="hydro",
    )

    assert report["problem_count"] == 1
    assert captured_members == [
        "problems/single-polygon/problem.xml",
        "problems/single-polygon/tests/01",
        "problems/single-polygon/tests/01.a",
    ]


def test_root_polygon_linux_export_preserves_full_payload_before_optimized_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "polygon-single-linux.zip"
    output = tmp_path / "output"
    test_nodes = "\n".join(
        f'                <test method="{("manual" if index <= 3 else "generated")}"/>'
        for index in range(1, 34)
    )
    problem_xml = f"""<?xml version="1.0" encoding="utf-8"?>
<problem revision="1" short-name="linux-single">
    <names>
        <name language="chinese" value="Linux single problem"/>
    </names>
    <statements>
        <statement charset="UTF-8" language="chinese" path="statements/chinese/problem.tex" type="application/x-tex"/>
    </statements>
    <judging>
        <testset name="tests">
            <time-limit>2000</time-limit>
            <memory-limit>268435456</memory-limit>
            <test-count>33</test-count>
            <input-path-pattern>tests/%02d</input-path-pattern>
            <answer-path-pattern>tests/%02d.a</answer-path-pattern>
            <tests>
{test_nodes}
            </tests>
        </testset>
    </judging>
    <files>
        <resources>
            <file path="files/testlib.h" type="h.g++"/>
        </resources>
    </files>
    <assets>
        <checker name="std::wcmp.cpp" type="testlib">
            <source path="files/check.cpp" type="cpp.g++17"/>
            <copy path="check.cpp"/>
        </checker>
    </assets>
</problem>
"""
    statement = "\\section*{Linux single problem}\nFull statement payload.\n"
    checker = '#include "testlib.h"\nint main() { return 0; }\n'
    testlib = "// minimal testlib fixture\n"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("problem.xml", problem_xml)
        archive.writestr("statements/chinese/problem.tex", statement)
        archive.writestr("files/check.cpp", checker)
        archive.writestr("files/testlib.h", testlib)
        archive.writestr("check.cpp", checker)
        for index in range(1, 34):
            stem = f"{index:02d}"
            archive.writestr(f"tests/{stem}", f"input-{index}\n")
            archive.writestr(f"tests/{stem}.a", f"answer-{index}\n")

    captured: dict[str, bytes] = {}

    def fake_polygon_route(
        normalized_source: Path,
        route_output: Path,
        **kwargs: object,
    ) -> dict[str, object]:
        with zipfile.ZipFile(normalized_source) as archive:
            captured.update(
                {
                    info.filename: archive.read(info)
                    for info in archive.infolist()
                    if not info.is_dir()
                }
            )
        route_output.mkdir(parents=True, exist_ok=True)
        return {
            "schema_version": 2,
            "source_format": "polygon",
            "target_format": "hydro",
            "problem_count": 1,
            "counts": {"warning": 0, "loss": 0, "fatal": 0},
            "issues": [],
            "artifacts": ["P1000-linux-single.zip"],
        }

    monkeypatch.setattr(package_converter, "_run_polygon_route", fake_polygon_route)

    report = convert_package(
        source,
        output,
        source_format="auto",
        target_format="hydro",
    )

    assert report["problem_count"] == 1
    root = "problems/polygon-single-linux"
    assert captured[f"{root}/problem.xml"] == problem_xml.encode()
    assert captured[f"{root}/statements/chinese/problem.tex"] == statement.encode()
    assert captured[f"{root}/files/check.cpp"] == checker.encode()
    assert captured[f"{root}/files/testlib.h"] == testlib.encode()
    assert captured[f"{root}/check.cpp"] == checker.encode()

    expected_test_members = {
        f"{root}/tests/{index:02d}{suffix}"
        for index in range(1, 34)
        for suffix in ("", ".a")
    }
    actual_test_members = {
        name for name in captured if name.startswith(f"{root}/tests/")
    }
    assert actual_test_members == expected_test_members
    for index in range(1, 34):
        stem = f"{index:02d}"
        assert captured[f"{root}/tests/{stem}"] == f"input-{index}\n".encode()
        assert captured[f"{root}/tests/{stem}.a"] == f"answer-{index}\n".encode()


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


def test_icpc_interactor_dependencies_are_preserved_in_dmoj_and_roundtrip(
    tmp_path: Path,
) -> None:
    source = tmp_path / "domjudge-interactive.zip"
    output = tmp_path / "dmoj"
    header = b"// exact source header\ninline int sentinel() { return 7; }\n"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "problem.yaml",
            "name: Interactive\nvalidation: custom interactive\n",
        )
        archive.writestr("domjudge-problem.ini", "short-name = I\n")
        archive.writestr("problem.pdf", b"%PDF-1.4\n%%EOF\n")
        archive.writestr("data/secret/1.in", "1\n")
        archive.writestr("data/secret/1.ans", "1\n")
        archive.writestr(
            "output_validators/interactor/interactor.cc",
            '#include "testlib.h"\nint main() { return sentinel() == 7 ? 0 : 1; }\n',
        )
        archive.writestr("output_validators/interactor/testlib.h", header)

    report = convert_package(source, output, source_format="icpc", target_format="dmoj")

    assert report["counts"]["fatal"] == 0
    package = next(output.glob("*.zip"))
    with zipfile.ZipFile(package) as archive:
        names = set(archive.namelist())
        config = archive.read("init.yml").decode("utf-8")
        assert {"interactor.cc", "testlib.h"} <= names
        assert archive.read("testlib.h") == header
        assert "files:\n  - interactor.cc\n  - testlib.h" in config
        assert "type: testlib" in config

    roundtrip = _read_bundle(package, "dmoj", tmp_path / "roundtrip")
    assert roundtrip.problems[0].interactor is not None
    assert roundtrip.problems[0].interactor.mode == "testlib"
    assert set(roundtrip.problems[0].interactor.auxiliary_files) == {"testlib.h"}


def test_dmoj_target_rejects_missing_local_interactor_dependency(
    tmp_path: Path,
) -> None:
    source = tmp_path / "domjudge-missing-header.zip"
    output = tmp_path / "dmoj"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "problem.yaml",
            "name: Interactive\nvalidation: custom interactive\n",
        )
        archive.writestr("problem.pdf", b"%PDF-1.4\n%%EOF\n")
        archive.writestr("data/secret/1.in", "1\n")
        archive.writestr("data/secret/1.ans", "1\n")
        archive.writestr(
            "output_validators/interactor/interactor.cc",
            '#include "testlib.h"\nint main() { return 0; }\n',
        )

    with pytest.raises(ValueError, match="missing local files: testlib.h"):
        convert_package(source, output, source_format="icpc", target_format="dmoj")

    assert {path.name for path in output.iterdir()} == {REPORT_FILENAME}
    report = json.loads((output / REPORT_FILENAME).read_text(encoding="utf-8"))
    assert any(
        issue["code"] == "dmoj-missing-program-dependency"
        and issue["field"] == "interactor"
        for issue in report["issues"]
    )


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

    assert report["schema_version"] == 2
    assert report["repair_ready"] is False
    assert report["repair_suggestions"] == []
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


def test_semantic_snapshot_hashes_judge_data_and_normalizes_statement_text(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "1.in"
    output_path = tmp_path / "1.ans"
    input_path.write_bytes(b"1 2\r\n")
    output_path.write_bytes(b"3\n")
    first = ProblemBundle(
        "hydro",
        [
            Problem(
                id="P1000",
                slug="sum",
                title="Sum",
                statements=[Statement("en", "markdown", content="# Sum\r\n")],
                cases=[IRTestCase("1", input_path, output_path)],
                time_ms=1000,
                memory_mb=256,
            )
        ],
    )
    second = ProblemBundle(
        "hydro",
        [
            Problem(
                id="renumbered",
                slug="sum",
                title="Sum",
                statements=[Statement("en", "markdown", content="# Sum\n")],
                cases=[IRTestCase("1", input_path, output_path)],
                time_ms=1000,
                memory_mb=256,
            )
        ],
    )

    assert first.semantic_snapshot() == second.semantic_snapshot()
    original_digest = first.semantic_digest()
    assert original_digest == second.semantic_digest()
    output_path.write_bytes(b"4\n")
    assert original_digest != second.semantic_digest()


def test_semantic_difference_requires_matching_loss_field() -> None:
    before = {"schema_version": 1, "problems": [{"groups": [{"name": "1"}]}]}
    after = {"schema_version": 1, "problems": [{"groups": []}]}

    assert compare_semantic_snapshots(before, after, target_format="icpc", issues=[])
    assert (
        compare_semantic_snapshots(
            before,
            after,
            target_format="icpc",
            issues=[
                ConversionIssue(
                    "loss",
                    "icpc-groups",
                    "groups are not representable",
                    field="groups",
                )
            ],
        )
        == []
    )


def test_missing_answer_generates_confirmable_repair_and_can_be_applied(
    tmp_path: Path,
) -> None:
    source = tmp_path / "missing-answer.zip"
    failed_output = tmp_path / "failed"
    repaired_output = tmp_path / "repaired"
    plan_path = tmp_path / "repair-plan.json"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("P1000/problem.yaml", "title: Sum\npid: P1000\n")
        archive.writestr("P1000/problem_en.md", "# Sum\n")
        archive.writestr(
            "P1000/testdata/config.yaml",
            "cases:\n  - input: 1.in\n    output: 1.ans\n",
        )
        archive.writestr("P1000/testdata/1.in", "1 2\n")
        archive.writestr("P1000/testdata/1.out", "3\n")

    with pytest.raises(ValueError, match="missing output"):
        convert_package(
            source,
            failed_output,
            source_format="hydro",
            target_format="hoj",
        )
    report = json.loads((failed_output / REPORT_FILENAME).read_text())
    suggestion = next(
        item
        for item in report["repair_suggestions"]
        if item["expected_path"].endswith("P1000/testdata/1.ans")
    )
    candidate = suggestion["candidates"][0]
    assert candidate == {
        "path": "P1000/testdata/1.out",
        "strategy": "extension-alias",
        "confidence": 0.95,
    }
    plan_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_sha256": _sha256(source),
                "selections": [
                    {
                        "suggestion_id": suggestion["id"],
                        "expected_path": suggestion["expected_path"],
                        "role": suggestion["role"],
                        "candidate_path": candidate["path"],
                        "strategy": candidate["strategy"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    repaired_report = convert_package(
        source,
        repaired_output,
        source_format="hydro",
        target_format="hoj",
        repair_plan=plan_path,
    )

    assert repaired_report["counts"]["fatal"] == 0
    assert repaired_report["applied_repairs"][0]["expected_path"].endswith("1.ans")
    assert _sha256(source) == json.loads(plan_path.read_text())["source_sha256"]


def test_repair_source_archive_copies_candidate_without_removing_original(
    tmp_path: Path,
) -> None:
    source = tmp_path / "missing-answer.zip"
    repaired = tmp_path / "repaired-source.zip"
    plan = tmp_path / "repair-plan.json"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("P1000/testdata/1.out", "3\n")
    plan.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_sha256": _sha256(source),
                "selections": [
                    {
                        "suggestion_id": "a" * 24,
                        "expected_path": "P1000/testdata/1.ans",
                        "role": "output",
                        "candidate_path": "P1000/testdata/1.out",
                        "strategy": "extension-alias",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    repair_source_archive(
        source,
        repaired,
        plan_path=plan,
        supplements_dir=None,
        workspace=tmp_path / "work",
    )

    with zipfile.ZipFile(repaired) as archive:
        assert archive.read("P1000/testdata/1.out") == b"3\n"
        assert archive.read("P1000/testdata/1.ans") == b"3\n"


def test_conflicting_output_aliases_remain_fatal(tmp_path: Path) -> None:
    source = tmp_path / "conflicting-answers.zip"
    output = tmp_path / "output"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("P1000/problem.yaml", "title: Sum\npid: P1000\n")
        archive.writestr("P1000/problem_en.md", "# Sum\n")
        archive.writestr(
            "P1000/testdata/config.yaml",
            "cases:\n  - input: 1.in\n    output: 1.ans\n",
        )
        archive.writestr("P1000/testdata/1.in", "1 2\n")
        archive.writestr("P1000/testdata/1.ans", "3\n")
        archive.writestr("P1000/testdata/1.out", "4\n")

    with pytest.raises(ValueError, match="unpaired file"):
        convert_package(
            source,
            output,
            source_format="hydro",
            target_format="hoj",
        )


def test_repair_plan_rejects_tampered_source_hash(tmp_path: Path) -> None:
    source = tmp_path / "source.zip"
    plan = tmp_path / "plan.json"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("problem.xml", "<fps version='1.6'/>")
    plan.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_sha256": "0" * 64,
                "selections": [
                    {
                        "suggestion_id": "1" * 24,
                        "expected_path": "1.ans",
                        "role": "output",
                        "candidate_path": "1.out",
                        "strategy": "extension-alias",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="source hash"):
        convert_package(
            source,
            tmp_path / "output",
            source_format="fps",
            target_format="hydro",
            repair_plan=plan,
        )


def test_confirmed_supplement_upload_repairs_missing_file(tmp_path: Path) -> None:
    source = tmp_path / "missing.zip"
    failed_output = tmp_path / "failed"
    repaired_output = tmp_path / "repaired"
    supplements = tmp_path / "supplements"
    supplements.mkdir()
    supplement = supplements / "0001.upload"
    supplement.write_text("3\n", encoding="utf-8")
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("P1000/problem.yaml", "title: Sum\npid: P1000\n")
        archive.writestr("P1000/problem_en.md", "# Sum\n")
        archive.writestr(
            "P1000/testdata/config.yaml",
            "cases:\n  - input: 1.in\n    output: 1.ans\n",
        )
        archive.writestr("P1000/testdata/1.in", "1 2\n")

    with pytest.raises(ValueError):
        convert_package(
            source,
            failed_output,
            source_format="hydro",
            target_format="hoj",
        )
    report = json.loads((failed_output / REPORT_FILENAME).read_text())
    suggestion = next(
        item
        for item in report["repair_suggestions"]
        if item["expected_path"].endswith("1.ans")
    )
    plan = tmp_path / "upload-plan.json"
    plan.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_sha256": _sha256(source),
                "selections": [
                    {
                        "suggestion_id": suggestion["id"],
                        "expected_path": suggestion["expected_path"],
                        "role": suggestion["role"],
                        "upload_name": supplement.name,
                        "upload_sha256": _sha256(supplement),
                        "strategy": "upload",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    repaired_report = convert_package(
        source,
        repaired_output,
        source_format="hydro",
        target_format="hoj",
        repair_plan=plan,
        supplements_dir=supplements,
    )

    assert repaired_report["counts"]["fatal"] == 0
    assert repaired_report["applied_repairs"][0]["strategy"] == "upload"


@pytest.mark.parametrize(
    ("source_format", "target_format"),
    [
        ("hydro", "icpc"),
        ("hydro", "hoj"),
        ("icpc", "hydro"),
        ("icpc", "hoj"),
        ("hoj", "hydro"),
        ("hoj", "icpc"),
    ],
)
def test_core_format_matrix_has_no_unexplained_semantic_difference(
    tmp_path: Path, source_format: str, target_format: str
) -> None:
    hydro_source = tmp_path / "hydro-source.zip"
    _hydro_package(hydro_source)
    if source_format == "hydro":
        source_archive = hydro_source
    else:
        generated_source = tmp_path / f"generated-{source_format}"
        generated_source.mkdir()
        convert_package(
            hydro_source,
            generated_source,
            source_format="hydro",
            target_format=source_format,
        )
        if source_format == "icpc":
            source_archive = next(generated_source.glob("*.zip"))
        else:
            source_archive = tmp_path / "hoj-source.zip"
            _zip_directory(generated_source, source_archive)

    converted = tmp_path / f"{source_format}-to-{target_format}"
    converted.mkdir()
    report = convert_package(
        source_archive,
        converted,
        source_format=source_format,
        target_format=target_format,
    )
    if target_format in {"hydro", "icpc"}:
        target_archive = next(converted.glob("*.zip"))
    else:
        target_archive = tmp_path / "hoj-target.zip"
        _zip_directory(converted, target_archive)

    source_bundle = _read_bundle(
        source_archive, source_format, tmp_path / "source-read"
    )
    target_bundle = _read_bundle(
        target_archive, target_format, tmp_path / "target-read"
    )
    issues = [
        ConversionIssue(
            issue["severity"],
            issue["code"],
            issue["message"],
            issue.get("problem"),
            issue.get("field"),
            issue.get("context", {}),
        )
        for issue in report["issues"]
    ]
    unexplained = compare_semantic_snapshots(
        source_bundle.semantic_snapshot(),
        target_bundle.semantic_snapshot(),
        target_format=target_format,
        issues=issues,
    )

    assert unexplained == []


def test_large_fast_fixture_reports_progress_for_twenty_problems(
    tmp_path: Path,
) -> None:
    source = tmp_path / "large-generic.zip"
    output = tmp_path / "output"
    stream = StringIO()
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for problem_index in range(20):
            root = f"p{problem_index:02d}"
            archive.writestr(f"{root}/statement.md", f"# Problem {problem_index}\n")
            for case_index in range(50):
                archive.writestr(f"{root}/{case_index:03d}.in", f"{case_index}\n")
                archive.writestr(f"{root}/{case_index:03d}.ans", f"{case_index}\n")

    with ProgressReporter(
        output_format="jsonl", stream=stream, heartbeat_seconds=0
    ) as reporter:
        report = convert_package(
            source,
            output,
            source_format="generic",
            target_format="hydro",
            reporter=reporter,
        )

    assert report["problem_count"] == 20
    assert len(list(output.glob("*.zip"))) == 20
    events = [
        json.loads(line.removeprefix("P2H_EVENT "))
        for line in stream.getvalue().splitlines()
    ]
    phases = {event["phase"] for event in events}
    assert phases == {
        "validate_archive",
        "extract",
        "detect",
        "read",
        "validate_ir",
        "write",
        "validate_output",
    }
    write_events = [event for event in events if event["phase"] == "write"]
    assert any(event["current"] == 20 for event in write_events)


@pytest.mark.skipif(
    os.getenv("P2H_RUN_NIGHTLY_LARGE") != "1",
    reason="nightly 49,400-entry compatibility fixture",
)
def test_nightly_large_fixture_nears_archive_entry_limit(tmp_path: Path) -> None:
    source = tmp_path / "nightly-large-generic.zip"
    output = tmp_path / "output"
    with zipfile.ZipFile(
        source, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
    ) as archive:
        for problem_index in range(200):
            root = f"p{problem_index:03d}"
            archive.writestr(f"{root}/statement.md", f"# Problem {problem_index}\n")
            for case_index in range(123):
                archive.writestr(f"{root}/{case_index:03d}.in", f"{case_index}\n")
                archive.writestr(f"{root}/{case_index:03d}.ans", f"{case_index}\n")

    report = convert_package(
        source,
        output,
        source_format="generic",
        target_format="hydro",
    )

    assert report["problem_count"] == 200
    assert len(list(output.glob("*.zip"))) == 200
