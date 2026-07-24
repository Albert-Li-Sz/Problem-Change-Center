from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import yaml
from defusedxml import ElementTree as DefusedElementTree
from yaml.events import AliasEvent
from yaml.nodes import MappingNode


MAX_ARCHIVE_ENTRIES = 50_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_COMPRESSION_RATIO = 200.0
MIN_ARCHIVE_COMPRESSION_RATIO_BYTES = 16 * 1024 * 1024
MAX_DETECTION_XML_BYTES = 8 * 1024 * 1024
MAX_NESTED_PACKAGES = 1000
MAX_PROBLEM_PREVIEWS = 200
MAX_DETECTION_METADATA_BYTES = 4 * 1024 * 1024
MAX_METADATA_DEPTH = 48
MAX_METADATA_NODES = 100_000
MAX_YAML_ALIASES = 32
WRITABLE_FORMATS = ("hydro", "icpc", "hoj", "fps", "qduoj", "uoj", "dmoj")


@dataclass(frozen=True)
class FormatDetection:
    format: str
    confidence: float
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class DetectedProblem:
    id: str
    path: str


@dataclass(frozen=True)
class PackageInspection:
    candidates: tuple[FormatDetection, ...]
    detected_format: str | None
    package_scope: str
    package_layout: str
    problem_count: int | None
    problems: tuple[DetectedProblem, ...]
    problems_truncated: bool
    supported_targets: tuple[str, ...]


@dataclass
class _ArchiveBudget:
    entries: int = 0
    uncompressed_bytes: int = 0


@dataclass(frozen=True)
class _ProblemIdentity:
    identifier: str
    fingerprint: str


@dataclass(frozen=True)
class _ContentInspection:
    candidates: tuple[FormatDetection, ...]
    layouts: dict[str, str]
    problems: dict[str, tuple[DetectedProblem, ...]]
    problem_counts: dict[str, int]
    problem_identities: dict[str, tuple[_ProblemIdentity | None, ...]] = field(
        default_factory=dict
    )


class _LimitedSafeLoader(yaml.SafeLoader):
    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._p2h_aliases = 0
        self._p2h_depth = 0
        self._p2h_nodes = 0

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(AliasEvent):
            self._p2h_aliases += 1
            if self._p2h_aliases > MAX_YAML_ALIASES:
                raise ValueError("YAML alias limit exceeded")
        self._p2h_depth += 1
        if self._p2h_depth > MAX_METADATA_DEPTH:
            raise ValueError("YAML nesting limit exceeded")
        try:
            node = super().compose_node(parent, index)
            self._p2h_nodes += 1
            if self._p2h_nodes > MAX_METADATA_NODES:
                raise ValueError("YAML node limit exceeded")
            return node
        finally:
            self._p2h_depth -= 1

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, MappingNode):
            raise ValueError("YAML mapping node is invalid")
        self.flatten_mapping(node)
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as exc:
                raise ValueError("YAML mapping contains an unhashable key") from exc
            if duplicate:
                raise ValueError("duplicate YAML mapping key")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def inspect_zip_package(
    path: Path,
    *,
    fallback_id: str | None = None,
) -> PackageInspection:
    budget = _ArchiveBudget()
    archive_id = _safe_identifier(fallback_id or path.stem, "problem")
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        _validate_archive_infos(infos, budget=budget)
        content = _inspect_archive_content(
            archive,
            infos,
            fallback_id=archive_id,
        )
        strong = [
            candidate for candidate in content.candidates if candidate.confidence >= 0.8
        ]
        if not strong:
            nested = _inspect_nested_archives(archive, infos, budget=budget)
            if nested is not None:
                content = nested
        elif len(strong) == 1 and strong[0].format == "hoj":
            nested = _inspect_nested_archives(archive, infos, budget=budget)
            if nested is not None:
                merged = _merge_expanded_hoj_content(content, nested)
                if merged is not None:
                    content = merged

    candidates = list(content.candidates)
    detected = detected_format(candidates)
    profile_format = detected
    if profile_format is None and len(candidates) == 1:
        profile_format = candidates[0].format
    if profile_format is None:
        profile_format = _shared_strong_profile(content)
    problem_count = (
        content.problem_counts.get(profile_format)
        if profile_format is not None
        else None
    )
    all_problems = (
        content.problems.get(profile_format, ()) if profile_format is not None else ()
    )
    problems = all_problems[:MAX_PROBLEM_PREVIEWS]
    layout = (
        content.layouts.get(profile_format, "unknown")
        if profile_format is not None
        else "mixed"
        if len({candidate.format for candidate in candidates}) > 1
        else "unknown"
    )
    scope = (
        "single"
        if problem_count == 1
        else "multi"
        if problem_count is not None and problem_count > 1
        else "unknown"
    )
    targets = (
        ()
        if layout == "mixed"
        else tuple(
            target
            for target in WRITABLE_FORMATS
            if detected is None or target != detected
        )
    )
    return PackageInspection(
        candidates=tuple(candidates),
        detected_format=detected,
        package_scope=scope,
        package_layout=layout,
        problem_count=problem_count,
        problems=tuple(problems),
        problems_truncated=len(all_problems) > len(problems),
        supported_targets=targets,
    )


