from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


MAX_ARCHIVE_ENTRIES = 50_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_COMPRESSION_RATIO = 200.0


@dataclass(frozen=True)
class FormatDetection:
    format: str
    confidence: float
    evidence: tuple[str, ...]


def detect_zip_format(path: Path) -> list[FormatDetection]:
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        _validate_archive_infos(infos)
        names = [
            _normalized_name(info.filename)
            for info in infos
            if info.filename and not info.is_dir()
        ]
    lowered = [name.lower() for name in names]
    result: list[FormatDetection] = []

    def add(format_id: str, confidence: float, *evidence: str) -> None:
        result.append(FormatDetection(format_id, confidence, tuple(evidence)))

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
    if (
        any(name.endswith("problem.yaml") for name in lowered)
        and any("/data/secret/" in f"/{name}" for name in lowered)
        and any(
            "/problem_statement/" in f"/{name}" or "/statement/problem." in f"/{name}"
            for name in lowered
        )
    ):
        add("icpc", 0.95, "problem.yaml", "data/secret", "statement directory")
    inputs = [
        name for name in lowered if PurePosixPath(name).suffix in {".in", ".input"}
    ]
    outputs = [
        name
        for name in lowered
        if PurePosixPath(name).suffix in {".ans", ".out", ".output"}
    ]
    if inputs and outputs:
        add(
            "generic",
            0.55,
            f"{len(inputs)} input files",
            f"{len(outputs)} output files",
        )
    return sorted(result, key=lambda item: (-item.confidence, item.format))


def detected_format(candidates: list[FormatDetection]) -> str | None:
    strong = [candidate for candidate in candidates if candidate.confidence >= 0.8]
    return strong[0].format if len(strong) == 1 else None


def _normalized_name(name: str) -> str:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        return ""
    return normalized


def _validate_archive_infos(infos: list[zipfile.ZipInfo]) -> None:
    if len(infos) > MAX_ARCHIVE_ENTRIES:
        raise ValueError(f"archive entry limit exceeded ({MAX_ARCHIVE_ENTRIES})")
    total = 0
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
        total += info.file_size
        if total > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            raise ValueError("archive exceeds uncompressed size limit")
        if (
            info.file_size
            and info.file_size / max(info.compress_size, 1)
            > MAX_ARCHIVE_COMPRESSION_RATIO
        ):
            raise ValueError(
                f"archive member exceeds compression ratio limit: {info.filename}"
            )
