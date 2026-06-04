from __future__ import annotations

import json
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


IN_SUFFIXES = {".in", ".input"}
OUT_SUFFIXES = {".ans", ".out", ".output"}
SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".java", ".kt", ".py", ".rs", ".go", ".pas"}
HEADER_SUFFIXES = {".h", ".hh", ".hpp", ".hxx"}


@dataclass(frozen=True)
class CaseFile:
    input_path: Path
    output_path: Path
    name: str
    sample: bool = False
    score: int | float | None = None


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

        print(f"start: target=hydro_to_domjudge total={len(problems)} output={output_dir}")
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
            _write_domjudge_problem(problem_dir, work_dir, meta, title, code, color, verbose=verbose)
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
    domjudge_meta: dict[str, Any] = {
        "name": title,
        "source": "converted from HydroOJ",
        "validation": "custom" if source_stats["checkers"] else "default",
        "limits": {
            "time_limit": time_seconds,
            "memory": memory_mb,
        },
    }
    (target_dir / "problem.yaml").write_text(_dump_yaml(domjudge_meta), encoding="utf-8")
    _write_domjudge_ini(target_dir / "domjudge-problem.ini", title, code, color, time_seconds)

    used_inputs = {case.input_path.resolve() for case in cases}
    extra_samples = _load_hydro_sample_cases(hydro_dir, used_inputs)
    sample_cases = extra_samples + [case for case in cases if case.sample]
    secret_cases = [case for case in cases if not case.sample]

    _copy_domjudge_cases(sample_cases, target_dir / "data" / "sample")
    _copy_domjudge_cases(secret_cases, target_dir / "data" / "secret")
    _copy_hydro_statements(hydro_dir, target_dir)
    _copy_optional_tree(hydro_dir / "additional_file", target_dir / "attachments")
    _copy_optional_tree(hydro_dir / "attachments", target_dir / "attachments")

    if verbose:
        print(
            "  cases: "
            f"sample={len(sample_cases)} secret={len(secret_cases)} "
            f"submissions={source_stats['submissions']} "
            f"checkers={source_stats['checkers']} "
            f"validators={source_stats['validators']} "
            f"generators={source_stats['generators']}"
        )


def _copy_domjudge_cases(cases: list[CaseFile], data_dir: Path) -> None:
    for idx, case in enumerate(cases, start=1):
        base = _safe_name(case.name) or f"{idx:03d}"
        if base[0].isdigit():
            base = f"{idx:03d}"
        _copy_file(case.input_path, data_dir / f"{base}.in")
        _copy_file(case.output_path, data_dir / f"{base}.ans")


def _copy_hydro_sources(hydro_dir: Path, target_dir: Path, config: dict[str, Any]) -> dict[str, int]:
    testdata_dir = hydro_dir / "testdata"
    stats = {"submissions": 0, "checkers": 0, "validators": 0, "generators": 0, "attachments": 0}
    handled: set[Path] = set()

    explicit_checker = _config_source_path(testdata_dir, config, "checker", "spj")
    if explicit_checker is not None and explicit_checker.exists():
        _copy_source_file(explicit_checker, target_dir / "output_validators" / "checker")
        handled.add(explicit_checker.resolve())
        stats["checkers"] += 1

    explicit_validator = _config_source_path(testdata_dir, config, "validator", "input_validator", "inputValidator")
    if explicit_validator is not None and explicit_validator.exists():
        _copy_source_file(explicit_validator, target_dir / "input_validators" / "validator")
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
        if category == "checker":
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
            _copy_file(source, target_dir / "attachments" / "sources" / source.relative_to(testdata_dir))
            stats["attachments"] += 1

    return stats


def _config_source_path(testdata_dir: Path, config: dict[str, Any], *keys: str) -> Path | None:
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
    if stem in {"check", "checker", "spj"} or stem.startswith(("check_", "checker_", "spj_")):
        return "checker"
    if stem in {"val", "validator", "input_validator", "inputvalidator"} or stem.startswith(("val_", "validator_")):
        return "validator"
    if stem in {"gen", "generator"} or stem.startswith(("gen", "generator")):
        return "generator"
    if stem in {"std", "standard", "solution", "sol", "main", "accepted", "ac"} or stem.startswith(("std", "accepted_")):
        return "submission"
    return "attachment"


def _copy_source_file(src: Path, dst_dir: Path) -> None:
    _copy_file(src, dst_dir / src.name)
    if src.suffix.lower() in {".cc", ".cpp", ".cxx"}:
        _copy_cpp_headers(src.parent, dst_dir)
        if dst_dir.parts[-2:] in [("output_validators", "checker"), ("input_validators", "validator")]:
            _copy_packaged_testlib(dst_dir)


def _copy_cpp_headers(src_dir: Path, dst_dir: Path) -> None:
    for header in sorted(src_dir.iterdir() if src_dir.exists() else []):
        if header.is_file() and header.suffix.lower() in HEADER_SUFFIXES:
            _copy_file(header, dst_dir / header.name)


def _copy_packaged_testlib(dst_dir: Path) -> None:
    try:
        import p2d  # type: ignore[import-not-found]
    except Exception:
        return

    testlib_path = Path(p2d.__file__).parent / "testlib" / "testlib.h"
    if testlib_path.exists() and not (dst_dir / "testlib.h").exists():
        _copy_file(testlib_path, dst_dir / "testlib.h")


