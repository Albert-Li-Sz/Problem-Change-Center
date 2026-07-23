from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "runner"
if str(RUNNER) not in sys.path:
    sys.path.insert(0, str(RUNNER))

from adapters import ADAPTERS, READABLE_FORMATS, WRITABLE_FORMATS  # noqa: E402


def test_adapter_registry_has_one_entry_for_every_readable_format() -> None:
    assert tuple(ADAPTERS) == READABLE_FORMATS
    assert all(adapter.format == format_id for format_id, adapter in ADAPTERS.items())


def test_adapter_read_write_contract_matches_declared_capabilities() -> None:
    for format_id, adapter in ADAPTERS.items():
        assert callable(adapter.detect)
        assert callable(adapter.read)
        if format_id in WRITABLE_FORMATS:
            assert callable(adapter.validate_target)
            assert callable(adapter.write)
        else:
            assert adapter.validate_target is None
            assert adapter.write is None


def test_each_detector_only_returns_its_own_format(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "1.in").write_text("1\n", encoding="utf-8")
    (tmp_path / "data" / "1.out").write_text("1\n", encoding="utf-8")

    matches = {
        format_id: detection
        for format_id, adapter in ADAPTERS.items()
        if (detection := adapter.detect(tmp_path)) is not None
    }

    assert set(matches) == {"generic"}
    assert matches["generic"].format == "generic"
    assert 0 < matches["generic"].confidence <= 1
    assert matches["generic"].evidence
