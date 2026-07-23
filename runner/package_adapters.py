from __future__ import annotations

import base64
import json
import re
import signal
import shutil
import tempfile
import threading
import uuid
import zipfile

# The standard library implementation is used only to create XML, never to parse it.
import xml.etree.ElementTree as ElementTree  # nosec B405
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable
from urllib.parse import unquote

from defusedxml import ElementTree as DefusedElementTree

import format_bridge as bridge
import hoj_bridge
from package_ir import Problem, ProblemBundle, Program, Statement, TestCase, TestGroup
from package_security import (
    MAX_METADATA_BYTES,
    load_json_file,
    load_json_value_file,
    load_yaml_file,
    read_limited_text,
)
from progress import ProgressReporter


READABLE_FORMATS = ("hydro", "icpc", "hoj", "fps", "qduoj", "uoj", "dmoj", "generic")
WRITABLE_FORMATS = ("hydro", "icpc", "hoj", "fps", "qduoj", "uoj", "dmoj")
MAX_XML_NODES = 100_000
MAX_XML_DEPTH = 48
MAX_EMBEDDED_IMAGE_BYTES = 64 * 1024 * 1024
MAX_DMOJ_REGEX_LENGTH = 2048
DMOJ_REGEX_TIMEOUT_SECONDS = 5.0
DMOJ_DEFAULT_INPUT_PATTERN = (
    r"^(?=.*?\.in|in).*?(?:(?:^|\W)(?P<batch>\d+)[^\d\s]+)?(?P<case>\d+)[^\d\s]*$"
)
DMOJ_DEFAULT_OUTPUT_PATTERN = (
    r"^(?=.*?\.out|out).*?(?:(?:^|\W)(?P<batch>\d+)[^\d\s]+)?(?P<case>\d+)[^\d\s]*$"
)
LANG_SUFFIXES = {
    ".c": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cxx": "C++",
    ".java": "Java",
    ".kt": "Kotlin",
    ".py": "Python3",
    ".go": "Golang",
    ".rs": "Rust",
    ".pas": "Pascal",
}


@dataclass(frozen=True)
class Detection:
    format: str
    confidence: float
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class Adapter:
    format: str
    detect: Callable[[Path], Detection | None]
    read: Callable[
        [
            Path,
            Path,
            Iterable[str],
            bridge.ArchiveExtractionBudget | None,
            ProgressReporter | None,
        ],
        ProblemBundle,
    ]
    validate_target: Callable[[ProblemBundle, dict[str, Any]], None] | None
    write: Callable[[ProblemBundle, Path, dict[str, Any]], list[str]] | None


def detect_extracted(root: Path) -> list[Detection]:
    files = [
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    ]
    lowered = [name.lower() for name in files]
    candidates: list[Detection] = []

    def add(format_id: str, confidence: float, *evidence: str) -> None:
        candidates.append(Detection(format_id, confidence, tuple(evidence)))

    if any(name.endswith("contest.xml") for name in lowered) and any(
        "problems/" in name for name in lowered
    ):
        add("polygon", 0.99, "contest.xml", "problems/ directory")
    if any(name.endswith("/testdata/config.yaml") for name in lowered) and any(
        name.endswith("/problem.yaml") for name in lowered
    ):
        add("hydro", 0.98, "problem.yaml", "testdata/config.yaml")
    qduoj_roots = {
        name.removesuffix("/problem.json")
        for name in lowered
        if re.match(r"(?:^|/)\d+/problem\.json$", name)
    }
    if any(
        any(name.startswith(f"{directory}/testcase/") for name in lowered)
        for directory in qduoj_roots
    ):
        add("qduoj", 0.99, "numbered problem.json", "testcase/ directory")
    if any(re.search(r"(?:^|/)problem_[^/]+\.json$", name) for name in lowered):
        add("hoj", 0.96, "problem_*.json", "paired data directory")
    if any(
        PurePosixPath(name).name in {"problem.xml", "fps.xml"}
        and "/problems/" not in f"/{name}"
        for name in lowered
    ):
        add("fps", 0.96, "problem.xml/fps.xml")
    if any(name.endswith("problem.conf") for name in lowered):
        add("uoj", 0.97, "problem.conf")
    if any(name.endswith("init.yml") for name in lowered):
        add("dmoj", 0.97, "init.yml")
    icpc_markers = any("/data/secret/" in f"/{name}" for name in lowered)
    icpc_statements = any(
        "/problem_statement/" in f"/{name}" or "/statement/problem." in f"/{name}"
        for name in lowered
    )
    if (
        any(name.endswith("problem.yaml") for name in lowered)
        and icpc_markers
        and icpc_statements
    ):
        add("icpc", 0.95, "problem.yaml", "data/secret", "statement directory")
    inputs = [name for name in lowered if Path(name).suffix in bridge.IN_SUFFIXES]
    outputs = [name for name in lowered if Path(name).suffix in bridge.OUT_SUFFIXES]
    if inputs and outputs:
        add(
            "generic",
            0.55,
            f"{len(inputs)} input files",
            f"{len(outputs)} output files",
        )
    return sorted(candidates, key=lambda item: (-item.confidence, item.format))


def choose_detected_format(candidates: list[Detection]) -> str:
    strong = [candidate for candidate in candidates if candidate.confidence >= 0.8]
    if len(strong) == 1:
        return strong[0].format
    if not strong:
        evidence = ", ".join(candidate.format for candidate in candidates) or "none"
        raise ValueError(f"unable to detect source format; candidates: {evidence}")
    names = ", ".join(candidate.format for candidate in strong)
    raise ValueError(f"ambiguous source format; choose one explicitly: {names}")


def _safe_name(value: Any, fallback: str = "problem") -> str:
    return bridge._safe_name(str(value or "")) or fallback


def _unique_output_stem(base: str, used: set[str], *, separator: str = "-") -> str:
    candidate = base
    index = 2
    while candidate.casefold() in used:
        candidate = f"{base}{separator}{index}"
        index += 1
    used.add(candidate.casefold())
    return candidate


def _language_from_name(path: Path, default: str = "und") -> str:
    match = re.search(r"problem[._-]([A-Za-z]{2,3}(?:[_-][A-Za-z]{2,4})?)\.", path.name)
    return match.group(1).replace("_", "-") if match else default


def _language_from_source(path: Path) -> str | None:
    return LANG_SUFFIXES.get(path.suffix.lower())


def _program_from_path(kind: str, path: Path, *, mode: str | None = None) -> Program:
    return Program(
        kind=kind, language=_language_from_source(path), mode=mode, path=path
    )  # type: ignore[arg-type]


def _find_first_source(root: Path) -> Path | None:
    if not root.exists():
        return None
    return next(
        (
            path
            for path in sorted(root.rglob("*"))
            if path.is_file() and path.suffix.lower() in bridge.SOURCE_SUFFIXES
        ),
        None,
    )


def _filter_problems(problems: list[Problem], only: Iterable[str]) -> list[Problem]:
    wanted = [_safe_name(value, "") for value in only if value]
    if not wanted:
        return problems
    lookup: dict[str, list[Problem]] = {}
    for problem in problems:
        for value in (problem.id, problem.slug, problem.title):
            key = _safe_name(value, "")
            matches = lookup.setdefault(key, []) if key else []
            if key and not any(item is problem for item in matches):
                matches.append(problem)
    missing = [key for key in wanted if key not in lookup]
    if missing:
        raise ValueError(f"unknown problem(s): {', '.join(missing)}")
    ambiguous = [key for key in wanted if len(lookup[key]) > 1]
    if ambiguous:
        raise ValueError(f"ambiguous problem selector(s): {', '.join(ambiguous)}")
    selected: list[Problem] = []
    for key in wanted:
        problem = lookup[key][0]
        if not any(item is problem for item in selected):
            selected.append(problem)
    return selected


def _report_read_progress(
    reporter: ProgressReporter | None,
    current: int,
    total: int,
    problem: str,
    detail: str,
) -> None:
    if reporter is not None:
        reporter.update(
            current=current,
            total=total,
            unit="problems",
            problem=problem,
            detail=f"{detail}: {problem}",
        )


def _validate_hydro_declared_cases(
    bundle: ProblemBundle,
    problem: str,
    testdata_dir: Path,
    config: dict[str, Any],
    source_root: Path,
) -> None:
    for index, (raw, _) in enumerate(bridge._iter_hydro_config_cases(config), start=1):
        if isinstance(raw, str):
            input_name = raw
            output_name = bridge._find_matching_output_name(testdata_dir, input_name)
        elif isinstance(raw, dict):
            input_name = bridge._first_string(raw, "input", "in", "inputFile", "stdin")
            output_name = bridge._first_string(
                raw, "output", "out", "answer", "answerFile", "stdout"
            )
            if output_name is None and input_name:
                output_name = bridge._find_matching_output_name(
                    testdata_dir, input_name
                )
        else:
            bundle.add_issue(
                "fatal",
                "hydro-invalid-test-entry",
                f"Hydro test case #{index} has an unsupported config entry",
                problem=problem,
                field="cases",
            )
            continue
        if not input_name or not output_name:
            bundle.add_issue(
                "fatal",
                "hydro-unpaired-test",
                f"Hydro test case #{index} does not declare both input and output",
                problem=problem,
                field="cases",
            )
            continue
        input_path = bridge._safe_join(testdata_dir, input_name)
        output_path = bridge._safe_join(testdata_dir, output_name)
        for role, path in (("input", input_path), ("output", output_path)):
            if not path.is_file():
                bundle.add_issue(
                    "fatal",
                    f"hydro-missing-test-{role}",
                    f"Hydro test case #{index} references a missing {role} file: {path.name}",
                    problem=problem,
                    field="cases",
                    context={
                        "expected_path": path.relative_to(source_root).as_posix(),
                        "role": role,
                        "source": "testdata/config.yaml",
                        "source_location": "testdata/config.yaml",
                    },
                )


def _case_groups_from_hydro(
    config: dict[str, Any],
) -> tuple[list[TestGroup], dict[str, str]]:
    groups: list[TestGroup] = []
    case_to_group: dict[str, str] = {}
    raw_groups = bridge._as_list(config.get("subtasks")) or bridge._as_list(
        config.get("groups")
    )
    for index, raw in enumerate(raw_groups, start=1):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or index)
        case_names: list[str] = []
        for case in bridge._as_list(raw.get("cases")):
            input_name = (
                case
                if isinstance(case, str)
                else bridge._first_string(case, "input", "in", "inputFile")
            )
            if not input_name:
                continue
            key = Path(input_name).as_posix()
            case_names.append(key)
            case_to_group[key] = name
        dependencies = tuple(
            str(item) for item in bridge._as_list(raw.get("dependencies"))
        )
        groups.append(
            TestGroup(name, tuple(case_names), raw.get("score"), dependencies)
        )
    return groups, case_to_group


def read_hydro(
    root: Path,
    workspace: Path,
    only: Iterable[str],
    budget: bridge.ArchiveExtractionBudget | None = None,
    reporter: ProgressReporter | None = None,
) -> ProblemBundle:
    package_root = bridge._strip_single_root(root)
    problem_dirs = bridge._find_hydro_problems(package_root)
    if not problem_dirs:
        raise ValueError("no Hydro problem package found")
    problems: list[Problem] = []
    bundle = ProblemBundle("hydro", problems)
    for index, problem_dir in enumerate(problem_dirs, start=1):
        _report_read_progress(
            reporter, index - 1, len(problem_dirs), problem_dir.name, "reading Hydro"
        )
        meta = load_yaml_file(problem_dir / "problem.yaml")
        config = load_yaml_file(problem_dir / "testdata" / "config.yaml")
        pid = str(meta.get("pid") or meta.get("id") or problem_dir.name)
        slug = bridge._problem_slug(problem_dir, meta)
        title = bridge._problem_title(problem_dir, meta)
        statements: list[Statement] = []
        for statement_path in sorted(problem_dir.glob("problem_*.md")):
            pdf = bridge._hydro_statement_pdf(problem_dir, statement_path)
            language = (
                statement_path.stem.removeprefix("problem_").replace("_", "-") or "und"
            )
            if pdf is not None:
                statements.append(Statement(language, "pdf", path=pdf))
            else:
                statement_text = read_limited_text(statement_path)
                pdf_reference = re.fullmatch(
                    r"\s*@\[pdf\]\(([^)]+)\)\s*", statement_text
                )
                if pdf_reference is not None:
                    source = unquote(pdf_reference.group(1).strip())
                    if source.startswith("file://"):
                        source = source.removeprefix("file://")
                    if "://" not in source:
                        expected = bridge._safe_join(
                            problem_dir / "additional_file", source
                        )
                        bundle.add_issue(
                            "fatal",
                            "hydro-missing-statement-pdf",
                            f"Hydro statement references a missing PDF file: {expected.name}",
                            problem=slug,
                            field="statements",
                            context={
                                "expected_path": expected.relative_to(root).as_posix(),
                                "role": "statement-pdf",
                                "source": statement_path.relative_to(root).as_posix(),
                                "source_location": statement_path.relative_to(
                                    root
                                ).as_posix(),
                            },
                        )
                        statements.append(Statement(language, "pdf", path=expected))
                        continue
                statements.append(
                    Statement(language, "markdown", content=statement_text)
                )
        groups, case_to_group = _case_groups_from_hydro(config)
        cases = []
        hydro_case_names: dict[str, str] = {}
        _validate_hydro_declared_cases(
            bundle, slug, problem_dir / "testdata", config, root
        )
        unpaired = _unpaired_case_files(problem_dir / "testdata")
        if unpaired:
            missing_path, missing_role = _missing_partner_for_unpaired(
                problem_dir / "testdata", unpaired[0]
            )
            bundle.add_issue(
                "fatal",
                "hydro-unpaired-test-file",
                f"Hydro testdata contains an unpaired file: {unpaired[0]}",
                problem=slug,
                field="cases",
                context={
                    "expected_path": missing_path.relative_to(root).as_posix(),
                    "role": missing_role,
                    "source": "testdata directory",
                    "source_location": "testdata directory",
                },
            )
        for case in bridge._load_hydro_cases(problem_dir, config):
            rel = case.input_path.relative_to(problem_dir / "testdata").as_posix()
            hydro_case_names[rel] = case.name
            cases.append(
                TestCase(
                    case.name,
                    case.input_path,
                    case.output_path,
                    case.sample,
                    case.score,
                    case_to_group.get(rel),
                )
            )
        groups = [
            TestGroup(
                group.name,
                tuple(
                    hydro_case_names.get(case_name, case_name)
                    for case_name in group.cases
                ),
                group.score,
                group.dependencies,
            )
            for group in groups
        ]
        used_inputs = {case.input_path.resolve() for case in cases}
        for case in bridge._load_hydro_sample_cases(problem_dir, used_inputs):
            cases.append(
                TestCase(case.name, case.input_path, case.output_path, True, 0)
            )
        checker_path = bridge._config_source_path(
            problem_dir / "testdata", config, "checker", "spj"
        )
        interactor_path = bridge._config_source_path(
            problem_dir / "testdata", config, "interactor"
        )
        validator_path = bridge._config_source_path(
            problem_dir / "testdata",
            config,
            "validator",
            "input_validator",
            "inputValidator",
        )
        for kind, path in (
            ("checker", checker_path),
            ("interactor", interactor_path),
            ("validator", validator_path),
        ):
            if path is not None and not path.is_file():
                bundle.add_issue(
                    "fatal",
                    f"hydro-missing-{kind}",
                    f"Hydro config references a missing {kind} file: {path.name}",
                    problem=slug,
                    field=kind,
                    context={
                        "expected_path": path.relative_to(root).as_posix(),
                        "role": kind,
                        "source": "testdata/config.yaml",
                        "source_location": "testdata/config.yaml",
                    },
                )
        problem_type = (
            "interactive" if bridge._is_hydro_interactive(config) else "default"
        )
        tags = [
            str(value) for value in bridge._as_list(meta.get("tag") or meta.get("tags"))
        ]
        attachments: dict[str, Path] = {}
        statement_files = {
            statement.path.resolve()
            for statement in statements
            if statement.path is not None and statement.path.is_file()
        }
        for base in (
            problem_dir / "additional_file" / "attachments",
            problem_dir / "attachments",
        ):
            if base.exists():
                for path in sorted(base.rglob("*")):
                    if path.is_file() and path.resolve() not in statement_files:
                        attachments.setdefault(path.relative_to(base).as_posix(), path)
        templates: dict[str, str] = {}
        template_file = problem_dir / "additional_file" / "code_templates.json"
        if template_file.is_file():
            document = load_json_value_file(template_file)
            if isinstance(document, list):
                for item in document:
                    if (
                        isinstance(item, dict)
                        and item.get("language")
                        and isinstance(item.get("code"), str)
                    ):
                        templates[str(item["language"])] = item["code"]
        solution_paths: list[Path] = []
        for base in (
            problem_dir / "testdata",
            problem_dir / "submissions" / "accepted",
            problem_dir / "additional_file" / "sources" / "submissions" / "accepted",
        ):
            if not base.exists():
                continue
            for path in sorted(base.rglob("*")):
                if (
                    path.is_file()
                    and path.suffix.lower() in bridge.SOURCE_SUFFIXES
                    and (
                        "accepted" in path.parts
                        or bridge._classify_source(path) == "submission"
                    )
                ):
                    solution_paths.append(path)
        solutions = [
            _program_from_path("solution", path, mode="accepted")
            for path in dict.fromkeys(solution_paths)
        ]
        problems.append(
            Problem(
                id=pid,
                slug=slug,
                title=title,
                statements=statements,
                cases=cases,
                groups=groups,
                time_ms=bridge._parse_time_ms(
                    config.get("time", meta.get("time")), default=1000
                ),
                memory_mb=bridge._parse_memory_mb(
                    config.get("memory", meta.get("memory")), default=256
                ),
                tags=tags,
                source=str(meta.get("source")) if meta.get("source") else None,
                problem_type=problem_type,
                checker=_program_from_path(
                    "checker",
                    checker_path,
                    mode=str(config.get("checker_type") or "custom"),
                )
                if checker_path and checker_path.is_file()
                else None,
                interactor=_program_from_path("interactor", interactor_path)
                if interactor_path and interactor_path.is_file()
                else None,
                validator=_program_from_path("validator", validator_path)
                if validator_path and validator_path.is_file()
                else None,
                file_io_base=str(config.get("filename"))
                if config.get("filename")
                else None,
                attachments=attachments,
                templates=templates,
                solutions=solutions,
                extra={"hydro_meta": meta, "hydro_config": config, "index": index},
            )
        )
        _report_read_progress(
            reporter, index, len(problem_dirs), slug, "read Hydro problem"
        )
    bundle.problems = _filter_problems(problems, only)
    return bundle


