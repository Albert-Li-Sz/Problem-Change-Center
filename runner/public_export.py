"""Trusted, read-only exporter invoked with Python isolated mode by the Worker."""

from __future__ import annotations

import os
import signal
import stat
import sys
import tarfile
import time
from pathlib import Path


def stop_descendants():
    # PID 1 is the read-only supervisor, asleep after emitting its completion
    # marker. Stop/kill every other task before traversing output. Repeat to
    # account for a process forking just before SIGSTOP. No capabilities needed:
    # all tasks have the same fixed UID and cannot switch it.
    myself = os.getpid()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        found = False
        for path in Path("/proc").iterdir():
            if not path.name.isdigit() or int(path.name) in {1, myself}:
                continue
            try:
                state = (path / "stat").read_text().rsplit(")", 1)[1].split()[0]
                if state in {"Z", "X"}:
                    continue
                found = True
                os.kill(int(path.name), signal.SIGSTOP)
                os.kill(int(path.name), signal.SIGKILL)
            except ProcessLookupError:
                pass
            except FileNotFoundError:
                pass
        if not found:
            return
    raise RuntimeError("Cannot quiesce container processes")


def main():
    stop_descendants()
    root = Path("/output")
    paths = []
    total = 0
    for directory, folders, files in os.walk(root, followlinks=False):
        for name in folders + files:
            path = Path(directory) / name
            info = path.lstat()
            if (
                not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                or info.st_nlink > 1
                and stat.S_ISREG(info.st_mode)
            ):
                raise ValueError("Output links and special files are forbidden")
            paths.append(path)
            total += info.st_size if stat.S_ISREG(info.st_mode) else 0
            if len(paths) > 50000 or total > 1024**3:
                raise ValueError("Output exceeds limits")
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
        for path in paths:
            archive.add(
                path, arcname=path.relative_to(root).as_posix(), recursive=False
            )


if __name__ == "__main__":
    main()
