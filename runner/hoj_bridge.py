from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import format_bridge as bridge
from package_security import load_json_file, load_json_text, load_json_value_file


HOJ_DEFAULT_LANGUAGES = ["C", "C++", "Java", "Python3", "Golang"]
HOJ_TO_HYDRO_LANG = {
    "c": "c",
    "c with o2": "c.c11o2",
    "c++": "cc",
    "c++ with o2": "cc.cc17o2",
    "java": "java",
    "python2": "py.py2",
    "pypy2": "py.pypy2",
    "python3": "py.py3",
    "pypy3": "py.pypy3",
    "golang": "go",
    "go": "go",
    "c#": "cs",
    "php": "php",
    "javascript node": "nodejs",
    "rust": "rs",
}
HYDRO_TO_HOJ_LANG = {
    "c": "C",
    "cc": "C++",
    "cpp": "C++",
    "java": "Java",
    "py": "Python3",
    "python": "Python3",
    "go": "Golang",
    "cs": "C#",
    "php": "PHP",
    "nodejs": "JavaScript Node",
    "rs": "Rust",
}


@dataclass(frozen=True)
class HojProblem:
    key: str
    json_path: Path
    data_dir: Path
    document: dict[str, Any]

    @property
    def problem(self) -> dict[str, Any]:
        value = self.document.get("problem")
        return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class HojCase:
    input_path: Path
    output_path: Path
    score: int | float | None = None
    group_num: int | None = None


def convert_hoj_to_hydro(
    source_zip: Path,
    output_dir: Path,
    *,
    pid_start: str = "P1000",
    owner: int = 1,
    tags: Iterable[str] = (),
    only: Iterable[str] = (),
    verbose: bool = False,
) -> int:
    pid_prefix, pid_number, pid_width = bridge._parse_pid_start(pid_start)
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="hoj-to-hydro-") as td:
        root = Path(td)
        extracted = root / "input"
        bridge._safe_extract_zip(source_zip, extracted)
        problems = _filter_hoj_problems(_find_hoj_problems(extracted), only)
        if not problems:
            raise ValueError("no HOJ problem export found")

        extra_tags = [str(tag).strip() for tag in tags if str(tag).strip()]
        print(f"start: target=hoj_to_hydro total={len(problems)} output={output_dir}")
        for idx, hoj_problem in enumerate(problems, start=1):
            pid = f"{pid_prefix}{pid_number + idx - 1:0{pid_width}d}"
            slug = _hoj_slug(hoj_problem)
            work_root = root / "hydro" / f"{idx:03d}-{slug}"
            hydro_dir = work_root / pid
            output_zip = output_dir / f"{pid}-{slug}.zip"
            if output_zip.exists():
                output_zip.unlink()

            print(f"[{idx}/{len(problems)}] {slug} -> {output_zip.name}")
            _write_hoj_as_hydro(
                hoj_problem, hydro_dir, pid, owner, extra_tags, verbose=verbose
            )
            bridge._zip_dir(work_root, output_zip)

    print(f"done: target=hoj_to_hydro total={len(problems)}")
    return 0


