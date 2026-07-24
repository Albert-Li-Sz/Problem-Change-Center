from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import format_bridge as bridge
from package_ir import (
    ProblemBundle,
    RepairCandidate,
    RepairSuggestion,
)
from package_security import load_json_file


REPAIR_PLAN_SCHEMA_VERSION = 1
MAX_REPAIR_SELECTIONS = 1000
MAX_SUPPLEMENT_BYTES = 256 * 1024 * 1024
_ANSWER_SUFFIXES = {".ans", ".out"}
_PROGRAM_ROLES = {"checker", "interactor", "validator", "solution"}


def populate_repair_suggestions(bundle: ProblemBundle, extracted_root: Path) -> None:
    files = [
        path.relative_to(extracted_root).as_posix()
        for path in sorted(extracted_root.rglob("*"))
        if path.is_file() and ".p2h-nested" not in path.parts
    ]
    lowered: dict[str, list[str]] = {}
    by_basename: dict[str, list[str]] = {}
    for name in files:
        lowered.setdefault(name.casefold(), []).append(name)
        by_basename.setdefault(PurePosixPath(name).name.casefold(), []).append(name)
    file_roles = _bundle_file_roles(bundle, extracted_root)

    suggested_targets = {
        suggestion.expected_path.casefold() for suggestion in bundle.repair_suggestions
    }
    for issue in bundle.issues:
        if issue.severity != "fatal":
            continue
        expected = _safe_member_name(issue.context.get("expected_path", ""))
        role = issue.context.get("role", "")
        if expected is None or not role:
            continue
        if expected.casefold() in suggested_targets:
            continue
        candidates = _candidate_paths(
            expected,
            role,
            lowered=lowered,
            by_basename=by_basename,
            file_roles=file_roles,
        )
        suggestion_id = hashlib.sha256(
            "\0".join((issue.code, issue.problem or "", role, expected)).encode("utf-8")
        ).hexdigest()[:24]
        suggestion = RepairSuggestion(
            id=suggestion_id,
            issue_code=issue.code,
            expected_path=expected,
            role=role,
            problem=issue.problem,
            candidates=tuple(candidates),
            requires_upload=not bool(candidates),
        )
        if suggestion not in bundle.repair_suggestions:
            bundle.repair_suggestions.append(suggestion)
            suggested_targets.add(expected.casefold())


def repair_source_archive(
    source_zip: Path,
    repaired_zip: Path,
    *,
    plan_path: Path,
    supplements_dir: Path | None,
    workspace: Path,
) -> list[dict[str, str]]:
    plan = load_json_file(plan_path)
    if plan.get("schema_version") != REPAIR_PLAN_SCHEMA_VERSION:
        raise ValueError("unsupported repair plan schema")
    expected_source_hash = plan.get("source_sha256")
    if not isinstance(expected_source_hash, str) or len(expected_source_hash) != 64:
        raise ValueError("repair plan must contain source_sha256")
    actual_source_hash = _sha256_file(source_zip)
    if actual_source_hash != expected_source_hash:
        raise ValueError("repair plan source hash does not match the uploaded archive")
    selections = plan.get("selections")
    if (
        not isinstance(selections, list)
        or not 1 <= len(selections) <= MAX_REPAIR_SELECTIONS
    ):
        raise ValueError("repair plan selections must be a non-empty bounded list")

    extracted = workspace / "repair-source"
    budget = bridge.ArchiveExtractionBudget.from_env()
    bridge._safe_extract_zip(source_zip, extracted, budget=budget)
    applied: list[dict[str, str]] = []
    seen_targets: set[str] = set()
    seen_ids: set[str] = set()
    for raw in selections:
        if not isinstance(raw, dict):
            raise ValueError("repair selection must be an object")
        suggestion_id = raw.get("suggestion_id")
        expected = _safe_member_name(raw.get("expected_path"))
        role = raw.get("role")
        if (
            not isinstance(suggestion_id, str)
            or len(suggestion_id) != 24
            or not isinstance(role, str)
            or not role
            or expected is None
        ):
            raise ValueError("invalid repair selection")
        if suggestion_id in seen_ids:
            raise ValueError("repair suggestion cannot be applied more than once")
        seen_ids.add(suggestion_id)
        target_key = expected.casefold()
        if target_key in seen_targets:
            raise ValueError("repair plan contains duplicate target paths")
        seen_targets.add(target_key)
        target = extracted.joinpath(*PurePosixPath(expected).parts)
        if target.exists() or target.is_symlink():
            raise ValueError(f"repair target already exists: {expected}")
        target.parent.mkdir(parents=True, exist_ok=True)

        candidate_name = _safe_member_name(raw.get("candidate_path"))
        upload_name = raw.get("upload_name")
        strategy = str(raw.get("strategy") or "")
        if candidate_name is not None and upload_name is not None:
            raise ValueError(
                "repair selection cannot use candidate and upload together"
            )
        if candidate_name is not None:
            candidate = extracted.joinpath(*PurePosixPath(candidate_name).parts)
            if not candidate.is_file() or candidate.is_symlink():
                raise ValueError(f"repair candidate does not exist: {candidate_name}")
            _validate_role_suffix(role, candidate)
            _copy_stream(candidate, target)
            source_description = candidate_name
        elif isinstance(upload_name, str) and upload_name:
            safe_upload = _safe_upload_name(upload_name)
            if supplements_dir is None:
                raise ValueError("repair plan references an unavailable upload")
            supplement = supplements_dir / safe_upload
            if (
                not supplement.is_file()
                or supplement.is_symlink()
                or supplement.stat().st_size > MAX_SUPPLEMENT_BYTES
            ):
                raise ValueError("invalid or oversized repair upload")
            expected_hash = raw.get("upload_sha256")
            if (
                not isinstance(expected_hash, str)
                or _sha256_file(supplement) != expected_hash
            ):
                raise ValueError("repair upload hash does not match the plan")
            _validate_role_suffix(role, supplement, target=target)
            _copy_stream(supplement, target)
            source_description = f"upload:{safe_upload}"
            strategy = "upload"
        else:
            raise ValueError("repair selection must choose a candidate or upload")
        applied.append(
            {
                "suggestion_id": suggestion_id,
                "expected_path": expected,
                "role": role,
                "source": source_description,
                "strategy": strategy,
                "sha256": _sha256_file(target),
            }
        )

    repaired_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        repaired_zip, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
    ) as archive:
        for path in sorted(extracted.rglob("*")):
            if path.is_file():
                name = path.relative_to(extracted).as_posix()
                with (
                    path.open("rb") as source,
                    archive.open(name, "w", force_zip64=True) as destination,
                ):
                    while chunk := source.read(1024 * 1024):
                        destination.write(chunk)
    bridge.validate_zip_archive(repaired_zip)
    return applied


