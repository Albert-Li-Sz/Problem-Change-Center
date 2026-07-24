from __future__ import annotations

import pytest

from app.config import Settings


ACCESS_KEY_HASH = f"pbkdf2_sha256$600000${'ab' * 16}${'cd' * 32}"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("P2H_MAX_UPLOAD_BYTES", "0"),
        ("P2H_JOB_TIMEOUT_SECONDS", "-1"),
        ("P2H_JOB_IDLE_TIMEOUT_SECONDS", "0"),
        ("P2H_JOB_STAGE_TIMEOUT_SECONDS", "0"),
        ("P2H_JOB_PROBLEM_TIMEOUT_SECONDS", "0"),
        ("P2H_MAX_CONCURRENT_JOBS", "0"),
        ("P2H_MAX_STORED_JOBS", "not-an-int"),
        ("P2H_MAX_STORAGE_BYTES", "-5"),
        ("P2H_MAX_LOG_BYTES", "0"),
        ("P2H_DOCKER_PIDS_LIMIT", "0"),
        ("P2H_RATE_LIMIT_REQUESTS_PER_MINUTE", "0"),
        ("P2H_RATE_LIMIT_UPLOADS_PER_MINUTE", "-1"),
        ("P2H_RATE_LIMIT_AUTH_FAILURES_PER_MINUTE", "0"),
        ("P2H_MAX_REQUEST_BODY_BYTES", "0"),
        ("P2H_READINESS_TIMEOUT_SECONDS", "0"),
    ],
)
def test_invalid_integer_limits_fail_at_startup(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        Settings.from_env()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("P2H_DOCKER_MEMORY", "garbage"),
        ("P2H_DOCKER_TMP_SIZE", "0"),
        ("P2H_DOCKER_WORK_SIZE", "-1g"),
        ("P2H_DOCKER_OUTPUT_SIZE", "NaN"),
        ("P2H_DOCKER_OUTPUT_SIZE", "9" * 100),
        ("P2H_DOCKER_WINE_HOME_SIZE", ""),
        ("P2H_DOCKER_CPUS", "0"),
        ("P2H_DOCKER_CPUS", "inf"),
    ],
)
def test_invalid_docker_limits_fail_at_startup(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        Settings.from_env()


def test_unlimited_pids_value_remains_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("P2H_DOCKER_PIDS_LIMIT", "-1")
    monkeypatch.setenv("P2H_DOCKER_WINE_PIDS_LIMIT", "-1")

    settings = Settings.from_env()

    assert settings.docker_pids_limit == -1
    assert settings.docker_wine_pids_limit == -1


def test_whitespace_is_trimmed_from_string_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("P2H_DOCKER_MEMORY", " 1536m ")
    monkeypatch.setenv("P2H_DOCKER_CPUS", " 1.5 ")
    monkeypatch.setenv("P2H_RUNNER_IMAGE", " p2h-runner:test ")

    settings = Settings.from_env()

    assert settings.docker_memory == "1536m"
    assert settings.docker_cpus == "1.5"
    assert settings.runner_image == "p2h-runner:test"


def test_empty_data_directory_fails_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("P2H_DATA_DIR", "")

    with pytest.raises(ValueError, match="P2H_DATA_DIR"):
        Settings.from_env()


def test_external_mode_requires_explicit_security_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("P2H_DEPLOYMENT_MODE", "external")
    monkeypatch.delenv("P2H_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("P2H_ALLOWED_ORIGINS", raising=False)
    monkeypatch.delenv("P2H_ACCESS_KEY_HASH", raising=False)

    with pytest.raises(ValueError, match="P2H_ALLOWED_HOSTS"):
        Settings.from_env()

    monkeypatch.setenv("P2H_ALLOWED_HOSTS", "converter.example.com")
    with pytest.raises(ValueError, match="P2H_ALLOWED_ORIGINS"):
        Settings.from_env()

    monkeypatch.setenv("P2H_ALLOWED_ORIGINS", "http://converter.example.com:11452")
    with pytest.raises(ValueError, match="P2H_ACCESS_KEY_HASH"):
        Settings.from_env()


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("P2H_ALLOWED_HOSTS", "*", "must not contain"),
        ("P2H_ALLOWED_ORIGINS", "*", "must not contain"),
        (
            "P2H_ALLOWED_ORIGINS",
            "https://converter.example.com:11452",
            "http://HOST:11452",
        ),
        (
            "P2H_ALLOWED_ORIGINS",
            "http://converter.example.com:not-a-port",
            "valid port",
        ),
        ("P2H_ACCESS_KEY_HASH", "too-short", "supported PBKDF2"),
    ],
)
def test_external_mode_rejects_invalid_boundary_values(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
    message: str,
) -> None:
    monkeypatch.setenv("P2H_DEPLOYMENT_MODE", "external")
    monkeypatch.setenv("P2H_ALLOWED_HOSTS", "converter.example.com")
    monkeypatch.setenv("P2H_ALLOWED_ORIGINS", "http://converter.example.com:11452")
    monkeypatch.setenv("P2H_ACCESS_KEY_HASH", ACCESS_KEY_HASH)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=message):
        Settings.from_env()


def test_request_body_limit_must_cover_upload_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("P2H_MAX_UPLOAD_BYTES", "1024")
    monkeypatch.setenv("P2H_MAX_REQUEST_BODY_BYTES", "1023")

    with pytest.raises(ValueError, match="must be at least"):
        Settings.from_env()


def test_valid_external_settings_are_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("P2H_DEPLOYMENT_MODE", "external")
    monkeypatch.setenv("P2H_ALLOWED_HOSTS", " converter.example.com,admin.example.com ")
    monkeypatch.setenv("P2H_ALLOWED_ORIGINS", " http://converter.example.com:11452 ")
    monkeypatch.setenv("P2H_ACCESS_KEY_HASH", ACCESS_KEY_HASH)

    settings = Settings.from_env()

    assert settings.is_external is True
    assert settings.allowed_hosts == (
        "converter.example.com",
        "admin.example.com",
    )
    assert settings.allowed_origins == ("http://converter.example.com:11452",)
    assert settings.access_key_hash == ACCESS_KEY_HASH
