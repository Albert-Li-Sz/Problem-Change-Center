from __future__ import annotations

import pytest

from app.config import Settings


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("P2H_MAX_UPLOAD_BYTES", "0"),
        ("P2H_JOB_TIMEOUT_SECONDS", "-1"),
        ("P2H_MAX_CONCURRENT_JOBS", "0"),
        ("P2H_MAX_STORED_JOBS", "not-an-int"),
        ("P2H_MAX_STORAGE_BYTES", "-5"),
        ("P2H_MAX_LOG_BYTES", "0"),
        ("P2H_DOCKER_PIDS_LIMIT", "0"),
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
