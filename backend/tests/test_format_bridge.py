from __future__ import annotations

import io
import sys
import types
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
RUNNER_DIR = ROOT / "runner"
sys.path.insert(0, str(RUNNER_DIR))

from format_bridge import (  # noqa: E402
    ArchiveExtractionBudget,
    _read_yaml,
    _safe_extract_zip,
    convert_domjudge_to_hydro,
    convert_hydro_to_domjudge,
)


def test_legacy_bridge_yaml_reader_enforces_alias_limit(tmp_path: Path) -> None:
    path = tmp_path / "problem.yaml"
    aliases = ", ".join("*base" for _ in range(40))
    path.write_text(f"base: &base [1]\nvalue: [{aliases}]\n", encoding="utf-8")

    with pytest.raises(ValueError, match="alias limit"):
        _read_yaml(path)


def _write_domjudge_problem(
    archive: zipfile.ZipFile,
    root: str,
    *,
    code: str,
    title: str,
    language: str = "zh_CN",
) -> None:
    prefix = f"{root.rstrip('/')}/" if root else ""
    archive.writestr(
        f"{prefix}problem.yaml",
        f"name: {title}\nlimits:\n  time_limit: 3\n  memory: 512\n",
    )
    archive.writestr(
        f"{prefix}domjudge-problem.ini",
        f"short-name = {code}\nname = {title}\ntimelimit = 2.5\nexternalid = {code.lower()}\n",
    )
    archive.writestr(
        f"{prefix}problem_statement/problem.{language}.pdf", b"%PDF-1.4\nfake\n"
    )
    archive.writestr(f"{prefix}data/sample/sample.in", "1 2\n")
    archive.writestr(f"{prefix}data/sample/sample.ans", "3\n")
    archive.writestr(f"{prefix}data/secret/case.in", "2 3\n")
    archive.writestr(f"{prefix}data/secret/case.ans", "5\n")
    archive.writestr(f"{prefix}submissions/accepted/solution.cpp", "int main() {}\n")
    archive.writestr(
        f"{prefix}output_validators/checker/checker.cpp", "int main() {}\n"
    )
    archive.writestr(f"{prefix}output_validators/checker/testlib.h", "// header\n")


def _nested_problem_zip(*, code: str, title: str) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _write_domjudge_problem(archive, "", code=code, title=title, language="en")
    return out.getvalue()


