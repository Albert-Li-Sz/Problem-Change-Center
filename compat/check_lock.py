#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


EXPECTED_PLATFORMS = {"hydro", "domjudge", "hoj"}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def validate_lock(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read compatibility lock: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("compatibility lock must use schema_version 1")
    platforms = data.get("platforms")
    if not isinstance(platforms, dict) or set(platforms) != EXPECTED_PLATFORMS:
        raise ValueError("compatibility lock must pin exactly hydro, domjudge, and hoj")
    for name, raw in platforms.items():
        if not isinstance(raw, dict):
            raise ValueError(f"{name} compatibility pin must be an object")
        for field in ("version", "source", "git_commit"):
            value = raw.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} compatibility pin is missing {field}")
        if not SHA_RE.fullmatch(raw["git_commit"]):
            raise ValueError(f"{name} git_commit must be a full immutable SHA")
        if name == "hydro":
            integrity = raw.get("integrity")
            if not isinstance(integrity, str) or not integrity.startswith("sha512-"):
                raise ValueError("Hydro npm pin must include sha512 integrity")
    return data


def main() -> int:
    path = Path(__file__).with_name("versions.lock.json")
    try:
        data = validate_lock(path)
    except ValueError as exc:
        print(f"compatibility lock error: {exc}", file=sys.stderr)
        return 1
    versions = ", ".join(
        f"{name}={entry['version']}"
        for name, entry in sorted(data["platforms"].items())
    )
    print(f"compatibility lock valid: {versions}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
