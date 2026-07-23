from __future__ import annotations

from pathlib import Path
from typing import Callable

from .protocol import Adapter, Detection, Reader, TargetValidator, Writer


Detector = Callable[[Path], list[Detection]]


def build_adapter(
    format_id: str,
    *,
    detector: Detector,
    reader: Reader,
    validator: TargetValidator | None = None,
    writer: Writer | None = None,
) -> Adapter:
    def detect(root: Path) -> Detection | None:
        return next(
            (
                candidate
                for candidate in detector(root)
                if candidate.format == format_id
            ),
            None,
        )

    return Adapter(format_id, detect, reader, validator, writer)