def detect_zip_format(path: Path) -> list[FormatDetection]:
    return list(inspect_zip_package(path).candidates)


def _shared_strong_profile(content: _ContentInspection) -> str | None:
    formats = [
        candidate.format
        for candidate in content.candidates
        if candidate.confidence >= 0.8
    ]
    if len(formats) < 2:
        return None
    reference_format = formats[0]
    reference = (
        content.layouts.get(reference_format),
        content.problem_counts.get(reference_format),
        content.problems.get(reference_format),
    )
    if any(value is None for value in reference):
        return None
    for format_id in formats[1:]:
        profile = (
            content.layouts.get(format_id),
            content.problem_counts.get(format_id),
            content.problems.get(format_id),
        )
        if profile != reference:
            return None
    return reference_format


def _inspect_archive_content(
    archive: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
    *,
    fallback_id: str,
) -> _ContentInspection:
    names = [
        _normalized_name(info.filename)
        for info in infos
        if info.filename and not info.is_dir()
    ]
    lowered_set = {name.casefold() for name in names}
    info_by_name = {
        _normalized_name(info.filename).casefold(): info
        for info in infos
        if info.filename and not info.is_dir()
    }
    result: list[FormatDetection] = []
    layouts: dict[str, str] = {}
    problems: dict[str, tuple[DetectedProblem, ...]] = {}
    problem_counts: dict[str, int] = {}
    problem_identities: dict[str, tuple[_ProblemIdentity | None, ...]] = {}

    def add(format_id: str, confidence: float, *evidence: str) -> None:
        result.append(FormatDetection(format_id, confidence, tuple(evidence)))

    problem_yaml_roots = _roots_for_suffix(names, "problem.yaml")
    probhub_sample_roots = _roots_for_marker(names, "data/sample/")
    probhub_secret_roots = _roots_for_marker(names, "data/secret/")
    probhub_statement_roots = {
        *_roots_for_marker(names, "problem_statement/"),
        *_roots_for_marker(names, "statement/"),
    }
    probhub_workspace = any(
        name.casefold().endswith(".probhub/workspace.yaml") for name in names
    ) and any(name.casefold().endswith("probhub.yaml") for name in names)
    probhub_workspace_roots = _roots_for_suffix(names, "probhub.yaml")
    root_pdf_problem_roots = {
        root
        for root in problem_yaml_roots
        if f"{root}domjudge-problem.ini".casefold() in lowered_set
        and f"{root}problem.pdf".casefold() in lowered_set
        and root in probhub_secret_roots
        and root not in probhub_statement_roots
    }
    probhub_legacy_candidates = _roots_for_suffix(names, "meta.json")
    probhub_legacy_roots = {
        root
        for root in probhub_legacy_candidates
        if (
            f"{root}problem.md".casefold() in lowered_set
            or f"{root}problem.pdf".casefold() in lowered_set
            or any(
                name.casefold().startswith(f"{root}problem.".casefold())
                and name.casefold().endswith(".md")
                for name in names
            )
        )
        and (
            f"{root}std.cpp".casefold() in lowered_set
            or f"{root}code/std.cpp".casefold() in lowered_set
        )
        and (
            f"{root}validator.cpp".casefold() in lowered_set
            or f"{root}code/validator.cpp".casefold() in lowered_set
        )
        and root in probhub_sample_roots
        and root in probhub_secret_roots
    }
    if probhub_workspace:
        add("probhub", 0.995, ".probhub/workspace.yaml", "probhub.yaml")
        workspace_entries = _probhub_workspace_problems(
            archive,
            names,
            info_by_name,
        )
        if workspace_entries:
            _set_entries(
                "probhub",
                "workspace",
                workspace_entries,
                layouts,
                problems,
                problem_counts,
            )
        else:
            _set_profile(
                "probhub",
                "workspace",
                probhub_workspace_roots,
                fallback_id,
                layouts,
                problems,
                problem_counts,
            )
    elif root_pdf_problem_roots:
        root_pdf_evidence = (
            "problem.yaml",
            "domjudge-problem.ini",
            "problem.pdf",
            "data/secret",
        )
        add(
            "probhub",
            0.98,
            *root_pdf_evidence,
        )
        _set_profile(
            "probhub",
            "directory",
            root_pdf_problem_roots,
            fallback_id,
            layouts,
            problems,
            problem_counts,
        )
        # ProbHub Core exports and legacy ICPC/DOMjudge packages can have the
        # same root-level PDF layout. Keep both parsers available for an
        # explicit user choice instead of guessing from indistinguishable files.
        add("icpc", 0.98, *root_pdf_evidence)
        _set_profile(
            "icpc",
            "directory",
            root_pdf_problem_roots,
            fallback_id,
            layouts,
            problems,
            problem_counts,
        )
    elif probhub_legacy_roots:
        add(
            "probhub",
            0.97,
            "meta.json",
            "data/sample and data/secret",
            "legacy statement and sources",
        )
        _set_entries(
            "probhub",
            "directory",
            _metadata_problems(
                archive,
                info_by_name,
                probhub_legacy_roots,
                "meta.json",
                ("problem", "id"),
                fallback_id=fallback_id,
            ),
            layouts,
            problems,
            problem_counts,
        )

    polygon_roots = {
        name[: -len("problem.xml")]
        for name in names
        if name.casefold().endswith("problem.xml")
        and "/problems/" in f"/{name.casefold()}"
    }
    if any(name.casefold().endswith("contest.xml") for name in names) and polygon_roots:
        add("polygon", 0.99, "contest.xml", "problems/ directory")
        _set_profile(
            "polygon",
            "contest",
            polygon_roots,
            fallback_id,
            layouts,
            problems,
            problem_counts,
        )
    else:
        polygon_single = _polygon_single_problem(
            archive, names, info_by_name, fallback_id
        )
        if polygon_single is not None:
            add("polygon", 0.97, "single problem.xml", "Polygon problem package")
            layouts["polygon"] = "directory"
            problems["polygon"] = (polygon_single,)
            problem_counts["polygon"] = 1

    hydro_roots = {
        root
        for root in _roots_for_suffix(names, "testdata/config.yaml")
        if f"{root}problem.yaml".casefold() in lowered_set
    }
    if hydro_roots:
        add("hydro", 0.98, "problem.yaml", "testdata/config.yaml")
        _set_entries(
            "hydro",
            "directory",
            _metadata_problems(
                archive,
                info_by_name,
                hydro_roots,
                "problem.yaml",
                ("pid",),
                ("id",),
                ("slug",),
                fallback_id=fallback_id,
            ),
            layouts,
            problems,
            problem_counts,
        )

    qduoj_roots = {
        name[: -len("problem.json")]
        for name in names
        if re.search(r"(?:^|/)\d+/problem\.json$", name, flags=re.IGNORECASE)
    }
    qduoj_roots = {
        root
        for root in qduoj_roots
        if any(
            name.casefold().startswith(f"{root}testcase/".casefold()) for name in names
        )
    }
    if qduoj_roots:
        add("qduoj", 0.99, "numbered problem.json", "testcase/ directory")
        _set_entries(
            "qduoj",
            "directory",
            _metadata_problems(
                archive,
                info_by_name,
                qduoj_roots,
                "problem.json",
                ("display_id",),
                fallback_id=fallback_id,
                indexed_fallback=True,
            ),
            layouts,
            problems,
            problem_counts,
        )

    hoj_roots = {
        name[: -len(".json")]
        for name in names
        if re.search(r"(?:^|/)problem_[^/]+\.json$", name, flags=re.IGNORECASE)
        and any(
            other.casefold().startswith(f"{name[: -len('.json')]}/".casefold())
            for other in names
        )
    }
    if hoj_roots:
        add("hoj", 0.96, "problem_*.json", "paired data directory")
        hoj_directories = {f"{root}/" for root in hoj_roots}
        hoj_entries, hoj_identities = _hoj_problems(
            archive,
            info_by_name,
            hoj_directories,
            fallback_id,
        )
        _set_entries(
            "hoj",
            "directory",
            hoj_entries,
            layouts,
            problems,
            problem_counts,
        )
        problem_identities["hoj"] = hoj_identities

    fps_entries = _fps_problems(archive, names, info_by_name, fallback_id)
    if fps_entries is not None:
        add("fps", 0.96, "problem.xml/fps.xml")
        layouts["fps"] = "xml"
        problems["fps"] = fps_entries
        problem_counts["fps"] = len(fps_entries)

    uoj_roots = _roots_for_suffix(names, "problem.conf")
    if uoj_roots:
        add("uoj", 0.97, "problem.conf")
        _set_profile(
            "uoj",
            "directory",
            uoj_roots,
            fallback_id,
            layouts,
            problems,
            problem_counts,
        )

    dmoj_roots = _roots_for_suffix(names, "init.yml")
    if dmoj_roots:
        add("dmoj", 0.97, "init.yml")
        _set_profile(
            "dmoj",
            "directory",
            dmoj_roots,
            fallback_id,
            layouts,
            problems,
            problem_counts,
        )

    icpc_roots = {
        root
        for root in problem_yaml_roots
        if root in probhub_secret_roots and root in probhub_statement_roots
    }
    if icpc_roots:
        add("icpc", 0.95, "problem.yaml", "data/secret", "statement directory")
        _set_profile(
            "icpc",
            "directory",
            icpc_roots,
            fallback_id,
            layouts,
            problems,
            problem_counts,
        )

    inputs = [
        name
        for name in names
        if PurePosixPath(name).suffix.lower() in {".in", ".input"}
    ]
    outputs = [
        name
        for name in names
        if PurePosixPath(name).suffix.lower() in {".ans", ".out", ".output"}
    ]
    if inputs and outputs:
        add(
            "generic",
            0.55,
            f"{len(inputs)} input files",
            f"{len(outputs)} output files",
        )
        generic_entries = _generic_problems(names, fallback_id)
        layouts["generic"] = "directory"
        problems["generic"] = generic_entries
        problem_counts["generic"] = len(generic_entries)

    return _ContentInspection(
        candidates=tuple(
            sorted(result, key=lambda item: (-item.confidence, item.format))
        ),
        layouts=layouts,
        problems=problems,
        problem_counts=problem_counts,
        problem_identities=problem_identities,
    )


