from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote

from package_security import load_yaml_file


IN_SUFFIXES = {".in", ".input"}
OUT_SUFFIXES = {".ans", ".out", ".output"}
SOURCE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".java",
    ".kt",
    ".py",
    ".rs",
    ".go",
    ".pas",
}
HEADER_SUFFIXES = {".h", ".hh", ".hpp", ".hxx"}
DEFAULT_MAX_ARCHIVE_ENTRIES = 50_000
DEFAULT_MAX_ARCHIVE_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
DEFAULT_MAX_ARCHIVE_MEMBER_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_ARCHIVE_COMPRESSION_RATIO = 200.0


@dataclass(frozen=True)
class CaseFile:
    input_path: Path
    output_path: Path
    name: str
    sample: bool = False
    score: int | float | None = None


@dataclass
class ArchiveExtractionBudget:
    max_entries: int
    max_uncompressed_bytes: int
    max_member_bytes: int
    max_compression_ratio: float
    used_entries: int = 0
    used_uncompressed_bytes: int = 0

    @classmethod
    def from_env(cls) -> ArchiveExtractionBudget:
        return cls(
            max_entries=_positive_int_env(
                "P2H_MAX_ARCHIVE_ENTRIES", DEFAULT_MAX_ARCHIVE_ENTRIES
            ),
            max_uncompressed_bytes=_positive_int_env(
                "P2H_MAX_ARCHIVE_UNCOMPRESSED_BYTES",
                DEFAULT_MAX_ARCHIVE_UNCOMPRESSED_BYTES,
            ),
            max_member_bytes=_positive_int_env(
                "P2H_MAX_ARCHIVE_MEMBER_BYTES", DEFAULT_MAX_ARCHIVE_MEMBER_BYTES
            ),
            max_compression_ratio=_positive_float_env(
                "P2H_MAX_ARCHIVE_COMPRESSION_RATIO",
                DEFAULT_MAX_ARCHIVE_COMPRESSION_RATIO,
            ),
        )

    def reserve(self, info: zipfile.ZipInfo) -> None:
        if info.file_size > self.max_member_bytes:
            raise ValueError(
                f"zip member exceeds uncompressed size limit ({self.max_member_bytes} bytes): {info.filename}"
            )
        ratio = info.file_size / max(info.compress_size, 1)
        if ratio > self.max_compression_ratio:
            raise ValueError(
                f"zip member exceeds compression ratio limit ({self.max_compression_ratio:g}): {info.filename}"
            )
        if self.used_entries + 1 > self.max_entries:
            raise ValueError(f"zip archives exceed entry limit ({self.max_entries})")
        if self.used_uncompressed_bytes + info.file_size > self.max_uncompressed_bytes:
            raise ValueError(
                f"zip archives exceed uncompressed size limit ({self.max_uncompressed_bytes} bytes)"
            )
        self.used_entries += 1
        self.used_uncompressed_bytes += info.file_size


def convert_domjudge_to_hydro(
    source_zip: Path,
    output_dir: Path,
    *,
    pid_start: str = "P1000",
    owner: int = 1,
    tags: Iterable[str] = (),
    only: Iterable[str] = (),
    verbose: bool = False,
) -> int:
    pid_prefix, pid_number, pid_width = _parse_pid_start(pid_start)
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="domjudge-to-hydro-") as td:
        root = Path(td)
        extracted = root / "input"
        extraction_budget = ArchiveExtractionBudget.from_env()
        _safe_extract_zip(source_zip, extracted, budget=extraction_budget)
        package_root = _strip_single_root(extracted)
        problems = _find_domjudge_problem_sources(
            package_root, root / "nested", extraction_budget
        )
        problems = _filter_domjudge_problem_dirs(problems, only)

        if not problems:
            raise ValueError("no DOMjudge problem package found")

        normalized_tags = [str(tag).strip() for tag in tags if str(tag).strip()]
        print(
            f"start: target=domjudge_to_hydro total={len(problems)} output={output_dir}"
        )
        for idx, problem_dir in enumerate(problems, start=1):
            pid = f"{pid_prefix}{pid_number + idx - 1:0{pid_width}d}"
            meta = _read_yaml(problem_dir / "problem.yaml")
            ini = _read_domjudge_ini(problem_dir / "domjudge-problem.ini")
            slug = _domjudge_problem_slug(problem_dir, meta, ini)
            title = _domjudge_problem_title(problem_dir, meta, ini)
            work_root = root / "hydro" / f"{idx:03d}-{slug}"
            hydro_dir = work_root / pid
            output_zip = output_dir / f"{pid}-{slug}.zip"
            if output_zip.exists():
                output_zip.unlink()

            print(f"[{idx}/{len(problems)}] {slug} -> {output_zip.name}")
            _write_hydro_problem(
                problem_dir,
                hydro_dir,
                meta,
                ini,
                title,
                pid,
                owner,
                normalized_tags,
                verbose=verbose,
            )
            _zip_dir(work_root, output_zip)

    print(f"done: target=domjudge_to_hydro total={len(problems)}")
    return 0


