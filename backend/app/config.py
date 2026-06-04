from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_DATA_DIR = Path.home() / ".p2h-web-ui" / "backend_data"
DEFAULT_DATABASE_URL = "postgresql://p2h:p2h_dev_password@127.0.0.1:5432/p2h"


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
    docker_tmp_size: str
    docker_work_size: str
    database_url: str = DEFAULT_DATABASE_URL
    auth_secret_key: str = "change-me-local-dev-secret"
    auth_token_ttl_seconds: int = 7 * 24 * 60 * 60
    default_daily_quota: int = 10
    max_concurrent_jobs: int = 2
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=Path(os.getenv("P2H_DATA_DIR", str(DEFAULT_DATA_DIR))).expanduser().resolve(),
            docker_bin=os.getenv("P2H_DOCKER_BIN", "docker"),
            runner_image=os.getenv("P2H_RUNNER_IMAGE", "p2h-runner"),
            max_upload_bytes=_int_env("P2H_MAX_UPLOAD_BYTES", 128 * 1024 * 1024),
            job_timeout_seconds=_int_env("P2H_JOB_TIMEOUT_SECONDS", 600),
            job_ttl_seconds=_int_env("P2H_JOB_TTL_SECONDS", 24 * 60 * 60),
            docker_memory=os.getenv("P2H_DOCKER_MEMORY", "1g"),
            docker_cpus=os.getenv("P2H_DOCKER_CPUS", "2"),
            docker_pids_limit=_int_env("P2H_DOCKER_PIDS_LIMIT", 256),
            docker_tmp_size=os.getenv("P2H_DOCKER_TMP_SIZE", "512m"),
            docker_work_size=os.getenv("P2H_DOCKER_WORK_SIZE", "1g"),
            database_url=os.getenv("P2H_DATABASE_URL", DEFAULT_DATABASE_URL),
            auth_secret_key=os.getenv("P2H_AUTH_SECRET_KEY", "change-me-local-dev-secret"),
            auth_token_ttl_seconds=_int_env("P2H_AUTH_TOKEN_TTL_SECONDS", 7 * 24 * 60 * 60),
            default_daily_quota=_int_env("P2H_DEFAULT_DAILY_QUOTA", 10),
            max_concurrent_jobs=_int_env("P2H_MAX_CONCURRENT_JOBS", 2),
            bootstrap_admin_email=os.getenv("P2H_BOOTSTRAP_ADMIN_EMAIL", ""),
            bootstrap_admin_password=os.getenv("P2H_BOOTSTRAP_ADMIN_PASSWORD", ""),
        )


settings = Settings.from_env()