def convert_hydro_to_hoj(
    source_zip: Path,
    output_dir: Path,
    *,
    only: Iterable[str] = (),
    verbose: bool = False,
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hydro-to-hoj-") as td:
        root = Path(td)
        extracted = root / "input"
        bridge._safe_extract_zip(source_zip, extracted)
        package_root = bridge._strip_single_root(extracted)
        problems = bridge._filter_problem_dirs(
            bridge._find_hydro_problems(package_root), only
        )
        if not problems:
            raise ValueError("no Hydro problem package found")

        print(f"start: target=hydro_to_hoj total={len(problems)} output={output_dir}")
        used_keys: set[str] = set()
        for idx, hydro_dir in enumerate(problems, start=1):
            meta = bridge._read_yaml(hydro_dir / "problem.yaml")
            slug = bridge._problem_slug(hydro_dir, meta)
            key = _unique_hoj_key(f"problem_{slug or idx}", used_keys)
            print(f"[{idx}/{len(problems)}] {slug} -> {key}.json")
            _write_hydro_as_hoj(hydro_dir, output_dir, key, meta, verbose=verbose)

    print(f"done: target=hydro_to_hoj total={len(problems)}")
    return 0


def convert_hoj_to_domjudge(
    source_zip: Path,
    output_dir: Path,
    *,
    code_start: str = "A",
    color: str = "#000000",
    only: Iterable[str] = (),
    verbose: bool = False,
) -> int:
    start_index = bridge._code_to_index(code_start)
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="hoj-to-domjudge-") as td:
        root = Path(td)
        extracted = root / "input"
        bridge._safe_extract_zip(source_zip, extracted)
        problems = _filter_hoj_problems(_find_hoj_problems(extracted), only)
        if not problems:
            raise ValueError("no HOJ problem export found")

        print(
            f"start: target=hoj_to_domjudge total={len(problems)} output={output_dir}"
        )
        for idx, hoj_problem in enumerate(problems, start=1):
            code = bridge._index_to_code(start_index + idx - 1)
            slug = _hoj_slug(hoj_problem)
            staging_dir = root / "hydro" / f"{idx:03d}-{slug}"
            _write_hoj_as_hydro(
                hoj_problem, staging_dir, f"H{idx}", 1, [], verbose=False
            )
            meta = bridge._read_yaml(staging_dir / "problem.yaml")
            title = bridge._problem_title(staging_dir, meta)
            domjudge_dir = root / "domjudge" / f"{idx:03d}-{slug}"
            output_zip = output_dir / f"{code}-{slug}.zip"
            if output_zip.exists():
                output_zip.unlink()

            print(f"[{idx}/{len(problems)}] {slug} -> {output_zip.name}")
            bridge._write_domjudge_problem(
                staging_dir,
                domjudge_dir,
                meta,
                title,
                code,
                color,
                verbose=verbose,
            )
            bridge._zip_dir(domjudge_dir, output_zip)

    print(f"done: target=hoj_to_domjudge total={len(problems)}")
    return 0


def _find_hoj_problems(root: Path) -> list[HojProblem]:
    result: list[HojProblem] = []
    for json_path in sorted(root.rglob("*.json")):
        data_dir = json_path.with_suffix("")
        if not data_dir.is_dir():
            continue
        value = load_json_file(json_path)
        if not isinstance(value, dict) or not isinstance(value.get("problem"), dict):
            continue
        result.append(HojProblem(json_path.stem, json_path, data_dir, value))
    return result


def _filter_hoj_problems(
    problems: list[HojProblem], only: Iterable[str]
) -> list[HojProblem]:
    wanted = [bridge._safe_name(item) for item in only if item]
    if not wanted:
        return problems

    by_key: dict[str, HojProblem] = {}
    for item in problems:
        pdoc = item.problem
        keys = {
            bridge._safe_name(item.key),
            _hoj_slug(item),
            bridge._safe_name(str(pdoc.get("problemId", ""))),
            bridge._safe_name(str(pdoc.get("display_id", ""))),
            bridge._safe_name(str(pdoc.get("title", ""))),
        }
        for key in keys:
            if key:
                by_key[key] = item
    missing = [item for item in wanted if item not in by_key]
    if missing:
        raise ValueError(f"unknown problem(s): {', '.join(missing)}")
    return [by_key[item] for item in wanted]


def _hoj_slug(item: HojProblem) -> str:
    pdoc = item.problem
    for value in (
        pdoc.get("problemId"),
        pdoc.get("display_id"),
        item.key,
        pdoc.get("title"),
    ):
        slug = bridge._safe_name(str(value or ""))
        if slug:
            return slug
    return "problem"


