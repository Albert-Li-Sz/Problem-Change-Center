from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from ..config import Settings

GIB = 1024**3
_SIZE = re.compile(r"[1-9][0-9]{0,5}[mg]")


def size_bytes(value: str) -> int:
    """Bytes for a Docker size such as 512m or 4g; 0 stays 0."""
    if value == "0":
        return 0
    if not _SIZE.fullmatch(value):
        raise ValueError(f"Invalid size {value!r}; use a whole number with m or g")
    return int(value[:-1]) * (GIB if value[-1] == "g" else 1024**2)


def _integer(name: str, default: int, low: int, high: int) -> int:
    value = int(os.getenv(name) or default)
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


@dataclass(frozen=True)
class PublicConfig:
    database_url: str
    origin: str = "https://localhost"
    data_dir: Path = Path("/srv/p2h/data")
    runner_image: str = "p2h-public-runner"
    wine_image: str = "p2h-public-runner-wine"
    turnstile_site_key: str = ""
    turnstile_secret: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    contact_email: str = ""
    development: bool = False
    captcha: str = "turnstile"
    max_concurrent: int = 10
    max_wine: int = 3
    max_upload: int = 512 * 1024**2
    session_seconds: int = 7 * 86400
    # Runner limits. tmpfs pages are charged to the task, so job_memory has to
    # cover work, output and home unless job_swap lets them spill to host swap.
    runner_runtime: str = "runc"
    job_memory: str = "4g"
    wine_memory: str = "6g"
    job_swap: str = "0"
    job_cpus: str = "2"
    work_size: str = "2g"
    output_size: str = "1g"
    home_size: str = "1g"
    wine_home_size: str = "2g"

    def __post_init__(self) -> None:
        if self.captcha not in {"turnstile", "off"}:
            raise ValueError("PUBLIC_CAPTCHA must be turnstile or off")
        if not 1 <= self.max_wine <= self.max_concurrent <= 64:
            raise ValueError("Need 1 <= PUBLIC_MAX_WINE <= PUBLIC_MAX_CONCURRENT <= 64")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", self.runner_runtime):
            raise ValueError("Invalid PUBLIC_RUNNER_RUNTIME")
        if not 0.25 <= float(self.job_cpus) <= 64:
            raise ValueError("PUBLIC_JOB_CPUS must be between 0.25 and 64")
        for name in ("job_memory", "wine_memory", "work_size", "output_size"):
            if size_bytes(getattr(self, name)) < 256 * 1024**2:
                raise ValueError(f"{name} must be at least 256m")
        for name in ("job_swap", "home_size", "wine_home_size"):
            size_bytes(getattr(self, name))
        if not 1024**2 <= self.max_upload <= 4 * GIB:
            raise ValueError("Upload limit must be between 1 MiB and 4 GiB")

    @property
    def output_limit(self) -> int:
        return min(GIB, size_bytes(self.output_size))

    @property
    def archive_limit(self) -> int:
        # Leave half of /work for the converter's own intermediate files.
        return min(GIB, size_bytes(self.work_size) // 2)

    @classmethod
    def from_env(cls) -> "PublicConfig":
        development = os.getenv("PUBLIC_DEVELOPMENT") == "1"
        origin = os.getenv("PUBLIC_ORIGIN", "https://localhost").rstrip("/")
        parsed = urlsplit(origin)
        if not parsed.hostname or parsed.path or parsed.query or parsed.fragment:
            raise ValueError("PUBLIC_ORIGIN must be an origin without a path")
        if parsed.username or parsed.password or parsed.scheme not in {"https", "http"}:
            raise ValueError("Invalid PUBLIC_ORIGIN")
        if not development and parsed.scheme != "https":
            raise ValueError("PUBLIC_ORIGIN requires HTTPS")
        return cls(
            database_url=os.environ["PUBLIC_DATABASE_URL"],
            origin=origin,
            data_dir=Path(os.getenv("PUBLIC_DATA_DIR", "/srv/p2h/data")).resolve(),
            runner_image=os.getenv("PUBLIC_RUNNER_IMAGE", "p2h-public-runner"),
            wine_image=os.getenv("PUBLIC_WINE_IMAGE", "p2h-public-runner-wine"),
            turnstile_site_key=os.getenv("TURNSTILE_SITE_KEY", ""),
            turnstile_secret=os.getenv("TURNSTILE_SECRET_KEY", ""),
            smtp_host=os.getenv("SMTP_HOST", ""),
            smtp_port=int(os.getenv("SMTP_PORT", "587")),
            smtp_user=os.getenv("SMTP_USER", ""),
            smtp_password=os.getenv("SMTP_PASSWORD", ""),
            smtp_from=os.getenv("SMTP_FROM", ""),
            contact_email=os.getenv("PUBLIC_CONTACT_EMAIL", ""),
            development=development,
            captcha=os.getenv("PUBLIC_CAPTCHA") or "turnstile",
            max_concurrent=_integer("PUBLIC_MAX_CONCURRENT", 10, 1, 64),
            max_wine=_integer("PUBLIC_MAX_WINE", 3, 1, 64),
            max_upload=_integer("PUBLIC_MAX_UPLOAD_MB", 512, 1, 4096) * 1024**2,
            runner_runtime=os.getenv("PUBLIC_RUNNER_RUNTIME") or "runc",
            job_memory=os.getenv("PUBLIC_JOB_MEMORY") or "4g",
            wine_memory=os.getenv("PUBLIC_WINE_MEMORY") or "6g",
            job_swap=os.getenv("PUBLIC_JOB_SWAP") or "0",
            job_cpus=os.getenv("PUBLIC_JOB_CPUS") or "2",
            work_size=os.getenv("PUBLIC_WORK_SIZE") or "2g",
            output_size=os.getenv("PUBLIC_OUTPUT_SIZE") or "1g",
            home_size=os.getenv("PUBLIC_HOME_SIZE") or "1g",
            wine_home_size=os.getenv("PUBLIC_WINE_HOME_SIZE") or "2g",
        )

    def validate_api(self) -> None:
        if self.development:
            return
        if not all((self.smtp_host, self.smtp_from, self.contact_email)):
            raise ValueError(
                "Configure SMTP and PUBLIC_CONTACT_EMAIL before public startup"
            )
        if self.captcha == "turnstile" and not (
            self.turnstile_site_key and self.turnstile_secret
        ):
            raise ValueError(
                "Configure Turnstile keys, or set PUBLIC_CAPTCHA=off to rely on "
                "the proof-of-work challenge and rate limits"
            )

    def storage_settings(self) -> Settings:
        return Settings(
            data_dir=self.data_dir,
            docker_bin="docker",
            runner_image=self.wine_image,
            max_upload_bytes=self.max_upload,
            max_request_body_bytes=self.max_upload + 16 * 1024**2,
            job_timeout_seconds=1800,
            job_ttl_seconds=86400,
            docker_memory=self.job_memory,
            docker_cpus=self.job_cpus,
            docker_pids_limit=512,
            docker_wine_pids_limit=1024,
            docker_wine_home_size=self.wine_home_size,
            docker_tmp_size="256m",
            docker_work_size=self.work_size,
            docker_output_size=self.output_size,
            max_concurrent_jobs=self.max_concurrent,
            max_stored_jobs=10000,
            max_storage_bytes=300 * 1024**3,
        )
