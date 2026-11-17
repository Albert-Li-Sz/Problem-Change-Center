from __future__ import annotations

import json
import sys
import types
import xml.etree.ElementTree as ET  # nosec B405
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "runner"))

import p2h_safe  # noqa: E402
from package_converter import convert_package  # noqa: E402
from package_ir import Problem, Statement, prefer_chinese  # noqa: E402
from polygon_statements import (  # noqa: E402
    hydro_language_code,
    order_languages,
    read_polygon_statements,
)


PROBLEM_XML = """<problem>
  <names>
    <name language="english" value="Sum"/>
    <name language="russian" value="Сумма"/>
    <name language="chinese" value="求和"/>
  </names>
  <statements>
    <statement language="english" path="statements/.html/english/problem.html" type="text/html"/>
    <statement language="russian" path="statements/russian/problem.tex" type="application/x-tex"/>
    <statement language="french" path="statements/.pdf/french/problem.pdf" type="application/pdf"/>
    <statement language="german" path="../../outside.tex" type="application/x-tex"/>
  </statements>
</problem>
"""


def _write(path: Path, content: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def _polygon_problem(base: Path) -> Path:
    _write(base / "problem.xml", PROBLEM_XML)
    _write(base / "statement-sections/chinese/legend.tex", "中文题面")
    _write(base / "statement-sections/chinese/shared.png", b"zh-image")
    _write(base / "statements/.html/english/problem.html", "<p>English</p>")
    _write(base / "statements/.html/english/shared.png", b"en-image")
    _write(base / "statements/.html/english/english-only.png", b"en-only")
    _write(base / "statements/russian/problem.tex", "Русский")
    _write(base / "statements/.pdf/french/problem.pdf", b"%PDF-1.4")
    _write(base.parent / "outside.tex", "must not be read")
    return base


def _read(base: Path):
    return read_polygon_statements(
        base,
        ET.fromstring((base / "problem.xml").read_text(encoding="utf-8")),
        is_interactive=False,
        sections_to_markdown=lambda directory, *, is_interactive: (
            f"SECTIONS {(directory / 'legend.tex').read_text(encoding='utf-8')}\n"
        ),
        html_to_markdown=lambda text: f"HTML {text}\n",
        tex_to_markdown=lambda text: f"TEX {text}\n",
    )


def test_language_order_puts_chinese_then_english_first() -> None:
    assert order_languages(
        ["russian", "English", "french", "chinese", "english", ""]
    ) == [
        "chinese",
        "english",
        "french",
        "russian",
    ]


@pytest.mark.parametrize(
    "language, code",
    [
        ("chinese", "zh"),
        ("English", "en"),
        ("russian", "ru"),
        ("Old Norse", "old_norse"),
    ],
)
def test_hydro_language_codes(language: str, code: str) -> None:
    assert hydro_language_code(language) == code


def test_every_text_statement_language_is_read(tmp_path: Path) -> None:
    base = _polygon_problem(tmp_path / "problems" / "sum")

    statements = _read(base)

    assert [(item.language, item.code) for item in statements] == [
        ("chinese", "zh"),
        ("english", "en"),
        ("russian", "ru"),
    ]
    assert statements[0].markdown == "SECTIONS 中文题面\n"
    assert statements[1].markdown == "HTML <p>English</p>\n"
    assert statements[2].markdown == "TEX Русский\n"
    assert statements[0].directory == base / "statement-sections" / "chinese"


# Minimal stand-ins for the pinned polygon2hydro modules.
@dataclass
class _ProblemData:
    slug: str
    title: str
    statement_md: str
    is_interactive: bool = False
    tests: list = field(default_factory=list)
    additional_files: list = field(default_factory=list)
    testdata_files: list = field(default_factory=list)
    time_ms: int | None = 1000
    memory_mb: int | None = 256
    interactor_name: str | None = None
    checker_name: str | None = "check.cpp"


@dataclass
class _Case:
    index: int
    input_data: bytes
    output_data: bytes
    input_name: str | None = None
    output_name: str | None = None


@pytest.fixture
def fake_p2h(monkeypatch):
    package = types.ModuleType("p2h")
    package.__path__ = []
    reader = types.ModuleType("p2h.polygon_reader")
    writer = types.ModuleType("p2h.hydro_writer")
    convert = types.ModuleType("p2h.convert")
    calls: dict[str, object] = {}

    def read_problem(work_root, slug, *, verbose=False, statement_language=None):
        calls["statement_language"] = statement_language
        return _ProblemData(
            slug=slug,
            title="求和",
            statement_md="upstream",
            tests=[_Case(1, b"1 2\n", b"3\n", "1.in", "1.out")],
            testdata_files=[("check.cpp", b"checker")],
            additional_files=[("upstream.png", b"upstream")],
        )

    def extract_additional_files(base, statement_dir):
        if statement_dir is None:
            return []
        return [
            (path.name, path.read_bytes())
            for path in sorted(statement_dir.iterdir())
            if path.suffix == ".png"
        ]

    def write_problem_zip(**kwargs):
        calls["upstream_write"] = True
        return Path(kwargs["output_dir"]) / "upstream.zip"

    reader._sections_to_markdown = lambda directory, *, is_interactive: (
        (directory / "legend.tex").read_text(encoding="utf-8") + "\n"
    )
    reader.html_to_markdown = lambda text: text + "\n"
    reader.tex_to_markdown = lambda text: text + "\n"
    reader._extract_additional_files = extract_additional_files
    writer._build_problem_yaml = p2h_safe.build_hydro_problem_yaml
    writer._build_config_yaml = lambda time_ms, memory_mb, test_files, **kwargs: (
        yaml.safe_dump({"time": f"{time_ms}ms", "cases": test_files})
    )
    writer._next_available_zip_path = lambda directory, name: directory / f"{name}.zip"
    writer._safe_filename = lambda name: name
    convert.read_problem = read_problem
    convert.write_problem_zip = write_problem_zip
    for name, module in (
        ("p2h", package),
        ("p2h.polygon_reader", reader),
        ("p2h.hydro_writer", writer),
        ("p2h.convert", convert),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    p2h_safe._install_multilingual_statement_patches(convert)
    p2h_safe._install_multilingual_statement_patches(convert)
    return convert, calls


def test_hydro_zip_keeps_every_statement_language(tmp_path: Path, fake_p2h) -> None:
    convert, calls = fake_p2h
    _polygon_problem(tmp_path / "work" / "problems" / "sum")

    problem = convert.read_problem(
        tmp_path / "work", "sum", verbose=False, statement_language="english"
    )
    output = convert.write_problem_zip(
        problem=problem,
        output_dir=tmp_path / "out",
        local_id=1,
        pid="P1000",
        owner=1,
        tags=[],
    )

    assert calls["statement_language"] == "chinese"
    assert "upstream_write" not in calls
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        assert archive.read("1/problem_zh.md").decode() == "中文题面\n"
        assert archive.read("1/problem_en.md").decode() == "<p>English</p>\n"
        assert archive.read("1/problem_ru.md").decode() == "Русский\n"
        # Images from every language; the Chinese one wins a name clash.
        assert archive.read("1/additional_file/shared.png") == b"zh-image"
        assert archive.read("1/additional_file/english-only.png") == b"en-only"
        assert yaml.safe_load(archive.read("1/problem.yaml"))["title"] == "求和"
        assert archive.read("1/testdata/1.in") == b"1 2\n"
        assert archive.read("1/testdata/check.cpp") == b"checker"
    assert "1/problem_fr.md" not in names
    assert "1/problem_de.md" not in names
    assert p2h_safe._PENDING_STATEMENTS == {}


def test_english_only_problem_is_not_labelled_chinese(tmp_path: Path, fake_p2h) -> None:
    convert, _ = fake_p2h
    base = tmp_path / "work" / "problems" / "sum"
    _write(
        base / "problem.xml",
        '<problem><statements><statement language="english" '
        'path="statements/english/problem.tex" type="application/x-tex"/>'
        "</statements></problem>",
    )
    _write(base / "statements/english/problem.tex", "English only")

    problem = convert.read_problem(tmp_path / "work", "sum")
    output = convert.write_problem_zip(
        problem=problem,
        output_dir=tmp_path / "out",
        local_id=1,
        pid="P1000",
        owner=1,
        tags=[],
    )

    with zipfile.ZipFile(output) as archive:
        statements = sorted(n for n in archive.namelist() if "/problem_" in n)
    assert statements == ["1/problem_en.md"]


def test_problem_without_text_statement_keeps_upstream_writer(
    tmp_path: Path, fake_p2h
) -> None:
    convert, calls = fake_p2h
    _write(tmp_path / "work/problems/sum/problem.xml", "<problem/>")

    problem = convert.read_problem(tmp_path / "work", "sum")
    convert.write_problem_zip(
        problem=problem,
        output_dir=tmp_path / "out",
        local_id=1,
        pid="P1000",
        owner=1,
        tags=[],
    )

    assert problem.statement_md == "upstream"
    assert calls["upstream_write"] is True


@pytest.mark.parametrize(
    "with_statement, expected",
    [
        (False, ["chinese", "english", "russian"]),
        (True, ["english", "chinese", "russian"]),
    ],
)
def test_domjudge_prefers_chinese_with_a_usable_pdf(
    tmp_path: Path, with_statement: bool, expected: list[str]
) -> None:
    _write(
        tmp_path / "problem.xml",
        """<problem>
  <names>
    <name language="russian" value="Сумма"/>
    <name language="english" value="Sum"/>
    <name language="chinese" value="求和"/>
  </names>
  <statements>
    <statement language="english" path="statements/.pdf/english/problem.pdf" type="application/pdf"/>
    <statement language="chinese" path="statements/.html/chinese/problem.html" type="text/html"/>
  </statements>
</problem>""",
    )

    assert (
        p2h_safe.domjudge_language_preference(tmp_path, with_statement=with_statement)
        == expected
    )


def test_ir_puts_chinese_statement_and_title_first() -> None:
    problem = Problem(
        id="P1",
        slug="sum",
        title="Sum",
        titles={"en": "Sum", "zh-TW": "求和（繁）", "zh": "求和"},
        statements=[
            Statement("en", "markdown", content="English"),
            Statement("ru", "markdown", content="Русский"),
            Statement("zh-TW", "markdown", content="繁體"),
            Statement("zh_CN", "markdown", content="简体"),
        ],
    )

    prefer_chinese(problem)

    assert [item.language for item in problem.statements] == [
        "zh_CN",
        "zh-TW",
        "en",
        "ru",
    ]
    assert problem.title == "求和"


def test_single_statement_targets_take_the_chinese_statement(tmp_path: Path) -> None:
    source = tmp_path / "hydro.zip"
    with zipfile.ZipFile(source, "w") as archive:
        for name, content in {
            "P1/problem.yaml": "title: Sum\npid: P1\n",
            "P1/problem_en.md": "ENGLISH STATEMENT\n",
            "P1/problem_zh.md": "中文题面\n",
            "P1/testdata/config.yaml": "time: 1s\n",
            "P1/testdata/a.in": "1\n",
            "P1/testdata/a.out": "1\n",
        }.items():
            archive.writestr(name, content)
    output = tmp_path / "out"

    convert_package(source, output, source_format="hydro", target_format="qduoj")

    document = json.loads(next(output.rglob("problem.json")).read_text())
    assert "中文题面" in json.dumps(document, ensure_ascii=False)
    assert "ENGLISH STATEMENT" not in json.dumps(document, ensure_ascii=False)


def test_a_broken_translation_is_skipped_with_a_warning(tmp_path: Path) -> None:
    base = _polygon_problem(tmp_path / "problems" / "sum")
    errors: list[tuple[str, str]] = []

    def tex_to_markdown(text: str) -> str:
        raise ValueError("TeX text formatting nesting limit exceeded")

    statements = read_polygon_statements(
        base,
        ET.fromstring((base / "problem.xml").read_text(encoding="utf-8")),
        is_interactive=False,
        sections_to_markdown=lambda directory, *, is_interactive: "中文\n",
        html_to_markdown=lambda text: "English\n",
        tex_to_markdown=tex_to_markdown,
        on_error=lambda language, exc: errors.append((language, str(exc))),
    )

    assert [item.code for item in statements] == ["zh", "en"]
    assert errors == [("russian", "TeX text formatting nesting limit exceeded")]

    with pytest.raises(ValueError, match="nesting limit"):
        read_polygon_statements(
            base,
            ET.fromstring((base / "problem.xml").read_text(encoding="utf-8")),
            is_interactive=False,
            sections_to_markdown=lambda directory, *, is_interactive: "中文\n",
            html_to_markdown=lambda text: "English\n",
            tex_to_markdown=tex_to_markdown,
        )