def _write_hydro_problem(
    domjudge_dir: Path,
    hydro_dir: Path,
    meta: dict[str, Any],
    ini: dict[str, str],
    title: str,
    pid: str,
    owner: int,
    tags: list[str],
    *,
    verbose: bool,
) -> None:
    hydro_dir.mkdir(parents=True, exist_ok=True)
    hydro_meta: dict[str, Any] = {
        "title": title,
        "tag": tags,
        "pid": pid,
        "owner": owner,
    }
    (hydro_dir / "problem.yaml").write_text(_dump_yaml(hydro_meta), encoding="utf-8")

    statement_count = _copy_domjudge_pdf_statements(domjudge_dir, hydro_dir)
    if statement_count == 0:
        raise ValueError(
            f"{domjudge_dir.name}: no PDF statement found in problem_statement"
        )

    secret_cases = _scan_case_pairs(domjudge_dir / "data" / "secret")
    sample_cases = _scan_case_pairs(domjudge_dir / "data" / "sample", sample=True)
    judged_cases = secret_cases or sample_cases
    if not judged_cases:
        raise ValueError(f"{domjudge_dir.name}: no paired testdata files found")

    testdata_dir = hydro_dir / "testdata"
    copied_cases = _copy_hydro_cases(judged_cases, testdata_dir)
    _copy_domjudge_samples(sample_cases, hydro_dir / "additional_file" / "samples")
    checker = _copy_domjudge_checker(domjudge_dir, testdata_dir)

    config: dict[str, Any] = {
        "type": "default",
        "time": f"{_format_number(_domjudge_time_seconds(meta, ini))}s",
        "memory": f"{_domjudge_memory_mb(meta)}m",
    }
    if checker is not None:
        config["checker_type"] = "testlib"
        config["checker"] = {"file": checker, "lang": "auto"}
    config["cases"] = [
        {"input": input_name, "output": output_name}
        for input_name, output_name in copied_cases
    ]
    (testdata_dir / "config.yaml").write_text(_dump_yaml(config), encoding="utf-8")

    _copy_optional_tree(
        domjudge_dir / "attachments", hydro_dir / "additional_file" / "attachments"
    )
    source_count = _copy_domjudge_sources(
        domjudge_dir, hydro_dir / "additional_file" / "sources"
    )

    if verbose:
        sample_fallback = (
            " (used as judged data because secret data is absent)"
            if not secret_cases
            else ""
        )
        print(
            "  files: "
            f"statements={statement_count} secret={len(secret_cases)} "
            f"sample={len(sample_cases)}{sample_fallback} "
            f"checker={'yes' if checker else 'no'} sources={source_count}"
        )


def _find_domjudge_problem_sources(
    root: Path,
    nested_root: Path,
    extraction_budget: ArchiveExtractionBudget,
) -> list[Path]:
    direct = _find_domjudge_problems(root)
    candidates = list(direct)
    nested_archives = [
        archive
        for archive in sorted(root.rglob("*.zip"))
        if not any(
            _is_relative_to(archive.resolve(), problem.resolve()) for problem in direct
        )
    ]
    for idx, archive in enumerate(nested_archives, start=1):
        target = nested_root / f"{idx:03d}-{_safe_name(archive.stem) or 'package'}"
        try:
            _safe_extract_zip(archive, target, budget=extraction_budget)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            raise ValueError(
                f"invalid nested DOMjudge package {archive.name}: {exc}"
            ) from exc
        candidates.extend(_find_domjudge_problems(_strip_single_root(target)))
    return _dedupe_problem_dirs(candidates, root)


def _find_domjudge_problems(root: Path) -> list[Path]:
    candidates: list[Path] = []
    marker_dirs = {marker.parent for marker in root.rglob("domjudge-problem.ini")}
    marker_dirs.update(marker.parent for marker in root.rglob("problem.yaml"))
    if (root / "domjudge-problem.ini").exists() or (root / "problem.yaml").exists():
        marker_dirs.add(root)

    for problem_dir in sorted(marker_dirs):
        if (problem_dir / "data").is_dir() and (
            (problem_dir / "domjudge-problem.ini").is_file()
            or (problem_dir / "problem_statement").is_dir()
            or (problem_dir / "problem.yaml").is_file()
        ):
            candidates.append(problem_dir)
    return _dedupe_problem_dirs(candidates, root)


def _filter_domjudge_problem_dirs(
    problems: list[Path], only: Iterable[str]
) -> list[Path]:
    wanted = [_safe_name(item) for item in only if item]
    if not wanted:
        return problems

    by_key: dict[str, Path] = {}
    for problem in problems:
        meta = _read_yaml(problem / "problem.yaml")
        ini = _read_domjudge_ini(problem / "domjudge-problem.ini")
        keys = {
            _safe_name(problem.name),
            _domjudge_problem_slug(problem, meta, ini),
            _safe_name(str(meta.get("id", ""))),
            _safe_name(str(meta.get("uuid", ""))),
            _safe_name(str(meta.get("name", ""))),
            _safe_name(ini.get("short-name", "")),
            _safe_name(ini.get("externalid", "")),
            _safe_name(ini.get("name", "")),
        }
        for key in keys:
            if key:
                by_key[key] = problem

    missing = [item for item in wanted if item not in by_key]
    if missing:
        raise ValueError(f"unknown problem(s): {', '.join(missing)}")
    return [by_key[item] for item in wanted]


def _read_domjudge_ini(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";", "[")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip().lower()] = value.strip().strip("\"'")
    return result


def _domjudge_problem_slug(
    problem_dir: Path, meta: dict[str, Any], ini: dict[str, str]
) -> str:
    candidates = [
        meta.get("id"),
        meta.get("slug"),
        ini.get("externalid"),
        ini.get("short-name"),
        problem_dir.name,
        meta.get("uuid"),
        meta.get("name"),
    ]
    for candidate in candidates:
        slug = _safe_name(str(candidate or ""))
        if slug and slug not in {"input", "package", "problem"}:
            return slug
    return "problem"


