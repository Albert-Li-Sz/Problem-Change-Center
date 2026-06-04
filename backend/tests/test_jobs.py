from __future__ import annotations

import stat
import time
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from fastapi import HTTPException

from app.auth import AuthenticatedUser
from app.config import Settings
from app.jobs import JobManager
from app.schemas import JobRequest
from app.storage import JobMetadata, Storage, utc_now_iso


def _settings(root: Path, docker_bin: Path, timeout: int = 5, max_concurrent_jobs: int = 2) -> Settings:
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
        max_concurrent_jobs=max_concurrent_jobs,
    )


def _write_fake_docker(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _prepare_job(storage: Storage, job_id: str, user_id: str | None = None) -> None:
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
            user_id=user_id,
        )
    )


def _wait_for_terminal(manager: JobManager, job_id: str, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if manager.response(job_id).status in {"success", "failed", "cancelled"} and job_id not in manager._runtime:
            return
        time.sleep(0.05)
    response = manager.response(job_id)
    raise AssertionError(f"Job {job_id} did not finish in time; status={response.status!r}")


def _assert_runtime_cleaned(storage: Storage, job_id: str) -> None:
    paths = storage.paths_for(job_id)
    assert paths.metadata_path.exists()
    assert paths.logs_path.exists()
    assert not paths.input_dir.exists()
    assert not paths.work_dir.exists()
    assert not paths.output_dir.exists()


class FakeAuth:
    def __init__(self) -> None:
        self.reserved = 0
        self.completed: list[tuple[str | None, bool]] = []

    def reserve_conversion(self, user: AuthenticatedUser) -> None:
        self.reserved += 1

    def complete_conversion(self, user_id: str | None, *, success: bool) -> None:
        self.completed.append((user_id, success))


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
        _wait_for_terminal(manager, job_id)

        response = manager.response(job_id)
        paths = storage.paths_for(job_id)

        assert response.status == "success"
        assert response.download_ready is True
        assert "fake docker started" in storage.read_logs(job_id)
        _assert_runtime_cleaned(storage, job_id)
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
        _wait_for_terminal(manager, job_id)
        response = manager.response(job_id)

        assert response.status == "failed"
        assert response.exit_code == 7
        assert response.error == "Runner exited with code 7"
        assert "missing answer" in storage.read_logs(job_id)
        _assert_runtime_cleaned(storage, job_id)


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
        _wait_for_terminal(manager, job_id, timeout=4)
        response = manager.response(job_id)

        assert response.status == "failed"
        assert response.error is not None
        assert "timed out" in response.error
        _assert_runtime_cleaned(storage, job_id)


def test_queue_respects_max_concurrent_jobs_fifo() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        trace = root / "trace.txt"
        fake_docker = root / "fake-docker"
        _write_fake_docker(
            fake_docker,
            f"""#!/usr/bin/env bash
set -e
if [ "${{1:-}}" = "rm" ]; then exit 0; fi
job="unknown"
previous=""
for arg in "$@"; do
  if [ "$previous" = "--name" ]; then
    job="${{arg#p2h-}}"
  fi
  previous="$arg"
done
echo "start $job" >> "{trace}"
for arg in "$@"; do
  case "$arg" in
    *:/output:rw)
      host="${{arg%%:/output:rw}}"
      mkdir -p "$host"
      printf 'ok' > "$host/$job.zip"
      ;;
  esac
done
sleep 0.3
echo "end $job" >> "{trace}"
""",
        )

        settings = _settings(root, fake_docker, max_concurrent_jobs=1)
        storage = Storage(settings)
        first_id = "1" * 32
        second_id = "2" * 32
        _prepare_job(storage, first_id)
        _prepare_job(storage, second_id)
        manager = JobManager(settings, storage)

        manager.start(JobRequest(job_id=first_id, pid_start="P1000", owner=1))
        manager.start(JobRequest(job_id=second_id, pid_start="P1000", owner=1))
        _wait_for_terminal(manager, first_id)
        _wait_for_terminal(manager, second_id)

        assert manager.response(first_id).status == "success"
        assert manager.response(second_id).status == "success"
        assert trace.read_text(encoding="utf-8").splitlines() == [
            f"start {first_id}",
            f"end {first_id}",
            f"start {second_id}",
            f"end {second_id}",
        ]


def test_quota_hold_finalized_on_success_and_queued_cancel() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        fake_docker = root / "fake-docker"
        _write_fake_docker(
            fake_docker,
            """#!/usr/bin/env bash
set -e
if [ "${1:-}" = "rm" ]; then exit 0; fi
for arg in "$@"; do
  case "$arg" in
    *:/output:rw)
      host="${arg%%:/output:rw}"
      mkdir -p "$host"
      printf 'ok' > "$host/result.zip"
      ;;
  esac
done
sleep 0.2
""",
        )

        settings = _settings(root, fake_docker, max_concurrent_jobs=1)
        storage = Storage(settings)
        auth = FakeAuth()
        user = AuthenticatedUser(
            id="user-1",
            email="user@example.com",
            is_admin=False,
            daily_quota=10,
            created_at=utc_now_iso(),
            email_verified_at=utc_now_iso(),
        )
        first_id = "3" * 32
        second_id = "4" * 32
        _prepare_job(storage, first_id, user.id)
        _prepare_job(storage, second_id, user.id)
        manager = JobManager(settings, storage, auth)  # type: ignore[arg-type]

        manager.start(JobRequest(job_id=first_id, pid_start="P1000", owner=1), user)
        manager.start(JobRequest(job_id=second_id, pid_start="P1000", owner=1), user)
        delete_response = manager.cancel_or_delete(second_id, user)
        _wait_for_terminal(manager, first_id)

        first_metadata = storage.read_metadata(first_id)
        second_metadata = storage.read_metadata(second_id)

        assert delete_response.status == "cancelled"
        assert auth.reserved == 2
        assert auth.completed == [(user.id, False), (user.id, True)]
        assert first_metadata.quota_finalized is True
        assert second_metadata.quota_finalized is True
        _assert_runtime_cleaned(storage, first_id)
        _assert_runtime_cleaned(storage, second_id)


def test_domjudge_validation_rejects_invalid_color_and_validator_combo() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        settings = _settings(root, root / "fake-docker")
        manager = JobManager(settings, Storage(settings))

        with pytest.raises(HTTPException) as bad_color:
            manager.start(JobRequest(job_id="f" * 32, target="domjudge", domjudge_color="black"))
        assert bad_color.value.status_code == 422

        with pytest.raises(HTTPException) as bad_validator:
            manager.start(
                JobRequest(
                    job_id="f" * 32,
                    target="domjudge",
                    domjudge_auto_validator=True,
                    domjudge_default_validator=True,
                )
            )
        assert bad_validator.value.status_code == 422