def test_domjudge_to_hydro_uses_pdf_statement_and_preserves_problem_files(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sum.zip"
    output = tmp_path / "output"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _write_domjudge_problem(archive, "sum", code="A", title="A + B")

    assert (
        convert_domjudge_to_hydro(
            source,
            output,
            pid_start="P0099",
            owner=7,
            tags=["校赛", "2026"],
            verbose=True,
        )
        == 0
    )

    packages = sorted(output.glob("*.zip"))
    assert len(packages) == 1
    with zipfile.ZipFile(packages[0]) as archive:
        names = set(archive.namelist())
        assert "P0099/problem.yaml" in names
        assert "P0099/problem_zh_CN.md" in names
        assert "P0099/additional_file/problem.zh_CN.pdf" in names
        assert "P0099/additional_file/samples/1.in" in names
        assert "P0099/additional_file/samples/1.ans" in names
        assert (
            "P0099/additional_file/sources/submissions/accepted/solution.cpp" in names
        )
        assert "P0099/testdata/1.in" in names
        assert "P0099/testdata/1.ans" in names
        assert "P0099/testdata/checker.cpp" in names
        assert "P0099/testdata/testlib.h" in names

        statement = archive.read("P0099/problem_zh_CN.md").decode()
        assert statement == "@[pdf](file://problem.zh_CN.pdf)\n"

        problem_yaml = archive.read("P0099/problem.yaml").decode()
        assert "A + B" in problem_yaml
        assert "P0099" in problem_yaml
        assert "owner: 7" in problem_yaml
        assert "校赛" in problem_yaml

        config = archive.read("P0099/testdata/config.yaml").decode()
        assert "time" in config and "2.5s" in config
        assert "memory" in config and "512m" in config
        assert "checker_type" in config and "testlib" in config
        assert "checker.cpp" in config


def test_domjudge_to_hydro_accepts_outer_zip_of_nested_problem_packages(
    tmp_path: Path,
) -> None:
    source = tmp_path / "contest.zip"
    output = tmp_path / "output"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("A-sum.zip", _nested_problem_zip(code="A", title="Sum"))
        archive.writestr("B-max.zip", _nested_problem_zip(code="B", title="Maximum"))

    assert convert_domjudge_to_hydro(source, output, pid_start="Q8", only=["b"]) == 0

    packages = sorted(output.glob("*.zip"))
    assert len(packages) == 1
    with zipfile.ZipFile(packages[0]) as archive:
        assert "Q8/problem_en.md" in archive.namelist()
        assert (
            archive.read("Q8/problem_en.md").decode()
            == "@[pdf](file://problem.en.pdf)\n"
        )


def test_domjudge_to_hydro_requires_pdf_statement(tmp_path: Path) -> None:
    source = tmp_path / "no-pdf.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("problem.yaml", "name: Missing PDF\n")
        archive.writestr("data/secret/1.in", "1\n")
        archive.writestr("data/secret/1.ans", "1\n")

    with pytest.raises(ValueError, match="no PDF statement"):
        convert_domjudge_to_hydro(source, tmp_path / "output")


def _write_hydro_problem(
    archive: zipfile.ZipFile,
    *,
    config: str,
    statement: str = "# Sum\n\nAdd two numbers.\n",
) -> None:
    archive.writestr("P1000/problem.yaml", "title: Sum\npid: P1000\n")
    archive.writestr("P1000/problem_en.md", statement)
    archive.writestr("P1000/testdata/config.yaml", config)
    archive.writestr("P1000/testdata/one/foo.in", "FIRST\n")
    archive.writestr("P1000/testdata/one/foo.ans", "FIRST-ANSWER\n")
    archive.writestr("P1000/testdata/two/foo.in", "SECOND\n")
    archive.writestr("P1000/testdata/two/foo.ans", "SECOND-ANSWER\n")


def test_hydro_to_domjudge_writes_importable_html_and_keeps_duplicate_case_names(
    tmp_path: Path,
) -> None:
    source = tmp_path / "hydro.zip"
    output = tmp_path / "output"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _write_hydro_problem(
            archive,
            config="""\
type: default
cases:
  - input: one/foo.in
    output: one/foo.ans
  - input: two/foo.in
    output: two/foo.ans
""",
        )

    assert convert_hydro_to_domjudge(source, output) == 0

    package = next(output.glob("*.zip"))
    with zipfile.ZipFile(package) as archive:
        names = set(archive.namelist())
        assert "problem_statement/problem.html" in names
        assert not any(
            name.endswith(".md")
            for name in names
            if name.startswith("problem_statement/")
        )
        statement = archive.read("problem_statement/problem.html").decode()
        assert "Add two numbers." in statement
        assert archive.read("data/secret/foo.in") == b"FIRST\n"
        assert archive.read("data/secret/foo-2.in") == b"SECOND\n"
        assert archive.read("data/secret/foo-2.ans") == b"SECOND-ANSWER\n"


def test_hydro_to_domjudge_restores_embedded_pdf_statement(tmp_path: Path) -> None:
    source = tmp_path / "hydro-pdf.zip"
    output = tmp_path / "output"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _write_hydro_problem(
            archive,
            config="cases:\n  - input: one/foo.in\n    output: one/foo.ans\n",
            statement="@[pdf](file://statement.pdf)\n",
        )
        archive.writestr(
            "P1000/additional_file/statement.pdf", b"%PDF-1.4\nroundtrip\n"
        )

    assert convert_hydro_to_domjudge(source, output) == 0

    with zipfile.ZipFile(next(output.glob("*.zip"))) as archive:
        assert archive.read("problem_statement/problem.pdf") == b"%PDF-1.4\nroundtrip\n"


@pytest.mark.parametrize(
    (
        "problem_type",
        "source_key",
        "source_name",
        "expected_validation",
        "validator_dir",
    ),
    [
        ("default", "checker", "checker.cc", "custom", "checker"),
        (
            "interactive",
            "interactor",
            "interactor.cc",
            "custom interactive",
            "interactor",
        ),
    ],
)
def test_hydro_to_domjudge_adapts_testlib_checker_and_interactor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    problem_type: str,
    source_key: str,
    source_name: str,
    expected_validation: str,
    validator_dir: str,
) -> None:
    fake_package = tmp_path / "fake-p2d"
    (fake_package / "testlib").mkdir(parents=True)
    (fake_package / "__init__.py").write_text("")
    (fake_package / "testlib" / "testlib.h").write_text("// DOMJUDGE-PATCHED\n")
    monkeypatch.setitem(
        sys.modules,
        "p2d",
        types.SimpleNamespace(__file__=str(fake_package / "__init__.py")),
    )

    source = tmp_path / f"hydro-{problem_type}.zip"
    output = tmp_path / "output"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _write_hydro_problem(
            archive,
            config=f"""\
type: {problem_type}
{source_key}: {source_name}
cases:
  - input: one/foo.in
    output: one/foo.ans
""",
        )
        archive.writestr(
            f"P1000/testdata/{source_name}", '#include "testlib.h"\nint main() {{}}\n'
        )
        archive.writestr("P1000/testdata/testlib.h", "// UNPATCHED\n")

    assert convert_hydro_to_domjudge(source, output) == 0

    with zipfile.ZipFile(next(output.glob("*.zip"))) as archive:
        problem_yaml = archive.read("problem.yaml").decode()
        assert f"validation: {expected_validation}" in problem_yaml
        prefix = f"output_validators/{validator_dir}"
        assert f"{prefix}/{source_name}" in archive.namelist()
        assert archive.read(f"{prefix}/testlib.h") == b"// DOMJUDGE-PATCHED\n"


