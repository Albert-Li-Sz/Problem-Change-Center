from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "runner"))

import format_bridge as bridge  # noqa: E402
from adapters import ADAPTERS  # noqa: E402
from hoj_bridge import _parse_hydro_samples, _split_hydro_statement  # noqa: E402
from package_converter import (  # noqa: E402
    _expand_nested_packages_for_detection,
    convert_package,
)
from package_ir import Problem, ProblemBundle, Statement, TestCase as IRCase  # noqa: E402


FORMATS = ("hydro", "icpc", "hoj", "fps", "qduoj", "uoj", "dmoj")
SECTIONS = ("Description", "Input", "Output", "Interaction", "Scoring", "Notes")


def _archive(directory: Path, destination: Path) -> Path:
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(directory))
    return destination


@pytest.mark.parametrize(
    ("source_format", "target_format"),
    [(source, target) for source in FORMATS for target in FORMATS if source != target],
)
def test_statement_sections_survive_format_matrix(
    tmp_path: Path, source_format: str, target_format: str
) -> None:
    input_path = tmp_path / "1.in"
    output_path = tmp_path / "1.ans"
    input_path.write_text("1\n")
    output_path.write_text("2\n")
    statement = "\n\n".join(f"## {section}\n\nBODY_{section}" for section in SECTIONS)
    bundle = ProblemBundle(
        "hydro",
        [
            Problem(
                id="P1",
                slug="preserve",
                title="Preserve statement",
                statements=[Statement("en", "markdown", content=statement)],
                cases=[IRCase("1", input_path, output_path)],
            )
        ],
    )
    generated = tmp_path / "source"
    generated.mkdir()
    ADAPTERS[source_format].write(bundle, generated, {})
    source = (
        next(generated.glob("*.zip"))
        if source_format in {"hydro", "icpc"}
        else _archive(generated, tmp_path / "source.zip")
    )
    output = tmp_path / "output"
    report = convert_package(
        source, output, source_format=source_format, target_format=target_format
    )
    assert report["counts"]["fatal"] == 0
    target = (
        next(output.glob("*.zip"))
        if target_format in {"hydro", "icpc"}
        else _archive(output, tmp_path / "target.zip")
    )
    extracted = tmp_path / "read"
    budget = bridge.ArchiveExtractionBudget.from_env()
    bridge._safe_extract_zip(target, extracted, budget=budget)
    _expand_nested_packages_for_detection(
        extracted, budget, requested_format=target_format
    )
    reread = ADAPTERS[target_format].read(extracted, tmp_path / "workspace", ())
    assert len(reread.problems) == 1
    result = "\n".join(s.content or "" for s in reread.problems[0].statements)
    for section in SECTIONS:
        assert f"BODY_{section}" in result, (source_format, target_format, result)


def test_hoj_export_keeps_heading_lines_inside_literal_code() -> None:
    markdown = (
        "## Description\n\nBODY_DESCRIPTION\n\n"
        "```python\n# Input\nCODE_BODY\n```\n\n"
        "## Input\n\nBODY_INPUT\n\n## Output\n\nBODY_OUTPUT\n"
    )

    parts = _split_hydro_statement(markdown, "Title")

    assert "```python\n# Input\nCODE_BODY\n```" in parts["description"]
    assert parts["input"] == "BODY_INPUT"
    assert parts["output"] == "BODY_OUTPUT"


def test_hoj_export_preserves_unpaired_sample_in_statement() -> None:
    markdown = "## Description\nBODY\n\n```output4\nOUTPUT_ONLY\n```\n"

    parts = _split_hydro_statement(markdown, "Title")

    assert "OUTPUT_ONLY" in parts["description"]
    assert parts["examples"] == ""


def test_hydro_samples_are_paired_by_number_not_position() -> None:
    markdown = (
        "```input2\nINPUT_TWO\n```\n\n```input1\nINPUT_ONE\n```\n\n"
        "```output1\nOUTPUT_ONE\n```\n\n```output2\nOUTPUT_TWO\n```\n"
    )

    assert _parse_hydro_samples(markdown) == [
        ("INPUT_TWO", "OUTPUT_TWO"),
        ("INPUT_ONE", "OUTPUT_ONE"),
    ]


@pytest.mark.parametrize("fence", ["```", "````", "~~~"])
def test_samples_support_markdown_fences_and_crlf(fence: str) -> None:
    markdown = (
        f"{fence}input01\r\nIN\r\n{fence}\r\n\r\n{fence}output1\r\nOUT\r\n{fence}"
    )

    assert _parse_hydro_samples(markdown) == [("IN", "OUT")]


def test_sample_looking_blocks_inside_literal_code_are_preserved() -> None:
    markdown = "````text\n```input1\nLITERAL\n```\n````\n"

    assert _parse_hydro_samples(markdown) == []
    assert _split_hydro_statement(markdown, "Title")["description"] == markdown.strip()


def test_hoj_unrepresentable_sample_stays_in_statement_with_loss(
    tmp_path: Path,
) -> None:
    source = tmp_path / "hydro.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("P1/problem.yaml", "title: Sample text\npid: P1\n")
        archive.writestr(
            "P1/problem_zh.md",
            "BODY\n\n```input1\n</input> literal\n```\n\n"
            "```output1\n</output> literal\n```\n",
        )
        archive.writestr("P1/testdata/1.in", "1\n")
        archive.writestr("P1/testdata/1.out", "1\n")
    output = tmp_path / "hoj"
    report = convert_package(source, output, source_format="hydro", target_format="hoj")
    metadata = json.loads(next(output.glob("problem_*.json")).read_text())
    assert metadata["problem"]["examples"] == ""
    assert "</input> literal" in metadata["problem"]["description"]
    assert "</output> literal" in metadata["problem"]["description"]
    assert "hoj-sample-delimiter" in {issue["code"] for issue in report["issues"]}