def _roots_for_suffix(names: list[str], suffix: str) -> set[str]:
    suffix_key = suffix.casefold()
    return {
        name[: -len(suffix)] for name in names if name.casefold().endswith(suffix_key)
    }


def _roots_for_marker(names: list[str], marker: str) -> set[str]:
    marker_key = marker.casefold()
    roots: set[str] = set()
    for name in names:
        index = name.casefold().find(marker_key)
        if index >= 0:
            roots.add(name[:index])
    return roots


def _safe_identifier(value: str, fallback: str) -> str:
    normalized = re.sub(r"[^\w.-]+", "-", value, flags=re.UNICODE).strip("._-")
    return normalized or fallback


def _problem_from_root(root: str, fallback_id: str) -> DetectedProblem:
    path = root.rstrip("/")
    identifier = _safe_identifier(PurePosixPath(path).name if path else "", fallback_id)
    return DetectedProblem(identifier, path or ".")


def _set_entries(
    format_id: str,
    layout: str,
    entries: tuple[DetectedProblem, ...],
    layouts: dict[str, str],
    problems: dict[str, tuple[DetectedProblem, ...]],
    problem_counts: dict[str, int],
) -> None:
    layouts[format_id] = layout
    problems[format_id] = entries
    problem_counts[format_id] = len(entries)