def _write_hoj_as_hydro(
    item: HojProblem,
    hydro_dir: Path,
    pid: str,
    owner: int,
    extra_tags: list[str],
    *,
    verbose: bool,
) -> None:
    pdoc = item.problem
    title = str(pdoc.get("title") or pdoc.get("problemId") or item.key)
    tags = _string_list(item.document.get("tags"))
    for tag in extra_tags:
        if tag not in tags:
            tags.append(tag)

    hydro_dir.mkdir(parents=True, exist_ok=True)
    (hydro_dir / "problem.yaml").write_text(
        bridge._dump_yaml({"title": title, "tag": tags, "pid": pid, "owner": owner}),
        encoding="utf-8",
    )
    statement, statement_samples = _build_hydro_statement(pdoc)
    (hydro_dir / "problem_zh.md").write_text(statement, encoding="utf-8")

    additional_dir = hydro_dir / "additional_file"
    for idx, (sample_input, sample_output) in enumerate(statement_samples, start=1):
        sample_dir = additional_dir / "samples"
        sample_dir.mkdir(parents=True, exist_ok=True)
        (sample_dir / f"{idx}.in").write_text(sample_input, encoding="utf-8")
        (sample_dir / f"{idx}.out").write_text(sample_output, encoding="utf-8")

    testdata_dir = hydro_dir / "testdata"
    cases = _load_hoj_cases(item)
    copied_cases = _copy_hoj_cases(cases, testdata_dir)
    if not copied_cases:
        raise ValueError(f"{item.key}: no paired testdata files found")

    judge_mode = str(
        item.document.get("judgeMode") or pdoc.get("judgeMode") or "default"
    ).lower()
    config: dict[str, Any] = {
        "type": "interactive" if judge_mode == "interactive" else "default",
        "time": f"{_positive_int(pdoc.get('timeLimit'), 1000)}ms",
        "memory": f"{_positive_int(pdoc.get('memoryLimit'), 256)}m",
    }
    if pdoc.get("isFileIO") and pdoc.get("ioReadFileName"):
        config["filename"] = Path(str(pdoc["ioReadFileName"])).stem

    languages = _hoj_languages_to_hydro(item.document.get("languages"))
    if languages:
        config["langs"] = languages

    code = str(pdoc.get("spjCode") or "")
    if judge_mode in {"spj", "interactive"} and code:
        suffix = (
            ".cc"
            if str(pdoc.get("spjLanguage", "")).lower() in {"c++", "cpp", "cc"}
            else ".c"
        )
        program_name = (
            "interactor" if judge_mode == "interactive" else "checker"
        ) + suffix
        testdata_dir.mkdir(parents=True, exist_ok=True)
        (testdata_dir / program_name).write_text(code, encoding="utf-8")
        if judge_mode == "interactive":
            config["interactor"] = program_name
        else:
            config["checker_type"] = "testlib"
            config["checker"] = program_name

    user_files = _hoj_extra_files(item, "userExtraFile")
    judge_files = _hoj_extra_files(item, "judgeExtraFile")
    copied_user = _copy_text_extra_files(user_files, testdata_dir, prefix="user")
    copied_judge = _copy_text_extra_files(judge_files, testdata_dir, prefix="judge")
    if copied_user:
        config["user_extra_files"] = copied_user
    if copied_judge:
        config["judge_extra_files"] = copied_judge

    _apply_hoj_case_config(
        config, copied_cases, str(pdoc.get("judgeCaseMode") or "default")
    )
    (testdata_dir / "config.yaml").write_text(
        bridge._dump_yaml(config), encoding="utf-8"
    )

    code_templates = item.document.get("codeTemplates")
    if isinstance(code_templates, list) and code_templates:
        (additional_dir / "code_templates.json").parent.mkdir(
            parents=True, exist_ok=True
        )
        (additional_dir / "code_templates.json").write_text(
            json.dumps(code_templates, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    if verbose:
        print(
            "  files: "
            f"cases={len(copied_cases)} statement_samples={len(statement_samples)} "
            f"judge_mode={judge_mode} user_extra={len(copied_user)} judge_extra={len(copied_judge)}"
        )


def _load_hoj_cases(item: HojProblem) -> list[HojCase]:
    result: list[HojCase] = []
    samples = item.document.get("samples")
    if isinstance(samples, list):
        for case in samples:
            if not isinstance(case, dict):
                continue
            input_name = case.get("input")
            output_name = case.get("output")
            if not isinstance(input_name, str) or not isinstance(output_name, str):
                continue
            input_path = bridge._safe_join(item.data_dir, input_name)
            output_path = bridge._safe_join(item.data_dir, output_name)
            if input_path.is_file() and output_path.is_file():
                result.append(
                    HojCase(
                        input_path,
                        output_path,
                        _number_or_none(case.get("score")),
                        _int_or_none(case.get("groupNum")),
                    )
                )
    if result:
        return result
    return [
        HojCase(case.input_path, case.output_path)
        for case in bridge._scan_case_pairs(item.data_dir)
    ]


def _copy_hoj_cases(cases: list[HojCase], testdata_dir: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for idx, case in enumerate(cases, start=1):
        input_name = f"{idx}.in"
        output_name = f"{idx}.out"
        bridge._copy_file(case.input_path, testdata_dir / input_name)
        bridge._copy_file(case.output_path, testdata_dir / output_name)
        result.append(
            {
                "input": input_name,
                "output": output_name,
                "score": case.score,
                "group_num": case.group_num,
            }
        )
    return result


def _apply_hoj_case_config(
    config: dict[str, Any], cases: list[dict[str, Any]], judge_case_mode: str
) -> None:
    grouped = judge_case_mode in {"subtask_lowest", "subtask_average"} and any(
        case.get("group_num") is not None for case in cases
    )
    if grouped:
        group_map: dict[int, list[dict[str, Any]]] = {}
        for idx, case in enumerate(cases, start=1):
            group_num = case.get("group_num")
            group_map.setdefault(
                int(group_num) if group_num is not None else idx, []
            ).append(case)
        subtasks = []
        for group_cases in group_map.values():
            scores = [
                case["score"] for case in group_cases if case.get("score") is not None
            ]
            subtask: dict[str, Any] = {
                "type": "min",
                "cases": [
                    {"input": case["input"], "output": case["output"]}
                    for case in group_cases
                ],
            }
            if scores:
                subtask["score"] = min(scores)
            subtasks.append(subtask)
        config["subtasks"] = subtasks
        return

    if any(case.get("score") is not None for case in cases):
        config["subtasks"] = [
            {
                **({"score": case["score"]} if case.get("score") is not None else {}),
                "cases": [{"input": case["input"], "output": case["output"]}],
            }
            for case in cases
        ]
        return
    config["cases"] = [
        {"input": case["input"], "output": case["output"]} for case in cases
    ]


def _build_hydro_statement(pdoc: dict[str, Any]) -> tuple[str, list[tuple[str, str]]]:
    parts: list[str] = []
    description = str(pdoc.get("description") or "").strip()
    if description:
        parts.append(description)
    input_text = str(pdoc.get("input") or "").strip()
    if input_text:
        parts.append(f"## 输入格式\n\n{input_text}")
    output_text = str(pdoc.get("output") or "").strip()
    if output_text:
        parts.append(f"## 输出格式\n\n{output_text}")

    samples = _parse_hoj_examples(str(pdoc.get("examples") or ""))
    if samples:
        lines = ["## 样例"]
        for idx, (sample_input, sample_output) in enumerate(samples, start=1):
            lines.extend(
                [
                    "",
                    f"```input{idx}",
                    sample_input.rstrip("\n"),
                    "```",
                    "",
                    f"```output{idx}",
                    sample_output.rstrip("\n"),
                    "```",
                ]
            )
        parts.append("\n".join(lines))

    hint = str(pdoc.get("hint") or "").strip()
    if hint:
        parts.append(f"## 提示\n\n{hint}")
    source = str(pdoc.get("source") or "").strip()
    if source:
        parts.append(f"## 来源\n\n{source}")
    if not parts:
        parts.append("Converted HOJ package did not include a statement.")
    return "\n\n".join(parts).rstrip() + "\n", samples


def _parse_hoj_examples(value: str) -> list[tuple[str, str]]:
    pattern = re.compile(
        r"<input>([\s\S]*?)</input>\s*<output>([\s\S]*?)</output>", re.I
    )
    return [(match.group(1), match.group(2)) for match in pattern.finditer(value)]


def _hoj_extra_files(item: HojProblem, key: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for container in (item.document, item.problem):
        value = container.get(key)
        if isinstance(value, str):
            value = load_json_text(value, source=f"embedded HOJ {key}")
        if isinstance(value, dict):
            for name, content in value.items():
                if isinstance(name, str) and isinstance(content, str):
                    result[name] = content
    return result


def _copy_text_extra_files(
    files: dict[str, str], testdata_dir: Path, *, prefix: str
) -> list[str]:
    result: list[str] = []
    for idx, (name, content) in enumerate(sorted(files.items()), start=1):
        relative = Path(name.replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts or not relative.name:
            raise ValueError(f"unsafe HOJ extra file path: {name}")
        target = testdata_dir / relative
        if target.exists():
            relative = Path(f"{prefix}_{idx}_{relative.name}")
            target = testdata_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        result.append(relative.as_posix())
    return result


def _write_hydro_as_hoj(
    hydro_dir: Path,
    output_dir: Path,
    key: str,
    meta: dict[str, Any],
    *,
    verbose: bool,
) -> None:
    config = bridge._read_yaml(hydro_dir / "testdata" / "config.yaml")
    cases = bridge._load_hydro_cases(hydro_dir, config)
    if not cases:
        raise ValueError(f"{hydro_dir.name}: no paired testdata files found")

    data_dir = output_dir / key
    data_dir.mkdir(parents=True, exist_ok=True)
    case_groups = _hydro_case_groups(hydro_dir / "testdata", config)
    exported_cases: list[dict[str, Any]] = []
    for idx, case in enumerate(cases, start=1):
        input_name = f"{idx}.in"
        output_name = f"{idx}.out"
        bridge._copy_file(case.input_path, data_dir / input_name)
        bridge._copy_file(case.output_path, data_dir / output_name)
        case_info: dict[str, Any] = {"input": input_name, "output": output_name}
        original_name = case.input_path.relative_to(hydro_dir / "testdata").as_posix()
        group_info = case_groups.get(original_name)
        score = group_info[1] if group_info is not None else case.score
        if score is not None:
            case_info["score"] = score
        if group_info is not None:
            case_info["groupNum"] = group_info[0]
        exported_cases.append(case_info)

    statement = _read_hydro_statement(hydro_dir)
    fields = _split_hydro_statement(statement, bridge._problem_title(hydro_dir, meta))
    checker = bridge._config_source_path(
        hydro_dir / "testdata", config, "checker", "spj"
    )
    interactor = bridge._config_source_path(
        hydro_dir / "testdata", config, "interactor"
    )
    score_groups = _hydro_score_groups(config)
    problem_type = (
        1
        if score_groups or any(case.get("score") is not None for case in exported_cases)
        else 0
    )
    judge_mode = (
        "interactive"
        if str(config.get("type", "")).lower() == "interactive"
        else "spj"
        if checker
        else "default"
    )
    program = interactor if judge_mode == "interactive" else checker

    filename = (
        config.get("filename") if isinstance(config.get("filename"), str) else None
    )
    time_value = bridge._first_existing(config, "time", "timeLimit", "time_limit")
    if time_value is None:
        time_value = bridge._first_existing(meta, "time", "timeLimit", "time_limit")
    memory_value = bridge._first_existing(
        config, "memory", "memoryLimit", "memory_limit"
    )
    if memory_value is None:
        memory_value = bridge._first_existing(
            meta, "memory", "memoryLimit", "memory_limit"
        )

    pdoc: dict[str, Any] = {
        "auth": 1,
        "author": "admin",
        "isRemote": False,
        "problemId": str(meta.get("pid") or hydro_dir.name),
        "description": fields["description"],
        "source": fields["source"],
        "title": bridge._problem_title(hydro_dir, meta),
        "type": problem_type,
        "judgeMode": judge_mode,
        "judgeCaseMode": "subtask_lowest" if score_groups else "default",
        "timeLimit": bridge._parse_time_ms(time_value, default=1000),
        "memoryLimit": bridge._parse_memory_mb(memory_value, default=256),
        "input": fields["input"],
        "output": fields["output"],
        "difficulty": 0,
        "examples": fields["examples"],
        "ioScore": _hoj_total_score(exported_cases, config),
        "codeShare": True,
        "hint": fields["hint"],
        "isRemoveEndBlank": True,
        "openCaseResult": True,
        "isFileIO": bool(filename),
        "ioReadFileName": f"{filename}.in" if filename else None,
        "ioWriteFileName": f"{filename}.out" if filename else None,
    }
    if program is not None and program.is_file():
        pdoc["spjLanguage"] = (
            "C++" if program.suffix.lower() in {".cc", ".cpp", ".cxx"} else "C"
        )
        pdoc["spjCode"] = program.read_text(encoding="utf-8", errors="replace")

    document: dict[str, Any] = {
        "judgeMode": judge_mode,
        "languages": _hydro_languages_to_hoj(config.get("langs")),
        "samples": exported_cases,
        "tags": _string_list(meta.get("tag") or meta.get("tags")),
        "problem": pdoc,
        "codeTemplates": _read_hydro_code_templates(hydro_dir),
    }
    user_files = _read_hydro_extra_files(
        hydro_dir / "testdata", config, "user_extra_files", "userExtraFiles"
    )
    judge_files = _read_hydro_extra_files(
        hydro_dir / "testdata", config, "judge_extra_files", "judgeExtraFiles"
    )
    if user_files:
        document["userExtraFile"] = user_files
    if judge_files:
        document["judgeExtraFile"] = judge_files

    (output_dir / f"{key}.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if verbose:
        print(
            "  files: "
            f"cases={len(exported_cases)} judge_mode={judge_mode} "
            f"user_extra={len(user_files)} judge_extra={len(judge_files)}"
        )


def _read_hydro_statement(hydro_dir: Path) -> str:
    preferred = [
        hydro_dir / "problem_zh.md",
        hydro_dir / "problem_zh_CN.md",
        hydro_dir / "problem_en.md",
        hydro_dir / "problem.md",
    ]
    for candidate in preferred + sorted(hydro_dir.glob("problem_*.md")):
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8", errors="replace")
    return ""


def _read_hydro_code_templates(hydro_dir: Path) -> list[dict[str, str]]:
    path = hydro_dir / "additional_file" / "code_templates.json"
    if not path.is_file():
        return []
    value = load_json_value_file(path)
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        language = item.get("language")
        code = item.get("code")
        if isinstance(language, str) and isinstance(code, str):
            result.append({"language": language, "code": code})
    return result


def _split_hydro_statement(markdown: str, title: str) -> dict[str, str]:
    sample_pairs = _parse_hydro_samples(markdown)
    without_samples = re.sub(
        r"```(?:input|output)\d*\s*\n[\s\S]*?\n```", "", markdown, flags=re.I
    )
    sections: dict[str, list[str]] = {
        key: [] for key in ("description", "input", "output", "hint", "source")
    }
    current = "description"
    for line in without_samples.splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if match:
            heading = re.sub(r"[：:]$", "", match.group(1).strip()).lower()
            section = _statement_section(heading)
            if section is not None:
                current = section
                continue
            if not sections["description"] and heading == title.strip().lower():
                continue
        sections[current].append(line)

    result = {key: "\n".join(lines).strip() for key, lines in sections.items()}
    result["examples"] = "".join(
        f"<input>{sample_input}</input><output>{sample_output}</output>"
        for sample_input, sample_output in sample_pairs
    )
    return result


def _statement_section(heading: str) -> str | None:
    normalized = heading.replace(" ", "")
    if normalized in {"题目描述", "描述", "description", "problemstatement"}:
        return "description"
    if normalized in {"输入", "输入格式", "input", "inputformat"}:
        return "input"
    if normalized in {"输出", "输出格式", "output", "outputformat"}:
        return "output"
    if normalized in {"提示", "说明", "备注", "hint", "hints", "note", "notes"}:
        return "hint"
    if normalized in {"来源", "题目来源", "source"}:
        return "source"
    if normalized in {"样例", "样例输入输出", "samples", "sample"}:
        return "description"
    return None


def _parse_hydro_samples(markdown: str) -> list[tuple[str, str]]:
    inputs = re.findall(r"```input\d*\s*\n([\s\S]*?)\n```", markdown, flags=re.I)
    outputs = re.findall(r"```output\d*\s*\n([\s\S]*?)\n```", markdown, flags=re.I)
    return [
        (value, outputs[idx] if idx < len(outputs) else "")
        for idx, value in enumerate(inputs)
    ]


def _hydro_case_groups(
    testdata_dir: Path, config: dict[str, Any]
) -> dict[str, tuple[int, int | float | None]]:
    result: dict[str, tuple[int, int | float | None]] = {}
    for group_num, group in enumerate(_hydro_score_groups(config), start=1):
        score = _number_or_none(group.get("score"))
        for case in bridge._as_list(group.get("cases")):
            input_name = (
                case.get("input")
                if isinstance(case, dict)
                else case
                if isinstance(case, str)
                else None
            )
            if isinstance(input_name, str):
                safe_path = bridge._safe_join(testdata_dir, input_name)
                if safe_path.exists():
                    result[safe_path.relative_to(testdata_dir).as_posix()] = (
                        group_num,
                        score,
                    )
    return result


def _hydro_score_groups(config: dict[str, Any]) -> list[dict[str, Any]]:
    subtasks = [
        item
        for item in bridge._as_list(config.get("subtasks"))
        if isinstance(item, dict)
    ]
    if subtasks:
        return subtasks
    return [
        item for item in bridge._as_list(config.get("groups")) if isinstance(item, dict)
    ]


def _read_hydro_extra_files(
    testdata_dir: Path, config: dict[str, Any], *keys: str
) -> dict[str, str]:
    value = bridge._first_existing(config, *keys)
    result: dict[str, str] = {}
    for name in bridge._as_list(value):
        if not isinstance(name, str):
            continue
        source = bridge._safe_join(testdata_dir, name)
        if source.is_file():
            result[name] = source.read_text(encoding="utf-8", errors="replace")
    return result


def _hoj_total_score(cases: list[dict[str, Any]], config: dict[str, Any]) -> int:
    score_groups = _hydro_score_groups(config)
    if score_groups:
        scores = [_number_or_none(item.get("score")) for item in score_groups]
        return int(sum(score for score in scores if score is not None)) or 100
    scores = [_number_or_none(case.get("score")) for case in cases]
    return int(sum(score for score in scores if score is not None)) or 100


def _hoj_languages_to_hydro(value: Any) -> list[str]:
    result: list[str] = []
    for language in _string_list(value):
        mapped = HOJ_TO_HYDRO_LANG.get(language.strip().lower())
        if mapped and mapped not in result:
            result.append(mapped)
    return result


def _hydro_languages_to_hoj(value: Any) -> list[str]:
    result: list[str] = []
    for language in _string_list(value):
        normalized = language.lower()
        prefix = re.split(r"[._-]", normalized, maxsplit=1)[0]
        mapped = HYDRO_TO_HOJ_LANG.get(normalized) or HYDRO_TO_HOJ_LANG.get(prefix)
        if mapped and mapped not in result:
            result.append(mapped)
    return result or list(HOJ_DEFAULT_LANGUAGES)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _positive_int(value: Any, default: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _number_or_none(value: Any) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        number = float(str(value))
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


def _unique_hoj_key(value: str, used: set[str]) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-") or "problem"
    candidate = cleaned
    index = 2
    while candidate.lower() in used:
        candidate = f"{cleaned}-{index}"
        index += 1
    used.add(candidate.lower())
    return candidate
