from __future__ import annotations

import json
import hashlib
import math
import re
from dataclasses import asdict, dataclass, field as dataclass_field
from pathlib import Path
from typing import Any, Iterable, Literal


IssueSeverity = Literal["warning", "loss", "fatal"]


@dataclass(frozen=True)
class ConversionIssue:
    severity: IssueSeverity
    code: str
    message: str
    problem: str | None = None
    field: str | None = None
    context: dict[str, str] = dataclass_field(default_factory=dict)


@dataclass(frozen=True)
class RepairCandidate:
    path: str
    strategy: Literal["case-only", "extension-alias", "unique-basename"]
    confidence: float


@dataclass(frozen=True)
class RepairSuggestion:
    id: str
    issue_code: str
    expected_path: str
    role: str
    problem: str | None = None
    candidates: tuple[RepairCandidate, ...] = ()
    requires_upload: bool = True


@dataclass(frozen=True)
class FormatCapabilities:
    statements: bool = True
    multilingual_statements: bool = True
    pdf_statements: bool = True
    cases: bool = True
    groups: bool = True
    group_dependencies: bool = True
    file_io: bool = True
    checker: bool = True
    interactor: bool = True
    validator: bool = True
    templates: bool = True
    attachments: bool = True
    solutions: bool = True
    tags: bool = True


TARGET_CAPABILITIES: dict[str, FormatCapabilities] = {
    "hydro": FormatCapabilities(),
    "icpc": FormatCapabilities(
        groups=False,
        group_dependencies=False,
        file_io=False,
        templates=False,
        tags=False,
    ),
    "hoj": FormatCapabilities(
        multilingual_statements=False,
        group_dependencies=False,
        validator=False,
        attachments=False,
        solutions=False,
    ),
    "fps": FormatCapabilities(
        multilingual_statements=False,
        groups=False,
        group_dependencies=False,
        file_io=False,
        validator=False,
        attachments=False,
    ),
    "qduoj": FormatCapabilities(
        multilingual_statements=False,
        groups=False,
        group_dependencies=False,
        file_io=False,
        interactor=False,
        validator=False,
        attachments=False,
        solutions=False,
    ),
    "uoj": FormatCapabilities(
        multilingual_statements=False,
        group_dependencies=False,
        file_io=False,
        interactor=False,
        validator=False,
        templates=False,
        attachments=False,
        solutions=False,
    ),
    "dmoj": FormatCapabilities(
        multilingual_statements=False,
        file_io=False,
        validator=False,
        templates=False,
        attachments=False,
        solutions=False,
    ),
}


@dataclass(frozen=True)
class Statement:
    language: str
    format: Literal["markdown", "html", "pdf"]
    content: str | None = None
    path: Path | None = None


@dataclass(frozen=True)
class TestCase:
    name: str
    input_path: Path
    output_path: Path
    sample: bool = False
    score: int | float | None = None
    group: str | None = None


@dataclass(frozen=True)
class TestGroup:
    name: str
    cases: tuple[str, ...]
    score: int | float | None = None
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class Program:
    kind: Literal["checker", "interactor", "validator", "solution"]
    language: str | None = None
    mode: str | None = None
    path: Path | None = None
    content: str | None = None
    auxiliary_files: dict[str, Path] = dataclass_field(default_factory=dict)


@dataclass
class Problem:
    id: str
    slug: str
    title: str
    titles: dict[str, str] = dataclass_field(default_factory=dict)
    statements: list[Statement] = dataclass_field(default_factory=list)
    cases: list[TestCase] = dataclass_field(default_factory=list)
    groups: list[TestGroup] = dataclass_field(default_factory=list)
    time_ms: int = 1000
    memory_mb: int = 256
    tags: list[str] = dataclass_field(default_factory=list)
    source: str | None = None
    problem_type: Literal["default", "interactive", "output-only"] = "default"
    checker: Program | None = None
    interactor: Program | None = None
    validator: Program | None = None
    file_io_base: str | None = None
    attachments: dict[str, Path] = dataclass_field(default_factory=dict)
    templates: dict[str, str] = dataclass_field(default_factory=dict)
    solutions: list[Program] = dataclass_field(default_factory=list)
    extra: dict[str, Any] = dataclass_field(default_factory=dict)


