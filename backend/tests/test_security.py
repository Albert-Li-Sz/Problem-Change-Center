from __future__ import annotations

import asyncio
import stat
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from app.config import Settings
from app.security import (
    InMemoryRateLimiter,
    RequestBodyLimitMiddleware,
    readiness_status,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        docker_bin="docker",
        runner_image="p2h-runner",
        max_upload_bytes=1024,
        job_timeout_seconds=10,
        job_ttl_seconds=3600,
        docker_memory="1g",
        docker_cpus="2",
        docker_pids_limit=1024,
        docker_wine_pids_limit=4096,
        docker_wine_home_size="4g",
        docker_tmp_size="512m",
        docker_work_size="1g",
    )


def test_streamed_request_body_limit_counts_actual_bytes(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), max_request_body_bytes=5)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/inspect",
        "headers": [],
        "app": SimpleNamespace(state=SimpleNamespace(settings=settings)),
    }
    incoming = [
        {"type": "http.request", "body": b"abc", "more_body": True},
        {"type": "http.request", "body": b"def", "more_body": False},
    ]
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return incoming.pop(0)

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    async def consume_body(
        _scope: dict[str, object],
        receive_body,
        send_response,
    ) -> None:
        while True:
            message = await receive_body()
            if not message.get("more_body"):
                break
        await send_response(
            {"type": "http.response.start", "status": 204, "headers": []}
        )
        await send_response({"type": "http.response.body", "body": b""})

    asyncio.run(RequestBodyLimitMiddleware(consume_body)(scope, receive, send))  # type: ignore[arg-type]

    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413


def test_rate_limiter_resets_after_window() -> None:
    limiter = InMemoryRateLimiter()

    assert limiter.check("203.0.113.1", "request", 1, now=10) == (True, 0)
    allowed, retry_after = limiter.check("203.0.113.1", "request", 1, now=11)
    assert allowed is False
    assert retry_after > 0
    assert limiter.check("203.0.113.1", "request", 1, now=70) == (True, 0)


def test_readiness_checks_storage_docker_and_runner(tmp_path: Path) -> None:
    fake_docker = tmp_path / "fake-docker"
    fake_docker.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_docker.chmod(fake_docker.stat().st_mode | stat.S_IXUSR)
    settings = replace(_settings(tmp_path), docker_bin=str(fake_docker))

    ready, checks = readiness_status(settings)

    assert ready is True
    assert checks == {
        "storage": "ok",
        "docker": "ok",
        "runner_image": "ok",
    }


def test_readiness_fails_closed_when_docker_is_missing(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), docker_bin=str(tmp_path / "missing-docker"))

    ready, checks = readiness_status(settings)

    assert ready is False
    assert checks["storage"] == "ok"
    assert checks["docker"] == "unavailable"
    assert checks["runner_image"] == "unchecked"


def test_deployment_templates_use_fixed_ports_and_system_docker() -> None:
    project_root = Path(__file__).resolve().parents[2]
    service = (project_root / "deploy/systemd/oj-package-converter.service").read_text(
        encoding="utf-8"
    )
    frontend_service = (
        project_root / "deploy/systemd/oj-package-converter-frontend.service"
    ).read_text(encoding="utf-8")
    production_env = (project_root / "deploy/production.env.example").read_text(
        encoding="utf-8"
    )
    frontend_server = (project_root / "scripts/serve-frontend.mjs").read_text(
        encoding="utf-8"
    )
    gitignore = (project_root / ".gitignore").read_text(encoding="utf-8")

    assert "After=network-online.target docker.service" in service
    assert "Requires=docker.service" in service
    assert "SupplementaryGroups=docker" in service
    assert "--host 0.0.0.0 --port 11451" in service
    assert "--workers 1" in service
    assert "--no-proxy-headers" in service
    assert "--limit-concurrency 128" in service
    assert "NoNewPrivileges=true" in service
    assert "ProtectSystem=strict" in service
    assert "CapabilityBoundingSet=" in service
    assert "serve-frontend.mjs" in frontend_service
    assert "NoNewPrivileges=true" in frontend_service
    assert "P2H_BACKEND_PORT=11451" in production_env
    assert "P2H_FRONTEND_PORT=11452" in production_env
    assert "P2H_ACCESS_KEY_HASH=" in production_env
    assert "DOCKER_HOST" not in production_env
    assert "rootless" not in production_env.lower()
    assert 'response.setHeader("Content-Security-Policy"' in frontend_server
    assert "frontendRootReal" in frontend_server
    assert "production.env" in gitignore.splitlines()
