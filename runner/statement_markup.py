"""Convert TeX text formatting while shielding code and mathematics.

The upstream Polygon renderer still handles lists and images.
Opaque tokens keep its regular expressions away from balanced formatting groups,
math commands and literal code.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from package_security import read_limited_text


_FONTS = {
    "textbf": "bold",
    "textit": "italic",
    "textsl": "italic",
    "emph": "italic",
    "texttt": "code",
    "t": "code",  # Polygon's olymp.sty shorthand.
    "underline": "underline",
    "sout": "strike",
    "textsuperscript": "sup",
    "textsubscript": "sub",
    **dict.fromkeys(
        (
            "textrm",
            "textsf",
            "textnormal",
            "textup",
            "textmd",
            "textsc",
            "mbox",
            "text",
        ),
        "plain",
    ),
}
_DECLARATIONS = {
    "bfseries": "bold",
    "bf": "bold",
    "itshape": "italic",
    "slshape": "italic",
    "it": "italic",
    "em": "italic",
    "ttfamily": "code",
    "tt": "code",
    "rmfamily": "plain",
    "sffamily": "plain",
    "normalfont": "plain",
    "scshape": "plain",
    "upshape": "plain",
    "mdseries": "plain",
}
_SYMBOLS = {
    "textbackslash": "\\",
    "textasciitilde": "~",
    "textasciicircum": "^",
    "ldots": "…",
    "dots": "…",
}
_ENVIRONMENT = re.compile(r"\\begin\{([A-Za-z]+\*?)\}")
_MATH_ENVS = {
    "math",
    "displaymath",
    "equation",
    "align",
    "alignat",
    "gather",
    "multline",
    "flalign",
    "eqnarray",
    "matrix",
    "pmatrix",
    "bmatrix",
    "vmatrix",
    "Vmatrix",
}
_CODE_ENVS = {"verbatim", "verbatim*", "BVerbatim", "lstlisting", "minted"}
_COMMAND = re.compile(r"\\([A-Za-z]+)")
_VERB = re.compile(r"\\(?:verb\*?|lstinline)(?![A-Za-z])(?:\[[^\]]*\])?")
_DECLARATION = re.compile(r"\s*\\([A-Za-z]+)(?:\s+|(?=[^A-Za-z]))")
_FENCE = re.compile(r"(?m)^[ \t]{0,3}(`{3,}|~{3,})[^\n]*\n")
_CENTER = re.compile(r"\\(?:begin|end)\{center\}")
MAX_FORMATTING_DEPTH = 48


def _code_span(text: str) -> str:
    text = re.sub(r"\r\n?|\n", " ", text)
    width = max((len(m[0]) for m in re.finditer(r"`+", text)), default=0) + 1
    fence = "`" * width
    padding = (
        text.startswith("`")
        or text.endswith("`")
        or (text.startswith(" ") and text.endswith(" ") and bool(text.strip(" ")))
    )
    content = f" {text} " if padding else text
    return f"{fence}{content}{fence}" if text else ""


def _code_block(
    text: str, language: str = "text", *, strip_outer_newlines: bool = True
) -> str:
    width = max(3, max((len(m[0]) for m in re.finditer(r"`+", text)), default=0) + 1)
    fence = "`" * width
    body = text.strip("\r\n") if strip_outer_newlines else text
    newline = "" if body.endswith("\n") else "\n"
    return f"\n{fence}{language}\n{body}{newline}{fence}\n"


class _Formatting:
    def __init__(self, text: str) -> None:
        self.prefix = "P2HFORMATKEEP"
        while self.prefix in text:
            self.prefix += "X"
        self.tokens: dict[str, str] = {}
        self.pattern = re.compile(re.escape(self.prefix) + r"\d+END")

    def keep(self, text: str) -> str:
        token = f"{self.prefix}{len(self.tokens)}END"
        self.tokens[token] = text
        return token

    def restore(self, text: str) -> str:
        return self.pattern.sub(lambda m: self.tokens[m[0]], text)

    @staticmethod
    def _end(text: str, start: int, delimiter: str) -> int | None:
        pos = start
        while pos < len(text):
            if text.startswith(delimiter, pos):
                return pos + len(delimiter)
            pos += 2 if text[pos] == "\\" else 1
        return None

    def math_end(self, text: str, pos: int) -> int | None:
        if text.startswith("$$", pos):
            return self._end(text, pos + 2, "$$")
        if text[pos] == "$":
            return self._end(text, pos + 1, "$")
        for opening, closing in ((r"\(", r"\)"), (r"\[", r"\]")):
            if text.startswith(opening, pos):
                return self._end(text, pos + len(opening), closing)
        env = _ENVIRONMENT.match(text, pos)
        if env and env[1].rstrip("*") in _MATH_ENVS:
            return self._end(text, env.end(), r"\end{" + env[1] + "}")
        return None

    def group_end(self, text: str, pos: int) -> int | None:
        level = 1
        pos += 1
        while pos < len(text):
            if text[pos] in "$\\" and (end := self.math_end(text, pos)) is not None:
                pos = end
                continue
            if text[pos] == "\\":
                pos += 2
                continue
            if text[pos] == "%":
                newline = text.find("\n", pos)
                if newline < 0:
                    return None
                pos = newline + 1
                continue
            if text[pos] == "{":
                level += 1
            elif text[pos] == "}":
                level -= 1
                if level == 0:
                    return pos
            pos += 1
        return None

    def code(self, text: str, pos: int) -> tuple[int, str] | None:
        env = _ENVIRONMENT.match(text, pos)
        if env and env[1] in _CODE_ENVS:
            start = env.end()
            language = "text"
            options = re.match(r"(?:\[[^\]]*\])?", text[start:])
            if options:
                start += options.end()
            if env[1] == "minted":
                lang = re.match(r"\{([A-Za-z0-9_+-]+)\}", text[start:])
                if lang:
                    language = lang[1]
                    start += lang.end()
            closing = r"\end{" + env[1] + "}"
            end = text.find(closing, start)
            if end >= 0:
                return end + len(closing), _code_block(text[start:end], language)
        verb = _VERB.match(text, pos)
        if verb:
            start = verb.end()
            if start < len(text) and not text[start].isspace():
                delimiter = text[start]
                end = (
                    self.group_end(text, start)
                    if delimiter == "{" and text.startswith(r"\lstinline", pos)
                    else text.find(delimiter, start + 1)
                )
                if end is None:
                    return None
                if end >= 0 and "\n" not in text[start + 1 : end]:
                    return end + 1, _code_span(text[start + 1 : end])
        # Already converted Markdown code must also stay literal.
        fence = _FENCE.match(text, pos)
        if fence:
            mark = re.escape(fence[1][0])
            closing = re.compile(
                r"(?m)^[ \t]{0,3}"
                + mark
                + "{"
                + str(len(fence[1]))
                + r",}[ \t]*(?:\n|$)"
            )
            end = closing.search(text, fence.end())
            if end:
                return end.end(), text[pos : end.end()]
        if text[pos] == "`":
            start = pos + 1
            while start < len(text) and text[start] == "`":
                start += 1
            closing = re.compile(r"(?<!`)" + re.escape(text[pos:start]) + r"(?!`)")
            end = closing.search(text, start)
            if end:
                return end.end(), text[pos : end.end()]
        return None

    def styled(
        self, body: str, style: str, styles: frozenset[str], literal: bool
    ) -> str:
        body = self.restore(body)
        if literal or style in styles or style == "plain":
            return body
        if style == "code":
            return _code_span(body)
        if not body.strip():
            return body
        left = body[: len(body) - len(body.lstrip())]
        right = body[len(body.rstrip()) :]
        content = body.strip()
        markers = {
            "bold": ("**", "**"),
            "italic": ("*", "*"),
            "underline": ("<u>", "</u>"),
            "strike": ("~~", "~~"),
            "sup": ("<sup>", "</sup>"),
            "sub": ("<sub>", "</sub>"),
        }
        opening, closing = markers[style]
        return left + opening + content + closing + right

    def parse(
        self,
        text: str,
        depth: int = 0,
        styles: frozenset[str] = frozenset(),
        *,
        literal: bool = False,
    ) -> str:
        if depth > MAX_FORMATTING_DEPTH:
            raise ValueError("TeX text formatting nesting limit exceeded")
        output: list[str] = []
        pos = 0
        while pos < len(text):
            if not literal:
                code = self.code(text, pos)
                if code:
                    pos, value = code
                    output.append(self.keep(value))
                    continue
                if text[pos] in "$\\" and (end := self.math_end(text, pos)) is not None:
                    output.append(self.keep(text[pos:end]))
                    pos = end
                    continue
                # Upstream's center handler extracts images and discards every
                # other child. Keep the body in the normal rendering pipeline.
                centered = _CENTER.match(text, pos)
                if centered:
                    output.append("\n\n")
                    pos = centered.end()
                    continue
            if text[pos] == "%":
                newline = text.find("\n", pos)
                if newline < 0:
                    break
                pos = newline
            command = _COMMAND.match(text, pos)
            if command:
                name = command[1]
                start = command.end()
                while start < len(text) and text[start].isspace():
                    start += 1
                if name in _FONTS and start < len(text) and text[start] == "{":
                    end = self.group_end(text, start)
                    if end is not None:
                        style = _FONTS[name]
                        body = self.parse(
                            text[start + 1 : end],
                            depth + 1,
                            styles | {style},
                            literal=literal or style == "code",
                        )
                        value = self.styled(body, style, styles, literal)
                        output.append(value if literal else self.keep(value))
                        pos = end + 1
                        continue
                if name in _SYMBOLS:
                    symbol = _SYMBOLS[name]
                    output.append(symbol if literal else self.keep(symbol))
                    pos = command.end() + (
                        2 if text.startswith("{}", command.end()) else 0
                    )
                    continue
            if text[pos] == "{":
                end = self.group_end(text, pos)
                declaration = _DECLARATION.match(text, pos + 1)
                declared_style = (
                    _DECLARATIONS.get(declaration[1]) if declaration else None
                )
                if end is not None and (literal or declared_style is not None):
                    style = declared_style or "plain"
                    start = (
                        declaration.end() if declaration and declared_style else pos + 1
                    )
                    body = self.parse(
                        text[start:end],
                        depth + 1,
                        styles | {style},
                        literal=literal or style == "code",
                    )
                    value = self.styled(body, style, styles, literal)
                    output.append(value if literal else self.keep(value))
                    pos = end + 1
                    continue
            if text[pos] == "\\" and pos + 1 < len(text):
                escaped = text[pos + 1]
                if literal and escaped in "%&#_{}$ ":
                    output.append(escaped)
                elif literal and escaped == "\\":
                    output.append(" ")
                else:
                    output.append(text[pos : pos + 2])
                pos += 2
                continue
            output.append(" " if text[pos] == "~" else text[pos])
            pos += 1
        return "".join(output)


def render_tex_with_formatting(text: str, renderer: Callable[[str], str]) -> str:
    text = text.replace("\r\\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
    formatting = _Formatting(text)
    return formatting.restore(renderer(formatting.parse(text)))


def render_polygon_sections(
    sections_dir: Path,
    *,
    is_interactive: bool,
    renderer: Callable[[str], str],
) -> str:
    def read(path: Path) -> str:
        if not path.is_file():
            return ""
        text = read_limited_text(path)
        return text.replace("\r\\n", "\n").replace("\r\n", "\n").replace("\r", "\n")

    sections = {
        name: read(sections_dir / f"{name}.tex").strip()
        for name in (
            "legend",
            "input",
            "output",
            "interaction",
            "scoring",
            "constraints",
            "notes",
        )
    }
    samples: dict[str, dict[str, Path]] = {}
    for path in sorted(sections_dir.iterdir()):
        match = re.fullmatch(r"example\.(\d+)(\.a)?", path.name)
        if path.is_file() and match:
            samples.setdefault(match[1], {})["output" if match[2] else "input"] = path
    # A directory with just name.tex must not mask a usable declared HTML/TeX
    # statement by returning a nonempty placeholder.
    if not any(sections.values()) and not samples:
        return ""

    lines = [
        "# Description",
        renderer(sections["legend"])
        if sections["legend"]
        else "(No description provided)",
        "",
    ]
    interactive = is_interactive or bool(sections["interaction"])
    if sections["input"] or sections["output"] or not interactive:
        lines.extend(["# Format", ""])
        for name in ("input", "output"):
            if sections[name] or not interactive:
                lines.extend(
                    [
                        f"## {name.title()}",
                        renderer(sections[name])
                        if sections[name]
                        else f"(No {name} format provided)",
                        "",
                    ]
                )
    if interactive:
        lines.extend(
            [
                "# Interaction",
                renderer(sections["interaction"])
                if sections["interaction"]
                else "(No interaction provided)",
                "",
            ]
        )
    for name in ("scoring", "constraints"):
        if sections[name]:
            lines.extend([f"# {name.title()}", renderer(sections[name]), ""])
    if samples:
        lines.extend(["# Samples", ""])
        for number, key in enumerate(
            sorted(samples, key=lambda key: (int(key), key)), 1
        ):
            for role in ("input", "output"):
                if role in samples[key]:
                    lines.append(
                        _code_block(
                            read(samples[key][role]),
                            f"{role}{number}",
                            strip_outer_newlines=False,
                        )
                    )
    if sections["notes"]:
        lines.extend(["# Note", renderer(sections["notes"]), ""])
    return "\n".join(lines).strip() + "\n"
