"""Unprivileged public runner; results remain on bounded tmpfs until collected.

All messages and files produced here are untrusted by the host Worker. Keeping
PID 1 alive preserves tmpfs until the read-only exporter collects the output.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404
import sys
import time
from dataclasses import asdict
from pathlib import Path


def main():
    code = 1
    try:
        if sys.argv[1] == "inspect":
            from format_detection import inspect_zip_package

            result = inspect_zip_package(
                Path("/input/contest.zip"), fallback_id="upload"
            )
            Path("/output/inspection.json").write_text(json.dumps(asdict(result)))
            code = 0
        else:
            # All arguments come from the validated conversion request.
            code = subprocess.call(  # nosec B603
                [sys.executable, "/opt/p2h_safe.py", *sys.argv[1:]],
                stdout=sys.stderr,
                stderr=sys.stderr,
            )
    except Exception as exc:
        print(str(exc)[:2048], file=sys.stderr, flush=True)
    print("P2H_PUBLIC_DONE " + str(code), flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