def test_hydro_to_domjudge_rejects_high_ratio_zip_bomb_and_cleans_extraction(
    tmp_path: Path,
) -> None:
    source = tmp_path / "bomb.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("P1000/huge.bin", b"\0" * (32 * 1024 * 1024))

    with pytest.raises(ValueError, match="compression ratio limit"):
        convert_hydro_to_domjudge(source, tmp_path / "output")


def test_safe_extract_allows_small_high_ratio_testdata(tmp_path: Path) -> None:
    source = tmp_path / "testdata.zip"
    target = tmp_path / "target"
    content = (b"1000000000\n" * 1_000_002)[:11_000_017]
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("problem_1292/18.in", content)

    with zipfile.ZipFile(source) as archive:
        info = archive.getinfo("problem_1292/18.in")
        assert info.file_size / info.compress_size > 200

    _safe_extract_zip(source, target)

    assert (target / "problem_1292" / "18.in").read_bytes() == content


def test_safe_extract_counts_directory_entries_toward_archive_limit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "many-directories.zip"
    target = tmp_path / "target"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("one/", b"")
        archive.writestr("two/", b"")
        archive.writestr("three/", b"")

    budget = ArchiveExtractionBudget(
        max_entries=2,
        max_uncompressed_bytes=1024,
        max_member_bytes=1024,
        max_compression_ratio=200,
        min_compression_ratio_bytes=16 * 1024 * 1024,
    )
    with pytest.raises(ValueError, match="entry limit"):
        _safe_extract_zip(source, target, budget=budget)
    assert not target.exists()


def test_safe_extract_rejects_duplicate_normalized_member_names(tmp_path: Path) -> None:
    source = tmp_path / "duplicates.zip"
    target = tmp_path / "target"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("data/1.in", "first\n")
        archive.writestr("data\\1.in", "second\n")

    with pytest.raises(ValueError, match="duplicate zip member path"):
        _safe_extract_zip(source, target)

    assert not target.exists()
