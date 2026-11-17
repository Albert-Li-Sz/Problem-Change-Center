"""Read every statement language of a Polygon problem.

Upstream polygon2hydro keeps one language per problem and always stores it as
problem_zh.md, even when the text is English. Hydro keeps one Markdown file per
language, so every language is converted and Chinese comes first.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from package_security import read_limited_text

# Chinese is the primary language; English is the usual second statement.
PRIMARY_LANGUAGES = ("chinese", "english")
MAX_STATEMENT_BYTES = 32 * 1024 * 1024

# Polygon names statement languages by English word. Hydro uses short codes in
# problem_<code>.md file names.
POLYGON_LANGUAGE_CODES = {
    "arabic": "ar",
    "armenian": "hy",
    "azerbaijani": "az",
    "belarusian": "be",
    "bengali": "bn",
    "bulgarian": "bg",
    "chinese": "zh",
    "croatian": "hr",
    "czech": "cs",
    "danish": "da",
    "dutch": "nl",
    "english": "en",
    "estonian": "et",
    "finnish": "fi",
    "french": "fr",
    "georgian": "ka",
    "german": "de",
    "greek": "el",
    "hebrew": "he",
    "hindi": "hi",
    "hungarian": "hu",
    "indonesian": "id",
    "italian": "it",
    "japanese": "ja",
    "kazakh": "kk",
    "korean": "ko",
    "kyrgyz": "ky",
    "latvian": "lv",
    "lithuanian": "lt",
    "mongolian": "mn",
    "norwegian": "no",
    "persian": "fa",
    "polish": "pl",
    "portuguese": "pt",
    "romanian": "ro",
    "russian": "ru",
    "serbian": "sr",
    "slovak": "sk",
    "slovenian": "sl",
    "spanish": "es",
    "swedish": "sv",
    "tajik": "tg",
    "thai": "th",
    "turkish": "tr",
    "turkmen": "tk",
    "ukrainian": "uk",
    "uzbek": "uz",
    "vietnamese": "vi",
}

Renderer = Callable[[str], str]


@dataclass(frozen=True)
class PolygonStatement:
    language: str
    code: str
    markdown: str
    directory: Path | None


def order_languages(languages: Iterable[str]) -> list[str]:
    """Return unique language names: Chinese, English, then the rest sorted."""
    unique = dict.fromkeys(
        language.strip().lower() for language in languages if language.strip()
    )

    def rank(language: str) -> tuple[int, str]:
        if language in PRIMARY_LANGUAGES:
            return PRIMARY_LANGUAGES.index(language), language
        return len(PRIMARY_LANGUAGES), language

    return sorted(unique, key=rank)


def hydro_language_code(language: str) -> str:
    normalized = language.strip().lower()
    code = POLYGON_LANGUAGE_CODES.get(normalized)
    if code is not None:
        return code
    return re.sub(r"[^a-z0-9]+", "_", normalized).strip("_") or "und"


def polygon_statement_languages(base: Path, root: Any) -> list[str]:
    found: list[str] = []
    sections_root = base / "statement-sections"
    if sections_root.is_dir():
        found.extend(path.name for path in sections_root.iterdir() if path.is_dir())
    statements = root.find("statements")
    if statements is not None:
        found.extend(
            statement.attrib.get("language", "")
            for statement in statements.findall("statement")
        )
    return order_languages(found)


def read_polygon_statements(
    base: Path,
    root: Any,
    *,
    is_interactive: bool,
    sections_to_markdown: Callable[..., str],
    html_to_markdown: Renderer,
    tex_to_markdown: Renderer,
    on_error: Callable[[str, Exception], None] | None = None,
) -> list[PolygonStatement]:
    """Convert each statement language, primary language first.

    Languages that only have a PDF, or whose source is empty, are skipped.
    With on_error, a language that fails to convert is reported and skipped
    so that one broken translation does not fail the whole problem.
    """
    statements: list[PolygonStatement] = []
    used_codes: set[str] = set()
    for language in polygon_statement_languages(base, root):
        code = hydro_language_code(language)
        if code in used_codes:
            continue
        try:
            rendered = _read_language(
                base,
                root,
                language,
                is_interactive=is_interactive,
                sections_to_markdown=sections_to_markdown,
                html_to_markdown=html_to_markdown,
                tex_to_markdown=tex_to_markdown,
            )
        except Exception as exc:
            if on_error is None:
                raise
            on_error(language, exc)
            continue
        if rendered is None:
            continue
        markdown, directory = rendered
        used_codes.add(code)
        statements.append(PolygonStatement(language, code, markdown, directory))
    return statements


def _read_language(
    base: Path,
    root: Any,
    language: str,
    *,
    is_interactive: bool,
    sections_to_markdown: Callable[..., str],
    html_to_markdown: Renderer,
    tex_to_markdown: Renderer,
) -> tuple[str, Path] | None:
    # Same source order as upstream: statement sections, then HTML, then TeX.
    sections_dir = base / "statement-sections" / language
    if sections_dir.is_dir():
        markdown = sections_to_markdown(sections_dir, is_interactive=is_interactive)
        if markdown.strip():
            return markdown, sections_dir

    statements = root.find("statements")
    if statements is None:
        return None
    sources: dict[str, str] = {}
    for statement in statements.findall("statement"):
        if statement.attrib.get("language", "").strip().lower() != language:
            continue
        path = statement.attrib.get("path")
        statement_type = statement.attrib.get("type", "")
        if path and statement_type in {"text/html", "application/x-tex"}:
            sources[statement_type] = path

    for statement_type, renderer in (
        ("text/html", html_to_markdown),
        ("application/x-tex", tex_to_markdown),
    ):
        relative = sources.get(statement_type)
        if relative is None:
            continue
        path = _problem_file(base, relative)
        if path is None:
            continue
        markdown = renderer(_read_statement_text(path))
        if markdown.strip():
            return markdown, path.parent
    return None


def _problem_file(base: Path, relative: str) -> Path | None:
    candidate = PurePosixPath(relative.replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    path = base / candidate
    try:
        path.resolve().relative_to(base.resolve())
    except ValueError:
        return None
    return path if path.is_file() else None


def _read_statement_text(path: Path) -> str:
    try:
        return read_limited_text(path, limit=MAX_STATEMENT_BYTES)
    except ValueError as exc:
        if "not valid UTF-8" not in str(exc):
            raise
    # Upstream reads statements with errors="ignore"; keep accepting them.
    return path.read_text(encoding="utf-8", errors="ignore")
