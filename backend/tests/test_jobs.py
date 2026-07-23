from __future__ import annotations

import stat
import sys
import threading
import json
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from fastapi import HTTPException

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
        docker_pids_limit=1024,
        docker_wine_pids_limit=4096,
        docker_wine_home_size="4g",
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
            f"""#!/usr/bin/env bash
set -e
if [ "${{1:-}}" = "rm" ]; then exit 0; fi
echo "fake docker started"
for arg in "$@"; do
  case "$arg" in
    *:/result:rw)
      host="${{arg%%:/result:rw}}"
      mkdir -p "$host"
      "{sys.executable}" - "$host/a.zip" <<'PY'
import sys
import zipfile

with zipfile.ZipFile(sys.argv[1], "w", compression=zipfile.ZIP_DEFLATED) as archive:
    archive.writestr("P1000/problem.yaml", "title: A\\n")
    archive.writestr("P1000/testdata/1.in", "1\\n")
PY
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
            assert archive.namelist() == ["P1000/problem.yaml", "P1000/testdata/1.in"]


def test_matrix_job_captures_report_before_packing_output() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        fake_docker = root / "fake-docker"
        _write_fake_docker(
            fake_docker,
            f'''#!/usr/bin/env bash
set -e
if [ "${{1:-}}" = "rm" ]; then exit 0; fi
for arg in "$@"; do
  case "$arg" in
    *:/result:rw)
      host="${{arg%%:/result:rw}}"
      mkdir -p "$host/1/testcase"
      printf '%s\n' '{{"title":"Sum"}}' > "$host/1/problem.json"
      printf '1 2\n' > "$host/1/testcase/1.in"
      printf '3\n' > "$host/1/testcase/1.out"
      "{sys.executable}" - "$host/.p2h-report.json" <<'PY'
import json
import sys

report = {{
    "schema_version": 1,
    "source_format": "fps",
    "target_format": "qduoj",
    "problem_count": 1,
    "counts": {{"warning": 0, "loss": 1, "fatal": 0}},
    "issues": [{{"severity": "loss", "code": "file_io", "message": "not representable"}}],
    "artifacts": ["1/problem.json"],
}}
with open(sys.argv[1], "w", encoding="utf-8") as output:
    json.dump(report, output)
PY
      ;;
  esac
done
''',
        )

        settings = _settings(root, fake_docker)
        storage = Storage(settings)
        job_id = "0" * 32
        _prepare_job(storage, job_id)
        manager = JobManager(settings, storage)
        request = JobRequest(job_id=job_id, source_format="fps", target_format="qduoj")

        manager.start(request)
        manager._runtime[job_id].thread.join(timeout=5)  # type: ignore[union-attr]

        response = manager.response(job_id)
        paths = storage.paths_for(job_id)
        assert response.status == "success"
        assert response.source_format == "fps"
        assert response.target_format == "qduoj"
        assert response.report_ready is True
        assert response.report_counts.loss == 1
        assert storage.read_report(job_id)["counts"] == {
            "warning": 0,
            "loss": 1,
            "fatal": 0,
        }
        assert not (paths.output_dir / ".p2h-report.json").exists()
        with zipfile.ZipFile(paths.result_path) as archive:
            assert ".p2h-report.json" not in archive.namelist()
            assert set(archive.namelist()) == {
                "1/problem.json",
                "1/testcase/1.in",
                "1/testcase/1.out",
            }


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


def test_failed_matrix_job_preserves_structured_report() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        fake_docker = root / "fake-docker"
        _write_fake_docker(
            fake_docker,
            f'''#!/usr/bin/env bash
if [ "${{1:-}}" = "rm" ]; then exit 0; fi
for arg in "$@"; do
  case "$arg" in
    *:/result:rw)
      host="${{arg%%:/result:rw}}"
      "{sys.executable}" - "$host/.p2h-report.json" <<'PY'
import json
import sys

with open(sys.argv[1], "w", encoding="utf-8") as output:
    json.dump({{
        "schema_version": 1,
        "source_format": "fps",
        "target_format": "hydro",
        "problem_count": 1,
        "counts": {{"warning": 0, "loss": 0, "fatal": 1}},
        "issues": [{{
            "severity": "fatal",
            "code": "missing-secret-tests",
            "message": "problem has no non-sample test cases",
            "problem": "empty",
            "field": "cases",
        }}],
        "artifacts": [],
    }}, output)
PY
      ;;
  esac
done
exit 7
''',
        )
        settings = _settings(root, fake_docker)
        storage = Storage(settings)
        job_id = "7" * 32
        _prepare_job(storage, job_id)
        manager = JobManager(settings, storage)

        manager.start(
            JobRequest(job_id=job_id, source_format="fps", target_format="hydro")
        )
        manager._runtime[job_id].thread.join(timeout=5)  # type: ignore[union-attr]
        response = manager.response(job_id)

        assert response.status == "failed"
        assert response.exit_code == 7
        assert response.report_ready is True
        assert response.report_counts.fatal == 1
        assert (
            storage.read_report(job_id)["issues"][0]["code"] == "missing-secret-tests"
        )


def test_conversion_report_rejects_symlinks_and_inconsistent_counts() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        settings = _settings(root, root / "fake-docker")
        storage = Storage(settings)
        job_id = "3" * 32
        _prepare_job(storage, job_id)
        paths = storage.paths_for(job_id)
        external = root / "external.json"
        external.write_text("{}", encoding="utf-8")
        (paths.output_dir / ".p2h-report.json").symlink_to(external)

        with pytest.raises(ValueError, match="symbolic link"):
            storage.capture_conversion_report(job_id)

        (paths.output_dir / ".p2h-report.json").unlink()
        (paths.output_dir / ".p2h-report.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source_format": "fps",
                    "target_format": "hydro",
                    "problem_count": 1,
                    "counts": {"warning": 0, "loss": 0, "fatal": 0},
                    "issues": [
                        {
                            "severity": "fatal",
                            "code": "missing-tests",
                            "message": "missing tests",
                            "problem": "a",
                            "field": "cases",
                        }
                    ],
                    "artifacts": [],
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="counts do not match"):
            storage.capture_conversion_report(job_id)


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


def test_cancel_before_worker_starts_remains_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        fake_docker = root / "fake-docker"
        _write_fake_docker(fake_docker, "#!/usr/bin/env bash\nexit 0\n")
        settings = _settings(root, fake_docker)
        storage = Storage(settings)
        job_id = "a" * 32
        _prepare_job(storage, job_id)
        manager = JobManager(settings, storage)
        worker_entered = threading.Event()
        release_worker = threading.Event()
        original_run_job = manager._run_job

        def delayed_run_job(request: JobRequest) -> None:
            worker_entered.set()
            assert release_worker.wait(timeout=5)
            original_run_job(request)

        monkeypatch.setattr(manager, "_run_job", delayed_run_job)
        manager.start(JobRequest(job_id=job_id, pid_start="P1000", owner=1))
        assert worker_entered.wait(timeout=5)
        thread = manager._runtime[job_id].thread
        assert thread is not None

        cancelled = manager.cancel_or_delete(job_id)
        cancelled_again = manager.cancel_or_delete(job_id)
        assert storage.paths_for(job_id).root.exists()
        release_worker.set()
        thread.join(timeout=5)

        assert cancelled.status == "cancelled"
        assert cancelled.deleted is False
        assert cancelled_again.status == "cancelled"
        assert cancelled_again.deleted is False
        assert not thread.is_alive()
        assert not storage.paths_for(job_id).root.exists()
        with pytest.raises(HTTPException) as deleted:
            manager.response(job_id)
        assert deleted.value.status_code == 404


def test_cancel_during_pack_remains_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        fake_docker = root / "fake-docker"
        _write_fake_docker(fake_docker, "#!/usr/bin/env bash\nexit 0\n")
        settings = _settings(root, fake_docker)
        storage = Storage(settings)
        job_id = "b" * 32
        _prepare_job(storage, job_id)
        manager = JobManager(settings, storage)
        pack_entered = threading.Event()
        release_pack = threading.Event()

        def delayed_pack(
            _output_dir: Path,
            result_path: Path,
            *,
            target: str,
            max_uncompressed_bytes: int,
        ) -> None:
            assert target == "hydro"
            assert max_uncompressed_bytes == 1024**3
            pack_entered.set()
            assert release_pack.wait(timeout=5)
            result_path.write_bytes(b"late result")

        monkeypatch.setattr("app.jobs.pack_output", delayed_pack)
        manager.start(JobRequest(job_id=job_id, pid_start="P1000", owner=1))
        assert pack_entered.wait(timeout=5)
        thread = manager._runtime[job_id].thread
        assert thread is not None

        cancelled = manager.cancel_or_delete(job_id)
        release_pack.set()
        thread.join(timeout=5)

        assert cancelled.status == "cancelled"
        assert cancelled.deleted is False
        assert not thread.is_alive()
        assert not storage.paths_for(job_id).root.exists()


def test_global_concurrency_limit_rejects_another_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        settings = replace(_settings(root, root / "fake-docker"), max_concurrent_jobs=1)
        storage = Storage(settings)
        first_job = "5" * 32
        second_job = "6" * 32
        _prepare_job(storage, first_job)
        _prepare_job(storage, second_job)
        manager = JobManager(settings, storage)
        entered = threading.Event()
        release = threading.Event()

        def blocked_worker(_request: JobRequest) -> None:
            entered.set()
            assert release.wait(timeout=5)

        monkeypatch.setattr(manager, "_run_job", blocked_worker)
        manager.start(JobRequest(job_id=first_job))
        assert entered.wait(timeout=5)

        with pytest.raises(HTTPException) as exc_info:
            manager.start(JobRequest(job_id=second_job))
        assert exc_info.value.status_code == 429

        release.set()
        manager._runtime[first_job].thread.join(timeout=5)  # type: ignore[union-attr]


def test_cleanup_expired_jobs_removes_only_inactive_expired_data() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        settings = replace(_settings(root, root / "fake-docker"), job_ttl_seconds=60)
        storage = Storage(settings)
        manager = JobManager(settings, storage)
        now = datetime.now(timezone.utc)
        expired_job = "7" * 32
        current_job = "8" * 32
        for job_id, created_at in (
            (expired_job, now - timedelta(seconds=61)),
            (current_job, now - timedelta(seconds=59)),
        ):
            storage.write_metadata(
                JobMetadata(job_id, "contest.zip", 1, "queued", created_at.isoformat())
            )

        assert manager.cleanup_expired(now=now) == 1
        assert not storage.paths_for(expired_job).root.exists()
        assert storage.paths_for(current_job).root.exists()


def test_logs_are_truncated_at_the_configured_byte_limit() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        settings = replace(_settings(root, root / "fake-docker"), max_log_bytes=128)
        storage = Storage(settings)
        job_id = "9" * 32
        _prepare_job(storage, job_id)

        storage.append_log(job_id, "x" * 1024)
        storage.append_log(job_id, "must not be appended")

        paths = storage.paths_for(job_id)
        assert paths.logs_path.stat().st_size == 128
        assert "[log truncated: size limit reached]" in storage.read_logs(job_id)


def test_domjudge_validation_rejects_invalid_color_and_validator_combo() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        settings = _settings(root, root / "fake-docker")
        manager = JobManager(settings, Storage(settings))

        with pytest.raises(HTTPException) as bad_color:
            manager.start(
                JobRequest(job_id="f" * 32, target="domjudge", domjudge_color="black")
            )
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

        with pytest.raises(HTTPException) as missing_rights_owner:
            manager.start(
                JobRequest(
                    job_id="f" * 32,
                    source_format="hydro",
                    target_format="icpc",
                    options={"icpc": {"license": "cc by-sa"}},
                )
            )
        assert missing_rights_owner.value.status_code == 422


def test_domjudge_to_hydro_validation_rejects_invalid_pid() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        settings = _settings(root, root / "fake-docker")
        manager = JobManager(settings, Storage(settings))

        with pytest.raises(HTTPException) as bad_pid:
            manager.start(
                JobRequest(
                    job_id="2" * 32, target="domjudge_to_hydro", pid_start="1000"
                )
            )
        assert bad_pid.value.status_code == 422

        with pytest.raises(HTTPException) as bad_hoj_pid:
            manager.start(
                JobRequest(job_id="3" * 32, target="hoj_to_hydro", pid_start="1000")
            )
        assert bad_hoj_pid.value.status_code == 422

        with pytest.raises(HTTPException) as bad_hoj_color:
            manager.start(
                JobRequest(
                    job_id="4" * 32, target="hoj_to_domjudge", domjudge_color="black"
                )
            )
        assert bad_hoj_color.value.status_code == 422


def test_matrix_request_rejects_detected_source_mismatch() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        settings = _settings(root, root / "fake-docker")
        storage = Storage(settings)
        job_id = "1" * 32
        _prepare_job(storage, job_id)
        metadata = storage.read_metadata(job_id)
        metadata.detected_format = "hydro"
        storage.write_metadata(metadata)
        manager = JobManager(settings, storage)

        with pytest.raises(HTTPException) as mismatch:
            manager.start(
                JobRequest(job_id=job_id, source_format="fps", target_format="hydro")
            )

        assert mismatch.value.status_code == 422
        assert "does not match" in str(mismatch.value.detail)
