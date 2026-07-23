from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict
from pathlib import Path

from app.config import Settings
from app.storage import JobMetadata, Storage, utc_now_iso


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        docker_bin="docker",
        runner_image="p2h-runner",
        max_upload_bytes=1024,
        job_timeout_seconds=30,
        job_ttl_seconds=3600,
        docker_memory="1g",
        docker_cpus="2",
        docker_pids_limit=1024,
        docker_wine_pids_limit=4096,
        docker_wine_home_size="4g",
        docker_tmp_size="512m",
        docker_work_size="1g",
    )


def _metadata(job_id: str, *, status: str = "queued") -> JobMetadata:
    return JobMetadata(
        id=job_id,
        filename="contest.zip",
        size=128,
        status=status,  # type: ignore[arg-type]
        created_at=utc_now_iso(),
        source_format="hydro",
        target_format="icpc",
    )


def test_storage_backfills_legacy_metadata_into_sqlite(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    job_id = "a" * 32
    root = settings.data_dir / "jobs" / job_id
    root.mkdir(parents=True)
    metadata = _metadata(job_id)
    (root / "metadata.json").write_text(json.dumps(asdict(metadata)), encoding="utf-8")

    storage = Storage(settings)

    assert storage.job_ids() == [job_id]
    assert storage.read_metadata(job_id) == metadata
    with sqlite3.connect(settings.data_dir / "jobs.sqlite3") as connection:
        row = connection.execute(
            "SELECT status, source_format, target_format FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
        schema_version = connection.execute("PRAGMA user_version").fetchone()
    assert row == ("queued", "hydro", "icpc")
    assert journal_mode == ("wal",)
    assert schema_version == (1,)


def test_sqlite_metadata_survives_missing_compatibility_sidecar(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    storage = Storage(settings)
    job_id = "b" * 32
    metadata = _metadata(job_id, status="success")
    storage.write_metadata(metadata)
    storage.paths_for(job_id).metadata_path.unlink()

    restarted = Storage(settings)

    assert restarted.job_ids() == [job_id]
    assert restarted.read_metadata(job_id) == metadata


def test_delete_job_removes_artifacts_and_sqlite_record(tmp_path: Path) -> None:
    storage = Storage(_settings(tmp_path))
    job_id = "c" * 32
    storage.write_metadata(_metadata(job_id))
    paths = storage.paths_for(job_id)
    paths.logs_path.write_text("log", encoding="utf-8")

    storage.delete_job(job_id)

    assert not paths.root.exists()
    assert storage.job_ids() == []
    assert storage.job_index.get(job_id) is None


def test_metadata_and_log_writes_wake_change_waiters(tmp_path: Path) -> None:
    storage = Storage(_settings(tmp_path))
    job_id = "d" * 32
    storage.write_metadata(_metadata(job_id))
    paths = storage.paths_for(job_id)
    paths.logs_path.write_text("", encoding="utf-8")
    initial_revision = storage.change_revision(job_id)
    result: list[int | None] = []

    waiter = threading.Thread(
        target=lambda: result.append(
            storage.wait_for_change(job_id, initial_revision, timeout=1)
        )
    )
    waiter.start()
    time.sleep(0.01)
    storage.append_log(job_id, "runner started\n")
    waiter.join(timeout=1)

    assert result == [initial_revision + 1]
