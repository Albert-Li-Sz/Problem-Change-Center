from __future__ import annotations

import argparse
import logging
import os
import re
import shlex
import shutil
import subprocess  # nosec B404
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from defusedxml import ElementTree as DefusedET

from format_bridge import (
    convert_domjudge_to_hydro,
    convert_hydro_to_domjudge,
    validate_zip_archive,
)
from hoj_bridge import (
    convert_hoj_to_domjudge,
    convert_hoj_to_hydro,
    convert_hydro_to_hoj,
)


EXECUTABLE_SUFFIXES = {".sh", ".bash", ".exe"}
NATIVE_CPP_SUFFIXES = {".cc", ".cpp", ".cxx"}
NATIVE_C_SUFFIXES = {".c"}
WINE_EXE_RE = re.compile(
    r"(?<![A-Za-z0-9_-])wine[ \t]+"
    r"(?P<target>[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.exe)"
)
MAX_POLYGON_SCRIPT_BYTES = 2 * 1024 * 1024

STRICT_DOALL_BASH_ENV = """\
read() {
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
    functions = set(
        re.findall(
            r"^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(\))?\s*\{",
            text,
            flags=re.M,
        )
    )

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

        if re.match(
            r"^(?:function\s+)?[A-Za-z_][A-Za-z0-9_]*\s*(?:\(\))?\s*\{", stripped
        ):
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*(?:\[[^]]*\])?\s*=", stripped):
            continue
        if stripped in {"(", ")", "{", "}"}:
            continue

        try:
            parts = shlex.split(stripped, posix=True)
        except ValueError:
            continue
        if not parts:
            continue

        first = parts[0]
        if first in SHELL_WORDS or first in functions:
            continue
        if first in {"bash", "sh"}:
            continue
        if first.startswith(("scripts/", "files/", "solutions/", "./", "../", "$")):
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


def normalize_polygon_testdata_line_endings(root: Path, slugs: list[str]) -> int:
    normalized = 0
    for slug in slugs:
        tests_dir = root / "problems" / slug / "tests"
        if not tests_dir.is_dir():
            continue

        for path in tests_dir.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            if _normalize_crlf_file(path):
                normalized += 1
    return normalized


def normalize_polygon_doall_inputs(root: Path, slugs: list[str]) -> int:
    normalized = 0
    relative_dirs = (
        Path("tests"),
        Path("files/tests/validator-tests"),
        Path("files/tests/checker-tests"),
    )
    for slug in slugs:
        problem_root = root / "problems" / slug
        for relative_dir in relative_dirs:
            data_dir = problem_root / relative_dir
            if not data_dir.is_dir():
                continue
            for path in data_dir.rglob("*"):
                if path.is_symlink() or not path.is_file():
                    continue
                if _normalize_crlf_file(path):
                    normalized += 1
    return normalized


def prepare_native_polygon_doall(
    work_root: Path, slugs: list[str], *, verbose: bool = False
) -> int:
    """Replace source-backed Wine commands with native Linux executables.

    Polygon exports created on Windows commonly contain both PE binaries and the
    C/C++ source used to build them. Rebuilding those binaries inside the runner
    avoids Wine entirely and is especially important on Apple Silicon, where an
    amd64 container plus Wine adds a second emulation layer.
    """

    rebuilt = 0
    for slug in slugs:
        problem_root = work_root / "problems" / slug
        if not problem_root.is_dir():
            continue

        scripts = _polygon_shell_scripts(problem_root)
        wine_targets: set[str] = set()
        script_text: dict[Path, str] = {}
        unsupported_wine_syntax = False
        for script in scripts:
            text = _read_polygon_script(script)
            if text is None:
                unsupported_wine_syntax = True
                break
            script_text[script] = text
            matches = list(WINE_EXE_RE.finditer(text))
            if re.search(r"(?<![A-Za-z0-9_-])wine(?:64)?[ \t]+", text):
                matched_starts = {match.start() for match in matches}
                wine_starts = {
                    match.start()
                    for match in re.finditer(r"(?<![A-Za-z0-9_-])wine[ \t]+", text)
                }
                if wine_starts != matched_starts:
                    unsupported_wine_syntax = True
                    break
            wine_targets.update(
                _normalize_wine_target(match.group("target")) for match in matches
            )

        if unsupported_wine_syntax or not wine_targets:
            continue

        source_map = _polygon_native_source_map(problem_root)
        towin_target = "files/towin.exe"
        compile_targets = wine_targets - {towin_target}
        if not compile_targets.issubset(source_map):
            if verbose:
                missing = ", ".join(sorted(compile_targets - source_map))
                print(
                    f"[{slug}] native doall fallback unavailable; "
                    f"missing supported source for: {missing}"
                )
            continue

        with tempfile.TemporaryDirectory(
            prefix=".p2h-native-build-", dir=work_root
        ) as build_dir_text:
            build_dir = Path(build_dir_text)
            compiled: dict[str, Path] = {}
            compile_total = len(compile_targets)
            try:
                for index, target in enumerate(sorted(compile_targets), start=1):
                    source, source_type = source_map[target]
                    output = build_dir / f"{index:04d}.exe"
                    command = _native_compile_command(source, source_type, output)
                    print(
                        f"[{slug}] native compile {index}/{compile_total}: {target}",
                        flush=True,
                    )
                    # Compiler name and flags are runner-controlled; uploaded
                    # paths are passed as argv and shell execution is disabled.
                    completed = subprocess.run(  # nosec B603
                        command,
                        cwd=problem_root,
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=180,
                    )
                    if completed.returncode != 0:
                        detail = (completed.stderr or completed.stdout).strip()
                        if len(detail) > 2000:
                            detail = detail[-2000:]
                        raise RuntimeError(
                            f"failed to rebuild {target} from "
                            f"{source.relative_to(problem_root)}"
                            + (f": {detail}" if detail else "")
                        )
                    output.chmod(0o755)
                    compiled[target] = output
            except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                print(
                    f"[{slug}] native doall fallback failed: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                continue

            for target, built_binary in compiled.items():
                destination = _safe_problem_path(problem_root, target)
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(built_binary, destination)
                destination.chmod(0o755)

            if towin_target in wine_targets:
                towin = _safe_problem_path(problem_root, towin_target)
                towin.parent.mkdir(parents=True, exist_ok=True)
                towin.write_text(
                    "#!/usr/bin/env bash\n"
                    "set -e\n"
                    'if [ "$#" -ne 1 ]; then exit 2; fi\n'
                    "sed 's/\\r$//' -- \"$1\"\n",
                    encoding="utf-8",
                )
                towin.chmod(0o755)

            for script, text in script_text.items():
                rewritten = WINE_EXE_RE.sub(_native_wine_replacement, text)
                if rewritten != text:
                    script.write_text(rewritten, encoding="utf-8")

            _make_polygon_doall_fail_fast(problem_root)
            rebuilt += len(compiled)
            print(
                f"[{slug}] native compile complete: {len(compiled)} executable(s)",
                flush=True,
            )

    return rebuilt


def ensure_polygon_wine_is_usable(work_root: Path, slugs: list[str]) -> None:
    requires_wine = False
    for slug in slugs:
        problem_root = work_root / "problems" / slug
        for script in _polygon_shell_scripts(problem_root):
            text = _read_polygon_script(script)
            if text and re.search(r"(?<![A-Za-z0-9_-])wine(?:64)?[ \t]+", text):
                requires_wine = True
                break
        if requires_wine:
            break

    wine_path = shutil.which("wine")
    if not requires_wine or wine_path is None:
        return

    try:
        completed = subprocess.run(  # nosec B603
            [wine_path, "cmd", "/c", "exit"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Wine runtime preflight failed: {exc}") from exc
    if completed.returncode == 0:
        return

    detail = (completed.stderr or completed.stdout).strip()
    if len(detail) > 1000:
        detail = detail[-1000:]
    message = (
        "Wine runtime preflight failed before doall.sh. "
        "On Apple Silicon, use Docker Desktop's Apple Virtualization framework "
        "with Rosetta, or provide source-backed executables so the runner can "
        "rebuild them natively"
    )
    if detail:
        message += f": {detail}"
    raise RuntimeError(message)


def _polygon_shell_scripts(problem_root: Path) -> list[Path]:
    if not problem_root.is_dir():
        return []
    return sorted(
        path
        for path in problem_root.rglob("*.sh")
        if path.is_file() and not path.is_symlink()
    )


def _read_polygon_script(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_POLYGON_SCRIPT_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError):
        return None


def _normalize_wine_target(target: str) -> str:
    normalized = str(PurePosixPath(target))
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _native_wine_replacement(match: re.Match[str]) -> str:
    target = match.group("target")
    return target if "/" in target else f"./{target}"


def _polygon_native_source_map(problem_root: Path) -> dict[str, tuple[Path, str]]:
    problem_xml = problem_root / "problem.xml"
    if not problem_xml.is_file():
        return {}
    try:
        root = DefusedET.parse(problem_xml).getroot()
    except (OSError, DefusedET.ParseError):
        return {}

    result: dict[str, tuple[Path, str]] = {}
    for node in root.iter():
        source_node = None
        binary_node = None
        for child in list(node):
            local_name = child.tag.rsplit("}", 1)[-1]
            if local_name == "source":
                source_node = child
            elif local_name == "binary":
                binary_node = child
        if source_node is None or binary_node is None:
            continue

        source_value = source_node.get("path", "")
        binary_value = binary_node.get("path", "")
        source_type = source_node.get("type", "")
        if not source_value or not binary_value:
            continue

        try:
            source = _safe_problem_path(problem_root, source_value)
            binary = _normalize_relative_problem_path(binary_value)
        except ValueError:
            continue
        if not source.is_file() or binary.suffix.lower() != ".exe":
            continue
        if source.suffix.lower() not in NATIVE_CPP_SUFFIXES | NATIVE_C_SUFFIXES:
            continue

        binary_text = binary.as_posix()
        existing = result.get(binary_text)
        candidate = (source, source_type)
        if existing is not None and existing != candidate:
            result.pop(binary_text, None)
            continue
        result[binary_text] = candidate
    return result


def _normalize_relative_problem_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value.replace("\\", "/"))
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".."} for part in path.parts)
    ):
        raise ValueError(f"unsafe Polygon path: {value}")
    return path


def _safe_problem_path(problem_root: Path, value: str) -> Path:
    relative = _normalize_relative_problem_path(value)
    root = problem_root.resolve()
    target = (root / Path(*relative.parts)).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"Polygon path escapes problem root: {value}")
    return target


def _native_compile_command(source: Path, source_type: str, output: Path) -> list[str]:
    suffix = source.suffix.lower()
    if suffix in NATIVE_C_SUFFIXES:
        standard_match = re.search(r"(?:gcc|c)(\d{2})", source_type)
        standard = standard_match.group(1) if standard_match else "11"
        return [
            "gcc",
            f"-std=gnu{standard}",
            "-O2",
            "-pipe",
            str(source),
            "-o",
            str(output),
        ]

    standard_match = re.search(r"g\+\+(\d{2})", source_type)
    standard = standard_match.group(1) if standard_match else "17"
    return [
        "g++",
        f"-std=gnu++{standard}",
        "-O2",
        "-pipe",
        str(source),
        "-o",
        str(output),
    ]


def _make_polygon_doall_fail_fast(problem_root: Path) -> None:
    doall = problem_root / "doall.sh"
    text = _read_polygon_script(doall)
    if text is None or "\nset -e\n" in text:
        return
    if text.startswith("#!"):
        first_line, separator, rest = text.partition("\n")
        text = f"{first_line}{separator}set -e\n{rest}"
    else:
        text = f"set -e\n{text}"
    doall.write_text(text, encoding="utf-8")


def _normalize_crlf_file(path: Path, *, chunk_size: int = 1024 * 1024) -> bool:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    fd = os.open(path, os.O_RDWR)
    try:
        size = os.fstat(fd).st_size
        read_offset = 0
        write_offset = 0
        pending = b""
        changed = False

        while read_offset < size:
            chunk = os.pread(fd, min(chunk_size, size - read_offset), read_offset)
            if not chunk:
                raise OSError(f"unexpected end of file while normalizing {path}")
            read_offset += len(chunk)

            data = pending + chunk
            if read_offset < size and data.endswith(b"\r"):
                data = data[:-1]
                pending = b"\r"
            else:
                pending = b""

            lf_data = data.replace(b"\r\n", b"\n")
            if lf_data != data:
                changed = True
            if changed:
                _pwrite_all(fd, lf_data, write_offset)
            write_offset += len(lf_data)

        if changed:
            os.ftruncate(fd, write_offset)
        return changed
    finally:
        os.close(fd)


def _pwrite_all(fd: int, data: bytes, offset: int) -> None:
    written = 0
    while written < len(data):
        count = os.pwrite(fd, data[written:], offset + written)
        if count <= 0:
            raise OSError("failed to write normalized test data")
        written += count


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


def ensure_checker_lang_auto(config_yaml: str) -> str:
    lines = config_yaml.splitlines()
    trailing_newline = config_yaml.endswith("\n")
    index = 0

    while index < len(lines):
        match = re.match(r"^(\s*)checker:\s*(?:#.*)?$", lines[index])
        if match is None:
            index += 1
            continue

        child_prefix = match.group(1) + "  "
        block_end = index + 1
        insert_at = index + 1
        has_language = False

        while block_end < len(lines):
            line = lines[block_end]
            stripped = line.strip()
            if stripped and not line.startswith(child_prefix):
                break
            if line.startswith(child_prefix):
                child = line[len(child_prefix) :]
                if re.match(r"(?:lang|language)\s*:", child):
                    has_language = True
                if re.match(r"file\s*:", child):
                    insert_at = block_end + 1
            block_end += 1

        if not has_language:
            lines.insert(insert_at, f"{child_prefix}lang: auto")
            if insert_at <= block_end:
                block_end += 1

        index = block_end

    result = "\n".join(lines)
    if trailing_newline:
        result += "\n"
    return result


def _install_hydro_writer_patches() -> None:
    import p2h.hydro_writer as hydro_writer

    original = getattr(hydro_writer, "_build_config_yaml", None)
    if original is None or getattr(original, "_p2h_safe_checker_language_patch", False):
        return

    def build_config_yaml_with_checker_language_auto(*args: Any, **kwargs: Any) -> str:
        return ensure_checker_lang_auto(original(*args, **kwargs))

    build_config_yaml_with_checker_language_auto._p2h_safe_checker_language_patch = True  # type: ignore[attr-defined]
    hydro_writer._build_config_yaml = build_config_yaml_with_checker_language_auto


def _install_p2h_patches() -> Any:
    try:
        _install_hydro_writer_patches()
    except ModuleNotFoundError as exc:
        if exc.name not in {"p2h", "p2h.hydro_writer"}:
            raise

    p2h_convert = _load_p2h_convert()
    p2h_convert._collect_tools_from_script = collect_tools_from_script

    original_detect = getattr(p2h_convert, "_detect_missing_doall_tools", None)
    if original_detect is not None and not getattr(
        original_detect, "_p2h_safe_native_patch", False
    ):

        def detect_missing_tools_with_native_fallback(
            work_root: Path, slugs: list[str]
        ) -> list[str]:
            prepare_native_polygon_doall(Path(work_root), slugs)
            ensure_polygon_wine_is_usable(Path(work_root), slugs)
            return original_detect(work_root, slugs)

        detect_missing_tools_with_native_fallback._p2h_safe_native_patch = True  # type: ignore[attr-defined]
        p2h_convert._detect_missing_doall_tools = (
            detect_missing_tools_with_native_fallback
        )

    original_safe_extract = getattr(p2h_convert, "_safe_extract_contest_zip", None)
    if original_safe_extract is not None and not getattr(
        original_safe_extract, "_p2h_safe_archive_limit_patch", False
    ):

        def extract_contest_with_limits(
            contest_zip: Path, *args: Any, **kwargs: Any
        ) -> Any:
            validate_zip_archive(Path(contest_zip))
            return original_safe_extract(contest_zip, *args, **kwargs)

        extract_contest_with_limits._p2h_safe_archive_limit_patch = True  # type: ignore[attr-defined]
        p2h_convert._safe_extract_contest_zip = extract_contest_with_limits

    original = getattr(p2h_convert, "_run_doall_for_all", None)
    if original is not None and not getattr(
        original, "_p2h_safe_executable_patch", False
    ):

        def run_doall_with_executable_fix(
            work_root: Path, slugs: list[str], *args: Any, **kwargs: Any
        ) -> Any:
            work_root = Path(work_root)
            for slug in slugs:
                _make_polygon_doall_fail_fast(work_root / "problems" / slug)
            prepare_native_polygon_doall(
                work_root, slugs, verbose=bool(kwargs.get("verbose"))
            )
            ensure_polygon_wine_is_usable(work_root, slugs)
            normalized_inputs = normalize_polygon_doall_inputs(work_root, slugs)
            if normalized_inputs and kwargs.get("verbose"):
                print(
                    "normalized CRLF to LF in "
                    f"{normalized_inputs} existing doall input file(s)"
                )
            fixed = normalize_polygon_executable_bits(work_root)
            if fixed and kwargs.get("verbose"):
                print(f"fixed executable bit on {fixed} extracted file(s) before doall")

            bash_env = _write_strict_doall_bash_env(work_root)
            previous_bash_env = os.environ.get("BASH_ENV")
            os.environ["BASH_ENV"] = str(bash_env)
            try:
                result = original(work_root, slugs, *args, **kwargs)
            finally:
                if previous_bash_env is None:
                    os.environ.pop("BASH_ENV", None)
                else:
                    os.environ["BASH_ENV"] = previous_bash_env

            normalized = normalize_polygon_testdata_line_endings(work_root, slugs)
            if normalized and kwargs.get("verbose"):
                print(
                    f"normalized CRLF to LF in {normalized} generated testdata file(s)"
                )
            return result

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
    parser.add_argument(
        "--auto-validator", dest="auto_validator", action="store_true", default=None
    )
    parser.add_argument(
        "--no-auto-validator", dest="auto_validator", action="store_false"
    )
    parser.add_argument(
        "--default-validator", dest="default_validator", action="store_true"
    )
    parser.add_argument("--with-statement", dest="with_statement", action="store_true")
    parser.add_argument(
        "--without-statement", dest="with_statement", action="store_false"
    )
    parser.set_defaults(with_statement=False)
    parser.add_argument(
        "--with-attachments", dest="with_attachments", action="store_true"
    )
    parser.add_argument(
        "--without-attachments", dest="with_attachments", action="store_false"
    )
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


def _build_domjudge_to_hydro_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="domjudge-to-hydro")
    parser.add_argument("source_zip", type=Path)
    parser.add_argument("-o", "--output", required=True, type=Path)
    parser.add_argument("--pid-start", default="P1000")
    parser.add_argument("--owner", type=int, default=1)
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--verbose", action="store_true")
    return parser


def _build_hoj_to_hydro_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hoj-to-hydro")
    parser.add_argument("source_zip", type=Path)
    parser.add_argument("-o", "--output", required=True, type=Path)
    parser.add_argument("--pid-start", default="P1000")
    parser.add_argument("--owner", type=int, default=1)
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--verbose", action="store_true")
    return parser


def _build_hydro_to_hoj_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hydro-to-hoj")
    parser.add_argument("source_zip", type=Path)
    parser.add_argument("-o", "--output", required=True, type=Path)
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--verbose", action="store_true")
    return parser


def _build_hoj_to_domjudge_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hoj-to-domjudge")
    parser.add_argument("source_zip", type=Path)
    parser.add_argument("-o", "--output", required=True, type=Path)
    parser.add_argument("--code-start", default="A")
    parser.add_argument("--color", default="#000000")
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--verbose", action="store_true")
    return parser


def _build_package_convert_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="package-convert")
    parser.add_argument("source_zip", type=Path)
    parser.add_argument("-o", "--output", required=True, type=Path)
    parser.add_argument(
        "--source-format",
        default="auto",
        choices=[
            "auto",
            "polygon",
            "hydro",
            "icpc",
            "hoj",
            "fps",
            "qduoj",
            "uoj",
            "dmoj",
            "generic",
        ],
    )
    parser.add_argument(
        "--target-format",
        required=True,
        choices=["hydro", "icpc", "hoj", "fps", "qduoj", "uoj", "dmoj"],
    )
    parser.add_argument("--loss-policy", choices=["warn", "error"], default="warn")
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--pid-start", default="P1000")
    parser.add_argument("--owner", type=int, default=1)
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--code-start", default="A")
    parser.add_argument("--color", default="#000000")
    parser.add_argument(
        "--icpc-profile", choices=["legacy-icpc", "2025-09"], default="legacy-icpc"
    )
    parser.add_argument(
        "--icpc-license",
        choices=[
            "unknown",
            "public domain",
            "cc0",
            "cc by",
            "cc by-sa",
            "educational",
            "permission",
        ],
        default="unknown",
    )
    parser.add_argument("--icpc-rights-owner", default="")
    parser.add_argument(
        "--fps-profile", choices=["hustoj-1.6", "qduoj-1.2"], default="hustoj-1.6"
    )
    parser.add_argument("--missing-env", choices=["warn", "error"], default="warn")
    parser.add_argument(
        "--validator-mode", choices=["auto", "default", "custom"], default="auto"
    )
    parser.add_argument("--run-doall", dest="run_doall", action="store_true")
    parser.add_argument("--no-run-doall", dest="run_doall", action="store_false")
    parser.set_defaults(run_doall=False)
    parser.add_argument("--with-statement", action="store_true")
    parser.add_argument("--with-attachments", action="store_true")
    parser.add_argument(
        "--progress-format", choices=["text", "jsonl", "none"], default="text"
    )
    parser.add_argument("--total-timeout", type=int, default=7200)
    parser.add_argument("--idle-timeout", type=int, default=300)
    parser.add_argument("--stage-timeout", type=int, default=1800)
    parser.add_argument("--problem-timeout", type=int, default=1200)
    parser.add_argument("--repair-plan", type=Path)
    parser.add_argument("--supplements-dir", type=Path)
    return parser


def _convert_package(argv: list[str]) -> int:
    parser = _build_package_convert_parser()
    args = parser.parse_args(argv)
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", args.color):
        parser.error("--color must be in #RRGGBB format")
    for name in (
        "total_timeout",
        "idle_timeout",
        "stage_timeout",
        "problem_timeout",
    ):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    options = {
        "pid_start": args.pid_start,
        "owner": args.owner,
        "tags": args.tag,
        "code_start": args.code_start,
        "color": args.color,
        "profile": args.icpc_profile
        if args.target_format == "icpc"
        else args.fps_profile,
        "license": args.icpc_license,
        "rights_owner": args.icpc_rights_owner,
        "missing_env": args.missing_env,
        "validator_mode": args.validator_mode,
        "run_doall": args.run_doall,
        "with_statement": args.with_statement,
        "with_attachments": args.with_attachments,
    }
    try:
        from package_converter import convert_package
        from progress import ProgressReporter

        with ProgressReporter(
            output_format=args.progress_format,
            total_timeout_seconds=args.total_timeout,
            idle_timeout_seconds=args.idle_timeout,
            stage_timeout_seconds=args.stage_timeout,
            problem_timeout_seconds=args.problem_timeout,
        ) as reporter:
            report = convert_package(
                args.source_zip,
                args.output,
                source_format=args.source_format,
                target_format=args.target_format,
                loss_policy=args.loss_policy,
                only=args.only,
                options=options,
                reporter=reporter,
                repair_plan=args.repair_plan,
                supplements_dir=args.supplements_dir,
            )
            reporter.phase(
                "package",
                detail="conversion artifacts finalized for host packaging",
                current=1,
                total=1,
                unit="outputs",
            )
            reporter.complete(detail="conversion artifacts are ready")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"package-convert failed: {exc}", file=sys.stderr)
        return 1
    counts = report.get("counts", {})
    print(
        "done: "
        f"source={report.get('source_format')} target={report.get('target_format')} "
        f"problems={report.get('problem_count')} warnings={counts.get('warning', 0)} "
        f"losses={counts.get('loss', 0)}"
    )
    return 0


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


def _convert_domjudge_to_hydro(argv: list[str]) -> int:
    parser = _build_domjudge_to_hydro_parser()
    args = parser.parse_args(argv)
    if args.owner < 1:
        parser.error("--owner must be positive")
    try:
        return convert_domjudge_to_hydro(
            args.source_zip,
            args.output,
            pid_start=args.pid_start,
            owner=args.owner,
            tags=args.tag,
            only=args.only,
            verbose=args.verbose,
        )
    except Exception as exc:
        print(f"domjudge-to-hydro failed: {exc}", file=sys.stderr)
        return 1


def _convert_hoj_to_hydro(argv: list[str]) -> int:
    parser = _build_hoj_to_hydro_parser()
    args = parser.parse_args(argv)
    if args.owner < 1:
        parser.error("--owner must be positive")
    try:
        return convert_hoj_to_hydro(
            args.source_zip,
            args.output,
            pid_start=args.pid_start,
            owner=args.owner,
            tags=args.tag,
            only=args.only,
            verbose=args.verbose,
        )
    except Exception as exc:
        print(f"hoj-to-hydro failed: {exc}", file=sys.stderr)
        return 1


def _convert_hydro_to_hoj(argv: list[str]) -> int:
    parser = _build_hydro_to_hoj_parser()
    args = parser.parse_args(argv)
    try:
        return convert_hydro_to_hoj(
            args.source_zip,
            args.output,
            only=args.only,
            verbose=args.verbose,
        )
    except Exception as exc:
        print(f"hydro-to-hoj failed: {exc}", file=sys.stderr)
        return 1


def _convert_hoj_to_domjudge(argv: list[str]) -> int:
    parser = _build_hoj_to_domjudge_parser()
    args = parser.parse_args(argv)
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", args.color):
        parser.error("--color must be in #RRGGBB format")
    try:
        return convert_hoj_to_domjudge(
            args.source_zip,
            args.output,
            code_start=args.code_start,
            color=args.color,
            only=args.only,
            verbose=args.verbose,
        )
    except Exception as exc:
        print(f"hoj-to-domjudge failed: {exc}", file=sys.stderr)
        return 1


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "package-convert":
        return _convert_package(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "domjudge-convert":
        return _convert_domjudge(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "hydro-to-domjudge":
        return _convert_hydro_to_domjudge(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "domjudge-to-hydro":
        return _convert_domjudge_to_hydro(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "hoj-to-hydro":
        return _convert_hoj_to_hydro(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "hydro-to-hoj":
        return _convert_hydro_to_hoj(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "hoj-to-domjudge":
        return _convert_hoj_to_domjudge(sys.argv[2:])

    _install_p2h_patches()

    from p2h.cli import main as cli_main

    return cli_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