def _set_profile(
    format_id: str,
    layout: str,
    roots: set[str],
    fallback_id: str,
    layouts: dict[str, str],
    problems: dict[str, tuple[DetectedProblem, ...]],
    problem_counts: dict[str, int],
) -> None:
    entries = tuple(
        _problem_from_root(root, fallback_id)
        for root in sorted(roots, key=lambda value: value.casefold())
    )
    _set_entries(
        format_id,
        layout,
        entries,
        layouts,
        problems,
        problem_counts,
    )


def _archive_mapping(
    archive: zipfile.ZipFile,
    info_by_name: dict[str, zipfile.ZipInfo],
    name: str,
) -> dict[str, Any]:
    info = info_by_name.get(name.casefold())
    if info is None or info.file_size > MAX_DETECTION_METADATA_BYTES:
        return {}
    try:
        text = archive.read(info).decode("utf-8-sig")
        if name.casefold().endswith((".yaml", ".yml")):
            value = yaml.load(text, Loader=_LimitedSafeLoader)  # nosec B506
        else:
            value = json.loads(text)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError, zipfile.BadZipFile):
        return {}
    return value if isinstance(value, dict) else {}


def _mapping_fingerprint(document: dict[str, Any]) -> str | None:
    try:
        payload = json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        return None
    return hashlib.sha256(payload).hexdigest()


