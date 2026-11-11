from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

from app.public.config import PublicConfig, size_bytes
from app.public.worker import command

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("public_tool", ROOT / "scripts/public.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


def configured(profile: str) -> dict[str, str]:
    values = {
        "PUBLIC_DOMAIN": "convert.example.org",
        "PUBLIC_CONTACT_EMAIL": "operator@example.org",
        "SMTP_HOST": "smtp.example.org",
        "SMTP_FROM": "noreply@example.org",
        "SMTP_PASSWORD": "p@ss $word#1",
    }
    tool.defaults(values)
    tool.apply_profile(values, profile)
    return values


def test_env_file_round_trips_special_characters(tmp_path):
    values = configured("small")
    values["PUBLIC_ADMIN_ALLOW"] = "203.0.113.7/32 2001:db8::/48"
    path = tmp_path / ".env.public"
    tool.write_env(path, values)
    assert path.stat().st_mode & 0o777 == 0o600
    assert tool.read_env(path) == values
    # Values Compose would expand or split are single-quoted.
    assert "SMTP_PASSWORD='p@ss $word#1'" in path.read_text()
    with pytest.raises(tool.Failure):
        tool.format_env({"SMTP_PASSWORD": "it's"})


def test_defaults_generate_distinct_secrets_once():
    values: dict[str, str] = {}
    tool.defaults(values)
    passwords = {values[f"PUBLIC_{r}_DB_PASSWORD"] for r in ("OWNER", "API", "WORKER")}
    assert len(passwords) == 3 and all(len(p) == 64 for p in passwords)
    before = dict(values)
    tool.defaults(values)
    assert values == before
    assert tool.missing(values) == [
        "PUBLIC_DOMAIN",
        "PUBLIC_CONTACT_EMAIL",
        "SMTP_HOST",
        "SMTP_FROM",
    ]


def test_upgrade_keeps_hand_tuned_limits():
    values = configured("small")
    values["PUBLIC_MAX_CONCURRENT"] = "3"
    del values["PUBLIC_JOB_SWAP"]
    tool.apply_profile(values, "small", overwrite=False)
    assert values["PUBLIC_MAX_CONCURRENT"] == "3"
    assert values["PUBLIC_JOB_SWAP"] == "2g"


@pytest.mark.parametrize("profile", sorted(tool.PROFILES))
def test_profiles_fit_their_memory_tier(profile, monkeypatch):
    """Every tier is accepted by the service and fits the host it is named for."""
    values = configured(profile)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PUBLIC_DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("PUBLIC_ORIGIN", "https://convert.example.org")
    config = PublicConfig.from_env()
    config.validate_api()

    wine = min(config.max_wine, config.max_concurrent)
    jobs = wine * size_bytes(config.wine_memory) + (
        config.max_concurrent - wine
    ) * size_bytes(config.job_memory)
    services = sum(
        size_bytes(values[f"PUBLIC_{name}_MEMORY"].lower())
        for name in ("API", "WORKER", "POSTGRES", "CADDY")
    )
    assert jobs + services <= tool.PROFILES[profile]["need"] * 1024**3
    # The proxy must accept what the API accepts, plus multipart overhead.
    body = int(values["PUBLIC_MAX_BODY"].removesuffix("MiB")) * 1024**2
    assert body >= config.max_upload
    # A task cannot be asked to unpack more than its work mount can hold.
    assert config.archive_limit <= size_bytes(config.work_size)
    assert size_bytes(config.job_memory) + size_bytes(config.job_swap) >= size_bytes(
        config.work_size
    ) + size_bytes(config.output_size)


def test_runner_command_uses_configured_limits(tmp_path):
    config = PublicConfig(
        database_url="postgresql://unused",
        data_dir=tmp_path,
        development=True,
        runner_runtime="runsc",
        job_memory="2g",
        job_swap="2g",
        job_cpus="1.5",
        work_size="1g",
        output_size="512m",
        home_size="512m",
    )

    class Paths:
        input_dir = tmp_path / "input"

    class Storage:
        def paths_for(self, _):
            return Paths

    row = {"id": "a" * 32, "lease_token": "b" * 32, "wine": False, "kind": "inspect"}
    argv = command(config, row, Storage())
    assert argv[argv.index("--runtime") + 1] == "runsc"
    assert argv[argv.index("--memory") + 1] == str(2 * 1024**3)
    assert argv[argv.index("--memory-swap") + 1] == str(4 * 1024**3)
    assert argv[argv.index("--cpus") + 1] == "1.5"
    assert any(a.startswith("/work:") and "size=1g" in a for a in argv)
    assert any(a.startswith("/output:") and "size=512m" in a for a in argv)
    assert f"P2H_MAX_ARCHIVE_UNCOMPRESSED_BYTES={512 * 1024**2}" in argv
    assert config.output_limit == 512 * 1024**2


@pytest.mark.parametrize(
    "change",
    [
        {"captcha": "maybe"},
        {"max_wine": 4, "max_concurrent": 2},
        {"runner_runtime": "runc; rm -rf /"},
        {"job_memory": "4 GB"},
        {"work_size": "64m"},
    ],
)
def test_invalid_limits_are_rejected(change):
    with pytest.raises(ValueError):
        PublicConfig(database_url="postgresql://unused", **change)


def test_captcha_off_needs_no_turnstile_keys():
    base = dict(
        database_url="postgresql://unused",
        smtp_host="smtp.example.org",
        smtp_from="noreply@example.org",
        contact_email="operator@example.org",
    )
    PublicConfig(**base, captcha="off").validate_api()
    with pytest.raises(ValueError, match="Turnstile"):
        PublicConfig(**base).validate_api()


def test_helpers():
    assert tool.version_tuple("29.4.3") > tool.version_tuple("29.4.2")
    assert tool.valid_domain("convert.example.org") is None
    assert tool.valid_domain("https://convert.example.org")
    assert tool.valid_networks("203.0.113.7/32 2001:db8::/48") is None
    assert tool.valid_networks("not-an-address")
    pulled: dict[str, str] = {}
    tool.apply_image_source(pulled, "pull")
    assert pulled["PUBLIC_WEB_IMAGE"] == "ghcr.io/albert-li-sz/p2h-public-web:public"


@pytest.mark.skipif(not shutil.which("docker"), reason="docker is not installed")
@pytest.mark.parametrize("profile", sorted(tool.PROFILES))
def test_generated_configuration_is_valid_compose(profile, tmp_path):
    path = tmp_path / ".env.public"
    tool.write_env(path, configured(profile))
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(path),
            "-f",
            str(ROOT / "docker-compose.public.yml"),
            "config",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "p@ss $$word#1" in result.stdout or "p@ss $word#1" in result.stdout


def test_wizard_collects_settings_without_echoing_secrets(monkeypatch):
    answers = iter(
        [
            "1",  # 8 GiB tier
            "Convert.Example.org",
            "operator@example.org",
            "",  # data directory default
            "smtp.example.org",
            "465",
            "mailer",
            "noreply@example.org",
            "",  # captcha default: off
            "y",  # restrict the admin API
            "203.0.113.7/32",
            "",  # runtime default
            "",  # build locally
            "n",  # no domestic mirrors
            "",  # no backup key yet
        ]
    )
    prompts: list[str] = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(tool.getpass, "getpass", lambda prompt="": "s3cret pass")
    monkeypatch.setattr(tool, "interactive", lambda: True)
    monkeypatch.setattr(tool, "say", lambda text="": None)
    values: dict[str, str] = {}
    tool.defaults(values)
    tool.configure(values)

    assert (
        values["PUBLIC_PROFILE"] == "small" and values["PUBLIC_MAX_CONCURRENT"] == "2"
    )
    assert values["PUBLIC_DOMAIN"] == "convert.example.org"
    assert values["SMTP_PORT"] == "465" and values["SMTP_PASSWORD"] == "s3cret pass"
    assert values["PUBLIC_CAPTCHA"] == "off"
    assert values["PUBLIC_ADMIN_ALLOW"] == "203.0.113.7/32"
    assert values["PUBLIC_RUNNER_RUNTIME"] == "runc"
    assert (
        values["PUBLIC_IMAGE_SOURCE"] == "build" and values["PUBLIC_BUILD_MIRROR"] == ""
    )
    assert tool.missing(values) == []
    assert not any("s3cret" in prompt for prompt in prompts)
