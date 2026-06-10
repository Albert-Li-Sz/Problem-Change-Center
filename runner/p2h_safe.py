from __future__ import annotations

import argparse
import logging
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Any

from format_bridge import convert_hydro_to_domjudge


EXECUTABLE_SUFFIXES = {".sh", ".bash", ".exe"}

STRICT_DOALL_BASH_ENV = """\
read() {
    if [ "$#" -gt 0 ] && ! { [ "$#" -eq 1 ] && [ "$1" = "-r" ]; }; then
        builtin read "$@"
        return "$?"
    fi

    builtin read "$@"
    local status=$?
    if [ "$status" -ne 0 ]; then
        exit "$status"
    fi
    return 0
}
"""


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


def normalize_polygon_executable_bits(root: Path) -> int:
    fixed = 0
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        if not _should_be_executable(path):
            continue

        current_mode = path.stat().st_mode
        executable_mode = current_mode | 0o111
        if executable_mode == current_mode:
            continue
        path.chmod(executable_mode)
        fixed += 1
    return fixed


def _should_be_executable(path: Path) -> bool:
    if path.suffix.lower() in EXECUTABLE_SUFFIXES:
        return True

    try:
        with path.open("rb") as handle:
            return handle.read(2) == b"#!"
    except OSError:
        return False


def _load_p2h_convert() -> Any:
    import p2h.convert as p2h_convert

    return p2h_convert


def _install_p2h_patches() -> Any:
    p2h_convert = _load_p2h_convert()
    p2h_convert._collect_tools_from_script = collect_tools_from_script

    original = getattr(p2h_convert, "_run_doall_for_all", None)
    if original is not None and not getattr(original, "_p2h_safe_executable_patch", False):

        def run_doall_with_executable_fix(work_root: Path, slugs: list[str], *args: Any, **kwargs: Any) -> Any:
            work_root = Path(work_root)
            fixed = normalize_polygon_executable_bits(work_root)
            if fixed and kwargs.get("verbose"):
                print(f"fixed executable bit on {fixed} extracted file(s) before doall")

            bash_env = _write_strict_doall_bash_env(work_root)
            previous_bash_env = os.environ.get("BASH_ENV")
            os.environ["BASH_ENV"] = str(bash_env)
            try:
                return original(work_root, slugs, *args, **kwargs)
            finally:
                if previous_bash_env is None:
                    os.environ.pop("BASH_ENV", None)
                else:
                    os.environ["BASH_ENV"] = previous_bash_env

        run_doall_with_executable_fix._p2h_safe_executable_patch = True  # type: ignore[attr-defined]
        p2h_convert._run_doall_for_all = run_doall_with_executable_fix

    return p2h_convert


def _write_strict_doall_bash_env(work_root: Path) -> Path:
    path = work_root / ".p2h-doall-bash-env"
    path.write_text(STRICT_DOALL_BASH_ENV, encoding="utf-8")
    return path


def _code_to_index(code: str) -> int:
    if not re.fullmatch(r"[A-Za-z]+", code):
        raise ValueError("code-start must contain letters only, for example A")

    value = 0
    for char in code.upper():
        value = value * 26 + (ord(char) - ord("A") + 1)
    return value - 1


def _index_to_code(index: int) -> str:
    if index < 0:
        raise ValueError("code index must be non-negative")

    chars: list[str] = []
    value = index
    while True:
        value, remainder = divmod(value, 26)
        chars.append(chr(ord("A") + remainder))
        if value == 0:
            break
        value -= 1
    return "".join(reversed(chars))


def _build_domjudge_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="domjudge-convert")
    parser.add_argument("contest_zip", type=Path)
    parser.add_argument("-o", "--output", required=True, type=Path)
    parser.add_argument("--code-start", default="A")
    parser.add_argument("--color", default="#000000")
    parser.add_argument("--missing-env", choices=["warn", "error"], default="warn")
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--run-doall", dest="run_doall", action="store_true")
    parser.add_argument("--no-run-doall", dest="run_doall", action="store_false")
    parser.set_defaults(run_doall=False)
    parser.add_argument("--auto-validator", dest="auto_validator", action="store_true", default=None)
    parser.add_argument("--no-auto-validator", dest="auto_validator", action="store_false")
    parser.add_argument("--default-validator", dest="default_validator", action="store_true")
    parser.add_argument("--with-statement", dest="with_statement", action="store_true")
    parser.add_argument("--without-statement", dest="with_statement", action="store_false")
    parser.set_defaults(with_statement=False)
    parser.add_argument("--with-attachments", dest="with_attachments", action="store_true")
    parser.add_argument("--without-attachments", dest="with_attachments", action="store_false")
    parser.set_defaults(with_attachments=False)
    parser.add_argument("--hide-sample", dest="hide_sample", action="store_true")
    parser.add_argument("--testset")
    parser.add_argument("--memory-limit", type=int)
    parser.add_argument("--output-limit", type=int, default=-1)
    parser.add_argument("--validator-flags")
    return parser