def _domjudge_problem_title(
    problem_dir: Path, meta: dict[str, Any], ini: dict[str, str]
) -> str:
    if ini.get("name"):
        return ini["name"]
    return _problem_title(problem_dir, meta)


def _copy_domjudge_pdf_statements(domjudge_dir: Path, hydro_dir: Path) -> int:
    statement_dir = domjudge_dir / "problem_statement"
    pdfs = sorted(
        path
        for path in statement_dir.rglob("*")
        if path.is_file() and path.suffix.lower() == ".pdf"
    )
    if not pdfs:
        return 0

    by_language: dict[str, list[str]] = {}
    used_names: set[str] = set()
    for idx, pdf in enumerate(pdfs, start=1):
        language = _statement_language(pdf)
        target_name = _unique_safe_filename(
            pdf.name, used_names, fallback=f"statement-{idx}.pdf"
        )
        _copy_file(pdf, hydro_dir / "additional_file" / target_name)
        by_language.setdefault(language, []).append(target_name)

    for language, filenames in by_language.items():
        content = (
            "\n\n".join(f"@[pdf](file://{filename})" for filename in filenames) + "\n"
        )
        (hydro_dir / f"problem_{language}.md").write_text(content, encoding="utf-8")
    return len(pdfs)


def _statement_language(path: Path) -> str:
    stem = path.stem
    match = re.search(
        r"(?:^|[._-])([a-zA-Z]{2,3}(?:[_-][a-zA-Z]{2,4})?)(?:$|[._-])", stem
    )
    if match is None:
        return "en"
    language = match.group(1).replace("-", "_")
    parts = language.split("_", 1)
    return parts[0].lower() + (f"_{parts[1].upper()}" if len(parts) == 2 else "")


def _unique_safe_filename(name: str, used: set[str], *, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(name).name).strip(".-") or fallback
    stem = Path(cleaned).stem
    suffix = Path(cleaned).suffix
    candidate = cleaned
    index = 2
    while candidate.lower() in used:
        candidate = f"{stem}-{index}{suffix}"
        index += 1
    used.add(candidate.lower())
    return candidate


def _copy_hydro_cases(cases: list[CaseFile], target_dir: Path) -> list[tuple[str, str]]:
    copied: list[tuple[str, str]] = []
    for idx, case in enumerate(cases, start=1):
        input_name = f"{idx}.in"
        output_name = f"{idx}.ans"
        _copy_file(case.input_path, target_dir / input_name)
        _copy_file(case.output_path, target_dir / output_name)
        copied.append((input_name, output_name))
    return copied


def _copy_domjudge_samples(cases: list[CaseFile], target_dir: Path) -> None:
    for idx, case in enumerate(cases, start=1):
        _copy_file(case.input_path, target_dir / f"{idx}.in")
        _copy_file(case.output_path, target_dir / f"{idx}.ans")


def _copy_domjudge_checker(domjudge_dir: Path, testdata_dir: Path) -> str | None:
    roots = [domjudge_dir / "output_validators", domjudge_dir / "output_validator"]
    candidates = sorted(
        path
        for root in roots
        if root.exists()
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in SOURCE_SUFFIXES
    )
    source = next(
        (path for path in candidates if _looks_like_testlib_checker(path)), None
    )
    if source is None:
        return None

    target_name = f"checker{source.suffix.lower()}"
    _copy_file(source, testdata_dir / target_name)
    _copy_cpp_headers(source.parent, testdata_dir)
    return target_name


def _looks_like_testlib_checker(path: Path) -> bool:
    if (path.parent / "testlib.h").is_file():
        return True
    try:
        return "testlib.h" in path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def _copy_domjudge_sources(domjudge_dir: Path, target_dir: Path) -> int:
    count = 0
    for directory in (
        "submissions",
        "input_validators",
        "input_validator",
        "output_validators",
        "output_validator",
        "generators",
    ):
        source_root = domjudge_dir / directory
        if not source_root.exists():
            continue
        for source in sorted(path for path in source_root.rglob("*") if path.is_file()):
            _copy_file(source, target_dir / directory / source.relative_to(source_root))
            count += 1
    return count


def _domjudge_time_seconds(meta: dict[str, Any], ini: dict[str, str]) -> int | float:
    if ini.get("timelimit"):
        return _parse_seconds(ini["timelimit"], default=1)
    limits = meta.get("limits") if isinstance(meta.get("limits"), dict) else {}
    value = _first_existing(limits, "time_limit", "timeout", "time")
    return _parse_seconds(value, default=1)


def _parse_seconds(value: Any, *, default: int) -> int | float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        text = str(value).strip().lower()
        match = re.fullmatch(
            r"([0-9]+(?:\.[0-9]+)?)\s*(ms|s|sec|second|seconds)?", text
        )
        if not match:
            return default
        seconds = float(match.group(1))
        if match.group(2) == "ms":
            seconds /= 1000
    seconds = max(0.001, seconds)
    return int(seconds) if seconds.is_integer() else round(seconds, 3)


def _domjudge_memory_mb(meta: dict[str, Any]) -> int:
    limits = meta.get("limits") if isinstance(meta.get("limits"), dict) else {}
    value = _first_existing(limits, "memory", "memory_limit")
    return _parse_memory_mb(value, default=1024)


def _parse_pid_start(pid_start: str) -> tuple[str, int, int]:
    match = re.fullmatch(r"([A-Za-z]+)([0-9]+)", pid_start)
    if match is None:
        raise ValueError("pid-start must look like P1000")
    return match.group(1), int(match.group(2)), len(match.group(2))