def _find_icpc_dirs(root: Path) -> list[Path]:
    candidates: list[Path] = []
    for marker in sorted(root.rglob("problem.yaml")):
        directory = marker.parent
        if (directory / "data").is_dir() and (
            (directory / "statement").is_dir()
            or (directory / "problem_statement").is_dir()
        ):
            candidates.append(directory)
    return bridge._dedupe_problem_dirs(candidates, root)


def _generated_verbatim_tex_body(text: str) -> str | None:
    lines = text.splitlines()
    if (
        len(lines) < 4
        or not lines[0].startswith(r"\problemname{")
        or not lines[0].endswith("}")
        or lines[1] != r"\begin{verbatim}"
        or lines[-1] != r"\end{verbatim}"
    ):
        return None
    return "\n".join(lines[2:-1]).rstrip("\n") + "\n"


def _is_generated_hash_validator(path: Path) -> bool:
    if path.suffix.lower() != ".py":
        return False
    try:
        content = read_limited_text(path)
    except (OSError, UnicodeError, ValueError):
        return False
    return (
        "ALLOWED = [" in content
        and "hashlib.sha256" in content
        and "digest in ALLOWED" in content
        and "SystemExit(42" in content
    )


def read_icpc(
    root: Path,
    workspace: Path,
    only: Iterable[str],
    budget: bridge.ArchiveExtractionBudget | None = None,
    reporter: ProgressReporter | None = None,
) -> ProblemBundle:
    problem_dirs = _find_icpc_dirs(root)
    if not problem_dirs:
        raise ValueError("no ICPC problem package found")
    problems: list[Problem] = []
    bundle = ProblemBundle("icpc", problems)
    for index, problem_dir in enumerate(problem_dirs, start=1):
        _report_read_progress(
            reporter, index - 1, len(problem_dirs), problem_dir.name, "reading ICPC"
        )
        meta = load_yaml_file(problem_dir / "problem.yaml")
        ini = bridge._read_domjudge_ini(problem_dir / "domjudge-problem.ini")
        slug = bridge._domjudge_problem_slug(problem_dir, meta, ini)
        title_value = meta.get("name")
        titles = (
            {str(k): str(v) for k, v in title_value.items()}
            if isinstance(title_value, dict)
            else {}
        )
        title = next(
            iter(titles.values()),
            bridge._domjudge_problem_title(problem_dir, meta, ini),
        )
        statements: list[Statement] = []
        attachments: dict[str, Path] = {}
        statement_root = problem_dir / (
            "statement" if (problem_dir / "statement").is_dir() else "problem_statement"
        )
        for path in sorted(statement_root.rglob("*")):
            if not path.is_file():
                continue
            language = _language_from_name(path)
            suffix = path.suffix.lower()
            if suffix == ".pdf":
                statements.append(Statement(language, "pdf", path=path))
            elif suffix == ".md":
                statements.append(
                    Statement(language, "markdown", content=read_limited_text(path))
                )
            elif suffix in {".html", ".htm"}:
                statements.append(
                    Statement(language, "html", content=read_limited_text(path))
                )
            elif suffix == ".tex":
                text = read_limited_text(path)
                generated_body = _generated_verbatim_tex_body(text)
                if generated_body is not None:
                    statements.append(
                        Statement(language, "markdown", content=generated_body)
                    )
                else:
                    attachments[
                        f"{statement_root.name}/{path.relative_to(statement_root).as_posix()}"
                    ] = path
                    bundle.add_issue(
                        "loss",
                        "icpc-tex-statement",
                        "LaTeX statement is preserved as an attachment but cannot be rendered by every target",
                        problem=slug,
                        field="statements",
                    )
        secret_cases = bridge._scan_case_pairs(
            problem_dir / "data" / "secret", sample=False
        )
        sample_cases = bridge._scan_case_pairs(
            problem_dir / "data" / "sample", sample=True
        )
        cases = [
            TestCase(
                case.name, case.input_path, case.output_path, case.sample, case.score
            )
            for case in sample_cases + secret_cases
        ]
        for data_directory in (
            problem_dir / "data" / "sample",
            problem_dir / "data" / "secret",
        ):
            unpaired = _unpaired_case_files(data_directory)
            if unpaired:
                missing_path, missing_role = _missing_partner_for_unpaired(
                    data_directory, unpaired[0]
                )
                bundle.add_issue(
                    "fatal",
                    "icpc-unpaired-test",
                    f"ICPC data directory contains an unpaired test file: {unpaired[0]}",
                    problem=slug,
                    field="cases",
                    context={
                        "expected_path": missing_path.relative_to(root).as_posix(),
                        "role": missing_role,
                        "source": "data directory",
                        "source_location": "data directory",
                    },
                )
        validation = str(meta.get("validation") or "").lower()
        is_interactive = "interactive" in validation
        checker_path = _find_first_source(
            problem_dir / "output_validator"
        ) or _find_first_source(problem_dir / "output_validators" / "checker")
        interactor_path = _find_first_source(
            problem_dir / "output_validators" / "interactor"
        )
        validator_path = _find_first_source(problem_dir / "input_validators")
        if validator_path is not None and _is_generated_hash_validator(validator_path):
            validator_path = None
        if is_interactive and interactor_path is None:
            interactor_path = checker_path
            checker_path = None
            if interactor_path is None:
                expected = (
                    problem_dir / "output_validators" / "interactor" / "interactor.cpp"
                )
                bundle.add_issue(
                    "fatal",
                    "icpc-missing-interactor",
                    "ICPC metadata declares an interactive problem but no interactor source was found",
                    problem=slug,
                    field="interactor",
                    context={
                        "expected_path": expected.relative_to(root).as_posix(),
                        "role": "interactor",
                        "source": "problem.yaml",
                        "source_location": "problem.yaml",
                    },
                )
        elif (
            not is_interactive
            and validation.startswith("custom")
            and checker_path is None
        ):
            expected = problem_dir / "output_validators" / "checker" / "checker.cpp"
            bundle.add_issue(
                "fatal",
                "icpc-missing-checker",
                "ICPC metadata requires custom validation but no output validator source was found",
                problem=slug,
                field="checker",
                context={
                    "expected_path": expected.relative_to(root).as_posix(),
                    "role": "checker",
                    "source": "problem.yaml",
                    "source_location": "problem.yaml",
                },
            )
        for base in (
            problem_dir / "attachments",
            problem_dir / "include",
            problem_dir / "generators",
        ):
            if base.exists():
                for path in sorted(base.rglob("*")):
                    if path.is_file():
                        attachments[
                            f"{base.name}/{path.relative_to(base).as_posix()}"
                        ] = path
        solutions = []
        submissions = problem_dir / "submissions"
        if submissions.exists():
            for path in sorted(submissions.rglob("*")):
                if path.is_file() and path.suffix.lower() in bridge.SOURCE_SUFFIXES:
                    relative_submission = path.relative_to(submissions)
                    if (
                        relative_submission.parts
                        and relative_submission.parts[0].lower() == "accepted"
                    ):
                        solutions.append(
                            _program_from_path("solution", path, mode="accepted")
                        )
                    else:
                        attachments[f"submissions/{relative_submission.as_posix()}"] = (
                            path
                        )
                        bundle.add_issue(
                            "loss",
                            "icpc-nonaccepted-submission",
                            "non-accepted submissions are preserved as attachments, not reference solutions",
                            problem=slug,
                            field="solutions",
                        )
        limits = meta.get("limits") if isinstance(meta.get("limits"), dict) else {}
        time_seconds = bridge._parse_seconds(
            limits.get("time_limit", ini.get("timelimit")), default=1
        )
        memory_mb = bridge._parse_memory_mb(limits.get("memory", 256), default=256)
        problems.append(
            Problem(
                id=str(ini.get("short-name") or slug or index),
                slug=slug,
                title=title,
                titles=titles,
                statements=statements,
                cases=cases,
                time_ms=max(1, int(float(time_seconds) * 1000)),
                memory_mb=memory_mb,
                source=str(meta.get("source")) if meta.get("source") else None,
                problem_type="interactive" if is_interactive else "default",
                checker=_program_from_path("checker", checker_path, mode="testlib")
                if checker_path
                else None,
                interactor=_program_from_path("interactor", interactor_path)
                if interactor_path
                else None,
                validator=_program_from_path("validator", validator_path)
                if validator_path
                else None,
                attachments=attachments,
                solutions=solutions,
                extra={"icpc_meta": meta, "index": index},
            )
        )
        _report_read_progress(
            reporter, index, len(problem_dirs), slug, "read ICPC problem"
        )
    bundle.problems = _filter_problems(problems, only)
    return bundle


def read_hoj(
    root: Path,
    workspace: Path,
    only: Iterable[str],
    budget: bridge.ArchiveExtractionBudget | None = None,
    reporter: ProgressReporter | None = None,
) -> ProblemBundle:
    items = hoj_bridge._filter_hoj_problems(hoj_bridge._find_hoj_problems(root), only)
    if not items:
        raise ValueError("no HOJ problem export found")
    missing_bundle = _hoj_missing_file_bundle(items, root)
    if missing_bundle is not None:
        return missing_bundle
    staged = workspace / "hoj-as-hydro"
    for index, item in enumerate(items, start=1):
        _report_read_progress(reporter, index - 1, len(items), item.key, "reading HOJ")
        pid = f"H{index}"
        hoj_bridge._write_hoj_as_hydro(item, staged / pid, pid, 1, [], verbose=False)
        _report_read_progress(
            reporter, index, len(items), item.key, "staged HOJ problem"
        )
    bundle = read_hydro(staged, workspace / "hoj-hydro-read", (), budget, reporter)
    bundle.source_format = "hoj"
    return bundle


def _hoj_missing_file_bundle(
    items: list[hoj_bridge.HojProblem], root: Path
) -> ProblemBundle | None:
    problems: list[Problem] = []
    bundle = ProblemBundle("hoj", problems)
    found_missing = False
    for index, item in enumerate(items, start=1):
        pdoc = item.problem
        title = str(pdoc.get("title") or pdoc.get("problemId") or item.key)
        statement, _ = hoj_bridge._build_hydro_statement(pdoc)
        cases = [
            TestCase(
                case.input_path.name,
                case.input_path,
                case.output_path,
                False,
                case.score,
                str(case.group_num) if case.group_num is not None else None,
            )
            for case in hoj_bridge._load_hoj_cases(item)
        ]
        problems.append(
            Problem(
                id=str(pdoc.get("problemId") or index),
                slug=_safe_name(item.key, f"problem-{index}"),
                title=title,
                statements=[Statement("zh", "markdown", content=statement)],
                cases=cases,
                time_ms=hoj_bridge._positive_int(pdoc.get("timeLimit"), 1000),
                memory_mb=hoj_bridge._positive_int(pdoc.get("memoryLimit"), 256),
                file_io_base=Path(str(pdoc.get("ioReadFileName") or "")).stem
                if pdoc.get("isFileIO") and pdoc.get("ioReadFileName")
                else None,
            )
        )
        declared = item.document.get("samples")
        if isinstance(declared, list):
            for case_index, raw in enumerate(declared, start=1):
                if not isinstance(raw, dict):
                    continue
                for role, key in (("input", "input"), ("output", "output")):
                    name = raw.get(key)
                    if not isinstance(name, str):
                        continue
                    expected = bridge._safe_join(item.data_dir, name)
                    if expected.is_file():
                        continue
                    found_missing = True
                    bundle.add_issue(
                        "fatal",
                        f"hoj-missing-test-{role}",
                        f"HOJ test case #{case_index} references a missing {role} file: {expected.name}",
                        problem=item.key,
                        field="cases",
                        context={
                            "expected_path": expected.relative_to(root).as_posix(),
                            "role": role,
                            "source": item.json_path.relative_to(root).as_posix(),
                            "source_location": item.json_path.relative_to(
                                root
                            ).as_posix(),
                        },
                    )
        for relative in _unpaired_case_files(item.data_dir):
            missing, role = _missing_partner_for_unpaired(item.data_dir, relative)
            if any(
                issue.context.get("expected_path")
                == missing.relative_to(root).as_posix()
                for issue in bundle.issues
            ):
                continue
            found_missing = True
            bundle.add_issue(
                "fatal",
                f"hoj-missing-test-{role}",
                f"HOJ data directory contains an unpaired file: {relative}",
                problem=item.key,
                field="cases",
                context={
                    "expected_path": missing.relative_to(root).as_posix(),
                    "role": role,
                    "source": item.data_dir.relative_to(root).as_posix(),
                    "source_location": item.data_dir.relative_to(root).as_posix(),
                },
            )
    return bundle if found_missing else None


