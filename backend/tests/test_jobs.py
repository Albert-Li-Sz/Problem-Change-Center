from __future__ import annotations

import os
import stat
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from app.config import Settings
from app.jobs import JobManager
from app.schemas import JobRequest
from app.storage import JobMetadata, Storage, utc_now_iso


def _settings(root: Path, docker_bin: Path, timeout: int = 5) -> Settings:
    return Settings(
        data_dir=root / "data",
        docker_bin=str(docker_bin),
        runner_image="p2h-runner",
        max_upload_bytes=1024,
        job_timeout_seconds=timeout,
        job_ttl_seconds=3600,
        docker_memory="1g",
        docker_cpus="2",
        docker_pids_limit=256,
        docker_tmp_size="512m",
        docker_work_size="1g",
    )


def _write_fake_docker(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _prepare_job(storage: Storage, job_id: str) -> None:
    paths = storage.paths_for(job_id)
    paths.input_dir.mkdir(parents=True)
    paths.output_dir.mkdir(parents=True)
    paths.logs_path.write_text("", encoding="utf-8")
    paths.upload_path.write_text("fake", encoding="utf-8")
    storage.write_metadata(
        JobMetadata(
            id=job_id,
            filename="contest.zip",
            size=4,
            status="queued",
            created_at=utc_now_iso(),
        )
    )


def test_job_success_creates_download_zip_and_logs_output() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        fake_docker = root / "fake-docker"
        _write_fake_docker(
            fake_docker,
            """#!/usr/bin/env bash
set -e
if [ "${1:-}" = "rm" ]; then exit 0; fi
echo "fake docker started"
for arg in "$@"; do
  case "$arg" in
    *:/output:rw)
      host="${arg%%:/output:rw}"
      mkdir -p "$host"
      printf 'ok' > "$host/a.zip"
      ;;
  esac
done
""",
        )

        settings = _settings(root, fake_docker)
        storage = Storage(settings)
        job_id = "c" * 32
        _prepare_job(storage, job_id)
        manager = JobManager(settings, storage)

        response = manager.start(JobRequest(job_id=job_id, pid_start="P1000", owner=1))
        assert response.status == "queued"
        manager._runtime[job_id].thread.join(timeout=5)  # type: ignore[union-attr]

        response = manager.response(job_id)
        paths = storage.paths_for(job_id)

        assert response.status == "success"
        assert response.download_ready is True
        assert "fake docker started" in storage.read_logs(job_id)
        with zipfile.ZipFile(paths.result_path) as archive:
            assert archive.namelist() == ["a.zip"]


def test_job_failure_records_exit_code_and_error() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        fake_docker = root / "fake-docker"
        _write_fake_docker(
            fake_docker,
            """#!/usr/bin/env bash
if [ "${1:-}" = "rm" ]; then exit 0; fi
echo "missing answer"
exit 7
""",
        )

        settings = _settings(root, fake_docker)
        storage = Storage(settings)
        job_id = "d" * 32
        _prepare_job(storage, job_id)
        manager = JobManager(settings, storage)

        manager.start(JobRequest(job_id=job_id, pid_start="P1000", owner=1))
        manager._runtime[job_id].thread.join(timeout=5)  # type: ignore[union-attr]
        response = manager.response(job_id)

        assert response.status == "failed"
        assert response.exit_code == 7
        assert response.error == "Runner exited with code 7"
        assert "missing answer" in storage.read_logs(job_id)


def test_job_timeout_marks_failed() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        fake_docker = root / "fake-docker"
        _write_fake_docker(
            fake_docker,
            """#!/usr/bin/env bash
if [ "${1:-}" = "rm" ]; then exit 0; fi
sleep 5
""",
        )

        settings = _settings(root, fake_docker, timeout=1)
        storage = Storage(settings)
        job_id = "e" * 32
        _prepare_job(storage, job_id)
        manager = JobManager(settings, storage)

        manager.start(JobRequest(job_id=job_id, pid_start="P1000", owner=1))
        manager._runtime[job_id].thread.join(timeout=4)  # type: ignore[union-attr]
        response = manager.response(job_id)

        assert response.status == "failed"
        assert response.error is not None
        assert "timed out" in response.error
