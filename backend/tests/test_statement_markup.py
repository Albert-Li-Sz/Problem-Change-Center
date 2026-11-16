from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "runner"))

import p2h_safe  # noqa: E402
from statement_markup import render_tex_with_formatting  # noqa: E402


def _render(text: str) -> str:
    return render_tex_with_formatting(text, lambda value: value)


@pytest.mark.parametrize(
    "source, expected",
    [
        (r"\texttt{a\_b < c & d}", "`a_b < c & d`"),
        (r"\t{yes}", "`yes`"),
        (r"\textbf{bold}", "**bold**"),
        (r"\textbf { spaced }", " **spaced** "),
        (r"\textit{italic} \emph{emphasis}", "*italic* *emphasis*"),
        (r"\textbf{cost $2^{10}$ and \texttt{x_y}}", "**cost $2^{10}$ and `x_y`**"),
        (r"\textbf{a \textbf{b} c}", "**a b c**"),
        (r"\textbf{\textit{both}}", "***both***"),
        (r"\textsf{sans} \textrm{roman} \textsc{small}", "sans roman small"),
        (r"\underline{under} \sout{gone}", "<u>under</u> ~~gone~~"),
        (r"x\textsuperscript{2} a\textsubscript{i}", "x<sup>2</sup> a<sub>i</sub>"),
        (r"{\bfseries bold \textit{and italic}}", "**bold *and italic***"),
        (r"{\ttfamily a\_b}", "`a_b`"),
        (r"\texttt{\{a\} `b`}", "`` {a} `b` ``"),
        (r"\texttt{\textbf{x} {y}}", "`x y`"),
        (r"\texttt{ a }", "`  a  `"),
        (r"\verb|\textbf{x} $y$ %|", "`\\textbf{x} $y$ %`"),
        (r"\lstinline{a_b}", "`a_b`"),
        ("\\textbf{a % ignored }\n b}", "**a \n b**"),
        (r"\textbf{}", ""),
    ],
)
def test_tex_text_formatting(source: str, expected: str) -> None:
    assert _render(source) == expected


@pytest.mark.parametrize(
    "formula",
    [
        r"$\mathbf{x}+\textbf{y}+\frac{1}{2}$",
        r"$$\texttt{a_b}+\operatorname{foo}(x)$$",
        r"\(a_1+\textbf{b}\)",
        r"\[\texttt{a_b}\]",
        r"\begin{align*}x&=\textbf{y}\\z&=\frac{1}{2}\end{align*}",
        r"$a+\$+b$",
    ],
)
def test_math_is_shielded_from_upstream_command_stripping(formula: str) -> None:
    def upstream(value: str) -> str:
        return re.sub(r"\\[A-Za-z]+", "", value).replace("_", " ")

    assert render_tex_with_formatting(formula, upstream) == formula
    assert (
        render_tex_with_formatting(r"\textbf{value " + formula + "}", upstream)
        == "**value " + formula + "**"
    )


def test_verbatim_code_and_percent_signs_stay_literal() -> None:
    source = "\\begin{verbatim}\n\\textbf{x} $y$ % not a comment\n```\n\\end{verbatim}"
    result = render_tex_with_formatting(
        source, lambda text: re.sub(r"%[^\n]*", "", text)
    )
    assert result == "\n````text\n\\textbf{x} $y$ % not a comment\n```\n````\n"


def test_existing_markdown_code_stays_literal() -> None:
    source = "```cpp\n\\textbf{x} % comment\n```\n`\\texttt{x}` \\textbf{yes}"
    assert (
        _render(source) == "```cpp\n\\textbf{x} % comment\n```\n`\\texttt{x}` **yes**"
    )


def test_tokens_do_not_collide_with_statement_text() -> None:
    assert _render(r"P2HFORMATKEEP0END \textbf{x}") == "P2HFORMATKEEP0END **x**"


def test_polygon_line_endings_are_normalized_inside_formatting() -> None:
    assert _render("\\textbf{a\r\n b}\r\\n\\texttt{x\r\ny}") == "**a\n b**\n`x y`"


def test_deep_text_formatting_fails_with_a_controlled_error() -> None:
    with pytest.raises(ValueError, match="formatting nesting limit"):
        _render(r"\textbf{" * 100 + "x" + "}" * 100)


def test_patch_updates_preimported_renderers_and_is_idempotent(monkeypatch) -> None:
    package = types.ModuleType("p2h")
    package.__path__ = []
    statement = types.ModuleType("p2h.statement_markdown")
    reader = types.ModuleType("p2h.polygon_reader")
    cli = types.ModuleType("p2h.cli")

    def original(text):
        return text

    for name in ("tex_to_markdown", "tex_block_to_markdown"):
        setattr(statement, name, original)
        setattr(reader, name, original)
        setattr(cli, name, original)
    for name, module in (
        ("p2h", package),
        ("p2h.statement_markdown", statement),
        ("p2h.polygon_reader", reader),
        ("p2h.cli", cli),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    p2h_safe._install_statement_patches()
    patched = statement.tex_to_markdown
    p2h_safe._install_statement_patches()

    assert statement.tex_to_markdown is patched
    assert reader.tex_to_markdown(r"\textbf{x}") == "**x**"
    assert cli.tex_block_to_markdown(r"\texttt{x}") == "`x`"
