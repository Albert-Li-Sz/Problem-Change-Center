from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal


IssueSeverity = Literal["warning", "loss", "fatal"]


@dataclass(frozen=True)
class ConversionIssue:
    severity: IssueSeverity
    code: str
    message: str
    problem: str | None = None
    field: str | None = None


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


@dataclass
class Problem:
    id: str
    slug: str
    title: str
    titles: dict[str, str] = field(default_factory=dict)
    statements: list[Statement] = field(default_factory=list)
    cases: list[TestCase] = field(default_factory=list)
    groups: list[TestGroup] = field(default_factory=list)
    time_ms: int = 1000
    memory_mb: int = 256
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    problem_type: Literal["default", "interactive", "output-only"] = "default"
    checker: Program | None = None
    interactor: Program | None = None
    validator: Program | None = None
    file_io_base: str | None = None
    attachments: dict[str, Path] = field(default_factory=dict)
    templates: dict[str, str] = field(default_factory=dict)
    solutions: list[Program] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProblemBundle:
    source_format: str
    problems: list[Problem]
    issues: list[ConversionIssue] = field(default_factory=list)

    def add_issue(
        self,
        severity: IssueSeverity,
        code: str,
        message: str,
        *,
        problem: str | None = None,
        field: str | None = None,
    ) -> None:
        issue = ConversionIssue(severity, code, message, problem, field)
        if issue not in self.issues:
            self.issues.append(issue)

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
                        self.add_issue(
                            "fatal",
                            f"missing-test-{role}",
                            f"test case {case.name} is missing its {role} file: {path.name}",
                            problem=problem.slug,
                            field="cases",
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
                    )
                elif program.path is None and not (program.content or "").strip():
                    self.add_issue(
                        "fatal",
                        f"missing-{program.kind}-content",
                        f"referenced {program.kind} has no source file or inline content",
                        problem=problem.slug,
                        field=program.kind,
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
                    )
            for name, path in problem.attachments.items():
                if not path.is_file():
                    self.add_issue(
                        "fatal",
                        "missing-attachment-file",
                        f"referenced attachment is missing: {name}",
                        problem=problem.slug,
                        field="attachments",
                    )

    def report(self, target_format: str, artifacts: list[str]) -> dict[str, Any]:
        counts = {
            severity: sum(issue.severity == severity for issue in self.issues)
            for severity in ("warning", "loss", "fatal")
        }
        return {
            "schema_version": 1,
            "source_format": self.source_format,
            "target_format": target_format,
            "problem_count": len(self.problems),
            "counts": counts,
            "issues": [asdict(issue) for issue in self.issues],
            "artifacts": artifacts,
        }


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
