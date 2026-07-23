#!/usr/bin/env python3
from __future__ import annotations

import json
import signal
import sys
import time
from pathlib import Path


def _argument(name: str, default: str) -> str:
    try:
        return sys.argv[sys.argv.index(name) + 1]
    except (ValueError, IndexError):
        return default


def _result_directory() -> Path:
    for index, argument in enumerate(sys.argv[:-1]):
        if argument != "-v":
            continue
        value = sys.argv[index + 1]
        suffix = ":/result:rw"
        if value.endswith(suffix):
            return Path(value[: -len(suffix)])
    raise SystemExit("missing /result volume")


def _progress(phase: str, current: int, total: int, detail: str) -> None:
    event = {
        "schema_version": 1,
        "event": "progress",
        "phase": phase,
        "current": current,
        "total": total,
        "unit": "problems",
        "problem": "sum",
        "detail": detail,
        "timestamp": "2026-07-23T12:00:00+00:00",
    }
    print("P2H_EVENT " + json.dumps(event, separators=(",", ":")), flush=True)


def main() -> int:
    if len(sys.argv) < 2:
        return 2
    if sys.argv[1] in {"rm", "info", "image"}:
        return 0

    source_format = _argument("--source-format", "hydro")
    target_format = _argument("--target-format", "icpc")
    result = _result_directory()
    result.mkdir(parents=True, exist_ok=True)

    _progress("read", 1, 1, "read compatibility corpus")
    if target_format == "dmoj":
        print("fake runner: waiting for cancellation", flush=True)

        def stop(_signum: int, _frame: object) -> None:
            raise SystemExit(143)

        signal.signal(signal.SIGTERM, stop)
        while True:
            time.sleep(0.2)

    artifact = result / "sum" / "problem.yaml"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("name: Sum\n", encoding="utf-8")
    report = {
        "schema_version": 1,
        "source_format": source_format,
        "target_format": target_format,
        "problem_count": 1,
        "counts": {"warning": 0, "loss": 0, "fatal": 0},
        "issues": [],
        "artifacts": ["sum/problem.yaml"],
    }
    (result / ".p2h-report.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    _progress("write", 1, 1, "wrote converted package")
    print("fake runner: conversion complete", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