def convert_hydro_to_domjudge(
    source_zip: Path,
    output_dir: Path,
    *,
    code_start: str = "A",
    color: str = "#000000",
    only: Iterable[str] = (),
    verbose: bool = False,
) -> int:
    start_index = _code_to_index(code_start)
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="hydro-to-domjudge-") as td:
        root = Path(td)
        extracted = root / "input"
        _safe_extract_zip(source_zip, extracted)
        package_root = _strip_single_root(extracted)
        problems = _filter_problem_dirs(_find_hydro_problems(package_root), only)

        if not problems:
            raise ValueError("no Hydro problem package found")

        print(
            f"start: target=hydro_to_domjudge total={len(problems)} output={output_dir}"
        )
        for idx, problem_dir in enumerate(problems, start=1):
            code = _index_to_code(start_index + idx - 1)
            meta = _read_yaml(problem_dir / "problem.yaml")
            slug = _problem_slug(problem_dir, meta)
            title = _problem_title(problem_dir, meta)
            work_dir = root / "domjudge" / f"{idx:03d}-{slug}"
            output_zip = output_dir / f"{code}-{slug}.zip"
            if output_zip.exists():
                output_zip.unlink()

            print(f"[{idx}/{len(problems)}] {slug} -> {output_zip.name}")
            _write_domjudge_problem(
                problem_dir, work_dir, meta, title, code, color, verbose=verbose
            )
            _zip_dir(work_dir, output_zip)

    print(f"done: target=hydro_to_domjudge total={len(problems)}")
    return 0


def _write_domjudge_problem(
    hydro_dir: Path,
    target_dir: Path,
    meta: dict[str, Any],
    title: str,
    code: str,
    color: str,
    *,
    verbose: bool,
) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    testdata_config = _read_yaml(hydro_dir / "testdata" / "config.yaml")
    cases = _load_hydro_cases(hydro_dir, testdata_config)
    if not cases:
        raise ValueError(f"{hydro_dir.name}: no paired testdata files found")

    source_stats = _copy_hydro_sources(hydro_dir, target_dir, testdata_config)
    time_seconds = _hydro_time_seconds(meta, testdata_config)
    memory_mb = _hydro_memory_mb(meta, testdata_config)
    interactive = _is_hydro_interactive(testdata_config)
    if interactive and not source_stats["interactors"]:
        raise ValueError(
            f"{hydro_dir.name}: interactive problem does not provide an interactor source"
        )
    validation = (
        "custom interactive"
        if source_stats["interactors"]
        else "custom"
        if source_stats["checkers"]
        else "default"
    )
    domjudge_meta: dict[str, Any] = {
        "name": title,
        "source": "converted from HydroOJ",
        "validation": validation,
        "limits": {
            "memory": memory_mb,
        },
    }
    (target_dir / "problem.yaml").write_text(
        _dump_yaml(domjudge_meta), encoding="utf-8"
    )
    _write_domjudge_ini(
        target_dir / "domjudge-problem.ini", title, code, color, time_seconds
    )

    used_inputs = {case.input_path.resolve() for case in cases}
    extra_samples = _load_hydro_sample_cases(hydro_dir, used_inputs)
    sample_cases = extra_samples + [case for case in cases if case.sample]
    secret_cases = [case for case in cases if not case.sample]

    _copy_domjudge_cases(sample_cases, target_dir / "data" / "sample")
    _copy_domjudge_cases(secret_cases, target_dir / "data" / "secret")
    _copy_hydro_statements(hydro_dir, target_dir)
    _copy_hydro_attachments(hydro_dir, target_dir / "attachments")
    if not source_stats["validators"]:
        validator = target_dir / "input_validators" / "validator.py"
        validator.parent.mkdir(parents=True, exist_ok=True)
        allowed_hashes = sorted(
            {_sha256_file(case.input_path) for case in [*sample_cases, *secret_cases]}
        )
        validator.write_text(
            "#!/usr/bin/env python3\n"
            "import hashlib\n"
            "import sys\n"
            f"ALLOWED = {allowed_hashes!r}\n"
            "digest = hashlib.sha256(sys.stdin.buffer.read()).hexdigest()\n"
            "raise SystemExit(42 if digest in ALLOWED else 43)\n",
            encoding="utf-8",
        )

    if verbose:
        print(
            "  cases: "
            f"sample={len(sample_cases)} secret={len(secret_cases)} "
            f"submissions={source_stats['submissions']} "
            f"checkers={source_stats['checkers']} "
            f"interactors={source_stats['interactors']} "
            f"validators={source_stats['validators']} "
            f"generators={source_stats['generators']}"
        )


def _copy_hydro_attachments(hydro_dir: Path, target_dir: Path) -> None:
    candidates: list[Path] = []
    explicit = hydro_dir / "attachments"
    if explicit.exists():
        candidates.extend(
            path for path in sorted(explicit.rglob("*")) if path.is_file()
        )
    additional = hydro_dir / "additional_file" / "attachments"
    if additional.exists():
        candidates.extend(
            path for path in sorted(additional.rglob("*")) if path.is_file()
        )
    used: set[str] = set()
    for index, source in enumerate(candidates, start=1):
        name = _unique_safe_filename(source.name, used, fallback=f"attachment-{index}")
        _copy_file(source, target_dir / name)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_domjudge_cases(cases: list[CaseFile], data_dir: Path) -> None:
    used_bases: set[str] = set()
    for idx, case in enumerate(cases, start=1):
        base = _safe_name(case.name) or f"{idx:03d}"
        if base[0].isdigit():
            base = f"{idx:03d}"
        original_base = base
        suffix = 2
        while base.casefold() in used_bases:
            base = f"{original_base}-{suffix}"
            suffix += 1
        used_bases.add(base.casefold())
        _copy_file(case.input_path, data_dir / f"{base}.in")
        _copy_file(case.output_path, data_dir / f"{base}.ans")