def _build_hydro_to_domjudge_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hydro-to-domjudge")
    parser.add_argument("source_zip", type=Path)
    parser.add_argument("-o", "--output", required=True, type=Path)
    parser.add_argument("--code-start", default="A")
    parser.add_argument("--color", default="#000000")
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--verbose", action="store_true")
    return parser


def _convert_domjudge(argv: list[str]) -> int:
    parser = _build_domjudge_parser()
    args = parser.parse_args(argv)

    if args.auto_validator is None:
        args.auto_validator = not args.default_validator
    if args.default_validator and args.auto_validator:
        parser.error("--default-validator and --auto-validator cannot be used together")
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", args.color):
        parser.error("--color must be in #RRGGBB format")

    try:
        start_index = _code_to_index(args.code_start)
    except ValueError as exc:
        parser.error(str(exc))

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    from p2d import ConvertOptions, DomjudgeOptions, convert
    from p2h.polygon_reader import list_problem_slugs_from_names

    p2h_convert = _install_p2h_patches()

    args.output.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    success = 0

    with tempfile.TemporaryDirectory(prefix="p2h-domjudge-contest-") as td:
        work_root = Path(td)
        try:
            names = p2h_convert._safe_extract_contest_zip(args.contest_zip, work_root)
        except Exception as exc:
            print(f"invalid contest zip: {exc}", file=sys.stderr)
            return 1

        all_slugs = list_problem_slugs_from_names(names)
        if not all_slugs:
            print("no problems found in contest zip", file=sys.stderr)
            return 1

        slugs = all_slugs
        if args.only:
            slug_set = set(all_slugs)
            missing = [slug for slug in args.only if slug not in slug_set]
            if missing:
                print(f"unknown slug(s): {', '.join(missing)}", file=sys.stderr)
                return 1
            slugs = args.only

        total = len(slugs)
        code_start = args.code_start.upper()
        print(
            "start: target=domjudge "
            f"total={total} output={args.output} "
            f"run_doall={'yes' if args.run_doall else 'no'} code_start={code_start}"
        )

        if args.run_doall:
            missing_tools = p2h_convert._detect_missing_doall_tools(work_root, slugs)
            if missing_tools:
                missing_text = ", ".join(missing_tools)
                msg = (
                    "missing environment tools for doall (precheck warning): "
                    f"{missing_text}; doall may still work in special environments"
                )
                if args.missing_env == "error":
                    print(msg + "; abort due to --missing-env error", file=sys.stderr)
                    return 1
                print(f"warning: {msg}", file=sys.stderr)

            try:
                p2h_convert._run_doall_for_all(work_root, slugs, verbose=args.verbose)
            except Exception as exc:
                print(f"doall failed: {exc}", file=sys.stderr)
                return 1

        for idx, slug in enumerate(slugs, start=1):
            code = _index_to_code(start_index + idx - 1)
            problem_dir = work_root / "problems" / slug
            output_zip = args.output / f"{code}-{slug}.zip"
            if output_zip.exists():
                output_zip.unlink()

            print(f"[{idx}/{total}] {slug} (code={code})")
            try:
                convert(
                    problem_dir,
                    short_name=code,
                    options=ConvertOptions(
                        output=output_zip,
                        options=DomjudgeOptions(
                            color=args.color,
                            force_default_validator=args.default_validator,
                            auto_detect_std_checker=args.auto_validator,
                            validator_flags=args.validator_flags,
                            hide_sample=args.hide_sample,
                            with_statement=args.with_statement,
                            with_attachments=args.with_attachments,
                            memory_limit_override=args.memory_limit,
                            output_limit_override=args.output_limit,
                        ),
                        testset_name=args.testset,
                    ),
                    confirm=lambda: True,
                )
                success += 1
                print(f"[{idx}/{total}] OK {slug} -> {output_zip}")
            except Exception as exc:
                errors.append(f"{slug}: {exc}")
                print(f"[{idx}/{total}] ERROR {slug}: {exc}")

    failed = len(slugs) - success
    print(f"done: target=domjudge total={len(slugs)} success={success} failed={failed}")
    for error in errors:
        print(f"- {error}", file=sys.stderr)
    return 0 if not errors else 1


def _convert_hydro_to_domjudge(argv: list[str]) -> int:
    parser = _build_hydro_to_domjudge_parser()
    args = parser.parse_args(argv)
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", args.color):
        parser.error("--color must be in #RRGGBB format")
    try:
        return convert_hydro_to_domjudge(
            args.source_zip,
            args.output,
            code_start=args.code_start,
            color=args.color,
            only=args.only,
            verbose=args.verbose,
        )
    except Exception as exc:
        print(f"hydro-to-domjudge failed: {exc}", file=sys.stderr)
        return 1


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "domjudge-convert":
        return _convert_domjudge(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "hydro-to-domjudge":
        return _convert_hydro_to_domjudge(sys.argv[2:])

    _install_p2h_patches()

    from p2h.cli import main as cli_main

    return cli_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
