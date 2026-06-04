from __future__ import annotations

import io
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.auth import AuthenticatedUser
from app.config import Settings
from app.jobs import JobManager
from app.main import app
from app.storage import Storage


def _make_zip() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("problems/a/problem.xml", "<problem></problem>")
    return out.getvalue()


def _settings(tmp_path: Path, *, max_upload_bytes: int = 1024 * 1024) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        docker_bin="docker",
        runner_image="p2h-runner",
        max_upload_bytes=max_upload_bytes,
        job_timeout_seconds=10,
        job_ttl_seconds=3600,
        docker_memory="1g",
        docker_cpus="2",
        docker_pids_limit=256,
        docker_tmp_size="512m",
        docker_work_size="1g",
    )


def _fake_user() -> AuthenticatedUser:
    return AuthenticatedUser(
        id="user-1",
        email="user@example.com",
        is_admin=False,
        daily_quota=10,
        daily_used=0,
        daily_pending=0,
        created_at="2026-01-01T00:00:00+00:00",
        email_verified_at="2026-01-01T00:00:00+00:00",
    )


def _install_test_services(settings: Settings) -> Storage:
    app.dependency_overrides.clear()
    storage = Storage(settings)
    app.state.storage = storage
    app.state.job_manager = JobManager(settings, storage)

    # The route module keeps module-level singletons, so patch them for this test.
    import app.main as main_module

    main_module.storage = app.state.storage
    main_module.job_manager = app.state.job_manager
    app.dependency_overrides[main_module.current_user] = _fake_user
    return storage


def test_inspect_accepts_zip_and_rejects_non_zip(tmp_path: Path) -> None:
    _install_test_services(_settings(tmp_path))

    client = TestClient(app)
    ok = client.post("/api/inspect", files={"file": ("contest.zip", _make_zip(), "application/zip")})
    assert ok.status_code == 200
    assert ok.json()["filename"] == "contest.zip"

    bad_ext = client.post("/api/inspect", files={"file": ("contest.txt", b"x", "text/plain")})
    assert bad_ext.status_code == 400

    bad_zip = client.post("/api/inspect", files={"file": ("contest.zip", b"x", "application/zip")})
    assert bad_zip.status_code == 400
    app.dependency_overrides.clear()


def test_inspect_rejects_upload_over_limit_and_cleans_job_dir(tmp_path: Path) -> None:
    storage = _install_test_services(_settings(tmp_path, max_upload_bytes=3))

    client = TestClient(app)
    response = client.post("/api/inspect", files={"file": ("contest.zip", b"abcd", "application/zip")})

    assert response.status_code == 413
    assert list(storage.jobs_dir.glob("*")) == []
    app.dependency_overrides.clear()