def _copy_hydro_sources(
    hydro_dir: Path, target_dir: Path, config: dict[str, Any]
) -> dict[str, int]:
    testdata_dir = hydro_dir / "testdata"
    stats = {
        "submissions": 0,
        "checkers": 0,
        "interactors": 0,
        "validators": 0,
        "generators": 0,
        "attachments": 0,
    }
    handled: set[Path] = set()

    explicit_interactor = _config_source_path(testdata_dir, config, "interactor")
    if explicit_interactor is not None and explicit_interactor.exists():
        _copy_source_file(
            explicit_interactor, target_dir / "output_validators" / "interactor"
        )
        handled.add(explicit_interactor.resolve())
        stats["interactors"] += 1

    explicit_checker = _config_source_path(testdata_dir, config, "checker", "spj")
    if explicit_checker is not None and explicit_checker.exists():
        _copy_source_file(
            explicit_checker, target_dir / "output_validators" / "checker"
        )
        handled.add(explicit_checker.resolve())
        stats["checkers"] += 1

    explicit_validator = _config_source_path(
        testdata_dir, config, "validator", "input_validator", "inputValidator"
    )
    if explicit_validator is not None and explicit_validator.exists():
        _copy_source_file(
            explicit_validator, target_dir / "input_validators" / "validator"
        )
        handled.add(explicit_validator.resolve())
        stats["validators"] += 1

    if not testdata_dir.exists():
        return stats

    for source in sorted(testdata_dir.rglob("*")):
        if not source.is_file() or source.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        resolved = source.resolve()
        if resolved in handled:
            continue

        category = _classify_source(source)
        if category == "interactor":
            _copy_source_file(source, target_dir / "output_validators" / "interactor")
            stats["interactors"] += 1
        elif category == "checker":
            _copy_source_file(source, target_dir / "output_validators" / "checker")
            stats["checkers"] += 1
        elif category == "validator":
            _copy_source_file(source, target_dir / "input_validators" / "validator")
            stats["validators"] += 1
        elif category == "generator":
            _copy_source_file(source, target_dir / "generators")
            stats["generators"] += 1
        elif category == "submission":
            _copy_source_file(source, target_dir / "submissions" / "accepted")
            stats["submissions"] += 1
        else:
            _copy_file(
                source,
                target_dir
                / "attachments"
                / "sources"
                / source.relative_to(testdata_dir),
            )
            stats["attachments"] += 1

    return stats


def _config_source_path(
    testdata_dir: Path, config: dict[str, Any], *keys: str
) -> Path | None:
    value = _first_existing(config, *keys)
    if isinstance(value, dict):
        source_name = _first_string(value, "file", "path", "source")
    elif isinstance(value, str):
        source_name = value
    else:
        source_name = None

    if not source_name:
        return None
    return _safe_join(testdata_dir, source_name)


def _classify_source(path: Path) -> str:
    stem = path.stem.lower()
    if stem in {"interactor", "interaction"} or stem.startswith(
        ("interactor_", "interaction_")
    ):
        return "interactor"
    if stem in {"check", "checker", "spj"} or stem.startswith(
        ("check_", "checker_", "spj_")
    ):
        return "checker"
    if stem in {
        "val",
        "validator",
        "input_validator",
        "inputvalidator",
    } or stem.startswith(("val_", "validator_")):
        return "validator"
    if stem in {"gen", "generator"} or stem.startswith(("gen", "generator")):
        return "generator"
    if stem in {
        "std",
        "standard",
        "solution",
        "sol",
        "main",
        "accepted",
        "ac",
    } or stem.startswith(("std", "accepted_", "solution_", "sol_")):
        return "submission"
    return "attachment"


def _copy_source_file(src: Path, dst_dir: Path) -> None:
    _copy_file(src, dst_dir / src.name)
    if src.suffix.lower() in {".cc", ".cpp", ".cxx"}:
        _copy_cpp_headers(src.parent, dst_dir)
        if dst_dir.parts[-2:] in [
            ("output_validators", "checker"),
            ("input_validators", "validator"),
        ]:
            _copy_packaged_testlib(dst_dir, overwrite=True)
        elif dst_dir.parts[-2:] == ("output_validators", "interactor"):
            _copy_packaged_testlib(dst_dir, overwrite=True)


def _copy_cpp_headers(src_dir: Path, dst_dir: Path) -> None:
    for header in sorted(src_dir.iterdir() if src_dir.exists() else []):
        if header.is_file() and header.suffix.lower() in HEADER_SUFFIXES:
            _copy_file(header, dst_dir / header.name)


def _copy_packaged_testlib(dst_dir: Path, *, overwrite: bool = False) -> None:
    try:
        import p2d  # type: ignore[import-not-found]
    except Exception:
        return

    testlib_path = Path(p2d.__file__).parent / "testlib" / "testlib.h"
    if testlib_path.exists() and (overwrite or not (dst_dir / "testlib.h").exists()):
        _copy_file(testlib_path, dst_dir / "testlib.h")