def _nested_mapping_value(
    document: dict[str, Any],
    path: tuple[str, ...],
) -> str | None:
    value: Any = document
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    return text if text and len(text) <= 256 else None


def _hoj_problems(
    archive: zipfile.ZipFile,
    info_by_name: dict[str, zipfile.ZipInfo],
    roots: set[str],
    fallback_id: str,
) -> tuple[
    tuple[DetectedProblem, ...],
    tuple[_ProblemIdentity | None, ...],
]:
    entries: list[DetectedProblem] = []
    identities: list[_ProblemIdentity | None] = []
    for root in sorted(roots, key=lambda value: value.casefold()):
        document = _archive_mapping(
            archive,
            info_by_name,
            f"{root.rstrip('/')}.json",
        )
        identifier = _nested_mapping_value(document, ("problem", "problemId"))
        base = _problem_from_root(root, fallback_id)
        entries.append(DetectedProblem(identifier or base.id, base.path))
        fingerprint = _mapping_fingerprint(document) if identifier is not None else None
        identities.append(
            _ProblemIdentity(identifier, fingerprint)
            if identifier is not None and fingerprint is not None
            else None
        )
    return tuple(entries), tuple(identities)


def _metadata_problems(
    archive: zipfile.ZipFile,
    info_by_name: dict[str, zipfile.ZipInfo],
    roots: set[str],
    metadata_name: str,
    *identifier_paths: tuple[str, ...],
    fallback_id: str,
    indexed_fallback: bool = False,
    metadata_name_for_root: Callable[[str], str] | None = None,
) -> tuple[DetectedProblem, ...]:
    entries: list[DetectedProblem] = []
    for index, root in enumerate(
        sorted(roots, key=lambda value: value.casefold()),
        start=1,
    ):
        name = (
            metadata_name_for_root(root)
            if metadata_name_for_root is not None
            else f"{root}{metadata_name}"
        )
        document = _archive_mapping(archive, info_by_name, name)
        identifier = next(
            (
                value
                for path in identifier_paths
                if (value := _nested_mapping_value(document, path)) is not None
            ),
            None,
        )
        base = _problem_from_root(root, fallback_id)
        if identifier is None and indexed_fallback:
            identifier = f"problem-{index}"
        entries.append(DetectedProblem(identifier or base.id, base.path))
    return tuple(entries)


