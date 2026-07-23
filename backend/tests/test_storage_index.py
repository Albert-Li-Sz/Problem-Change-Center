from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import closing
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
    with closing(sqlite3.connect(settings.data_dir / "jobs.sqlite3")) as connection:
        row = connection.execute(
            "SELECT status, source_format, target_format FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
        schema_version = connection.execute("PRAGMA user_version").fetchone()
    assert row == ("queued", "hydro", "icpc")
    assert journal_mode == ("wal",)
    assert schema_version == (1,)


def test_job_index_closes_connections_after_each_operation(
    tmp_path: Path, monkeypatch
) -> None:
    from app import job_index as job_index_module
    from app.job_index import JobIndex

    opened: list[sqlite3.Connection] = []
    original_connect = sqlite3.connect

    class TrackingConnection(sqlite3.Connection):
        was_closed = False

        def close(self) -> None:
            self.was_closed = True
            super().close()

    def tracking_connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        connection = original_connect(*args, factory=TrackingConnection, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(job_index_module.sqlite3, "connect", tracking_connect)
    index = JobIndex(tmp_path / "jobs.sqlite3")
    metadata = _metadata("f" * 32)

    index.upsert(metadata, updated_at=utc_now_iso())
    assert index.get(metadata.id) is not None
    assert index.ids() == [metadata.id]
    index.prune_missing({metadata.id})
    index.delete(metadata.id)

    assert len(opened) == 6
    assert all(connection.was_closed for connection in opened)  # type: ignore[attr-defined]


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


def test_log_chunks_use_byte_offsets_and_reset_invalid_cursors(
    tmp_path: Path,
) -> None:
    storage = Storage(_settings(tmp_path))
    job_id = "e" * 32
    storage.write_metadata(_metadata(job_id))
    paths = storage.paths_for(job_id)
    paths.logs_path.write_text("开始\nfinished\n", encoding="utf-8")
    first_size = len("开始\n".encode("utf-8"))

    text, next_offset, reset = storage.read_log_chunk(job_id, first_size)

    assert text == "finished\n"
    assert next_offset == paths.logs_path.stat().st_size
    assert reset is False

    text, next_offset, reset = storage.read_log_chunk(job_id, 999_999)
    assert text == "开始\nfinished\n"
    assert next_offset == paths.logs_path.stat().st_size
    assert reset is True

    text, next_offset, reset = storage.read_log_chunk(job_id, 0, limit=4)
    assert text == "开始"
    assert next_offset == len("开始".encode("utf-8"))
    assert reset is False