def _validate_xml_tree(root: Any) -> None:
    count = 0
    stack: list[tuple[Any, int]] = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > MAX_XML_NODES:
            raise ValueError(f"XML node limit exceeded ({MAX_XML_NODES})")
        if depth > MAX_XML_DEPTH:
            raise ValueError(f"XML nesting limit exceeded ({MAX_XML_DEPTH})")
        stack.extend((child, depth + 1) for child in list(node))


def _fps_text(node: Any, name: str, default: str = "") -> str:
    child = node.find(name)
    return child.text if child is not None and child.text is not None else default


def _fps_limit(node: Any, name: str, *, default: int, memory: bool = False) -> int:
    child = node.find(name)
    if child is None or not (child.text or "").strip():
        return default
    try:
        value = float((child.text or "").strip())
    except ValueError as exc:
        raise ValueError(f"invalid FPS {name}") from exc
    unit = str(child.attrib.get("unit") or ("mb" if memory else "s")).lower()
    if memory:
        return max(1, int(value / 1024 if unit == "kb" else value))
    return max(1, int(value if unit == "ms" else value * 1000))


def _materialize_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _program_from_fps(node: Any, kind: str) -> Program | None:
    child = node.find(kind)
    if child is None or not (child.text or "").strip():
        return None
    language = str(child.attrib.get("language") or "") or None
    mapped_kind = "checker" if kind in {"spj", "tpj"} else kind
    return Program(
        kind=mapped_kind, language=language, mode="testlib", content=child.text
    )  # type: ignore[arg-type]


def read_fps(
    root: Path,
    workspace: Path,
    only: Iterable[str],
    budget: bridge.ArchiveExtractionBudget | None = None,
    reporter: ProgressReporter | None = None,
) -> ProblemBundle:
    xml_paths = [
        path
        for path in sorted(root.rglob("*.xml"))
        if path.name.lower() in {"problem.xml", "fps.xml"}
    ]
    if not xml_paths:
        raise ValueError("no FPS problem.xml or fps.xml found")
    if len(xml_paths) > 1:
        raise ValueError("multiple FPS XML files found")
    xml_text = read_limited_text(xml_paths[0], limit=MAX_METADATA_BYTES)
    try:
        xml_root = DefusedElementTree.fromstring(
            xml_text,
            forbid_dtd=False,
            forbid_entities=True,
            forbid_external=True,
        )
    except Exception as exc:
        raise ValueError(f"invalid or unsafe FPS XML: {exc}") from exc
    if xml_root.tag != "fps":
        raise ValueError("FPS XML root must be <fps>")
    _validate_xml_tree(xml_root)
    version = str(xml_root.attrib.get("version") or "1.1")
    if version not in {"1.1", "1.2", "1.3", "1.4", "1.5", "1.6"}:
        raise ValueError(f"unsupported FPS version: {version}")

    bundle = ProblemBundle("fps", [])
    image_total = 0
    for index, item in enumerate(xml_root.findall("item"), start=1):
        title = _fps_text(item, "title", f"Problem {index}")
        slug = _safe_name(_fps_text(item, "remote_id") or title, f"problem-{index}")
        statement_parts = []
        for heading, tag in (
            ("Description", "description"),
            ("Input", "input"),
            ("Output", "output"),
            ("Hint", "hint"),
        ):
            value = _fps_text(item, tag)
            if value:
                statement_parts.append(f"<h2>{heading}</h2>\n{value}")
        statements = (
            [Statement("und", "html", content="\n".join(statement_parts))]
            if statement_parts
            else []
        )
        problem_workspace = workspace / "fps" / f"{index:04d}"
        cases: list[TestCase] = []
        sample_inputs = item.findall("sample_input")
        sample_outputs = item.findall("sample_output")
        if len(sample_inputs) != len(sample_outputs):
            bundle.add_issue(
                "fatal",
                "fps-unpaired-samples",
                "FPS sample input/output count differs",
                problem=slug,
            )
        for case_index, (input_node, output_node) in enumerate(
            zip(sample_inputs, sample_outputs), start=1
        ):
            input_path = _materialize_text(
                problem_workspace / "sample" / f"{case_index}.in", input_node.text or ""
            )
            output_path = _materialize_text(
                problem_workspace / "sample" / f"{case_index}.out",
                output_node.text or "",
            )
            cases.append(
                TestCase(f"sample-{case_index}", input_path, output_path, True, 0)
            )
        test_inputs = item.findall("test_input")
        test_outputs = item.findall("test_output")
        if len(test_inputs) != len(test_outputs):
            bundle.add_issue(
                "fatal",
                "fps-unpaired-tests",
                "FPS test input/output count differs",
                problem=slug,
            )
        for case_index, (input_node, output_node) in enumerate(
            zip(test_inputs, test_outputs), start=1
        ):
            name = _safe_name(input_node.attrib.get("name"), str(case_index))
            input_path = _materialize_text(
                problem_workspace / "secret" / f"{case_index:03d}.in",
                input_node.text or "",
            )
            output_path = _materialize_text(
                problem_workspace / "secret" / f"{case_index:03d}.out",
                output_node.text or "",
            )
            cases.append(TestCase(name, input_path, output_path))
        attachments: dict[str, Path] = {}
        for image_index, image in enumerate(item.findall("img"), start=1):
            source = _fps_text(image, "src", f"image-{image_index}.bin")
            encoded = _fps_text(image, "base64")
            try:
                data = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise ValueError(f"invalid FPS Base64 image in {slug}") from exc
            image_total += len(data)
            if image_total > MAX_EMBEDDED_IMAGE_BYTES:
                raise ValueError(
                    f"FPS embedded images exceed {MAX_EMBEDDED_IMAGE_BYTES} bytes"
                )
            suffix = Path(source).suffix.lower()
            if suffix not in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".webp"}:
                raise ValueError(
                    f"unsupported FPS image extension: {suffix or '(none)'}"
                )
            target = problem_workspace / "images" / f"{image_index:04d}{suffix}"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            attachments[f"images/{target.name}"] = target
        templates: dict[str, str] = {}
        for template in item.findall("template"):
            language = str(template.attrib.get("language") or "unknown")
            templates[language] = template.text or ""
        solutions = [
            Program(
                "solution",
                language=str(solution.attrib.get("language") or "") or None,
                mode="accepted",
                content=solution.text or "",
            )
            for solution in item.findall("solution")
        ]
        checker = _program_from_fps(item, "spj") or _program_from_fps(item, "tpj")
        interactor = _program_from_fps(item, "interactor")
        bundle.problems.append(
            Problem(
                id=_fps_text(item, "remote_id", slug),
                slug=slug,
                title=title,
                statements=statements,
                cases=cases,
                time_ms=_fps_limit(item, "time_limit", default=1000),
                memory_mb=_fps_limit(item, "memory_limit", default=256, memory=True),
                source=_fps_text(item, "source") or None,
                problem_type="interactive" if interactor else "default",
                checker=checker,
                interactor=interactor,
                attachments=attachments,
                templates=templates,
                solutions=solutions,
                extra={"fps_version": version},
            )
        )
    if not bundle.problems:
        raise ValueError("FPS package contains no <item>")
    bundle.problems = _filter_problems(bundle.problems, only)
    return bundle


def _format_value(document: dict[str, Any], key: str) -> tuple[str, str]:
    value = document.get(key)
    if isinstance(value, dict):
        return str(value.get("format") or "html"), str(value.get("value") or "")
    return "html", str(value or "")


def read_qduoj(
    root: Path,
    workspace: Path,
    only: Iterable[str],
    budget: bridge.ArchiveExtractionBudget | None = None,
    reporter: ProgressReporter | None = None,
) -> ProblemBundle:
    documents = [
        path
        for path in sorted(root.rglob("problem.json"))
        if path.parent.name.isdigit() and (path.parent / "testcase").is_dir()
    ]
    if not documents:
        raise ValueError("no QDUOJ native problem export found")
    problems: list[Problem] = []
    bundle = ProblemBundle("qduoj", problems)
    for index, document_path in enumerate(documents, start=1):
        document = load_json_file(document_path)
        slug = _safe_name(document.get("display_id"), f"problem-{index}")
        title = str(document.get("title") or slug)
        statement_parts = []
        statement_format = "html"
        for heading, key in (
            ("Description", "description"),
            ("Input", "input_description"),
            ("Output", "output_description"),
            ("Hint", "hint"),
        ):
            value_format, value = _format_value(document, key)
            if value:
                statement_format = value_format
                marker = (
                    f"## {heading}"
                    if value_format == "markdown"
                    else f"<h2>{heading}</h2>"
                )
                statement_parts.append(f"{marker}\n{value}")
        statements = (
            [
                Statement(
                    "und",
                    "markdown" if statement_format == "markdown" else "html",
                    content="\n\n".join(statement_parts),
                )
            ]
            if statement_parts
            else []
        )
        testcase_dir = document_path.parent / "testcase"
        cases: list[TestCase] = []
        raw_scores = document.get("test_case_score")
        if not isinstance(raw_scores, list):
            raw_scores = []
        for case_index, raw in enumerate(raw_scores, start=1):
            if not isinstance(raw, dict):
                continue
            input_name = str(raw.get("input_name") or "")
            output_name = str(raw.get("output_name") or "")
            if not input_name or not output_name:
                bundle.add_issue(
                    "fatal",
                    "qduoj-unpaired-test",
                    "QDUOJ test case is missing input/output",
                    problem=slug,
                )
                continue
            input_path = bridge._safe_join(testcase_dir, input_name)
            output_path = bridge._safe_join(testcase_dir, output_name)
            if not input_path.is_file() or not output_path.is_file():
                bundle.add_issue(
                    "fatal",
                    "qduoj-missing-test",
                    f"missing QDUOJ test files for {input_name}",
                    problem=slug,
                )
                continue
            cases.append(
                TestCase(
                    str(case_index), input_path, output_path, score=raw.get("score")
                )
            )
        samples = document.get("samples")
        if isinstance(samples, list):
            for sample_index, raw in enumerate(samples, start=1):
                if not isinstance(raw, dict):
                    continue
                input_path = _materialize_text(
                    workspace / "qduoj" / slug / "sample" / f"{sample_index}.in",
                    str(raw.get("input") or ""),
                )
                output_path = _materialize_text(
                    workspace / "qduoj" / slug / "sample" / f"{sample_index}.out",
                    str(raw.get("output") or ""),
                )
                cases.append(
                    TestCase(f"sample-{sample_index}", input_path, output_path, True, 0)
                )
        raw_spj = document.get("spj")
        checker = None
        if isinstance(raw_spj, dict) and raw_spj.get("code"):
            checker = Program(
                "checker",
                language=str(raw_spj.get("language") or "") or None,
                mode="custom",
                content=str(raw_spj["code"]),
            )
        templates: dict[str, str] = {}
        raw_templates = document.get("template")
        if isinstance(raw_templates, dict):
            for language, raw in raw_templates.items():
                if isinstance(raw, dict):
                    templates[str(language)] = "".join(
                        str(raw.get(key) or "")
                        for key in ("prepend", "template", "append")
                    )
        solutions = []
        for raw in (
            document.get("answers") if isinstance(document.get("answers"), list) else []
        ):
            if isinstance(raw, dict) and raw.get("code"):
                solutions.append(
                    Program(
                        "solution",
                        language=str(raw.get("language") or "") or None,
                        mode="accepted",
                        content=str(raw["code"]),
                    )
                )
        tags = (
            [str(value) for value in document.get("tags", [])]
            if isinstance(document.get("tags"), list)
            else []
        )
        groups = []
        if str(document.get("rule_type")) == "OI":
            groups = [
                TestGroup(str(i), (case.name,), case.score)
                for i, case in enumerate(cases, start=1)
                if not case.sample
            ]
        problems.append(
            Problem(
                id=str(document.get("display_id") or slug),
                slug=slug,
                title=title,
                statements=statements,
                cases=cases,
                groups=groups,
                time_ms=max(1, int(document.get("time_limit") or 1000)),
                memory_mb=max(1, int(document.get("memory_limit") or 256)),
                tags=tags,
                source=str(document.get("source")) if document.get("source") else None,
                checker=checker,
                templates=templates,
                solutions=solutions,
                extra={"qduoj_rule_type": str(document.get("rule_type") or "ACM")},
            )
        )
    bundle.problems = _filter_problems(problems, only)
    return bundle