def _probhub_workspace_problems(
    archive: zipfile.ZipFile,
    names: list[str],
    info_by_name: dict[str, zipfile.ZipInfo],
) -> tuple[DetectedProblem, ...]:
    manifests = [
        name for name in names if name.casefold().endswith(".probhub/workspace.yaml")
    ]
    if len(manifests) != 1:
        return ()
    manifest_name = manifests[0]
    document = _archive_mapping(archive, info_by_name, manifest_name)
    raw_entries = document.get("problems")
    if not isinstance(raw_entries, list) or not raw_entries:
        return ()
    workspace_prefix = manifest_name[: -len(".probhub/workspace.yaml")]
    entries: list[DetectedProblem] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            return ()
        identifier = raw.get("id")
        directory = raw.get("directory")
        if not isinstance(identifier, (str, int)) or isinstance(identifier, bool):
            return ()
        identifier_text = str(identifier).strip()
        if (
            not identifier_text
            or len(identifier_text) > 256
            or not isinstance(directory, str)
        ):
            return ()
        normalized_directory = _normalized_name(directory.strip("/"))
        if not normalized_directory:
            return ()
        problem_path = f"{workspace_prefix}{normalized_directory}".rstrip("/")
        config_name = f"{problem_path}/probhub.yaml".casefold()
        if config_name not in info_by_name:
            return ()
        entries.append(DetectedProblem(identifier_text, problem_path))
    return tuple(entries)


def _xml_root(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
) -> object | None:
    if info.file_size > MAX_DETECTION_XML_BYTES:
        return None
    try:
        payload = archive.read(info)
        return DefusedElementTree.fromstring(
            payload,
            forbid_dtd=False,
            forbid_entities=True,
            forbid_external=True,
        )
    except (OSError, ValueError, zipfile.BadZipFile, DefusedElementTree.ParseError):
        return None


def _local_xml_name(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1].casefold()


def _polygon_single_problem(
    archive: zipfile.ZipFile,
    names: list[str],
    info_by_name: dict[str, zipfile.ZipInfo],
    fallback_id: str,
) -> DetectedProblem | None:
    candidates = [
        name
        for name in names
        if PurePosixPath(name).name.casefold() == "problem.xml"
        and "/problems/" not in f"/{name.casefold()}"
    ]
    for name in candidates:
        root = _xml_root(archive, info_by_name[name.casefold()])
        if root is not None and _local_xml_name(getattr(root, "tag", "")) == "problem":
            directory = name[: -len("problem.xml")]
            return _problem_from_root(directory, fallback_id)
    return None


def _fps_problems(
    archive: zipfile.ZipFile,
    names: list[str],
    info_by_name: dict[str, zipfile.ZipInfo],
    fallback_id: str,
) -> tuple[DetectedProblem, ...] | None:
    candidates = [
        name
        for name in names
        if PurePosixPath(name).name.casefold() in {"problem.xml", "fps.xml"}
        and "/problems/" not in f"/{name.casefold()}"
    ]
    if len(candidates) != 1:
        return None
    name = candidates[0]
    root = _xml_root(archive, info_by_name[name.casefold()])
    if root is None or _local_xml_name(getattr(root, "tag", "")) != "fps":
        return None
    entries: list[DetectedProblem] = []
    for index, item in enumerate(list(root), start=1):  # type: ignore[arg-type]
        if _local_xml_name(getattr(item, "tag", "")) != "item":
            continue
        values: dict[str, str] = {}
        for child in list(item):
            key = _local_xml_name(getattr(child, "tag", ""))
            if key in {"remote_id", "title"} and getattr(child, "text", None):
                values[key] = str(child.text).strip()
        identifier = _safe_identifier(
            values.get("remote_id") or values.get("title") or "",
            f"{fallback_id}-{index}",
        )
        entries.append(DetectedProblem(identifier, f"{name}#item-{index}"))
    return tuple(entries) if entries else None


def _generic_problems(
    names: list[str],
    fallback_id: str,
) -> tuple[DetectedProblem, ...]:
    normalized = _strip_common_root_for_cases(names)
    lowered = {name.casefold() for name in normalized}
    paired_inputs = []
    for name in normalized:
        path = PurePosixPath(name)
        if path.suffix.lower() not in {".in", ".input"}:
            continue
        stem = path.with_suffix("").as_posix()
        if any(
            f"{stem}{suffix}".casefold() in lowered
            for suffix in (".ans", ".out", ".output")
        ):
            paired_inputs.append(name)
    if not paired_inputs:
        return ()
    top_level = {
        PurePosixPath(name).parts[0]
        for name in paired_inputs
        if len(PurePosixPath(name).parts) > 1
    }
    if any(len(PurePosixPath(name).parts) == 1 for name in paired_inputs):
        return (DetectedProblem(fallback_id, "."),)
    if len(top_level) <= 1:
        value = next(iter(top_level), fallback_id)
        return (DetectedProblem(_safe_identifier(value, fallback_id), value),)
    return tuple(
        DetectedProblem(_safe_identifier(value, fallback_id), value)
        for value in sorted(top_level, key=str.casefold)
    )


