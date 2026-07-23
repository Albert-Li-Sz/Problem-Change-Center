from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .storage import JobMetadata


SCHEMA_VERSION = 1


class JobIndex:
    """Durable, queryable index for job metadata stored beside job artifacts."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    size INTEGER NOT NULL CHECK (size >= 0),
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    source_format TEXT,
                    target_format TEXT,
                    parent_job_id TEXT,
                    metadata_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS jobs_status_created_idx "
                "ON jobs(status, created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS jobs_parent_idx ON jobs(parent_job_id)"
            )
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def upsert(self, metadata: JobMetadata, *, updated_at: str) -> None:
        payload = json.dumps(
            asdict(metadata), ensure_ascii=False, separators=(",", ":")
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, filename, size, status, created_at, started_at, finished_at,
                    source_format, target_format, parent_job_id, metadata_json,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    filename = excluded.filename,
                    size = excluded.size,
                    status = excluded.status,
                    created_at = excluded.created_at,
                    started_at = excluded.started_at,
                    finished_at = excluded.finished_at,
                    source_format = excluded.source_format,
                    target_format = excluded.target_format,
                    parent_job_id = excluded.parent_job_id,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    metadata.id,
                    metadata.filename,
                    metadata.size,
                    metadata.status,
                    metadata.created_at,
                    metadata.started_at,
                    metadata.finished_at,
                    metadata.source_format,
                    metadata.target_format,
                    metadata.parent_job_id,
                    payload,
                    updated_at,
                ),
            )

    def get(self, job_id: str) -> dict[str, object] | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT metadata_json FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            return None
        value = json.loads(row["metadata_json"])
        if not isinstance(value, dict):
            raise ValueError("invalid indexed job metadata")
        return value

    def delete(self, job_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def ids(self) -> list[str]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute("SELECT id FROM jobs ORDER BY id").fetchall()
        return [str(row["id"]) for row in rows]

    def prune_missing(self, existing_ids: set[str]) -> None:
        with closing(self._connect()) as connection, connection:
            indexed_ids = {
                str(row["id"])
                for row in connection.execute("SELECT id FROM jobs").fetchall()
            }
            connection.executemany(
                "DELETE FROM jobs WHERE id = ?",
                ((job_id,) for job_id in sorted(indexed_ids - existing_ids)),
            )