def _is_hydro_interactive(config: dict[str, Any]) -> bool:
    problem_type = str(config.get("type", "")).strip().lower()
    return (
        "interactive" in problem_type
        or _first_existing(config, "interactor") is not None
    )


def _safe_extract_zip(
    zip_path: Path,
    target_dir: Path,
    *,
    budget: ArchiveExtractionBudget | None = None,
) -> None:
    extraction_budget = budget or ArchiveExtractionBudget.from_env()
    target_dir.mkdir(parents=True, exist_ok=True)
    root = target_dir.resolve()
    try:
        with zipfile.ZipFile(zip_path) as archive:
            members: list[tuple[zipfile.ZipInfo, Path]] = []
            seen_members: set[str] = set()
            for info in archive.infolist():
                name = info.filename.replace("\\", "/")
                if not name:
                    continue
                member_path = Path(name)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise ValueError(f"unsafe zip member path: {info.filename}")
                if _is_zip_symlink(info):
                    raise ValueError(f"zip symlinks are not supported: {info.filename}")
                member_key = member_path.as_posix().rstrip("/").casefold()
                if member_key in seen_members:
                    raise ValueError(f"duplicate zip member path: {info.filename}")
                seen_members.add(member_key)
                output_path = target_dir / member_path
                if not _is_relative_to(output_path.resolve(), root):
                    raise ValueError(f"unsafe zip member path: {info.filename}")
                extraction_budget.reserve(info)
                if name.endswith("/"):
                    continue
                members.append((info, output_path))

            for info, output_path in members:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with archive.open(info) as src, output_path.open("wb") as dst:
                    while chunk := src.read(1024 * 1024):
                        written += len(chunk)
                        if (
                            written > info.file_size
                            or written > extraction_budget.max_member_bytes
                        ):
                            raise ValueError(
                                f"zip member expanded beyond declared size: {info.filename}"
                            )
                        dst.write(chunk)
    except Exception:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise


def validate_zip_archive(
    zip_path: Path,
    *,
    budget: ArchiveExtractionBudget | None = None,
) -> None:
    archive_budget = budget or ArchiveExtractionBudget.from_env()
    with zipfile.ZipFile(zip_path) as archive:
        seen_members: set[str] = set()
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if not name:
                continue
            member_path = Path(name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError(f"unsafe zip member path: {info.filename}")
            if _is_zip_symlink(info):
                raise ValueError(f"zip symlinks are not supported: {info.filename}")
            member_key = member_path.as_posix().rstrip("/").casefold()
            if member_key in seen_members:
                raise ValueError(f"duplicate zip member path: {info.filename}")
            seen_members.add(member_key)
            archive_budget.reserve(info)


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _positive_float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    return ((info.external_attr >> 16) & 0o170000) == 0o120000


def _strip_single_root(path: Path) -> Path:
    current = path
    while True:
        items = [item for item in current.iterdir() if item.name != "__MACOSX"]
        if len(items) != 1 or not items[0].is_dir():
            return current
        current = items[0]


def _find_hydro_problems(root: Path) -> list[Path]:
    candidates: list[Path] = []
    for marker in sorted(root.rglob("problem.yaml")):
        problem_dir = marker.parent
        if (problem_dir / "testdata").exists() or list(
            problem_dir.glob("problem_*.md")
        ):
            candidates.append(problem_dir)
    return _dedupe_problem_dirs(candidates, root)


def _dedupe_problem_dirs(candidates: list[Path], root: Path) -> list[Path]:
    seen: set[Path] = set()
    result: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(candidate)
    if len(result) > 1 and root in result:
        result = [item for item in result if item != root]
    return result


def _filter_problem_dirs(problems: list[Path], only: Iterable[str]) -> list[Path]:
    wanted = [_safe_name(item) for item in only if item]
    if not wanted:
        return problems

    by_key: dict[str, Path] = {}
    for problem in problems:
        meta = _read_yaml(problem / "problem.yaml")
        keys = {
            _safe_name(problem.name),
            _problem_slug(problem, meta),
            _safe_name(str(meta.get("pid", ""))),
            _safe_name(str(meta.get("id", ""))),
            _safe_name(str(meta.get("slug", ""))),
            _safe_name(str(meta.get("name", ""))),
            _safe_name(str(meta.get("title", ""))),
        }
        for key in keys:
            if key:
                by_key[key] = problem

    missing = [item for item in wanted if item not in by_key]
    if missing:
        raise ValueError(f"unknown problem(s): {', '.join(missing)}")
    return [by_key[item] for item in wanted]


def _load_hydro_cases(problem_dir: Path, config: dict[str, Any]) -> list[CaseFile]:
    testdata_dir = problem_dir / "testdata"
    cases = _hydro_cases_from_config(testdata_dir, config)
    if cases:
        return cases
    return _scan_case_pairs(testdata_dir)


def _hydro_cases_from_config(
    testdata_dir: Path, config: dict[str, Any]
) -> list[CaseFile]:
    result: list[CaseFile] = []
    for case, inherited_score in _iter_hydro_config_cases(config):
        if isinstance(case, str):
            input_name = case
            output_name = _find_matching_output_name(testdata_dir, input_name)
            sample = "sample" in input_name.lower()
            score = inherited_score
        elif isinstance(case, dict):
            input_name = _first_string(case, "input", "in", "inputFile", "stdin")
            output_name = _first_string(
                case, "output", "out", "answer", "answerFile", "stdout"
            )
            if output_name is None and input_name:
                output_name = _find_matching_output_name(testdata_dir, input_name)
            sample = (
                bool(case.get("sample"))
                or str(case.get("type", "")).lower() == "sample"
            )
            score = case.get("score", inherited_score)
        else:
            continue
        if not input_name or not output_name:
            continue
        input_path = _safe_join(testdata_dir, input_name)
        output_path = _safe_join(testdata_dir, output_name)
        if input_path.exists() and output_path.exists():
            name = _safe_name(Path(input_name).stem)
            result.append(
                CaseFile(input_path, output_path, name, sample or score == 0, score)
            )
    return result


def _iter_hydro_config_cases(
    config: dict[str, Any],
) -> Iterable[tuple[Any, int | float | None]]:
    for case in _as_list(config.get("cases")):
        yield case, None
    for subtask in _as_list(config.get("subtasks")):
        if not isinstance(subtask, dict):
            continue
        score = subtask.get("score")
        for case in _as_list(subtask.get("cases")):
            yield case, score
    for group in _as_list(config.get("groups")):
        if not isinstance(group, dict):
            continue
        score = group.get("score")
        for case in _as_list(group.get("cases")):
            yield case, score


def _load_hydro_sample_cases(
    problem_dir: Path, used_inputs: set[Path]
) -> list[CaseFile]:
    samples: list[CaseFile] = []
    samples.extend(_scan_case_pairs(problem_dir / "testdata" / "sample", sample=True))
    samples.extend(_scan_case_pairs(problem_dir / "additional_file", sample=True))
    samples.extend(_scan_example_pairs(problem_dir / "additional_file"))

    result: list[CaseFile] = []
    seen: set[Path] = set()
    for case in samples:
        resolved = case.input_path.resolve()
        if resolved in used_inputs or resolved in seen:
            continue
        seen.add(resolved)
        result.append(case)
    return result


def _scan_case_pairs(root: Path, *, sample: bool | None = None) -> list[CaseFile]:
    if not root.exists():
        return []
    cases: list[CaseFile] = []
    for input_path in sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in IN_SUFFIXES
    ):
        output_path = _find_matching_output_path(input_path)
        if output_path is None:
            continue
        rel = input_path.relative_to(root)
        case_sample = (
            sample
            if sample is not None
            else any("sample" in part.lower() for part in rel.parts)
        )
        cases.append(
            CaseFile(input_path, output_path, _safe_name(input_path.stem), case_sample)
        )
    return cases