def _read_uoj_conf(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        read_limited_text(path).splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition(" ")
        if not separator or not key or not value.strip():
            raise ValueError(f"invalid problem.conf line {line_number}")
        if key in result:
            raise ValueError(f"duplicate problem.conf key: {key}")
        result[key] = value.strip()
    return result


def _positive_conf_int(conf: dict[str, str], key: str, default: int = 0) -> int:
    try:
        value = int(conf.get(key, str(default)))
    except ValueError as exc:
        raise ValueError(f"invalid integer in problem.conf: {key}") from exc
    if value < 0 or value > 100_000:
        raise ValueError(f"problem.conf value out of range: {key}")
    return value


def _sidecar_statement(problem_root: Path) -> list[Statement]:
    for pattern, format_name in (
        ("statement.md", "markdown"),
        ("problem.md", "markdown"),
        ("*.statement.md", "markdown"),
        ("statement.html", "html"),
        ("problem.html", "html"),
        ("*.statement.html", "html"),
    ):
        paths = (
            [problem_root / pattern]
            if "*" not in pattern
            else sorted(problem_root.glob(pattern))
        )
        for path in paths:
            if path.is_file():
                return [Statement("und", format_name, content=read_limited_text(path))]  # type: ignore[arg-type]
    for pattern in ("statement.pdf", "problem.pdf", "*.statement.pdf"):
        paths = (
            [problem_root / pattern]
            if "*" not in pattern
            else sorted(problem_root.glob(pattern))
        )
        for path in paths:
            if path.is_file():
                return [Statement("und", "pdf", path=path)]
    return []


def _sidecar_metadata(problem_root: Path) -> dict[str, Any]:
    candidates = [
        problem_root / "metadata.json",
        *sorted(problem_root.glob("*.metadata.json")),
    ]
    path = next((path for path in candidates if path.is_file()), None)
    return load_json_file(path) if path is not None else {}


def read_uoj(
    root: Path,
    workspace: Path,
    only: Iterable[str],
    budget: bridge.ArchiveExtractionBudget | None = None,
    reporter: ProgressReporter | None = None,
) -> ProblemBundle:
    conf_paths = sorted(root.rglob("problem.conf"))
    if not conf_paths:
        raise ValueError("no UOJ problem.conf found")
    bundle = ProblemBundle("uoj", [])
    for index, conf_path in enumerate(conf_paths, start=1):
        problem_root = conf_path.parent
        conf = _read_uoj_conf(conf_path)
        metadata = _sidecar_metadata(problem_root)
        slug = _safe_name(metadata.get("id") or problem_root.name, f"problem-{index}")
        statements = _sidecar_statement(problem_root)
        if not statements:
            statements = [
                Statement(
                    "und",
                    "markdown",
                    content=f"# {slug}\n\n> 原 UOJ 数据包不包含题面，请在导入后补充。\n",
                )
            ]
            bundle.add_issue(
                "loss",
                "uoj-missing-statement",
                "UOJ data package has no statement; generated a placeholder",
                problem=slug,
                field="statements",
            )
        if conf.get("use_builtin_judger", "on").lower() not in {
            "on",
            "1",
            "true",
            "yes",
        }:
            bundle.add_issue(
                "fatal",
                "uoj-custom-judger",
                "UOJ custom judger configurations are not supported",
                problem=slug,
                field="judge",
            )
        input_pre = conf.get("input_pre", "input")
        input_suf = conf.get("input_suf", "txt")
        output_pre = conf.get("output_pre", "output")
        output_suf = conf.get("output_suf", "txt")
        cases: list[TestCase] = []
        n_tests = _positive_conf_int(conf, "n_tests")
        for case_index in range(1, n_tests + 1):
            input_path = problem_root / f"{input_pre}{case_index}.{input_suf}"
            output_path = problem_root / f"{output_pre}{case_index}.{output_suf}"
            if not input_path.is_file() or not output_path.is_file():
                bundle.add_issue(
                    "fatal",
                    "uoj-missing-test",
                    f"missing UOJ test #{case_index}",
                    problem=slug,
                    field="cases",
                )
                continue
            score_text = conf.get(f"point_score_{case_index}")
            score = float(score_text) if score_text is not None else None
            cases.append(
                TestCase(str(case_index), input_path, output_path, score=score)
            )
        n_extra = _positive_conf_int(conf, "n_ex_tests")
        n_samples = _positive_conf_int(conf, "n_sample_tests")
        for case_index in range(1, n_extra + 1):
            input_path = problem_root / f"ex_{input_pre}{case_index}.{input_suf}"
            output_path = problem_root / f"ex_{output_pre}{case_index}.{output_suf}"
            if input_path.is_file() and output_path.is_file():
                cases.append(
                    TestCase(
                        f"extra-{case_index}",
                        input_path,
                        output_path,
                        case_index <= n_samples,
                        0,
                    )
                )
            else:
                bundle.add_issue(
                    "fatal",
                    "uoj-missing-extra-test",
                    f"missing UOJ extra test #{case_index}",
                    problem=slug,
                    field="cases",
                )
        groups: list[TestGroup] = []
        n_subtasks = _positive_conf_int(conf, "n_subtasks")
        previous_end = 0
        for group_index in range(1, n_subtasks + 1):
            end = _positive_conf_int(conf, f"subtask_end_{group_index}")
            if end <= previous_end or end > n_tests:
                bundle.add_issue(
                    "fatal",
                    "uoj-invalid-subtask",
                    f"invalid UOJ subtask #{group_index}",
                    problem=slug,
                    field="groups",
                )
                continue
            score_text = conf.get(f"subtask_score_{group_index}")
            score = float(score_text) if score_text is not None else None
            names = tuple(str(value) for value in range(previous_end + 1, end + 1))
            groups.append(TestGroup(str(group_index), names, score))
            previous_end = end
        checker = None
        builtin_checker = conf.get("use_builtin_checker")
        custom_checker = next(
            (
                problem_root / name
                for name in ("chk.cpp", "chk.cc", "chk.c")
                if (problem_root / name).is_file()
            ),
            None,
        )
        if custom_checker:
            checker = _program_from_path("checker", custom_checker, mode="testlib")
        elif builtin_checker:
            checker = Program("checker", mode=f"uoj-builtin:{builtin_checker}")
        validator_path = next(
            (
                problem_root / name
                for name in ("val.cpp", "val.cc", "val.c")
                if (problem_root / name).is_file()
            ),
            None,
        )
        bundle.problems.append(
            Problem(
                id=slug,
                slug=slug,
                title=str(metadata.get("title") or slug),
                statements=statements,
                cases=cases,
                groups=groups,
                time_ms=max(1, _positive_conf_int(conf, "time_limit", 1) * 1000),
                memory_mb=max(1, _positive_conf_int(conf, "memory_limit", 256)),
                tags=[str(value) for value in metadata.get("tags", [])]
                if isinstance(metadata.get("tags"), list)
                else [],
                source=str(metadata.get("source")) if metadata.get("source") else None,
                checker=checker,
                validator=_program_from_path("validator", validator_path)
                if validator_path
                else None,
                extra={"uoj_conf": conf},
            )
        )
    bundle.problems = _filter_problems(bundle.problems, only)
    return bundle


def _dmoj_case_entries(
    raw_cases: Any,
) -> list[tuple[dict[str, Any], str | None, int | float | None, tuple[str, ...]]]:
    result: list[
        tuple[dict[str, Any], str | None, int | float | None, tuple[str, ...]]
    ] = []
    if not isinstance(raw_cases, list):
        return result
    for group_index, raw in enumerate(raw_cases, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"DMOJ test case #{group_index} must be a mapping")
        if isinstance(raw.get("batched"), list):
            group_name = str(group_index)
            raw_dependencies = raw.get("dependencies", [])
            if not isinstance(raw_dependencies, list) or any(
                type(value) is not int or value < 1 or value >= group_index
                for value in raw_dependencies
            ):
                raise ValueError(f"DMOJ batch #{group_index} has invalid dependencies")
            dependencies = tuple(str(value) for value in raw_dependencies)
            for child in raw["batched"]:
                if not isinstance(child, dict):
                    raise ValueError(
                        f"DMOJ batch #{group_index} contains a non-mapping case"
                    )
                result.append((child, group_name, raw.get("points"), dependencies))
        else:
            result.append((raw, None, raw.get("points"), ()))
    return result


@contextmanager
def _bounded_dmoj_regex_matching() -> Iterable[None]:
    if threading.current_thread() is not threading.main_thread() or not hasattr(
        signal, "setitimer"
    ):
        raise ValueError(
            "DMOJ regex test matching requires a main-thread POSIX converter"
        )

    def timeout_handler(signum: int, frame: Any) -> None:
        raise TimeoutError("DMOJ test-case regex matching timed out")

    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, timeout_handler)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, DMOJ_REGEX_TIMEOUT_SECONDS)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)


def _dmoj_regex_pattern(raw: Any, default: str, name: str) -> re.Pattern[str]:
    pattern = default if raw is None else raw
    if (
        not isinstance(pattern, str)
        or not pattern
        or len(pattern) > MAX_DMOJ_REGEX_LENGTH
    ):
        raise ValueError(f"invalid DMOJ {name}")
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"invalid DMOJ {name}: {exc}") from exc
    if "case" not in compiled.groupindex:
        raise ValueError(f"DMOJ {name} must define a named 'case' group")
    return compiled


def _dmoj_sort_key(value: str | int) -> tuple[bool, str | int]:
    return isinstance(value, int), value


def _dmoj_match_value(match: re.Match[str], name: str) -> str | int | None:
    try:
        value = match.group(name)
    except IndexError:
        return None
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return value


def _dmoj_regex_points(raw: Any, count: int, default: int | float) -> list[int | float]:
    if raw is None:
        return [default] * count
    if not isinstance(raw, list) or len(raw) < count:
        raise ValueError("DMOJ case_points must provide a score for every matched case")
    points: list[int | float] = []
    for value in raw[:count]:
        if type(value) not in {int, float}:
            raise ValueError("DMOJ case_points must contain only numbers")
        points.append(value)
    return points


def _dmoj_cases_from_regex(
    data_root: Path, raw: dict[str, Any] | None, config: dict[str, Any]
) -> tuple[list[TestCase], list[TestGroup]]:
    raw = raw or {}
    input_pattern = _dmoj_regex_pattern(
        raw.get("input_format"), DMOJ_DEFAULT_INPUT_PATTERN, "input_format"
    )
    output_pattern = _dmoj_regex_pattern(
        raw.get("output_format"), DMOJ_DEFAULT_OUTPUT_PATTERN, "output_format"
    )
    files = [
        path
        for path in sorted(data_root.rglob("*"))
        if path.is_file() and path.name != "init.yml"
    ]
    matched: dict[str | int, dict[str | int, dict[str, Path]]] = {}
    batch_ids: set[str | int] = set()
    with _bounded_dmoj_regex_matching():
        for role, pattern in (("input", input_pattern), ("output", output_pattern)):
            for path in files:
                relative = path.relative_to(data_root).as_posix()
                match = pattern.match(relative)
                if match is None:
                    continue
                case = _dmoj_match_value(match, "case")
                batch = _dmoj_match_value(match, "batch")
                if case is None:
                    raise ValueError(
                        "DMOJ test-case regex matched without a case identifier"
                    )
                if batch is not None:
                    batch_ids.add(batch)
                else:
                    batch = case
                slot = matched.setdefault(batch, {}).setdefault(case, {})
                if role in slot:
                    raise ValueError(
                        f"DMOJ regex maps multiple {role} files to batch {batch}, case {case}"
                    )
                slot[role] = path
    ordered_batches = sorted(matched, key=_dmoj_sort_key)
    points = _dmoj_regex_points(
        raw.get("case_points"),
        len(ordered_batches),
        config.get("points", 1),
    )
    cases: list[TestCase] = []
    groups: list[TestGroup] = []
    used_names: set[str] = set()
    for index, batch in enumerate(ordered_batches):
        children = matched[batch]
        child_names: list[str] = []
        for case, pair in sorted(
            children.items(), key=lambda item: _dmoj_sort_key(item[0])
        ):
            if "input" not in pair or "output" not in pair:
                raise ValueError(
                    f"DMOJ regex produced an unpaired test for batch {batch}, case {case}"
                )
            base = _safe_name(f"{batch}-{case}" if batch in batch_ids else str(case))
            name = _unique_output_stem(base, used_names)
            child_names.append(name)
            cases.append(
                TestCase(
                    name,
                    pair["input"],
                    pair["output"],
                    score=None if batch in batch_ids else points[index],
                    group=str(batch) if batch in batch_ids else None,
                )
            )
        if batch in batch_ids:
            groups.append(TestGroup(str(batch), tuple(child_names), points[index]))
    return cases, groups