def _candidate_paths(
    expected: str,
    role: str,
    *,
    lowered: dict[str, list[str]],
    by_basename: dict[str, list[str]],
    file_roles: dict[str, set[str]],
) -> list[RepairCandidate]:
    result: list[RepairCandidate] = []
    expected_path = PurePosixPath(expected)
    case_matches = [
        name for name in lowered.get(expected.casefold(), []) if name != expected
    ]
    for name in case_matches:
        if _role_allows(role, PurePosixPath(name), file_roles.get(name.casefold())):
            result.append(RepairCandidate(name, "case-only", 1.0))

    if expected_path.suffix.casefold() in _ANSWER_SUFFIXES:
        aliases = _ANSWER_SUFFIXES - {expected_path.suffix.casefold()}
        for suffix in aliases:
            alias = expected_path.with_suffix(suffix).as_posix()
            for name in lowered.get(alias.casefold(), []):
                if _role_allows(
                    role, PurePosixPath(name), file_roles.get(name.casefold())
                ):
                    result.append(RepairCandidate(name, "extension-alias", 0.95))

    basename_matches = by_basename.get(expected_path.name.casefold(), [])
    if len(basename_matches) == 1:
        name = basename_matches[0]
        if name.casefold() != expected.casefold() and _role_allows(
            role,
            PurePosixPath(name),
            file_roles.get(name.casefold()),
        ):
            result.append(RepairCandidate(name, "unique-basename", 0.85))

    unique: dict[str, RepairCandidate] = {}
    for candidate in result:
        previous = unique.get(candidate.path)
        if previous is None or candidate.confidence > previous.confidence:
            unique[candidate.path] = candidate
    return sorted(unique.values(), key=lambda item: (-item.confidence, item.path))


def _role_allows(
    role: str, path: PurePosixPath, classified_roles: set[str] | None = None
) -> bool:
    if classified_roles and role not in classified_roles:
        return False
    suffix = path.suffix.casefold()
    if role in {"input", "sample-input"}:
        return suffix == ".in"
    if role in {"output", "sample-output"}:
        return suffix in _ANSWER_SUFFIXES
    if role in _PROGRAM_ROLES:
        return suffix in bridge.SOURCE_SUFFIXES
    if role == "statement-pdf":
        return suffix == ".pdf"
    return True


def _validate_role_suffix(role: str, path: Path, *, target: Path | None = None) -> None:
    logical = PurePosixPath((target or path).name)
    source = PurePosixPath(path.name)
    if (
        role in {"output", "sample-output"}
        and source.suffix.casefold() in _ANSWER_SUFFIXES
    ):
        return
    if not _role_allows(role, source if target is None else logical):
        raise ValueError(f"repair file type is not valid for role {role}")


def _safe_member_name(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 1024:
        return None
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or "." in path.parts or not path.name:
        return None
    if any(not part or "\x00" in part for part in path.parts):
        return None
    return path.as_posix()


def _bundle_file_roles(
    bundle: ProblemBundle, extracted_root: Path
) -> dict[str, set[str]]:
    roles: dict[str, set[str]] = {}
    root = extracted_root.resolve()

    def register(path: Path | None, role: str) -> None:
        if path is None:
            return
        try:
            relative = path.resolve().relative_to(root).as_posix().casefold()
        except ValueError:
            return
        roles.setdefault(relative, set()).add(role)

    for problem in bundle.problems:
        for case in problem.cases:
            prefix = "sample-" if case.sample else ""
            register(case.input_path, f"{prefix}input")
            register(case.output_path, f"{prefix}output")
        for program in (
            problem.checker,
            problem.interactor,
            problem.validator,
            *problem.solutions,
        ):
            if program is not None:
                register(program.path, program.kind)
                for path in program.auxiliary_files.values():
                    register(path, f"{program.kind}-auxiliary")
        for statement in problem.statements:
            if statement.format == "pdf":
                register(statement.path, "statement-pdf")
        for path in problem.attachments.values():
            register(path, "attachment")
    return roles


def _safe_upload_name(value: str) -> str:
    path = PurePosixPath(value)
    if path.name != value or not value or len(value) > 128:
        raise ValueError("invalid repair upload name")
    if any(character in value for character in ("/", "\\", "\x00")):
        raise ValueError("invalid repair upload name")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_stream(source_path: Path, destination_path: Path) -> None:
    with source_path.open("rb") as source, destination_path.open("wb") as destination:
        while chunk := source.read(1024 * 1024):
            destination.write(chunk)