def _scan_example_pairs(root: Path) -> list[CaseFile]:
    if not root.exists():
        return []
    cases: list[CaseFile] = []
    for input_path in sorted(path for path in root.rglob("*") if path.is_file()):
        lower_name = input_path.name.lower()
        if not lower_name.startswith(("example", "sample")) or lower_name.endswith(
            ".a"
        ):
            continue
        output_path = input_path.with_name(input_path.name + ".a")
        if output_path.exists():
            cases.append(
                CaseFile(input_path, output_path, _safe_name(input_path.name), True)
            )
    return cases


def _find_matching_output_path(input_path: Path) -> Path | None:
    for suffix in OUT_SUFFIXES:
        candidate = input_path.with_suffix(suffix)
        if candidate.exists():
            return candidate
    for suffix in OUT_SUFFIXES:
        candidate = input_path.parent / f"{input_path.stem}{suffix}"
        if candidate.exists():
            return candidate
    return None


def _find_matching_output_name(testdata_dir: Path, input_name: str) -> str:
    input_path = _safe_join(testdata_dir, input_name)
    output_path = _find_matching_output_path(input_path)
    if output_path is not None:
        return output_path.relative_to(testdata_dir).as_posix()
    return Path(input_name).with_suffix(".ans").as_posix()


def _copy_hydro_statements(hydro_dir: Path, target_dir: Path) -> None:
    statement_dir = target_dir / "problem_statement"
    statements = sorted(hydro_dir.glob("problem_*.md"))
    for statement in statements:
        pdf = _hydro_statement_pdf(hydro_dir, statement)
        if pdf is not None:
            _copy_file(pdf, statement_dir / "problem.pdf")
            return

    title = _problem_title(hydro_dir, _read_yaml(hydro_dir / "problem.yaml"))
    _write_domjudge_html_statement(statement_dir / "problem.html", title, statements)


def _hydro_statement_pdf(hydro_dir: Path, statement: Path) -> Path | None:
    match = re.fullmatch(
        r"\s*@\[pdf\]\(([^)]+)\)\s*",
        statement.read_text(encoding="utf-8", errors="replace"),
    )
    if match is None:
        return None
    source = unquote(match.group(1).strip())
    if source.startswith("file://"):
        source = source.removeprefix("file://")
    if "://" in source:
        return None
    candidate = _safe_join(hydro_dir / "additional_file", source)
    return (
        candidate
        if candidate.is_file() and candidate.suffix.lower() == ".pdf"
        else None
    )


