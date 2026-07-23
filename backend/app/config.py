from __future__ import annotations

import ipaddress
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


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


def _csv_env(name: str, default: str, *, required: bool = False) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None:
        if required:
            raise ValueError(f"{name} must be explicitly configured")
        raw = default
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not values:
        raise ValueError(f"{name} must contain at least one value")
    if len(values) > 64 or any(len(item) > 255 for item in values):
        raise ValueError(f"{name} contains too many or overly long values")
    return values


def _deployment_mode_env() -> str:
    value = os.getenv("P2H_DEPLOYMENT_MODE", "local").strip().lower()
    if value not in {"local", "production"}:
        raise ValueError("P2H_DEPLOYMENT_MODE must be local or production")
    return value


def _proxy_secret_env(*, required: bool) -> str | None:
    value = os.getenv("P2H_TRUSTED_PROXY_SECRET")
    if value is None or not value.strip():
        if required:
            raise ValueError(
                "P2H_TRUSTED_PROXY_SECRET must contain at least 32 characters "
                "in production"
            )
        return None
    if value != value.strip():
        raise ValueError("P2H_TRUSTED_PROXY_SECRET must not have outer whitespace")
    if len(value) < 32 or len(value) > 256:
        raise ValueError(
            "P2H_TRUSTED_PROXY_SECRET must contain at least 32 and at most "
            "256 characters"
        )
    if any(ord(character) < 33 or ord(character) > 126 for character in value):
        raise ValueError(
            "P2H_TRUSTED_PROXY_SECRET must contain printable ASCII without spaces"
        )
    if len(set(value)) < 8 or value.lower().startswith(
        ("change", "generate", "replace")
    ):
        raise ValueError("P2H_TRUSTED_PROXY_SECRET must be a randomly generated secret")
    return value


def _validate_production_origins(origins: tuple[str, ...]) -> None:
    for origin in origins:
        if origin == "*":
            raise ValueError("P2H_ALLOWED_ORIGINS must not contain * in production")
        parsed = urlsplit(origin)
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError(
                "P2H_ALLOWED_ORIGINS entries must contain a valid port"
            ) from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "P2H_ALLOWED_ORIGINS entries must be origins such as "
                "https://converter.example.com"
            )
        if parsed.scheme != "https" and parsed.hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ValueError(
                "P2H_ALLOWED_ORIGINS must use HTTPS outside loopback in production"
            )


def _validate_allowed_hosts(hosts: tuple[str, ...], *, production: bool) -> None:
    for host in hosts:
        if host == "*" and production:
            raise ValueError("P2H_ALLOWED_HOSTS must not contain * in production")
        if any(character.isspace() for character in host) or any(
            marker in host for marker in ("/", "@", "?", "#")
        ):
            raise ValueError(
                "P2H_ALLOWED_HOSTS entries must be hostnames without scheme, "
                "port, path, or credentials"
            )
        if ":" in host:
            try:
                ipaddress.ip_address(host)
            except ValueError as exc:
                raise ValueError(
                    "P2H_ALLOWED_HOSTS entries must not include ports"
                ) from exc


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
    job_idle_timeout_seconds: int = 300
    job_stage_timeout_seconds: int = 1800
    job_problem_timeout_seconds: int = 1200
    deployment_mode: str = "local"
    allowed_hosts: tuple[str, ...] = (
        "localhost",
        "127.0.0.1",
        "::1",
        "testserver",
    )
    allowed_origins: tuple[str, ...] = (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    )
    trusted_proxy_secret: str | None = None
    rate_limit_requests_per_minute: int = 240
    rate_limit_uploads_per_minute: int = 12
    max_request_body_bytes: int = 528 * 1024 * 1024
    readiness_timeout_seconds: int = 5
    hsts_max_age_seconds: int = 31_536_000

    @property
    def is_production(self) -> bool:
        return self.deployment_mode == "production"

    @classmethod
    def from_env(cls) -> "Settings":
        deployment_mode = _deployment_mode_env()
        is_production = deployment_mode == "production"
        max_upload_bytes = _int_env("P2H_MAX_UPLOAD_BYTES", 512 * 1024 * 1024)
        max_request_body_bytes = _int_env(
            "P2H_MAX_REQUEST_BODY_BYTES",
            max_upload_bytes + 16 * 1024 * 1024,
        )
        if max_request_body_bytes < max_upload_bytes:
            raise ValueError(
                "P2H_MAX_REQUEST_BODY_BYTES must be at least P2H_MAX_UPLOAD_BYTES"
            )
        allowed_hosts = _csv_env(
            "P2H_ALLOWED_HOSTS",
            "localhost,127.0.0.1,::1,testserver",
            required=is_production,
        )
        _validate_allowed_hosts(allowed_hosts, production=is_production)
        allowed_origins = _csv_env(
            "P2H_ALLOWED_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173",
            required=is_production,
        )
        if is_production:
            _validate_production_origins(allowed_origins)
        return cls(
            data_dir=Path(_nonempty_env("P2H_DATA_DIR", str(DEFAULT_DATA_DIR)))
            .expanduser()
            .resolve(),
            docker_bin=_nonempty_env("P2H_DOCKER_BIN", "docker"),
            runner_image=_nonempty_env("P2H_RUNNER_IMAGE", "p2h-runner"),
            max_upload_bytes=max_upload_bytes,
            job_timeout_seconds=_int_env("P2H_JOB_TIMEOUT_SECONDS", 7200),
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
            job_idle_timeout_seconds=_int_env("P2H_JOB_IDLE_TIMEOUT_SECONDS", 300),
            job_stage_timeout_seconds=_int_env("P2H_JOB_STAGE_TIMEOUT_SECONDS", 1800),
            job_problem_timeout_seconds=_int_env(
                "P2H_JOB_PROBLEM_TIMEOUT_SECONDS", 1200
            ),
            deployment_mode=deployment_mode,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
            trusted_proxy_secret=_proxy_secret_env(required=is_production),
            rate_limit_requests_per_minute=_int_env(
                "P2H_RATE_LIMIT_REQUESTS_PER_MINUTE", 240
            ),
            rate_limit_uploads_per_minute=_int_env(
                "P2H_RATE_LIMIT_UPLOADS_PER_MINUTE", 12
            ),
            max_request_body_bytes=max_request_body_bytes,
            readiness_timeout_seconds=_int_env("P2H_READINESS_TIMEOUT_SECONDS", 5),
            hsts_max_age_seconds=_int_env(
                "P2H_HSTS_MAX_AGE_SECONDS", 31_536_000, minimum=0
            ),
        )


settings = Settings.from_env()