def read_dmoj(
    root: Path,
    workspace: Path,
    only: Iterable[str],
    budget: bridge.ArchiveExtractionBudget | None = None,
    reporter: ProgressReporter | None = None,
) -> ProblemBundle:
    init_paths = sorted(root.rglob("init.yml"))
    if not init_paths:
        raise ValueError("no DMOJ init.yml found")
    bundle = ProblemBundle("dmoj", [])
    for index, init_path in enumerate(init_paths, start=1):
        problem_root = init_path.parent
        config = load_yaml_file(init_path)
        metadata = _sidecar_metadata(problem_root)
        slug = _safe_name(metadata.get("code") or problem_root.name, f"problem-{index}")
        data_root = problem_root
        archive_name = config.get("archive")
        if isinstance(archive_name, str):
            archive_path = bridge._safe_join(problem_root, archive_name)
            if not archive_path.is_file() or not zipfile.is_zipfile(archive_path):
                raise ValueError(f"invalid DMOJ archive: {archive_name}")
            data_root = workspace / "dmoj" / slug / "archive"
            bridge._safe_extract_zip(archive_path, data_root, budget=budget)
        statements = _sidecar_statement(problem_root)
        if not statements:
            statements = [
                Statement(
                    "und",
                    "markdown",
                    content=f"# {slug}\n\n> 原 DMOJ 评测数据不包含题面，请在导入后补充。\n",
                )
            ]
            bundle.add_issue(
                "loss",
                "dmoj-missing-statement",
                "DMOJ judge-data package has no statement; generated a placeholder",
                problem=slug,
                field="statements",
            )
        cases: list[TestCase] = []
        groups: list[TestGroup] = []
        raw_test_cases = config.get("test_cases")
        if isinstance(raw_test_cases, list):
            groups_by_name: dict[str, dict[str, Any]] = {}
            try:
                entries = _dmoj_case_entries(raw_test_cases)
            except ValueError as exc:
                bundle.add_issue(
                    "fatal",
                    "dmoj-invalid-test-list",
                    str(exc),
                    problem=slug,
                    field="cases",
                )
                entries = []
            for case_index, (raw, group, inherited_score, dependencies) in enumerate(
                entries, start=1
            ):
                input_name = raw.get("in")
                output_name = raw.get("out")
                if not isinstance(input_name, str) or not isinstance(output_name, str):
                    bundle.add_issue(
                        "fatal",
                        "dmoj-unpaired-test",
                        f"DMOJ case #{case_index} lacks in/out",
                        problem=slug,
                    )
                    continue
                input_path = bridge._safe_join(data_root, input_name)
                output_path = bridge._safe_join(data_root, output_name)
                if not input_path.is_file() or not output_path.is_file():
                    bundle.add_issue(
                        "fatal",
                        "dmoj-missing-test",
                        f"missing DMOJ case files for {input_name}",
                        problem=slug,
                    )
                    continue
                name = _safe_name(Path(input_name).stem, str(case_index))
                score = raw.get("points", inherited_score)
                cases.append(
                    TestCase(name, input_path, output_path, score=score, group=group)
                )
                if group:
                    state = groups_by_name.setdefault(
                        group,
                        {
                            "cases": [],
                            "score": inherited_score,
                            "dependencies": dependencies,
                        },
                    )
                    state["cases"].append(name)
            groups = [
                TestGroup(name, tuple(raw["cases"]), raw["score"], raw["dependencies"])
                for name, raw in groups_by_name.items()
            ]
        elif raw_test_cases is None or isinstance(raw_test_cases, dict):
            if not isinstance(archive_name, str):
                bundle.add_issue(
                    "fatal",
                    "dmoj-regex-requires-archive",
                    "DMOJ regex/default test matching requires an archive",
                    problem=slug,
                    field="cases",
                )
            else:
                try:
                    cases, groups = _dmoj_cases_from_regex(
                        data_root, raw_test_cases, config
                    )
                except (TimeoutError, ValueError) as exc:
                    bundle.add_issue(
                        "fatal",
                        "dmoj-invalid-test-regex",
                        str(exc),
                        problem=slug,
                        field="cases",
                    )
                else:
                    bundle.add_issue(
                        "warning",
                        "dmoj-regex-cases",
                        "DMOJ regex/default test matching was normalized to an explicit case list",
                        problem=slug,
                        field="cases",
                    )
        else:
            bundle.add_issue(
                "fatal",
                "dmoj-invalid-test-cases",
                "DMOJ test_cases must be a list, regex mapping, or null",
                problem=slug,
                field="cases",
            )
        checker = None
        checker_conf = config.get("checker")
        if isinstance(checker_conf, dict) and checker_conf.get("name") == "bridged":
            args = (
                checker_conf.get("args")
                if isinstance(checker_conf.get("args"), dict)
                else {}
            )
            raw_files = args.get("files")
            files = [raw_files] if isinstance(raw_files, str) else raw_files
            if (
                not isinstance(files, list)
                or not files
                or not all(isinstance(name, str) for name in files)
            ):
                bundle.add_issue(
                    "fatal",
                    "dmoj-missing-checker",
                    "DMOJ bridged checker references no source files",
                    problem=slug,
                    field="checker",
                )
            elif len(files) != 1:
                bundle.add_issue(
                    "fatal",
                    "dmoj-multifile-checker",
                    "multi-file DMOJ bridged checkers cannot be represented safely by the current intermediate model",
                    problem=slug,
                    field="checker",
                )
            else:
                source = bridge._safe_join(problem_root, files[0])
                if source.is_file():
                    checker = Program(
                        "checker",
                        language=str(args.get("lang") or "")
                        or _language_from_source(source),
                        mode=str(args.get("type") or "bridged"),
                        path=source,
                    )
                else:
                    bundle.add_issue(
                        "fatal",
                        "dmoj-missing-checker",
                        "DMOJ bridged checker references no existing source file",
                        problem=slug,
                        field="checker",
                    )
        elif isinstance(checker_conf, dict):
            checker_name = checker_conf.get("name")
            checker_args = checker_conf.get("args")
            if not isinstance(checker_name, str) or not checker_name:
                bundle.add_issue(
                    "fatal",
                    "dmoj-invalid-checker",
                    "DMOJ checker mapping has no valid name",
                    problem=slug,
                    field="checker",
                )
            elif checker_args:
                bundle.add_issue(
                    "fatal",
                    "dmoj-checker-arguments",
                    "DMOJ builtin checker arguments cannot be preserved safely",
                    problem=slug,
                    field="checker",
                )
            else:
                checker = Program("checker", mode=f"dmoj-builtin:{checker_name}")
        elif isinstance(checker_conf, str):
            if "." in checker_conf:
                bundle.add_issue(
                    "fatal",
                    "dmoj-python-checker",
                    "DMOJ Python checker modules are not portable; use a bridged checker source instead",
                    problem=slug,
                    field="checker",
                )
            else:
                checker = Program("checker", mode=f"dmoj-builtin:{checker_conf}")
        elif checker_conf is not None:
            bundle.add_issue(
                "fatal",
                "dmoj-invalid-checker",
                "DMOJ checker must be a builtin name or checker mapping",
                problem=slug,
                field="checker",
            )
        interactor_path = None
        interactor_mode = None
        interactor_language = None
        interactive = config.get("interactive")
        if interactive is not None:
            if not isinstance(interactive, dict):
                bundle.add_issue(
                    "fatal",
                    "dmoj-invalid-interactor",
                    "DMOJ interactive configuration must be a mapping",
                    problem=slug,
                    field="interactor",
                )
            else:
                raw_files = interactive.get("files")
                files = [raw_files] if isinstance(raw_files, str) else raw_files
                if (
                    not isinstance(files, list)
                    or not files
                    or not all(isinstance(name, str) for name in files)
                ):
                    bundle.add_issue(
                        "fatal",
                        "dmoj-missing-interactor",
                        "DMOJ interactive configuration has no source files",
                        problem=slug,
                        field="interactor",
                    )
                elif len(files) != 1:
                    bundle.add_issue(
                        "fatal",
                        "dmoj-multifile-interactor",
                        "multi-file DMOJ interactors cannot be represented safely by the current intermediate model",
                        problem=slug,
                        field="interactor",
                    )
                else:
                    candidate = bridge._safe_join(problem_root, files[0])
                    if candidate.is_file():
                        interactor_path = candidate
                        interactor_mode = str(interactive.get("type") or "default")
                        interactor_language = str(
                            interactive.get("lang") or ""
                        ) or _language_from_source(candidate)
                    else:
                        bundle.add_issue(
                            "fatal",
                            "dmoj-missing-interactor",
                            f"DMOJ interactor file is missing: {candidate.name}",
                            problem=slug,
                            field="interactor",
                        )
        custom_judge = config.get("custom_judge")
        if custom_judge is not None:
            bundle.add_issue(
                "fatal",
                "dmoj-custom-judger",
                "DMOJ custom_judge Python graders are not supported because approximating them can change judging semantics",
                problem=slug,
                field="judge",
            )
        attachments: dict[str, Path] = {}
        generator = config.get("generator")
        if generator:
            generator_names: list[str] = []
            stack = [generator]
            while stack:
                value = stack.pop()
                if isinstance(value, str):
                    generator_names.append(value)
                elif isinstance(value, list):
                    stack.extend(value)
                elif isinstance(value, dict):
                    stack.extend(value.values())
            for name in generator_names:
                try:
                    source = bridge._safe_join(problem_root, name)
                except ValueError:
                    continue
                if source.is_file():
                    attachments[f"generator/{source.name}"] = source
            bundle.add_issue(
                "loss",
                "dmoj-generator",
                "DMOJ dynamic generator is preserved only as source metadata",
                problem=slug,
                field="generator",
            )
        bundle.problems.append(
            Problem(
                id=slug,
                slug=slug,
                title=str(metadata.get("title") or slug),
                statements=statements,
                cases=cases,
                groups=groups,
                time_ms=max(1, int(float(config.get("time_limit") or 1) * 1000)),
                memory_mb=max(1, int(config.get("memory_limit") or 256)),
                tags=[str(value) for value in metadata.get("tags", [])]
                if isinstance(metadata.get("tags"), list)
                else [],
                source=str(metadata.get("source")) if metadata.get("source") else None,
                problem_type="interactive" if interactor_path else "default",
                checker=checker,
                interactor=Program(
                    "interactor",
                    language=interactor_language,
                    mode=interactor_mode,
                    path=interactor_path,
                )
                if interactor_path
                else None,
                attachments=attachments,
                extra={"dmoj_config": config},
            )
        )
    bundle.problems = _filter_problems(bundle.problems, only)
    return bundle


def _generic_problem_roots(root: Path) -> list[Path]:
    direct_pair = any(
        path.is_file()
        and path.suffix.lower() in bridge.IN_SUFFIXES
        and bridge._find_matching_output_path(path) is not None
        for path in root.iterdir()
    )
    if direct_pair:
        return [root]
    candidates = [
        child
        for child in sorted(root.iterdir())
        if child.is_dir() and bridge._scan_case_pairs(child)
    ]
    if len(candidates) > 1:
        return candidates
    root_has_metadata = any(
        path.is_file()
        and (
            path.name.lower().startswith(("statement.", "problem."))
            or path.suffix.lower() in bridge.SOURCE_SUFFIXES
        )
        for path in root.iterdir()
    )
    if len(candidates) == 1 and not root_has_metadata:
        return candidates
    return [root] if bridge._scan_case_pairs(root) else []


def _unpaired_case_files(root: Path) -> list[str]:
    inputs = [
        path
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix.lower() in bridge.IN_SUFFIXES
    ]
    outputs = [
        path
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix.lower() in bridge.OUT_SUFFIXES
    ]
    outputs_by_stem: dict[str, list[Path]] = {}
    for output_path in outputs:
        key = output_path.relative_to(root).with_suffix("").as_posix().casefold()
        outputs_by_stem.setdefault(key, []).append(output_path)
    paired_outputs: set[Path] = set()
    unpaired = []
    for input_path in inputs:
        output_path = bridge._find_matching_output_path(input_path)
        if output_path is None:
            unpaired.append(input_path.relative_to(root).as_posix())
        else:
            paired_outputs.add(output_path.resolve())
            key = input_path.relative_to(root).with_suffix("").as_posix().casefold()
            aliases = outputs_by_stem.get(key, [])
            if all(_files_have_equal_contents(output_path, alias) for alias in aliases):
                paired_outputs.update(alias.resolve() for alias in aliases)
    unpaired.extend(
        path.relative_to(root).as_posix()
        for path in outputs
        if path.resolve() not in paired_outputs
    )
    return unpaired


def _files_have_equal_contents(left: Path, right: Path) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False
    with left.open("rb") as left_file, right.open("rb") as right_file:
        while True:
            left_chunk = left_file.read(1024 * 1024)
            right_chunk = right_file.read(1024 * 1024)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def _missing_partner_for_unpaired(root: Path, relative: str) -> tuple[Path, str]:
    path = bridge._safe_join(root, relative)
    if path.suffix.lower() in bridge.IN_SUFFIXES:
        return path.with_suffix(".ans"), "output"
    return path.with_suffix(".in"), "input"


def read_generic(
    root: Path,
    workspace: Path,
    only: Iterable[str],
    budget: bridge.ArchiveExtractionBudget | None = None,
    reporter: ProgressReporter | None = None,
) -> ProblemBundle:
    problem_roots = _generic_problem_roots(bridge._strip_single_root(root))
    if not problem_roots:
        raise ValueError(
            "generic package contains no paired .in/.out or .in/.ans files"
        )
    bundle = ProblemBundle("generic", [])
    for index, problem_root in enumerate(problem_roots, start=1):
        slug = _safe_name(problem_root.name, f"problem-{index}")
        unpaired = _unpaired_case_files(problem_root)
        if unpaired:
            bundle.add_issue(
                "fatal",
                "generic-unpaired-test",
                f"generic package contains unpaired test data: {unpaired[0]}",
                problem=slug,
                field="cases",
            )
        statements = _sidecar_statement(problem_root)
        if not statements:
            pdf = next(iter(sorted(problem_root.glob("*.pdf"))), None)
            if pdf:
                statements = [Statement("und", "pdf", path=pdf)]
            else:
                statements = [
                    Statement(
                        "und",
                        "markdown",
                        content=f"# {slug}\n\n> 通用数据包未提供题面，请在导入后补充。\n",
                    )
                ]
                bundle.add_issue(
                    "loss",
                    "generic-missing-statement",
                    "generic package has no statement; generated a placeholder",
                    problem=slug,
                    field="statements",
                )
        scanned = bridge._scan_case_pairs(problem_root)
        cases = [
            TestCase(case.name, case.input_path, case.output_path, case.sample)
            for case in scanned
        ]
        checker_path = next(
            (
                path
                for path in sorted(problem_root.rglob("*"))
                if path.is_file()
                and path.suffix.lower() in bridge.SOURCE_SUFFIXES
                and bridge._classify_source(path) == "checker"
            ),
            None,
        )
        bundle.add_issue(
            "warning",
            "generic-default-limits",
            "generic package has no portable limits; using 1 second and 256 MiB",
            problem=slug,
        )
        bundle.problems.append(
            Problem(
                id=slug,
                slug=slug,
                title=slug,
                statements=statements,
                cases=cases,
                checker=_program_from_path("checker", checker_path, mode="testlib")
                if checker_path
                else None,
            )
        )
    bundle.problems = _filter_problems(bundle.problems, only)
    return bundle


def _program_suffix(program: Program, default: str = ".cpp") -> str:
    if program.path and program.path.suffix:
        return program.path.suffix.lower()
    language = (program.language or "").lower()
    if language in {"c", "gcc"}:
        return ".c"
    if language in {"python", "python3", "py"}:
        return ".py"
    if language == "java":
        return ".java"
    return default


def _dmoj_program_language(program: Program) -> str | None:
    language = re.sub(r"[^a-z0-9+]", "", (program.language or "").lower())
    suffix = _program_suffix(program, default="")
    if language in {"c", "gcc", "c11"} or suffix == ".c":
        return "C11"
    if language in {"c++", "cpp", "cpp17", "cpp20", "g++"} or suffix in {
        ".cc",
        ".cpp",
        ".cxx",
    }:
        return "CPP20"
    if language in {"python", "python3", "py", "py3"} or suffix == ".py":
        return "PY3"
    if language == "java" or suffix == ".java":
        return "JAVA"
    if language == "kotlin" or suffix == ".kt":
        return "KOTLIN"
    if language in {"go", "golang"} or suffix == ".go":
        return "GO"
    if language == "rust" or suffix == ".rs":
        return "RUST"
    if language == "pascal" or suffix == ".pas":
        return "PAS"
    return None