def _safe_extract_zip(zip_path: Path, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    root = target_dir.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if not name or name.endswith("/"):
                continue
            member_path = Path(name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError(f"unsafe zip member path: {info.filename}")
            if _is_zip_symlink(info):
                raise ValueError(f"zip symlinks are not supported: {info.filename}")
            output_path = target_dir / member_path
            if not _is_relative_to(output_path.resolve(), root):
                raise ValueError(f"unsafe zip member path: {info.filename}")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, output_path.open("wb") as dst:
                shutil.copyfileobj(src, dst)


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
        if (problem_dir / "testdata").exists() or list(problem_dir.glob("problem_*.md")):
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


def _hydro_cases_from_config(testdata_dir: Path, config: dict[str, Any]) -> list[CaseFile]:
    result: list[CaseFile] = []
    for case, inherited_score in _iter_hydro_config_cases(config):
        if isinstance(case, str):
            input_name = case
            output_name = _find_matching_output_name(testdata_dir, input_name)
            sample = "sample" in input_name.lower()
            score = inherited_score
        elif isinstance(case, dict):
            input_name = _first_string(case, "input", "in", "inputFile", "stdin")
            output_name = _first_string(case, "output", "out", "answer", "answerFile", "stdout")
            if output_name is None and input_name:
                output_name = _find_matching_output_name(testdata_dir, input_name)
            sample = bool(case.get("sample")) or str(case.get("type", "")).lower() == "sample"
            score = case.get("score", inherited_score)
        else:
            continue
        if not input_name or not output_name:
            continue
        input_path = _safe_join(testdata_dir, input_name)
        output_path = _safe_join(testdata_dir, output_name)
        if input_path.exists() and output_path.exists():
            name = _safe_name(Path(input_name).stem)
            result.append(CaseFile(input_path, output_path, name, sample or score == 0, score))
    return result


def _iter_hydro_config_cases(config: dict[str, Any]) -> Iterable[tuple[Any, int | float | None]]:
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


def _load_hydro_sample_cases(problem_dir: Path, used_inputs: set[Path]) -> list[CaseFile]:
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
    for input_path in sorted(path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in IN_SUFFIXES):
        output_path = _find_matching_output_path(input_path)
        if output_path is None:
            continue
        rel = input_path.relative_to(root)
        case_sample = sample if sample is not None else any("sample" in part.lower() for part in rel.parts)
        cases.append(CaseFile(input_path, output_path, _safe_name(input_path.stem), case_sample))
    return cases


def _scan_example_pairs(root: Path) -> list[CaseFile]:
    if not root.exists():
        return []
    cases: list[CaseFile] = []
    for input_path in sorted(path for path in root.rglob("*") if path.is_file()):
        lower_name = input_path.name.lower()
        if not lower_name.startswith(("example", "sample")) or lower_name.endswith(".a"):
            continue
        output_path = input_path.with_name(input_path.name + ".a")
        if output_path.exists():
            cases.append(CaseFile(input_path, output_path, _safe_name(input_path.name), True))
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
    for statement in sorted(hydro_dir.glob("problem_*.md")):
        lang = statement.stem.removeprefix("problem_").replace("_", "-") or "en"
        _copy_file(statement, statement_dir / f"problem.{lang}.md")
    if not statement_dir.exists():
        title = _problem_title(hydro_dir, _read_yaml(hydro_dir / "problem.yaml"))
        _write_generated_statement(statement_dir / "problem.en.md", title)


def _write_generated_statement(path: Path, title: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {title}\n\nConverted package did not include a Markdown statement.\n", encoding="utf-8")


def _write_domjudge_ini(path: Path, title: str, code: str, color: str, time_seconds: int | float) -> None:
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
    shutil.copy2(src, dst)


def _zip_dir(src_dir: Path, output_zip: Path) -> None:
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(src_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(src_dir).as_posix())


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        import yaml  # type: ignore[import-not-found]

        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return _read_simple_yaml(text)


def _read_simple_yaml(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    current_list_key: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" ") and ":" in line:
            key, raw_value = line.split(":", 1)
            key = key.strip()
            value = raw_value.strip()
            if value:
                result[key] = _parse_scalar(value)
                current_list_key = None
            else:
                result[key] = []
                current_list_key = key
        elif current_list_key and line.strip().startswith("- "):
            result.setdefault(current_list_key, []).append(_parse_scalar(line.strip()[2:].strip()))
    return result


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


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if value[0:1] in {"'", '"'} and value[-1:] == value[0]:
        return value[1:-1]
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


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
    for candidate in (meta.get("title"), meta.get("name"), meta.get("pid"), problem_dir.name):
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
    value = _first_existing(meta, "time", "timeLimit", "time_limit") or _first_existing(config, "time", "timeLimit", "time_limit")
    ms = _parse_time_ms(value, default=1000)
    seconds = ms / 1000
    return int(seconds) if seconds.is_integer() else round(seconds, 3)


def _hydro_memory_mb(meta: dict[str, Any], config: dict[str, Any]) -> int:
    value = _first_existing(meta, "memory", "memoryLimit", "memory_limit") or _first_existing(config, "memory", "memoryLimit", "memory_limit")
    return _parse_memory_mb(value, default=1024)


def _parse_time_ms(value: Any, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower()
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*(ms|millisecond|milliseconds|s|sec|second|seconds)?", text)
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
