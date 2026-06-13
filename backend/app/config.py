from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_DATA_DIR = Path.home() / ".p2h-web-ui" / "backend_data"


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


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

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=Path(os.getenv("P2H_DATA_DIR", str(DEFAULT_DATA_DIR))).expanduser().resolve(),
            docker_bin=os.getenv("P2H_DOCKER_BIN", "docker"),
            runner_image=os.getenv("P2H_RUNNER_IMAGE", "p2h-runner"),
            max_upload_bytes=_int_env("P2H_MAX_UPLOAD_BYTES", 512 * 1024 * 1024),
            job_timeout_seconds=_int_env("P2H_JOB_TIMEOUT_SECONDS", 600),
            job_ttl_seconds=_int_env("P2H_JOB_TTL_SECONDS", 24 * 60 * 60),
            docker_memory=os.getenv("P2H_DOCKER_MEMORY", "1g"),
            docker_cpus=os.getenv("P2H_DOCKER_CPUS", "2"),
            docker_pids_limit=_int_env("P2H_DOCKER_PIDS_LIMIT", 1024),
            docker_wine_pids_limit=_int_env("P2H_DOCKER_WINE_PIDS_LIMIT", 4096),
            docker_wine_home_size=os.getenv("P2H_DOCKER_WINE_HOME_SIZE", "4g"),
            docker_tmp_size=os.getenv("P2H_DOCKER_TMP_SIZE", "512m"),
            docker_work_size=os.getenv("P2H_DOCKER_WORK_SIZE", "1g"),
        )


settings = Settings.from_env()