def _write_domjudge_html_statement(
    path: Path, title: str, statements: list[Path]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sections: list[str] = []
    for statement in statements:
        language = statement.stem.removeprefix("problem_").replace("_", "-") or "und"
        content = statement.read_text(encoding="utf-8", errors="replace")
        sections.append(
            f'<section lang="{escape(language, quote=True)}">'
            f"<h2>{escape(language)}</h2><pre>{escape(content)}</pre></section>"
        )
    if not sections:
        sections.append("<p>Converted package did not include a statement.</p>")
    document = (
        '<!doctype html>\n<html><head><meta charset="utf-8">'
        f"<title>{escape(title)}</title>"
        "<style>body{font-family:sans-serif;max-width:60rem;margin:auto;padding:2rem}"
        "pre{white-space:pre-wrap;font:inherit}section+section{margin-top:3rem}</style>"
        f"</head><body><h1>{escape(title)}</h1>{''.join(sections)}</body></html>\n"
    )
    path.write_text(document, encoding="utf-8")


def _write_domjudge_ini(
    path: Path, title: str, code: str, color: str, time_seconds: int | float
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(
        [
            f"short-name = {code}",
            f"name = {title}",
            f"timelimit = {_format_number(time_seconds)}",
            f"color = {color}",
            f"externalid = {code}",
        ]
    )
    path.write_text(content + "\n", encoding="utf-8")


def _copy_optional_tree(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    if src.is_file():
        _copy_file(src, dst / src.name)
        return
    for item in sorted(src.rglob("*")):
        if item.is_file():
            _copy_file(item, dst / item.relative_to(src))


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with src.open("rb") as source, dst.open("wb") as destination:
        while chunk := source.read(1024 * 1024):
            destination.write(chunk)
    shutil.copystat(src, dst, follow_symlinks=False)


def _zip_dir(src_dir: Path, output_zip: Path) -> None:
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(src_dir.rglob("*")):
            if path.is_file():
                name = path.relative_to(src_dir).as_posix()
                with (
                    path.open("rb") as source,
                    archive.open(name, "w", force_zip64=True) as destination,
                ):
                    while chunk := source.read(1024 * 1024):
                        destination.write(chunk)


def _read_yaml(path: Path) -> dict[str, Any]:
    return load_yaml_file(path)


def _dump_yaml(data: dict[str, Any]) -> str:
    try:
        import yaml  # type: ignore[import-not-found]

        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    except Exception:
        return "\n".join(_emit_yaml_lines(data, 0)) + "\n"


def _emit_yaml_lines(value: Any, indent: int) -> list[str]:
    prefix = " " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                lines.append(f"{prefix}{key}:")
                lines.extend(_emit_yaml_lines(item, indent + 2))
            else:
                lines.append(f"{prefix}{key}: {_format_yaml_scalar(item)}")
        return lines
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, (dict, list)):
                lines.append(f"{prefix}-")
                lines.extend(_emit_yaml_lines(item, indent + 2))
            else:
                lines.append(f"{prefix}- {_format_yaml_scalar(item)}")
        return lines
    return [f"{prefix}{_format_yaml_scalar(value)}"]


def _format_yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _format_number(value)
    return json.dumps(str(value), ensure_ascii=False)


def _problem_slug(problem_dir: Path, meta: dict[str, Any]) -> str:
    candidates = [
        meta.get("pid"),
        meta.get("id"),
        meta.get("slug"),
        meta.get("name"),
        meta.get("title"),
        problem_dir.name,
    ]
    for candidate in candidates:
        slug = _safe_name(str(candidate or ""))
        if slug:
            return slug
    return "problem"


def _problem_title(problem_dir: Path, meta: dict[str, Any]) -> str:
    for candidate in (
        meta.get("title"),
        meta.get("name"),
        meta.get("pid"),
        problem_dir.name,
    ):
        if isinstance(candidate, dict):
            for lang in ("zh", "zh-cn", "en"):
                value = candidate.get(lang)
                if value:
                    return str(value)
        elif candidate:
            return str(candidate)
    return problem_dir.name


def _safe_name(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = value.strip(".-_")
    return value


def _safe_join(root: Path, relative_name: str) -> Path:
    relative = Path(relative_name.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe relative path: {relative_name}")
    target = root / relative
    if not _is_relative_to(target.resolve(), root.resolve()):
        raise ValueError(f"unsafe relative path: {relative_name}")
    return target


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _first_string(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _first_existing(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return None


def _hydro_time_seconds(meta: dict[str, Any], config: dict[str, Any]) -> int | float:
    value = _first_existing(meta, "time", "timeLimit", "time_limit") or _first_existing(
        config, "time", "timeLimit", "time_limit"
    )
    ms = _parse_time_ms(value, default=1000)
    seconds = ms / 1000
    return int(seconds) if seconds.is_integer() else round(seconds, 3)


def _hydro_memory_mb(meta: dict[str, Any], config: dict[str, Any]) -> int:
    value = _first_existing(
        meta, "memory", "memoryLimit", "memory_limit"
    ) or _first_existing(config, "memory", "memoryLimit", "memory_limit")
    return _parse_memory_mb(value, default=1024)


def _parse_time_ms(value: Any, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower()
    match = re.fullmatch(
        r"([0-9]+(?:\.[0-9]+)?)\s*(ms|millisecond|milliseconds|s|sec|second|seconds)?",
        text,
    )
    if not match:
        return default
    number = float(match.group(1))
    unit = match.group(2) or "ms"
    if unit.startswith("s"):
        number *= 1000
    return max(1, int(round(number)))


def _parse_memory_mb(value: Any, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip().lower()
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*(b|kb|kib|mb|mib|m|gb|gib|g)?", text)
    if not match:
        return default
    number = float(match.group(1))
    unit = match.group(2) or "mb"
    if unit == "b":
        number /= 1024 * 1024
    elif unit in {"kb", "kib"}:
        number /= 1024
    elif unit in {"gb", "gib", "g"}:
        number *= 1024
    return max(1, int(round(number)))


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


def _format_number(value: int | float) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
