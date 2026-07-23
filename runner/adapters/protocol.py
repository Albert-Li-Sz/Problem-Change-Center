from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import format_bridge as bridge
from package_ir import ProblemBundle
from progress import ProgressReporter


READABLE_FORMATS = (
    "probhub",
    "hydro",
    "icpc",
    "hoj",
    "fps",
    "qduoj",
    "uoj",
    "dmoj",
    "generic",
)
WRITABLE_FORMATS = ("hydro", "icpc", "hoj", "fps", "qduoj", "uoj", "dmoj")


@dataclass(frozen=True)
class Detection:
    format: str
    confidence: float
    evidence: tuple[str, ...]


Reader = Callable[
    [
        Path,
        Path,
        Iterable[str],
        bridge.ArchiveExtractionBudget | None,
        ProgressReporter | None,
    ],
    ProblemBundle,
]
TargetValidator = Callable[[ProblemBundle, dict[str, Any]], None]
Writer = Callable[[ProblemBundle, Path, dict[str, Any]], list[str]]


@dataclass(frozen=True)
class Adapter:
    format: str
    detect: Callable[[Path], Detection | None]
    read: Reader
    validate_target: TargetValidator | None
    write: Writer | None