def _write_program(program: Program, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    if program.path is not None:
        bridge._copy_file(program.path, target)
    elif program.content is not None:
        target.write_text(program.content, encoding="utf-8")
    else:
        raise ValueError(f"{program.kind} has neither a source path nor inline content")
    return target


def _uoj_builtin_checker_source(name: str) -> str | None:
    normalized = name.lower()
    if normalized == "wcmp":
        body = 'while (!ans.seekEof() && !ouf.seekEof()) if (ans.readToken() != ouf.readToken()) quitf(_wa, "token differs");'
    elif normalized == "ncmp":
        body = 'while (!ans.seekEof() && !ouf.seekEof()) if (ans.readLong() != ouf.readLong()) quitf(_wa, "integer differs");'
    elif normalized == "uncmp":
        body = 'std::vector<long long> a,b; while(!ans.seekEof()) a.push_back(ans.readLong()); while(!ouf.seekEof()) b.push_back(ouf.readLong()); std::sort(a.begin(),a.end()); std::sort(b.begin(),b.end()); if(a!=b) quitf(_wa, "multiset differs");'
    elif normalized == "yesno":
        body = 'while (!ans.seekEof() && !ouf.seekEof()) { std::string a=lowerCase(ans.readToken()), b=lowerCase(ouf.readToken()); if(a!="yes" && a!="no") quitf(_fail, "invalid jury YES/NO token"); if(b!="yes" && b!="no") quitf(_pe, "invalid participant YES/NO token"); if(a!=b) quitf(_wa, "YES/NO differs"); }'
    else:
        return None
    return (
        '#include "testlib.h"\n#include <algorithm>\n#include <cmath>\n#include <string>\n#include <vector>\n'
        "int main(int argc, char** argv) { registerTestlibCmd(argc, argv); "
        + body
        + ' if (!ans.seekEof() || !ouf.seekEof()) quitf(_wa, "length differs"); quitf(_ok, "accepted"); }\n'
    )


def _normalized_checker(bundle: ProblemBundle, problem: Problem) -> Program | None:
    checker = problem.checker
    if checker is None or checker.path is not None or checker.content is not None:
        return checker
    mode = checker.mode or ""
    if mode.startswith("uoj-builtin:"):
        name = mode.partition(":")[2]
        source = _uoj_builtin_checker_source(name)
        if source is None:
            bundle.add_issue(
                "fatal",
                "unsupported-uoj-checker",
                f"UOJ builtin checker is not supported: {name}",
                problem=problem.slug,
                field="checker",
            )
            return None
        return Program("checker", language="C++", mode="testlib", content=source)
    if mode.startswith("dmoj-builtin:"):
        name = mode.partition(":")[2]
        if name.lower() == "standard":
            return None
        bundle.add_issue(
            "fatal",
            "unsupported-dmoj-checker",
            f"DMOJ builtin checker is not supported: {name}",
            problem=problem.slug,
            field="checker",
        )
        return None
    return checker


def _unique_case_names(cases: list[TestCase]) -> list[tuple[TestCase, str]]:
    used: set[str] = set()
    result: list[tuple[TestCase, str]] = []
    for index, case in enumerate(cases, start=1):
        base = _safe_name(case.name, str(index))
        candidate = base
        suffix = 2
        while candidate.casefold() in used:
            candidate = f"{base}-{suffix}"
            suffix += 1
        used.add(candidate.casefold())
        result.append((case, candidate))
    return result


def _write_hydro_problem_dir(
    bundle: ProblemBundle,
    problem: Problem,
    target: Path,
    *,
    pid: str,
    owner: int,
    extra_tags: list[str],
) -> None:
    target.mkdir(parents=True, exist_ok=True)
    meta = {
        "title": problem.title,
        "pid": pid,
        "owner": owner,
        "tag": list(dict.fromkeys([*problem.tags, *extra_tags])),
    }
    if problem.source:
        meta["source"] = problem.source
    (target / "problem.yaml").write_text(bridge._dump_yaml(meta), encoding="utf-8")
    if not problem.statements:
        problem.statements.append(
            Statement(
                "und",
                "markdown",
                content=f"# {problem.title}\n\n> 转换源未提供题面，请在导入后补充。\n",
            )
        )
        bundle.add_issue(
            "loss",
            "missing-statement",
            "source package has no statement; generated a placeholder",
            problem=problem.slug,
            field="statements",
        )
    used_statement_languages: set[str] = set()
    used_pdf_names: set[str] = set()
    for statement_index, statement in enumerate(problem.statements, start=1):
        language = _safe_name(statement.language, "und").replace("-", "_")
        if language in used_statement_languages:
            language = f"{language}_{statement_index}"
        used_statement_languages.add(language)
        statement_path = target / f"problem_{language}.md"
        if statement.format == "pdf":
            if statement.path is None or not statement.path.is_file():
                bundle.add_issue(
                    "fatal",
                    "missing-pdf",
                    "PDF statement file is missing",
                    problem=problem.slug,
                )
                continue
            pdf_name = bridge._unique_safe_filename(
                statement.path.name,
                used_pdf_names,
                fallback=f"statement-{statement_index}.pdf",
            )
            bridge._copy_file(statement.path, target / "additional_file" / pdf_name)
            statement_path.write_text(f"@[pdf](file://{pdf_name})\n", encoding="utf-8")
        else:
            statement_path.write_text(statement.content or "", encoding="utf-8")

    testdata = target / "testdata"
    copied: dict[str, tuple[str, str]] = {}
    cases_config: list[dict[str, Any]] = []
    sample_dir = target / "additional_file" / "samples"
    for index, (case, name) in enumerate(_unique_case_names(problem.cases), start=1):
        if case.sample:
            input_name = f"{index}.in"
            output_name = f"{index}.out"
            bridge._copy_file(case.input_path, sample_dir / input_name)
            bridge._copy_file(case.output_path, sample_dir / output_name)
            continue
        input_name = f"{name}.in"
        output_name = f"{name}.ans"
        bridge._copy_file(case.input_path, testdata / input_name)
        bridge._copy_file(case.output_path, testdata / output_name)
        copied[case.name] = (input_name, output_name)
        entry: dict[str, Any] = {"input": input_name, "output": output_name}
        if case.score is not None and not problem.groups:
            entry["score"] = case.score
        cases_config.append(entry)

    config: dict[str, Any] = {
        "type": "interactive" if problem.problem_type == "interactive" else "default",
        "time": f"{max(1, problem.time_ms)}ms",
        "memory": f"{max(1, problem.memory_mb)}m",
    }
    if problem.file_io_base:
        config["filename"] = _safe_name(problem.file_io_base, "data")
    if problem.groups:
        subtasks = []
        grouped_names: set[str] = set()
        for group in problem.groups:
            raw_cases = []
            for case_name in group.cases:
                names = copied.get(case_name)
                if names:
                    raw_cases.append({"input": names[0], "output": names[1]})
                    grouped_names.add(case_name)
            if not raw_cases:
                continue
            raw_group: dict[str, Any] = {"cases": raw_cases}
            if group.score is not None:
                raw_group["score"] = group.score
            if group.dependencies:
                raw_group["dependencies"] = list(group.dependencies)
            subtasks.append(raw_group)
        config["subtasks"] = subtasks
        ungrouped = [
            entry
            for case, entry in zip(
                [c for c in problem.cases if not c.sample], cases_config
            )
            if case.name not in grouped_names
        ]
        if ungrouped:
            config["cases"] = ungrouped
    else:
        config["cases"] = cases_config

    checker = _normalized_checker(bundle, problem)
    if checker is not None:
        suffix = _program_suffix(checker)
        checker_name = f"checker{suffix}"
        _write_program(checker, testdata / checker_name)
        config["checker_type"] = (
            "testlib"
            if checker.mode in {"testlib", "custom", "bridged"}
            else checker.mode or "custom"
        )
        config["checker"] = {"file": checker_name, "lang": "auto"}
        if config["checker_type"] == "testlib":
            bridge._copy_packaged_testlib(testdata)
    if problem.interactor is not None:
        suffix = _program_suffix(problem.interactor)
        interactor_name = f"interactor{suffix}"
        _write_program(problem.interactor, testdata / interactor_name)
        config["interactor"] = interactor_name
        bridge._copy_packaged_testlib(testdata)
    if problem.validator is not None:
        suffix = _program_suffix(problem.validator)
        validator_name = f"validator{suffix}"
        _write_program(problem.validator, testdata / validator_name)
        config["validator"] = validator_name
        bridge._copy_packaged_testlib(testdata)
    for solution_index, solution in enumerate(problem.solutions, start=1):
        suffix = _program_suffix(solution)
        _write_program(solution, testdata / f"solution_{solution_index}{suffix}")
    if problem.templates:
        document = [
            {"language": language, "code": code}
            for language, code in sorted(problem.templates.items())
        ]
        template_path = target / "additional_file" / "code_templates.json"
        template_path.parent.mkdir(parents=True, exist_ok=True)
        template_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    for name, source in sorted(problem.attachments.items()):
        safe_target = bridge._safe_join(
            target / "additional_file" / "attachments", name
        )
        bridge._copy_file(source, safe_target)
    testdata.mkdir(parents=True, exist_ok=True)
    (testdata / "config.yaml").write_text(bridge._dump_yaml(config), encoding="utf-8")


def write_hydro(
    bundle: ProblemBundle, output: Path, options: dict[str, Any]
) -> list[str]:
    pid_start = str(options.get("pid_start") or "P1000")
    prefix, number, width = bridge._parse_pid_start(pid_start)
    owner = int(options.get("owner") or 1)
    extra_tags = [
        str(value).strip() for value in options.get("tags", []) if str(value).strip()
    ]
    artifacts: list[str] = []
    with tempfile.TemporaryDirectory(prefix="ir-to-hydro-") as temporary:
        root = Path(temporary)
        for index, problem in enumerate(bundle.problems, start=1):
            _report_problem_progress(
                options, index - 1, len(bundle.problems), problem.slug, "writing Hydro"
            )
            pid = f"{prefix}{number + index - 1:0{width}d}"
            package_root = root / f"{index:04d}"
            _write_hydro_problem_dir(
                bundle,
                problem,
                package_root / pid,
                pid=pid,
                owner=owner,
                extra_tags=extra_tags,
            )
            destination = output / f"{pid}-{_safe_name(problem.slug)}.zip"
            bridge._zip_dir(package_root, destination)
            artifacts.append(destination.name)
            _report_problem_progress(
                options, index, len(bundle.problems), problem.slug, "wrote Hydro"
            )
    return artifacts


def _normalized_statement_language(value: str) -> str:
    language = re.sub(r"-+", "-", value.lower().replace("_", "-"))
    language = re.sub(r"[^a-z0-9-]", "", language).strip("-")
    if language == "und" or not re.fullmatch(
        r"[a-z]{2,3}(?:-[a-z0-9]{1,8})*", language
    ):
        return "en"
    return language


def _unique_statement_language(statement: Statement, index: int, used: set[str]) -> str:
    base = _normalized_statement_language(statement.language)
    language = base
    duplicate = index
    while language in used:
        language = f"{base}-x-{duplicate}"
        duplicate += 1
    used.add(language)
    return language


def _statement_title(problem: Problem, statement: Statement, language: str) -> str:
    candidates = {
        statement.language.casefold(),
        statement.language.replace("_", "-").casefold(),
        language.casefold(),
    }
    for key, value in problem.titles.items():
        if (
            key.casefold() in candidates
            or key.replace("_", "-").casefold() in candidates
        ):
            return value
    return problem.title


def _write_icpc_2025_statements(package_dir: Path, problem: Problem) -> dict[str, str]:
    legacy_statement = package_dir / "problem_statement"
    if legacy_statement.exists():
        shutil.rmtree(legacy_statement)
    statement_dir = package_dir / "statement"
    statement_dir.mkdir(parents=True, exist_ok=True)
    used_languages: set[str] = set()
    titles: dict[str, str] = {}
    for index, statement in enumerate(problem.statements, start=1):
        language = _unique_statement_language(statement, index, used_languages)
        titles[language] = _statement_title(problem, statement, language)
        if statement.format == "pdf":
            if statement.path is None or not statement.path.is_file():
                raise ValueError("ICPC 2025-09 PDF statement file is missing")
            bridge._copy_file(statement.path, statement_dir / f"problem.{language}.pdf")
        else:
            body = (statement.content or "").replace(r"\end{verbatim}", "end verbatim")
            tex = (
                f"\\problemname{{{_latex_escape(titles[language])}}}\n"
                "\\begin{verbatim}\n"
                f"{body.rstrip()}\n"
                "\\end{verbatim}\n"
            )
            (statement_dir / f"problem.{language}.tex").write_text(
                tex, encoding="utf-8"
            )
    return titles


def _latex_escape(value: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "{": r"\{",
        "}": r"\}",
        "#": r"\#",
        "$": r"\$",
        "%": r"\%",
        "&": r"\&",
        "_": r"\_",
        "^": r"\^{}",
        "~": r"\~{}",
    }
    return "".join(replacements.get(char, char) for char in value)


def _write_icpc_legacy_statements(package_dir: Path, problem: Problem) -> None:
    statement_dir = package_dir / "problem_statement"
    if statement_dir.exists():
        shutil.rmtree(statement_dir)
    statement_dir.mkdir(parents=True)
    used: set[str] = set()
    for index, statement in enumerate(problem.statements, start=1):
        language = _unique_statement_language(statement, index, used)
        if statement.format == "pdf":
            if statement.path is None or not statement.path.is_file():
                raise ValueError("ICPC PDF statement file is missing")
            bridge._copy_file(statement.path, statement_dir / f"problem.{language}.pdf")
            continue
        body = (statement.content or "").replace(r"\end{verbatim}", "end verbatim")
        tex = (
            f"\\problemname{{{_latex_escape(problem.title)}}}\n"
            "\\begin{verbatim}\n"
            f"{body.rstrip()}\n"
            "\\end{verbatim}\n"
        )
        (statement_dir / f"problem.{language}.tex").write_text(tex, encoding="utf-8")


def _normalize_icpc_legacy(
    package_dir: Path, problem: Problem, options: dict[str, Any]
) -> None:
    _write_icpc_legacy_statements(package_dir, problem)
    meta = load_yaml_file(package_dir / "problem.yaml")
    # The selected profile stays within the legacy ICPC subset. Declare the
    # legacy superset because the current reference problemtools release does
    # not yet recognize the otherwise specification-valid "legacy-icpc"
    # version string.
    meta["problem_format_version"] = "legacy"
    meta["uuid"] = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"p2h:{problem.id}:{problem.title}")
    )
    meta["license"] = str(options.get("license") or "unknown")
    rights_owner = str(options.get("rights_owner") or "").strip()
    if rights_owner:
        meta["rights_owner"] = rights_owner
    (package_dir / "problem.yaml").write_text(bridge._dump_yaml(meta), encoding="utf-8")


def _upgrade_icpc_2025(
    package_dir: Path, problem: Problem, options: dict[str, Any]
) -> None:
    titles = _write_icpc_2025_statements(package_dir, problem)
    legacy_validators = package_dir / "output_validators"
    for name in ("checker", "interactor"):
        validator = legacy_validators / name
        if validator.exists():
            destination = package_dir / "output_validator"
            if destination.exists():
                shutil.rmtree(destination)
            validator.replace(destination)
            break
    if legacy_validators.exists():
        shutil.rmtree(legacy_validators)
    meta = {
        "problem_format_version": "2025-09",
        "name": titles,
        "uuid": str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"p2h:{problem.id}:{problem.title}")
        ),
        "type": "pass-fail",
        "source": problem.source or "converted by OJ Package Converter",
        "license": str(options.get("license") or "unknown"),
        "limits": {"time_limit": problem.time_ms / 1000, "memory": problem.memory_mb},
    }
    rights_owner = str(options.get("rights_owner") or "").strip()
    if rights_owner:
        meta["rights_owner"] = rights_owner
    (package_dir / "problem.yaml").write_text(bridge._dump_yaml(meta), encoding="utf-8")
    ini = package_dir / "domjudge-problem.ini"
    if ini.exists():
        ini.unlink()


def write_icpc(
    bundle: ProblemBundle, output: Path, options: dict[str, Any]
) -> list[str]:
    start_index = bridge._code_to_index(str(options.get("code_start") or "A"))
    color = str(options.get("color") or "#000000")
    profile = str(options.get("profile") or "legacy-icpc")
    if profile not in {"legacy-icpc", "2025-09"}:
        raise ValueError("ICPC profile must be legacy-icpc or 2025-09")
    artifacts: list[str] = []
    used_archive_names: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="ir-to-icpc-") as temporary:
        root = Path(temporary)
        for index, problem in enumerate(bundle.problems, start=1):
            _report_problem_progress(
                options, index - 1, len(bundle.problems), problem.slug, "writing ICPC"
            )
            if profile == "2025-09" and not any(
                (solution.mode or "").lower() == "accepted"
                for solution in problem.solutions
            ):
                bundle.add_issue(
                    "fatal",
                    "icpc-accepted-solution",
                    "ICPC 2025-09 requires at least one accepted submission",
                    problem=problem.slug,
                    field="solutions",
                )
                continue
            hydro_dir = root / "hydro" / f"H{index}"
            _write_hydro_problem_dir(
                bundle, problem, hydro_dir, pid=f"H{index}", owner=1, extra_tags=[]
            )
            package_dir = root / "icpc" / f"{index:04d}"
            code = bridge._index_to_code(start_index + index - 1)
            meta = load_yaml_file(hydro_dir / "problem.yaml")
            bridge._write_domjudge_problem(
                hydro_dir, package_dir, meta, problem.title, code, color, verbose=False
            )
            if profile == "2025-09":
                _upgrade_icpc_2025(package_dir, problem, options)
            else:
                _normalize_icpc_legacy(package_dir, problem, options)
            archive_name = _unique_output_stem(
                re.sub(r"[^a-z0-9]", "", problem.slug.lower()) or f"p{index}",
                used_archive_names,
                separator="",
            )
            destination = output / f"{archive_name}.zip"
            bridge._zip_dir(package_dir, destination)
            artifacts.append(destination.name)
            _report_problem_progress(
                options, index, len(bundle.problems), problem.slug, "wrote ICPC"
            )
    return artifacts