def _strip_common_root_for_cases(names: list[str]) -> list[str]:
    case_names = [
        name
        for name in names
        if PurePosixPath(name).suffix.lower()
        in {".in", ".input", ".ans", ".out", ".output"}
    ]
    first_parts = {
        PurePosixPath(name).parts[0]
        for name in case_names
        if len(PurePosixPath(name).parts) > 1
    }
    if (
        case_names
        and len(first_parts) == 1
        and all(len(PurePosixPath(name).parts) > 1 for name in case_names)
    ):
        root = next(iter(first_parts))
        prefix = f"{root}/"
        return [
            name[len(prefix) :] if name.startswith(prefix) else name for name in names
        ]
    return names


def _inspect_nested_archives(
    archive: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
    *,
    budget: _ArchiveBudget,
) -> _ContentInspection | None:
    nested_infos = [
        info
        for info in infos
        if not info.is_dir() and info.filename.casefold().endswith(".zip")
    ]
    if not nested_infos:
        return None
    if len(nested_infos) > MAX_NESTED_PACKAGES:
        raise ValueError(f"nested package count exceeds {MAX_NESTED_PACKAGES}")

    nested_contents: list[tuple[zipfile.ZipInfo, _ContentInspection, str]] = []
    for info in nested_infos:
        with tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024) as temporary:
            try:
                with archive.open(info) as source:
                    shutil.copyfileobj(source, temporary, length=1024 * 1024)
                temporary.seek(0)
                with zipfile.ZipFile(temporary) as nested_archive:
                    nested_members = nested_archive.infolist()
                    _validate_archive_infos(nested_members, budget=budget)
                    fallback_id = _safe_identifier(
                        PurePosixPath(info.filename).stem, "problem"
                    )
                    nested_contents.append(
                        (
                            info,
                            _inspect_archive_content(
                                nested_archive,
                                nested_members,
                                fallback_id=fallback_id,
                            ),
                            fallback_id,
                        )
                    )
            except zipfile.BadZipFile:
                continue
    if not nested_contents:
        return None

    selected: list[
        tuple[zipfile.ZipInfo, _ContentInspection, str, FormatDetection]
    ] = []
    for info, content, fallback_id in nested_contents:
        strong = [
            candidate for candidate in content.candidates if candidate.confidence >= 0.8
        ]
        if len(strong) != 1:
            return None
        selected.append((info, content, fallback_id, strong[0]))

    formats = {item[3].format for item in selected}
    if len(formats) > 1:
        candidates = []
        for format_id in sorted(formats):
            confidence = max(
                item[3].confidence for item in selected if item[3].format == format_id
            )
            candidates.append(
                FormatDetection(
                    format_id,
                    confidence,
                    ("mixed nested ZIP packages",),
                )
            )
        return _ContentInspection(tuple(candidates), {}, {}, {})

    format_id = next(iter(formats))
    entries: list[DetectedProblem] = []
    identities: list[_ProblemIdentity | None] = []
    count = 0
    confidence = 1.0
    for info, content, fallback_id, candidate in selected:
        confidence = min(confidence, candidate.confidence)
        inner_entries = content.problems.get(format_id, ())
        inner_identities = content.problem_identities.get(format_id, ())
        inner_count = content.problem_counts.get(format_id)
        count += inner_count if inner_count is not None else 1
        if inner_entries:
            outer_path = PurePosixPath(info.filename).as_posix()
            entries.extend(
                DetectedProblem(
                    entry.id,
                    outer_path if entry.path == "." else f"{outer_path}!/{entry.path}",
                )
                for entry in inner_entries
            )
            identities.extend(
                inner_identities
                if len(inner_identities) == len(inner_entries)
                else (None,) * len(inner_entries)
            )
        else:
            entries.append(
                DetectedProblem(
                    fallback_id,
                    PurePosixPath(info.filename).as_posix(),
                )
            )
            identities.append(None)
    candidate = FormatDetection(
        format_id,
        confidence,
        (f"{len(selected)} nested ZIP problem packages",),
    )
    return _ContentInspection(
        candidates=(candidate,),
        layouts={format_id: "nested"},
        problems={format_id: tuple(entries)},
        problem_counts={format_id: count},
        problem_identities={format_id: tuple(identities)},
    )


