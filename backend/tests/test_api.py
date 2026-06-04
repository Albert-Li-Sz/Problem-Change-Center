from __future__ import annotations

import io
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.jobs import JobManager
from app.main import app
from app.storage import Storage


def _make_zip() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("problems/a/problem.xml", "<problem></problem>")
    return out.getvalue()


def test_inspect_accepts_zip_and_rejects_non_zip(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        docker_bin="docker",
        runner_image="p2h-runner",
        max_upload_bytes=1024 * 1024,
        job_timeout_seconds=10,
        job_ttl_seconds=3600,
        docker_memory="1g",
        docker_cpus="2",
        docker_pids_limit=256,
        docker_tmp_size="512m",
        docker_work_size="1g",
    )
    app.dependency_overrides.clear()
    app.state.storage = Storage(settings)
    app.state.job_manager = JobManager(settings, app.state.storage)

    # The route module keeps module-level singletons, so patch them for this test.
    import app.main as main_module

    main_module.storage = app.state.storage
    main_module.job_manager = app.state.job_manager

    client = TestClient(app)
    ok = client.post("/api/inspect", files={"file": ("contest.zip", _make_zip(), "application/zip")})
    assert ok.status_code == 200
    assert ok.json()["filename"] == "contest.zip"

    bad_ext = client.post("/api/inspect", files={"file": ("contest.txt", b"x", "text/plain")})
    assert bad_ext.status_code == 400

    bad_zip = client.post("/api/inspect", files={"file": ("contest.zip", b"x", "application/zip")})
    assert bad_zip.status_code == 400