def write_hoj(
    bundle: ProblemBundle, output: Path, options: dict[str, Any]
) -> list[str]:
    artifacts: list[str] = []
    with tempfile.TemporaryDirectory(prefix="ir-to-hoj-") as temporary:
        root = Path(temporary)
        used: set[str] = set()
        for index, problem in enumerate(bundle.problems, start=1):
            _report_problem_progress(
                options, index - 1, len(bundle.problems), problem.slug, "writing HOJ"
            )
            hydro_dir = root / f"H{index}"
            _write_hydro_problem_dir(
                bundle,
                problem,
                hydro_dir,
                pid=problem.id or f"H{index}",
                owner=1,
                extra_tags=[],
            )
            key = hoj_bridge._unique_hoj_key(
                f"problem_{_safe_name(problem.slug)}", used
            )
            meta = load_yaml_file(hydro_dir / "problem.yaml")
            hoj_bridge._write_hydro_as_hoj(hydro_dir, output, key, meta, verbose=False)
            artifacts.extend([f"{key}.json", key])
            _report_problem_progress(
                options, index, len(bundle.problems), problem.slug, "wrote HOJ"
            )
    return artifacts


def _report_problem_progress(
    options: dict[str, Any],
    current: int,
    total: int,
    problem: str,
    detail: str,
) -> None:
    reporter = options.get("_progress")
    if reporter is not None and hasattr(reporter, "update"):
        reporter.update(
            current=current,
            total=total,
            unit="problems",
            problem=problem,
            detail=f"{detail}: {problem}",
        )


def _program_text(program: Program) -> str:
    if program.content is not None:
        return program.content
    if program.path is not None:
        return read_limited_text(program.path)
    return ""


def _statement_text(
    problem: Problem, bundle: ProblemBundle, target: str
) -> tuple[str, str]:
    text_statements = [
        statement
        for statement in problem.statements
        if statement.format in {"markdown", "html"}
    ]
    if text_statements:
        statement = text_statements[0]
        return statement.format, statement.content or ""
    if problem.statements:
        bundle.add_issue(
            "loss",
            f"{target}-pdf-statement",
            f"{target} output cannot embed a PDF statement as its native statement",
            problem=problem.slug,
            field="statements",
        )
    return (
        "markdown",
        f"# {problem.title}\n\n> 目标格式无法携带原题面，请在导入后补充。\n",
    )


def write_fps(
    bundle: ProblemBundle, output: Path, options: dict[str, Any]
) -> list[str]:
    profile = str(options.get("profile") or "hustoj-1.6")
    versions = {"hustoj-1.6": "1.6", "qduoj-1.2": "1.2"}
    if profile not in versions:
        raise ValueError("FPS profile must be hustoj-1.6 or qduoj-1.2")
    root = ElementTree.Element(
        "fps",
        {
            "version": versions[profile],
            "url": "https://github.com/zhblue/freeproblemset/",
        },
    )
    ElementTree.SubElement(
        root,
        "generator",
        {"name": "OJ Package Converter", "url": "https://github.com/"},
    )
    image_total = 0
    for problem in bundle.problems:
        item = ElementTree.SubElement(root, "item")
        ElementTree.SubElement(item, "title").text = problem.title
        ElementTree.SubElement(item, "time_limit", {"unit": "ms"}).text = str(
            problem.time_ms
        )
        ElementTree.SubElement(item, "memory_limit", {"unit": "mb"}).text = str(
            problem.memory_mb
        )
        statement_format, statement = _statement_text(problem, bundle, "FPS")
        ElementTree.SubElement(item, "description").text = statement
        ElementTree.SubElement(item, "input").text = ""
        ElementTree.SubElement(item, "output").text = ""
        samples = [case for case in problem.cases if case.sample]
        secret = [case for case in problem.cases if not case.sample]
        for case in samples:
            ElementTree.SubElement(item, "sample_input").text = read_limited_text(
                case.input_path, limit=bridge.DEFAULT_MAX_ARCHIVE_MEMBER_BYTES
            )
            ElementTree.SubElement(item, "sample_output").text = read_limited_text(
                case.output_path, limit=bridge.DEFAULT_MAX_ARCHIVE_MEMBER_BYTES
            )
        for case in secret:
            ElementTree.SubElement(
                item, "test_input", {"name": case.name}
            ).text = read_limited_text(
                case.input_path, limit=bridge.DEFAULT_MAX_ARCHIVE_MEMBER_BYTES
            )
            ElementTree.SubElement(
                item, "test_output", {"name": case.name}
            ).text = read_limited_text(
                case.output_path, limit=bridge.DEFAULT_MAX_ARCHIVE_MEMBER_BYTES
            )
        ElementTree.SubElement(item, "hint").text = ""
        ElementTree.SubElement(item, "source").text = problem.source or ""
        ElementTree.SubElement(item, "remote_oj").text = bundle.source_format
        ElementTree.SubElement(item, "remote_id").text = problem.id
        checker = _normalized_checker(bundle, problem)
        if checker is not None:
            ElementTree.SubElement(
                item, "spj", {"language": checker.language or "C++"}
            ).text = _program_text(checker)
        if problem.interactor is not None:
            if profile == "qduoj-1.2":
                bundle.add_issue(
                    "fatal",
                    "qduoj-fps-interactor",
                    "QDUOJ FPS 1.2 does not support interactors",
                    problem=problem.slug,
                    field="interactor",
                )
            else:
                ElementTree.SubElement(
                    item,
                    "interactor",
                    {"language": problem.interactor.language or "C++"},
                ).text = _program_text(problem.interactor)
        for language, code in sorted(problem.templates.items()):
            ElementTree.SubElement(item, "template", {"language": language}).text = code
        for solution in problem.solutions:
            ElementTree.SubElement(
                item, "solution", {"language": solution.language or "unknown"}
            ).text = _program_text(solution)
        for name, image_path in sorted(problem.attachments.items()):
            if image_path.suffix.lower() not in {
                ".png",
                ".jpg",
                ".jpeg",
                ".gif",
                ".bmp",
                ".svg",
                ".webp",
            }:
                continue
            data = image_path.read_bytes()
            image_total += len(data)
            if image_total > MAX_EMBEDDED_IMAGE_BYTES:
                raise ValueError(
                    f"FPS embedded images exceed {MAX_EMBEDDED_IMAGE_BYTES} bytes"
                )
            image = ElementTree.SubElement(item, "img")
            ElementTree.SubElement(image, "src").text = name
            ElementTree.SubElement(image, "base64").text = base64.b64encode(
                data
            ).decode("ascii")
    output_path = output / "problem.xml"
    xml_body = ElementTree.tostring(root, encoding="utf-8")
    doctype = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<!DOCTYPE fps PUBLIC "-//freeproblemset//An opensource XML standard for Algorithm Contest Problem Set//EN" '
        b'"http://hustoj.com/fps.current.dtd">\n'
    )
    output_path.write_bytes(doctype + xml_body + b"\n")
    return [output_path.name]


def _qduoj_statement_parts(
    problem: Problem, bundle: ProblemBundle
) -> dict[str, dict[str, str]]:
    statement_format, content = _statement_text(problem, bundle, "QDUOJ")
    format_name = "markdown" if statement_format == "markdown" else "html"
    return {
        "description": {"format": format_name, "value": content},
        "input_description": {"format": format_name, "value": ""},
        "output_description": {"format": format_name, "value": ""},
        "hint": {"format": format_name, "value": ""},
    }


def _qduoj_case_scores(
    bundle: ProblemBundle, problem: Problem, cases: list[TestCase]
) -> dict[str, int]:
    scores: dict[str, int] = {}
    for case in cases:
        if case.score is not None:
            scores[case.name] = max(1, int(round(float(case.score))))
    for group in problem.groups:
        unresolved = [name for name in group.cases if name not in scores]
        if not unresolved or group.score is None:
            continue
        total = max(len(unresolved), int(round(float(group.score))))
        base, remainder = divmod(total, len(unresolved))
        for index, name in enumerate(unresolved):
            scores[name] = max(1, base + (1 if index < remainder else 0))
        if len(group.cases) > 1 or group.dependencies:
            bundle.add_issue(
                "loss",
                "qduoj-group-flattened",
                "QDUOJ native format cannot preserve all-or-nothing groups/dependencies; scores were distributed",
                problem=problem.slug,
                field="groups",
            )
    return scores


def write_qduoj(
    bundle: ProblemBundle, output: Path, options: dict[str, Any]
) -> list[str]:
    artifacts: list[str] = []
    for index, problem in enumerate(bundle.problems, start=1):
        problem_root = output / str(index)
        testcase_dir = problem_root / "testcase"
        testcase_dir.mkdir(parents=True, exist_ok=True)
        secret = [case for case in problem.cases if not case.sample]
        samples = [case for case in problem.cases if case.sample]
        scores = _qduoj_case_scores(bundle, problem, secret)
        test_case_score = []
        for case_index, case in enumerate(secret, start=1):
            input_name = f"{case_index}.in"
            output_name = f"{case_index}.out"
            bridge._copy_file(case.input_path, testcase_dir / input_name)
            bridge._copy_file(case.output_path, testcase_dir / output_name)
            test_case_score.append(
                {
                    "score": scores.get(case.name, 100 if not problem.groups else 1),
                    "input_name": input_name,
                    "output_name": output_name,
                }
            )
        statement_parts = _qduoj_statement_parts(problem, bundle)
        checker = _normalized_checker(bundle, problem)
        document: dict[str, Any] = {
            "display_id": problem.id,
            "title": problem.title,
            **statement_parts,
            "tags": problem.tags,
            "test_case_score": test_case_score,
            "time_limit": problem.time_ms,
            "memory_limit": problem.memory_mb,
            "samples": [
                {
                    "input": read_limited_text(case.input_path),
                    "output": read_limited_text(case.output_path),
                }
                for case in samples
            ],
            "template": {
                language: {"prepend": "", "template": code, "append": ""}
                for language, code in sorted(problem.templates.items())
            },
            "spj": {
                "code": _program_text(checker),
                "language": checker.language or "C++",
            }
            if checker
            else None,
            "rule_type": "OI"
            if problem.groups or any(case.score is not None for case in secret)
            else "ACM",
            "source": problem.source or "",
            "answers": [
                {
                    "code": _program_text(solution),
                    "language": solution.language or "C++",
                }
                for solution in problem.solutions
            ],
        }
        if problem.problem_type == "interactive":
            bundle.add_issue(
                "fatal",
                "qduoj-interactive",
                "QDUOJ native export does not support interactive problems",
                problem=problem.slug,
                field="interactor",
            )
        if problem.file_io_base:
            bundle.add_issue(
                "loss",
                "qduoj-file-io",
                "QDUOJ official export schema does not include file IO metadata",
                problem=problem.slug,
                field="file_io",
            )
        json_path = problem_root / "problem.json"
        json_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        artifacts.extend([f"{index}/problem.json", f"{index}/testcase"])
    return artifacts


def _write_text_statement_sidecar(
    problem: Problem, bundle: ProblemBundle, path: Path, target: str
) -> Path:
    pdf = next(
        (
            statement.path
            for statement in problem.statements
            if statement.format == "pdf"
            and statement.path is not None
            and statement.path.is_file()
        ),
        None,
    )
    if pdf is not None:
        path = path.with_suffix(".pdf")
        bridge._copy_file(pdf, path)
        return path
    format_name, content = _statement_text(problem, bundle, target)
    if format_name == "html":
        path = path.with_suffix(".html")
    path.write_text(content, encoding="utf-8")
    return path


def _write_inert_payload(
    problem: Problem, root: Path, *, include_samples: bool
) -> None:
    for name, source in sorted(problem.attachments.items()):
        bridge._copy_file(source, bridge._safe_join(root / "attachments", name))
    if problem.templates:
        path = root / "sidecar" / "code_templates.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(problem.templates, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    for index, solution in enumerate(problem.solutions, start=1):
        suffix = _program_suffix(solution)
        _write_program(solution, root / "sidecar" / "solutions" / f"{index}{suffix}")
    if problem.validator is not None:
        suffix = _program_suffix(problem.validator)
        _write_program(problem.validator, root / "sidecar" / f"validator{suffix}")
    if include_samples:
        samples = [case for case in problem.cases if case.sample]
        for index, case in enumerate(samples, start=1):
            bridge._copy_file(
                case.input_path, root / "sidecar" / "samples" / f"{index}.in"
            )
            bridge._copy_file(
                case.output_path, root / "sidecar" / "samples" / f"{index}.out"
            )


