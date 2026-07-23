from __future__ import annotations

import os
import math
import re
from dataclasses import dataclass
from pathlib import Path


DEFAULT_DATA_DIR = Path.home() / ".p2h-web-ui" / "backend_data"


_SIZE_RE = re.compile(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([kmgt]?)(?:i?b)?\s*", re.IGNORECASE)


def _int_env(
    name: str,
    default: int,
    *,
    minimum: int = 1,
    allow_minus_one: bool = False,
) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if allow_minus_one and value == -1:
        return value
    if value < minimum:
        suffix = " or -1" if allow_minus_one else ""
        raise ValueError(f"{name} must be at least {minimum}{suffix}")
    return value


def _size_env(name: str, default: str) -> str:
    value = os.getenv(name, default).strip()
    if len(value) > 32:
        raise ValueError(f"{name} must be a positive Docker size such as 512m or 1g")
    match = _SIZE_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"{name} must be a positive Docker size such as 512m or 1g")
    multipliers = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
    numeric = float(match.group(1))
    if not math.isfinite(numeric):
        raise ValueError(f"{name} must be a positive Docker size")
    size = int(numeric * multipliers[match.group(2).lower()])
    if size <= 0:
        raise ValueError(f"{name} must be a positive Docker size")
    return value


def _positive_number_env(name: str, default: str) -> str:
    value = os.getenv(name, default).strip()
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{name} must be a positive number")
    return value


def _nonempty_env(name: str, default: str) -> str:
    value = os.getenv(name, default).strip()
    if not value:
        raise ValueError(f"{name} must not be empty")
    return value


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    docker_bin: str
    runner_image: str
    max_upload_bytes: int
    job_timeout_seconds: int
    job_ttl_seconds: int
    docker_memory: str
    docker_cpus: str
    docker_pids_limit: int
    docker_wine_pids_limit: int | None
    docker_wine_home_size: str
    docker_tmp_size: str
    docker_work_size: str
    docker_output_size: str = "1g"
    max_concurrent_jobs: int = 2
    max_stored_jobs: int = 100
    max_storage_bytes: int = 10 * 1024 * 1024 * 1024
    max_log_bytes: int = 10 * 1024 * 1024

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=Path(_nonempty_env("P2H_DATA_DIR", str(DEFAULT_DATA_DIR)))
            .expanduser()
            .resolve(),
            docker_bin=_nonempty_env("P2H_DOCKER_BIN", "docker"),
            runner_image=_nonempty_env("P2H_RUNNER_IMAGE", "p2h-runner"),
            max_upload_bytes=_int_env("P2H_MAX_UPLOAD_BYTES", 512 * 1024 * 1024),
            job_timeout_seconds=_int_env("P2H_JOB_TIMEOUT_SECONDS", 600),
            job_ttl_seconds=_int_env("P2H_JOB_TTL_SECONDS", 24 * 60 * 60),
            docker_memory=_size_env("P2H_DOCKER_MEMORY", "1g"),
            docker_cpus=_positive_number_env("P2H_DOCKER_CPUS", "2"),
            docker_pids_limit=_int_env(
                "P2H_DOCKER_PIDS_LIMIT", 1024, allow_minus_one=True
            ),
            docker_wine_pids_limit=_int_env(
                "P2H_DOCKER_WINE_PIDS_LIMIT", 4096, allow_minus_one=True
            ),
            docker_wine_home_size=_size_env("P2H_DOCKER_WINE_HOME_SIZE", "4g"),
            docker_tmp_size=_size_env("P2H_DOCKER_TMP_SIZE", "512m"),
            docker_work_size=_size_env("P2H_DOCKER_WORK_SIZE", "1g"),
            docker_output_size=_size_env("P2H_DOCKER_OUTPUT_SIZE", "1g"),
            max_concurrent_jobs=_int_env("P2H_MAX_CONCURRENT_JOBS", 2),
            max_stored_jobs=_int_env("P2H_MAX_STORED_JOBS", 100),
            max_storage_bytes=_int_env(
                "P2H_MAX_STORAGE_BYTES", 10 * 1024 * 1024 * 1024
            ),
            max_log_bytes=_int_env("P2H_MAX_LOG_BYTES", 10 * 1024 * 1024),
        )


settings = Settings.from_env()
