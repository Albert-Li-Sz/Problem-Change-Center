from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from ..config import Settings


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
    max_concurrent: int = 10
    max_wine: int = 3
    max_upload: int = 512 * 1024**2
    session_seconds: int = 7 * 86400

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
        )

    def validate_api(self) -> None:
        if not self.development and not all(
            (
                self.turnstile_site_key,
                self.turnstile_secret,
                self.smtp_host,
                self.smtp_from,
                self.contact_email,
            )
        ):
            raise ValueError(
                "Configure Turnstile, SMTP and PUBLIC_CONTACT_EMAIL before public startup"
            )

    def storage_settings(self) -> Settings:
        return Settings(
            data_dir=self.data_dir,
            docker_bin="docker",
            runner_image=self.wine_image,
            max_upload_bytes=self.max_upload,
            job_timeout_seconds=1800,
            job_ttl_seconds=86400,
            docker_memory="2g",
            docker_cpus="2",
            docker_pids_limit=512,
            docker_wine_pids_limit=1024,
            docker_wine_home_size="1g",
            docker_tmp_size="256m",
            docker_work_size="2g",
            docker_output_size="1g",
            max_concurrent_jobs=self.max_concurrent,
            max_stored_jobs=10000,
            max_storage_bytes=300 * 1024**3,
        )