def write_uoj(
    bundle: ProblemBundle, output: Path, options: dict[str, Any]
) -> list[str]:
    artifacts: list[str] = []
    used_slugs: set[str] = set()
    for problem in bundle.problems:
        slug = _unique_output_stem(_safe_name(problem.slug), used_slugs)
        with tempfile.TemporaryDirectory(prefix="ir-to-uoj-") as temporary:
            root = Path(temporary)
            secret = [case for case in problem.cases if not case.sample]
            samples = [case for case in problem.cases if case.sample]
            ordered: list[TestCase] = []
            group_ranges: list[tuple[TestGroup, int]] = []
            by_name = {case.name: case for case in secret}
            used: set[str] = set()
            for group in problem.groups:
                start = len(ordered)
                for name in group.cases:
                    if name in by_name and name not in used:
                        ordered.append(by_name[name])
                        used.add(name)
                if len(ordered) == start:
                    _loss(
                        bundle,
                        problem,
                        "uoj-empty-subtask",
                        f"UOJ output skipped subtask {group.name} because it has no unique cases",
                        "groups",
                    )
                    continue
                if len(ordered) - start < len(group.cases):
                    _loss(
                        bundle,
                        problem,
                        "uoj-overlapping-subtasks",
                        "UOJ contiguous subtask output cannot preserve overlapping case membership",
                        "groups",
                    )
                group_ranges.append((group, len(ordered)))
                if group.dependencies:
                    bundle.add_issue(
                        "loss",
                        "uoj-group-dependencies",
                        "UOJ output does not preserve cross-subtask dependencies",
                        problem=problem.slug,
                        field="groups",
                    )
            ordered.extend(case for case in secret if case.name not in used)
            input_pre = "input"
            output_pre = "output"
            conf = [
                f"n_tests {len(ordered)}",
                f"n_ex_tests {len(samples)}",
                f"n_sample_tests {len(samples)}",
                f"input_pre {input_pre}",
                "input_suf in",
                f"output_pre {output_pre}",
                "output_suf out",
                f"time_limit {max(1, (problem.time_ms + 999) // 1000)}",
                f"memory_limit {problem.memory_mb}",
                "output_limit 64",
                "use_builtin_judger on",
            ]
            checker = _normalized_checker(bundle, problem)
            if checker is None:
                conf.append("use_builtin_checker wcmp")
            else:
                _write_program(checker, root / f"chk{_program_suffix(checker)}")
            for case_index, case in enumerate(ordered, start=1):
                bridge._copy_file(case.input_path, root / f"{input_pre}{case_index}.in")
                bridge._copy_file(
                    case.output_path, root / f"{output_pre}{case_index}.out"
                )
                if case.score is not None and not problem.groups:
                    conf.append(
                        f"point_score_{case_index} {bridge._format_number(case.score)}"
                    )
            for case_index, case in enumerate(samples, start=1):
                bridge._copy_file(
                    case.input_path, root / f"ex_{input_pre}{case_index}.in"
                )
                bridge._copy_file(
                    case.output_path, root / f"ex_{output_pre}{case_index}.out"
                )
            if group_ranges:
                conf.append(f"n_subtasks {len(group_ranges)}")
                for group_index, (group, end) in enumerate(group_ranges, start=1):
                    conf.append(f"subtask_end_{group_index} {end}")
                    if group.score is not None:
                        conf.append(
                            f"subtask_score_{group_index} {bridge._format_number(group.score)}"
                        )
            if problem.validator:
                _write_program(
                    problem.validator, root / f"val{_program_suffix(problem.validator)}"
                )
            if problem.problem_type == "interactive":
                bundle.add_issue(
                    "fatal",
                    "uoj-interactive",
                    "generic UOJ traditional-problem output does not support interactors",
                    problem=problem.slug,
                    field="interactor",
                )
            _write_inert_payload(problem, root, include_samples=False)
            (root / "problem.conf").write_text("\n".join(conf) + "\n", encoding="utf-8")
            destination = output / f"{slug}.zip"
            bridge._zip_dir(root, destination)
        statement = _write_text_statement_sidecar(
            problem, bundle, output / f"{slug}.statement.md", "UOJ"
        )
        metadata = output / f"{slug}.metadata.json"
        metadata.write_text(
            json.dumps(
                {
                    "id": problem.id,
                    "title": problem.title,
                    "tags": problem.tags,
                    "source": problem.source,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        artifacts.extend([destination.name, statement.name, metadata.name])
    return artifacts


def write_dmoj(
    bundle: ProblemBundle, output: Path, options: dict[str, Any]
) -> list[str]:
    artifacts: list[str] = []
    used_slugs: set[str] = set()
    for problem in bundle.problems:
        slug = _unique_output_stem(_safe_name(problem.slug), used_slugs)
        with tempfile.TemporaryDirectory(prefix="ir-to-dmoj-") as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir()
            secret = [case for case in problem.cases if not case.sample]
            case_names: dict[str, tuple[str, str]] = {}
            for index, case in enumerate(secret, start=1):
                input_name = f"{index}.in"
                output_name = f"{index}.out"
                bridge._copy_file(case.input_path, data / input_name)
                bridge._copy_file(case.output_path, data / output_name)
                case_names[case.name] = (input_name, output_name)
            archive_path = root / "data.zip"
            bridge._zip_dir(data, archive_path)
            config: dict[str, Any] = {"archive": "data.zip", "test_cases": []}
            grouped: set[str] = set()
            group_positions = {
                group.name: index for index, group in enumerate(problem.groups, start=1)
            }
            for group in problem.groups:
                children = []
                for name in group.cases:
                    names = case_names.get(name)
                    if names:
                        children.append({"in": names[0], "out": names[1]})
                        grouped.add(name)
                if not children:
                    continue
                entry: dict[str, Any] = {"batched": children}
                if group.score is not None:
                    entry["points"] = group.score
                if group.dependencies:
                    entry["dependencies"] = [
                        group_positions[value] for value in group.dependencies
                    ]
                config["test_cases"].append(entry)
            for case in secret:
                if case.name in grouped:
                    continue
                names = case_names[case.name]
                entry = {"in": names[0], "out": names[1]}
                if case.score is not None:
                    entry["points"] = case.score
                config["test_cases"].append(entry)
            checker = _normalized_checker(bundle, problem)
            if checker is not None:
                checker_language = _dmoj_program_language(checker)
                if checker_language is None:
                    raise ValueError(
                        f"unsupported DMOJ checker language: {checker.language or _program_suffix(checker)}"
                    )
                checker_name = f"checker{_program_suffix(checker)}"
                _write_program(checker, root / checker_name)
                config["checker"] = {
                    "name": "bridged",
                    "args": {
                        "files": [checker_name],
                        "lang": checker_language,
                        "type": checker.mode or "testlib",
                    },
                }
            if problem.interactor is not None:
                interactor_language = _dmoj_program_language(problem.interactor)
                if interactor_language is None:
                    raise ValueError(
                        "unsupported DMOJ interactor language: "
                        f"{problem.interactor.language or _program_suffix(problem.interactor)}"
                    )
                interactor_name = f"interactor{_program_suffix(problem.interactor)}"
                _write_program(problem.interactor, root / interactor_name)
                config["interactive"] = {
                    "files": [interactor_name],
                    "lang": interactor_language,
                    "type": problem.interactor.mode or "default",
                    "unbuffered": True,
                }
            config["time_limit"] = max(0.001, problem.time_ms / 1000)
            config["memory_limit"] = problem.memory_mb
            (root / "init.yml").write_text(bridge._dump_yaml(config), encoding="utf-8")
            _write_inert_payload(problem, root, include_samples=True)
            shutil.rmtree(data)
            destination = output / f"{slug}.zip"
            bridge._zip_dir(root, destination)
        statement = _write_text_statement_sidecar(
            problem, bundle, output / f"{slug}.statement.md", "DMOJ"
        )
        metadata = output / f"{slug}.metadata.json"
        metadata.write_text(
            json.dumps(
                {
                    "code": problem.id,
                    "title": problem.title,
                    "tags": problem.tags,
                    "source": problem.source,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        artifacts.extend([destination.name, statement.name, metadata.name])
    return artifacts


def _registered_detection(format_id: str) -> Callable[[Path], Detection | None]:
    def detect(root: Path) -> Detection | None:
        return next(
            (item for item in detect_extracted(root) if item.format == format_id),
            None,
        )

    return detect


def _loss(
    bundle: ProblemBundle,
    problem: Problem,
    code: str,
    message: str,
    field: str,
) -> None:
    bundle.add_issue("loss", code, message, problem=problem.slug, field=field)


def _validate_hydro_target(bundle: ProblemBundle, options: dict[str, Any]) -> None:
    return None


def _validate_icpc_target(bundle: ProblemBundle, options: dict[str, Any]) -> None:
    profile = str(options.get("profile") or "legacy-icpc")
    for problem in bundle.problems:
        if problem.groups or any(
            case.score is not None for case in problem.cases if not case.sample
        ):
            _loss(
                bundle,
                problem,
                "icpc-scoring-normalized",
                "the current ICPC writer emits pass/fail data and cannot preserve scoring groups",
                "groups",
            )
        if problem.file_io_base:
            _loss(
                bundle,
                problem,
                "icpc-file-io",
                "ICPC output does not preserve source file-IO metadata",
                "file_io",
            )
        if problem.templates:
            _loss(
                bundle,
                problem,
                "icpc-templates",
                "ICPC output preserves templates only as attachments, not native templates",
                "templates",
            )
        if problem.tags:
            _loss(
                bundle,
                problem,
                "icpc-tags",
                "ICPC problem packages do not have a portable native tag field",
                "tags",
            )
        if profile == "legacy-icpc" and len(problem.statements) > 1:
            _loss(
                bundle,
                problem,
                "icpc-legacy-multilingual",
                "legacy ICPC output keeps statement files but has no portable multilingual title mapping",
                "statements",
            )


def _validate_hoj_target(bundle: ProblemBundle, options: dict[str, Any]) -> None:
    for problem in bundle.problems:
        if any(case.sample for case in problem.cases):
            _loss(
                bundle,
                problem,
                "hoj-samples",
                "HOJ export derives samples from statement markup and cannot preserve separate sample files exactly",
                "cases",
            )
        if any(group.dependencies for group in problem.groups):
            _loss(
                bundle,
                problem,
                "hoj-group-dependencies",
                "HOJ output cannot preserve subtask dependencies",
                "groups",
            )
        if problem.validator:
            _loss(
                bundle,
                problem,
                "hoj-validator",
                "HOJ native export does not preserve a separate input validator",
                "validator",
            )
        if problem.attachments:
            _loss(
                bundle,
                problem,
                "hoj-attachments",
                "HOJ native export cannot represent arbitrary attachments",
                "attachments",
            )
        if problem.solutions:
            _loss(
                bundle,
                problem,
                "hoj-solutions",
                "HOJ native export does not preserve accepted solutions",
                "solutions",
            )
        if len(problem.statements) > 1:
            _loss(
                bundle,
                problem,
                "hoj-multilingual-statement",
                "HOJ native export keeps only one statement language",
                "statements",
            )
        elif problem.statements:
            _loss(
                bundle,
                problem,
                "hoj-statement-normalization",
                "HOJ stores statement sections as HTML fields, so Markdown formatting and language metadata are normalized",
                "statements",
            )


def _validate_fps_target(bundle: ProblemBundle, options: dict[str, Any]) -> None:
    profile = str(options.get("profile") or "hustoj-1.6")
    for problem in bundle.problems:
        if problem.groups or any(
            case.score is not None for case in problem.cases if not case.sample
        ):
            _loss(
                bundle,
                problem,
                "fps-scoring",
                "FPS cannot preserve subtask dependencies or per-test scores",
                "groups",
            )
        if problem.file_io_base:
            _loss(
                bundle,
                problem,
                "fps-file-io",
                "FPS does not have portable file-IO metadata",
                "file_io",
            )
        if problem.validator:
            _loss(
                bundle,
                problem,
                "fps-validator",
                "FPS output does not preserve a separate input validator",
                "validator",
            )
        if any(
            path.suffix.lower()
            not in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".webp"}
            for path in problem.attachments.values()
        ):
            _loss(
                bundle,
                problem,
                "fps-non-image-attachments",
                "FPS output embeds images but cannot preserve other attachments",
                "attachments",
            )
        if len(problem.statements) > 1:
            _loss(
                bundle,
                problem,
                "fps-multilingual-statement",
                "FPS output keeps only one statement language",
                "statements",
            )
        if profile == "qduoj-1.2" and problem.interactor:
            bundle.add_issue(
                "fatal",
                "qduoj-fps-interactor",
                "QDUOJ FPS 1.2 does not support interactors",
                problem=problem.slug,
                field="interactor",
            )


def _validate_qduoj_target(bundle: ProblemBundle, options: dict[str, Any]) -> None:
    for problem in bundle.problems:
        if problem.problem_type == "interactive" or problem.interactor:
            bundle.add_issue(
                "fatal",
                "qduoj-interactive",
                "QDUOJ native export does not support interactive problems",
                problem=problem.slug,
                field="interactor",
            )
        if problem.validator:
            _loss(
                bundle,
                problem,
                "qduoj-validator",
                "QDUOJ native export does not preserve an input validator",
                "validator",
            )
        if problem.attachments:
            _loss(
                bundle,
                problem,
                "qduoj-attachments",
                "QDUOJ official export schema does not preserve arbitrary attachments",
                "attachments",
            )
        if len(problem.statements) > 1:
            _loss(
                bundle,
                problem,
                "qduoj-multilingual-statement",
                "QDUOJ native export keeps only one statement language",
                "statements",
            )


def _validate_uoj_target(bundle: ProblemBundle, options: dict[str, Any]) -> None:
    for problem in bundle.problems:
        if problem.problem_type == "interactive" or problem.interactor:
            bundle.add_issue(
                "fatal",
                "uoj-interactive",
                "traditional UOJ problem.conf output does not support interactors",
                problem=problem.slug,
                field="interactor",
            )
        if problem.file_io_base:
            _loss(
                bundle,
                problem,
                "uoj-file-io",
                "UOJ output does not preserve source file-IO metadata",
                "file_io",
            )
        if problem.templates:
            _loss(
                bundle,
                problem,
                "uoj-templates",
                "UOJ judge data preserves templates only as inert sidecar files",
                "templates",
            )
        if problem.solutions:
            _loss(
                bundle,
                problem,
                "uoj-solutions",
                "UOJ judge data preserves solutions only as inert sidecar files",
                "solutions",
            )
        if problem.attachments:
            _loss(
                bundle,
                problem,
                "uoj-attachments",
                "UOJ judge data preserves attachments as inert files without import semantics",
                "attachments",
            )
        if len(problem.statements) > 1:
            _loss(
                bundle,
                problem,
                "uoj-multilingual-statement",
                "UOJ output writes only one statement sidecar",
                "statements",
            )


def _validate_dmoj_target(bundle: ProblemBundle, options: dict[str, Any]) -> None:
    for problem in bundle.problems:
        group_positions = {
            group.name: index for index, group in enumerate(problem.groups, start=1)
        }
        for group in problem.groups:
            if any(
                group_positions.get(dependency, len(problem.groups) + 1)
                >= group_positions[group.name]
                for dependency in group.dependencies
            ):
                bundle.add_issue(
                    "fatal",
                    "dmoj-dependency-order",
                    "DMOJ batch dependencies must refer to an earlier test group",
                    problem=problem.slug,
                    field="groups",
                )
        if any(case.sample for case in problem.cases):
            _loss(
                bundle,
                problem,
                "dmoj-samples-sidecar",
                "DMOJ judge data preserves samples only as inert sidecar files",
                "cases",
            )
        if problem.file_io_base:
            _loss(
                bundle,
                problem,
                "dmoj-file-io",
                "DMOJ output does not preserve source file-IO metadata",
                "file_io",
            )
        if problem.validator:
            _loss(
                bundle,
                problem,
                "dmoj-validator",
                "DMOJ output preserves validators only as inert attachments",
                "validator",
            )
        if problem.templates or problem.solutions or problem.attachments:
            _loss(
                bundle,
                problem,
                "dmoj-inert-attachments",
                "DMOJ output preserves templates, solutions, and attachments without judge semantics",
                "attachments",
            )
        if len(problem.statements) > 1:
            _loss(
                bundle,
                problem,
                "dmoj-multilingual-statement",
                "DMOJ output writes only one statement sidecar",
                "statements",
            )


ADAPTERS: dict[str, Adapter] = {
    "hydro": Adapter(
        "hydro",
        _registered_detection("hydro"),
        read_hydro,
        _validate_hydro_target,
        write_hydro,
    ),
    "icpc": Adapter(
        "icpc",
        _registered_detection("icpc"),
        read_icpc,
        _validate_icpc_target,
        write_icpc,
    ),
    "hoj": Adapter(
        "hoj", _registered_detection("hoj"), read_hoj, _validate_hoj_target, write_hoj
    ),
    "fps": Adapter(
        "fps", _registered_detection("fps"), read_fps, _validate_fps_target, write_fps
    ),
    "qduoj": Adapter(
        "qduoj",
        _registered_detection("qduoj"),
        read_qduoj,
        _validate_qduoj_target,
        write_qduoj,
    ),
    "uoj": Adapter(
        "uoj", _registered_detection("uoj"), read_uoj, _validate_uoj_target, write_uoj
    ),
    "dmoj": Adapter(
        "dmoj",
        _registered_detection("dmoj"),
        read_dmoj,
        _validate_dmoj_target,
        write_dmoj,
    ),
    "generic": Adapter(
        "generic", _registered_detection("generic"), read_generic, None, None
    ),
}