def _merge_expanded_hoj_content(
    expanded: _ContentInspection,
    nested: _ContentInspection,
) -> _ContentInspection | None:
    nested_strong = [
        candidate for candidate in nested.candidates if candidate.confidence >= 0.8
    ]
    if len(nested_strong) != 1 or nested_strong[0].format != "hoj":
        return None

    expanded_entries = expanded.problems.get("hoj", ())
    nested_entries = nested.problems.get("hoj", ())
    expanded_identities = expanded.problem_identities.get("hoj", ())
    nested_identities = nested.problem_identities.get("hoj", ())
    if (
        not expanded_entries
        or not nested_entries
        or len(expanded_entries) != len(expanded_identities)
        or len(nested_entries) != len(nested_identities)
        or any(identity is None for identity in expanded_identities)
        or any(identity is None for identity in nested_identities)
    ):
        return None

    merged_entries: list[DetectedProblem] = []
    merged_identities: list[_ProblemIdentity] = []
    fingerprints: dict[str, str] = {}
    for entry, identity in zip(
        (*expanded_entries, *nested_entries),
        (*expanded_identities, *nested_identities),
        strict=True,
    ):
        if identity is None:
            return None
        existing = fingerprints.get(identity.identifier)
        if existing is not None:
            if existing != identity.fingerprint:
                return None
            continue
        fingerprints[identity.identifier] = identity.fingerprint
        merged_entries.append(entry)
        merged_identities.append(identity)

    layouts = dict(expanded.layouts)
    problems = dict(expanded.problems)
    problem_counts = dict(expanded.problem_counts)
    problem_identities = dict(expanded.problem_identities)
    layouts["hoj"] = "nested"
    problems["hoj"] = tuple(merged_entries)
    problem_counts["hoj"] = len(merged_entries)
    problem_identities["hoj"] = tuple(merged_identities)
    return _ContentInspection(
        candidates=expanded.candidates,
        layouts=layouts,
        problems=problems,
        problem_counts=problem_counts,
        problem_identities=problem_identities,
    )


def detected_format(candidates: list[FormatDetection]) -> str | None:
    strong = [candidate for candidate in candidates if candidate.confidence >= 0.8]
    return strong[0].format if len(strong) == 1 else None


def _normalized_name(name: str) -> str:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        return ""
    return normalized


def _validate_archive_infos(
    infos: list[zipfile.ZipInfo],
    *,
    budget: _ArchiveBudget | None = None,
) -> None:
    archive_budget = budget or _ArchiveBudget()
    if archive_budget.entries + len(infos) > MAX_ARCHIVE_ENTRIES:
        raise ValueError(f"archive entry limit exceeded ({MAX_ARCHIVE_ENTRIES})")
    seen: set[str] = set()
    for info in infos:
        name = _normalized_name(info.filename)
        if not name:
            raise ValueError(f"unsafe zip member path: {info.filename}")
        key = name.rstrip("/").casefold()
        if key in seen:
            raise ValueError(f"duplicate zip member path: {info.filename}")
        seen.add(key)
        if ((info.external_attr >> 16) & 0o170000) == 0o120000:
            raise ValueError(f"zip symlinks are not supported: {info.filename}")
        if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
            raise ValueError(f"archive member exceeds size limit: {info.filename}")
        archive_budget.entries += 1
        archive_budget.uncompressed_bytes += info.file_size
        if archive_budget.uncompressed_bytes > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            raise ValueError("archive exceeds uncompressed size limit")
        if (
            info.file_size > MIN_ARCHIVE_COMPRESSION_RATIO_BYTES
            and info.file_size / max(info.compress_size, 1)
            > MAX_ARCHIVE_COMPRESSION_RATIO
        ):
            raise ValueError(
                f"archive member exceeds compression ratio limit: {info.filename}"
            )