@dataclass
class ProblemBundle:
    source_format: str
    problems: list[Problem]
    issues: list[ConversionIssue] = dataclass_field(default_factory=list)
    repair_suggestions: list[RepairSuggestion] = dataclass_field(default_factory=list)
    applied_repairs: list[dict[str, str]] = dataclass_field(default_factory=list)

    def add_issue(
        self,
        severity: IssueSeverity,
        code: str,
        message: str,
        *,
        problem: str | None = None,
        field: str | None = None,
        context: dict[str, str] | None = None,
    ) -> None:
        issue = ConversionIssue(
            severity, code, message, problem, field, dict(context or {})
        )
        if issue not in self.issues:
            self.issues.append(issue)

    def semantic_snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "problems": [
                _problem_semantic_snapshot(problem)
                for problem in sorted(self.problems, key=lambda item: item.slug)
            ],
        }

    def semantic_digest(self) -> str:
        payload = json.dumps(
            self.semantic_snapshot(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def ensure_convertible(self, loss_policy: str) -> None:
        fatal = [issue for issue in self.issues if issue.severity == "fatal"]
        if fatal:
            raise ValueError(fatal[0].message)
        if loss_policy == "error":
            losses = [issue for issue in self.issues if issue.severity == "loss"]
            if losses:
                raise ValueError(f"loss_policy=error: {losses[0].message}")

    def validate_integrity(self) -> None:
        if not self.problems:
            self.add_issue(
                "fatal",
                "missing-problems",
                "source package does not contain any selected problems",
                field="problems",
            )
            return
        for problem in self.problems:
            if (
                type(problem.time_ms) is not int
                or problem.time_ms <= 0
                or problem.time_ms > 2_147_483_647
            ):
                self.add_issue(
                    "fatal",
                    "invalid-time-limit",
                    "problem time limit must be a positive 32-bit integer in milliseconds",
                    problem=problem.slug,
                    field="time_ms",
                )
            if (
                type(problem.memory_mb) is not int
                or problem.memory_mb <= 0
                or problem.memory_mb > 2_147_483_647
            ):
                self.add_issue(
                    "fatal",
                    "invalid-memory-limit",
                    "problem memory limit must be a positive 32-bit integer in MiB",
                    problem=problem.slug,
                    field="memory_mb",
                )
            if not problem.statements:
                self.add_issue(
                    "loss",
                    "missing-statement",
                    "source problem has no usable statement; the target will receive a visible placeholder",
                    problem=problem.slug,
                    field="statements",
                )
            if problem.problem_type != "output-only" and not any(
                not case.sample for case in problem.cases
            ):
                self.add_issue(
                    "fatal",
                    "missing-secret-tests",
                    "problem has no non-sample test cases; add at least one paired input/output test",
                    problem=problem.slug,
                    field="cases",
                )
            case_names: set[str] = set()
            duplicate_case_names: set[str] = set()
            for case in problem.cases:
                if not case.sample:
                    if case.name in case_names:
                        duplicate_case_names.add(case.name)
                    case_names.add(case.name)
                if case.score is not None and (
                    type(case.score) not in {int, float}
                    or not math.isfinite(float(case.score))
                    or case.score < 0
                ):
                    self.add_issue(
                        "fatal",
                        "invalid-test-score",
                        f"test case {case.name} has an invalid score",
                        problem=problem.slug,
                        field="cases",
                    )
                for role, path in (
                    ("input", case.input_path),
                    ("output", case.output_path),
                ):
                    if not path.is_file():
                        repair_role = f"sample-{role}" if case.sample else role
                        self.add_issue(
                            "fatal",
                            f"missing-test-{role}",
                            f"test case {case.name} is missing its {role} file: {path.name}",
                            problem=problem.slug,
                            field="cases",
                            context={
                                "expected_path": _report_path(path),
                                "role": repair_role,
                            },
                        )
            if duplicate_case_names:
                self.add_issue(
                    "fatal",
                    "duplicate-test-name",
                    "problem contains duplicate test case names: "
                    + ", ".join(sorted(duplicate_case_names)),
                    problem=problem.slug,
                    field="cases",
                )
            secret_case_names = {case.name for case in problem.cases if not case.sample}
            group_names = [group.name for group in problem.groups]
            unique_group_names = set(group_names)
            if len(unique_group_names) != len(group_names):
                self.add_issue(
                    "fatal",
                    "duplicate-group-name",
                    "problem contains duplicate test group names",
                    problem=problem.slug,
                    field="groups",
                )
            for group in problem.groups:
                if group.score is not None and (
                    type(group.score) not in {int, float}
                    or not math.isfinite(float(group.score))
                    or group.score < 0
                ):
                    self.add_issue(
                        "fatal",
                        "invalid-group-score",
                        f"test group {group.name} has an invalid score",
                        problem=problem.slug,
                        field="groups",
                    )
                if len(set(group.cases)) != len(group.cases):
                    self.add_issue(
                        "fatal",
                        "duplicate-group-case",
                        f"test group {group.name} lists a case more than once",
                        problem=problem.slug,
                        field="groups",
                    )
                unknown_cases = sorted(set(group.cases) - secret_case_names)
                if unknown_cases:
                    self.add_issue(
                        "fatal",
                        "unknown-group-case",
                        f"test group {group.name} references unknown or sample cases: "
                        + ", ".join(unknown_cases),
                        problem=problem.slug,
                        field="groups",
                    )
                if len(set(group.dependencies)) != len(group.dependencies):
                    self.add_issue(
                        "fatal",
                        "duplicate-group-dependency",
                        f"test group {group.name} lists a dependency more than once",
                        problem=problem.slug,
                        field="groups",
                    )
                unknown_dependencies = sorted(
                    set(group.dependencies) - unique_group_names
                )
                if unknown_dependencies:
                    self.add_issue(
                        "fatal",
                        "unknown-group-dependency",
                        f"test group {group.name} references unknown dependencies: "
                        + ", ".join(unknown_dependencies),
                        problem=problem.slug,
                        field="groups",
                    )
                if group.name in group.dependencies:
                    self.add_issue(
                        "fatal",
                        "self-group-dependency",
                        f"test group {group.name} depends on itself",
                        problem=problem.slug,
                        field="groups",
                    )
            if len(unique_group_names) == len(group_names):
                dependencies = {
                    group.name: set(group.dependencies) for group in problem.groups
                }
                ready = [name for name, values in dependencies.items() if not values]
                visited = 0
                while ready:
                    resolved = ready.pop()
                    visited += 1
                    for name, values in dependencies.items():
                        if resolved in values:
                            values.remove(resolved)
                            if not values:
                                ready.append(name)
                if visited != len(dependencies):
                    self.add_issue(
                        "fatal",
                        "cyclic-group-dependency",
                        "test group dependencies contain a cycle",
                        problem=problem.slug,
                        field="groups",
                    )
            if problem.problem_type == "interactive" and problem.interactor is None:
                self.add_issue(
                    "fatal",
                    "missing-interactor",
                    "interactive problem is missing its interactor program",
                    problem=problem.slug,
                    field="interactor",
                )
            programs = [
                problem.checker,
                problem.interactor,
                problem.validator,
                *problem.solutions,
            ]
            for program in (item for item in programs if item is not None):
                if program.kind == "checker" and (program.mode or "").startswith(
                    ("uoj-builtin:", "dmoj-builtin:")
                ):
                    continue
                if program.path is not None and not program.path.is_file():
                    self.add_issue(
                        "fatal",
                        f"missing-{program.kind}-file",
                        f"referenced {program.kind} file is missing: {program.path.name}",
                        problem=problem.slug,
                        field=program.kind,
                        context={
                            "expected_path": _report_path(program.path),
                            "role": program.kind,
                        },
                    )
                elif program.path is None and not (program.content or "").strip():
                    self.add_issue(
                        "fatal",
                        f"missing-{program.kind}-content",
                        f"referenced {program.kind} has no source file or inline content",
                        problem=problem.slug,
                        field=program.kind,
                    )
                for name, path in program.auxiliary_files.items():
                    relative = Path(name.replace("\\", "/"))
                    if relative.is_absolute() or ".." in relative.parts or not name:
                        self.add_issue(
                            "fatal",
                            f"invalid-{program.kind}-auxiliary-path",
                            f"referenced {program.kind} auxiliary file has an unsafe path: {name}",
                            problem=problem.slug,
                            field=program.kind,
                        )
                    elif not path.is_file():
                        self.add_issue(
                            "fatal",
                            f"missing-{program.kind}-auxiliary-file",
                            f"referenced {program.kind} auxiliary file is missing: {name}",
                            problem=problem.slug,
                            field=program.kind,
                            context={
                                "expected_path": _report_path(path),
                                "role": f"{program.kind}-auxiliary",
                            },
                        )
            for statement in problem.statements:
                if statement.format == "pdf" and (
                    statement.path is None or not statement.path.is_file()
                ):
                    self.add_issue(
                        "fatal",
                        "missing-statement-pdf",
                        "referenced PDF statement file is missing",
                        problem=problem.slug,
                        field="statements",
                        context={
                            "expected_path": _report_path(statement.path),
                            "role": "statement-pdf",
                        },
                    )
            for name, path in problem.attachments.items():
                if not path.is_file():
                    self.add_issue(
                        "fatal",
                        "missing-attachment-file",
                        f"referenced attachment is missing: {name}",
                        problem=problem.slug,
                        field="attachments",
                        context={
                            "expected_path": _report_path(path),
                            "role": "attachment",
                        },
                    )

    def report(self, target_format: str, artifacts: list[str]) -> dict[str, Any]:
        counts = {
            severity: sum(issue.severity == severity for issue in self.issues)
            for severity in ("warning", "loss", "fatal")
        }
        return {
            "schema_version": 2,
            "source_format": self.source_format,
            "target_format": target_format,
            "problem_count": len(self.problems),
            "counts": counts,
            "issues": [asdict(issue) for issue in self.issues],
            "artifacts": artifacts,
            "repair_ready": bool(self.repair_suggestions),
            "repair_suggestions": [
                asdict(suggestion) for suggestion in self.repair_suggestions
            ],
            "applied_repairs": list(self.applied_repairs),
            "source_semantic_digest": self.semantic_digest() if self.problems else None,
        }


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def compare_semantic_snapshots(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    target_format: str,
    issues: Iterable[ConversionIssue] = (),
) -> list[str]:
    """Return semantic paths that changed without a target capability/loss explanation."""
    changed = _semantic_differences(before, after)
    loss_fields = {
        issue.field
        for issue in issues
        if issue.severity in {"loss", "fatal"} and issue.field
    }
    capabilities = TARGET_CAPABILITIES.get(target_format, FormatCapabilities())
    declared_fields = set(capabilities.__dataclass_fields__)
    unexplained: list[str] = []
    for path in changed:
        semantic_field = _semantic_field_for_path(path)
        if semantic_field in loss_fields and semantic_field in declared_fields:
            continue
        unexplained.append(path)
    return unexplained


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _report_path(path: Path | None) -> str:
    if path is None:
        return ""
    parts = [part for part in path.parts if part not in {path.anchor, "/", ""}]
    return "/".join(parts[-4:])


def _hash_text(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _path_or_content_digest(path: Path | None, content: str | None) -> str | None:
    if path is not None and path.is_file():
        return _hash_file(path)
    if content is not None:
        return _hash_text(content)
    return None


def _program_semantic_snapshot(program: Program | None) -> dict[str, Any] | None:
    if program is None:
        return None
    return {
        "kind": program.kind,
        "language": program.language,
        "mode": program.mode,
        "suffix": program.path.suffix.lower() if program.path is not None else None,
        "sha256": _path_or_content_digest(program.path, program.content),
        "auxiliary_files": {
            name: _hash_file(path) if path.is_file() else None
            for name, path in sorted(program.auxiliary_files.items())
        },
    }


def _problem_semantic_snapshot(problem: Problem) -> dict[str, Any]:
    return {
        "title": problem.title,
        "titles": dict(sorted(problem.titles.items())),
        "statements": sorted(
            (
                {
                    "language": statement.language,
                    "format": statement.format,
                    "sha256": _path_or_content_digest(
                        statement.path, statement.content
                    ),
                }
                for statement in problem.statements
            ),
            key=lambda item: (
                str(item["language"]),
                str(item["format"]),
                str(item["sha256"]),
            ),
        ),
        "time_ms": problem.time_ms,
        "memory_mb": problem.memory_mb,
        "tags": sorted(problem.tags),
        "source": _normalized_source(problem.source),
        "problem_type": problem.problem_type,
        "file_io_base": problem.file_io_base,
        "cases": sorted(
            (
                {
                    "input_sha256": _hash_file(case.input_path)
                    if case.input_path.is_file()
                    else None,
                    "output_sha256": _hash_file(case.output_path)
                    if case.output_path.is_file()
                    else None,
                    "sample": case.sample,
                    "score": None if case.sample else case.score,
                    "group": case.group,
                }
                for case in problem.cases
            ),
            key=lambda item: (
                bool(item["sample"]),
                str(item["input_sha256"]),
                str(item["output_sha256"]),
            ),
        ),
        "groups": sorted(
            (
                {
                    "name": group.name,
                    "cases": sorted(group.cases),
                    "score": group.score,
                    "dependencies": sorted(group.dependencies),
                }
                for group in problem.groups
            ),
            key=lambda item: str(item["name"]),
        ),
        "checker": _program_semantic_snapshot(problem.checker),
        "interactor": _program_semantic_snapshot(problem.interactor),
        "validator": _program_semantic_snapshot(problem.validator),
        "templates": {
            name: _hash_text(content)
            for name, content in sorted(problem.templates.items())
        },
        "attachments": {
            name: _hash_file(path) if path.is_file() else None
            for name, path in sorted(problem.attachments.items())
        },
        "solutions": sorted(
            (
                snapshot
                for program in problem.solutions
                if (snapshot := _program_semantic_snapshot(program)) is not None
            ),
            key=lambda item: (
                str(item["mode"]),
                str(item["language"]),
                str(item["sha256"]),
            ),
        ),
    }


def _semantic_differences(before: Any, after: Any, path: str = "") -> list[str]:
    if type(before) is not type(after):
        return [path or "$"]
    if isinstance(before, dict):
        changed: list[str] = []
        for key in sorted(set(before) | set(after)):
            child = f"{path}.{key}" if path else str(key)
            if key not in before or key not in after:
                changed.append(child)
            else:
                changed.extend(_semantic_differences(before[key], after[key], child))
        return changed
    if isinstance(before, list):
        if len(before) != len(after):
            return [path]
        changed = []
        for index, (left, right) in enumerate(zip(before, after, strict=True)):
            changed.extend(_semantic_differences(left, right, f"{path}[{index}]"))
        return changed
    return [] if before == after else [path]


def _semantic_field_for_path(path: str) -> str:
    match = re.match(r"^problems\[\d+\]\.([^\[.]+)", path)
    leaf = match.group(1) if match else path.split(".", 1)[0].split("[", 1)[0]
    aliases = {
        "multilingual_statements": "statements",
        "pdf_statements": "statements",
        "group_dependencies": "groups",
        "file_io_base": "file_io",
    }
    return aliases.get(leaf, leaf)


def _normalized_source(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if normalized.casefold() in {
        "converted from hydrooj",
        "converted by oj package converter",
        "converted by polygon-to-hydro",
    }:
        return None
    return normalized
