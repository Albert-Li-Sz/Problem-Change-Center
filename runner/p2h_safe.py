from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

import p2h.convert


SHELL_WORDS = {
    "!",
    "(",
    ")",
    ":",
    "[",
    "[[",
    "]",
    "]]",
    "{",
    "}",
    ".",
    "break",
    "case",
    "cd",
    "command",
    "continue",
    "declare",
    "do",
    "done",
    "echo",
    "elif",
    "else",
    "esac",
    "eval",
    "exec",
    "exit",
    "export",
    "false",
    "fi",
    "for",
    "function",
    "if",
    "in",
    "local",
    "printf",
    "pwd",
    "read",
    "readonly",
    "return",
    "set",
    "shift",
    "source",
    "test",
    "then",
    "time",
    "times",
    "trap",
    "true",
    "type",
    "typeset",
    "ulimit",
    "umask",
    "unset",
    "until",
    "wait",
    "while",
}


def collect_tools_from_script(script_path: Path, tools: set[str]) -> None:
    if not script_path.exists() or not script_path.is_file():
        return

    text = script_path.read_text(encoding="utf-8", errors="ignore")
    functions = set(re.findall(r"^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(\))?\s*\{", text, flags=re.M))

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if re.search(r"(^|[;&|({\s])wine\s", stripped):
            tools.add("wine")
        if re.search(r"(^|[;&|({\s])java\s", stripped):
            tools.add("java")
        if re.search(r"(^|[;&|({\s])javac\s", stripped):
            tools.add("javac")

        if re.match(r"^(?:function\s+)?[A-Za-z_][A-Za-z0-9_]*\s*(?:\(\))?\s*\{", stripped):
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*(?:\[[^]]*\])?\s*=", stripped):
            continue
        if stripped in {"(", ")", "{", "}"}:
            continue

        try:
            parts = shlex.split(stripped, posix=True)
        except Exception:
            continue
        if not parts:
            continue

        first = parts[0]
        if first in SHELL_WORDS or first in functions:
            continue
        if first in {"bash", "sh"}:
            continue
        if first.startswith(("scripts/", "./", "../", "$")):
            continue
        if "=" in first and first.split("=", 1)[0].isidentifier():
            continue

        tools.add(first)


def main() -> int:
    p2h.convert._collect_tools_from_script = collect_tools_from_script
    from p2h.cli import main as cli_main

    return cli_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
