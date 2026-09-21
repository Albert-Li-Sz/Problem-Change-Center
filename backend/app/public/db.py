from __future__ import annotations

import hashlib
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool


class Database:
    def __init__(self, url: str):
        self.pool = ConnectionPool(
            url,
            min_size=1,
            max_size=12,
            open=False,
            timeout=5,
            kwargs={"row_factory": dict_row, "connect_timeout": 5},
        )

    def open(self):
        self.pool.open(wait=True, timeout=20)

    def close(self):
        self.pool.close()

    @contextmanager
    def connect(self):
        with self.pool.connection() as conn:
            yield conn

    def one(self, query, params=()):
        with self.connect() as conn:
            return conn.execute(query, params).fetchone()

    def all(self, query, params=()):
        with self.connect() as conn:
            return conn.execute(query, params).fetchall()

    def execute(self, query, params=()):
        with self.connect() as conn:
            return conn.execute(query, params).rowcount

    def migrate(self):
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(728601)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY)"
            )
            for path in sorted(Path(__file__).with_name("migrations").glob("*.sql")):
                if not conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=%s", (path.name,)
                ).fetchone():
                    conn.execute(path.read_text())
                    conn.execute(
                        "INSERT INTO schema_migrations VALUES (%s)", (path.name,)
                    )

    def rate_limit(self, key: str, limit: int, seconds: int):
        digest = hashlib.sha256(key.encode()).hexdigest()
        row = self.one(
            """INSERT INTO rate_limits(key,hits,expires_at) VALUES (%s,1,now()+%s*interval '1 second')
               ON CONFLICT(key) DO UPDATE SET
                 hits=CASE WHEN rate_limits.expires_at<now() THEN 1 ELSE rate_limits.hits+1 END,
                 expires_at=CASE WHEN rate_limits.expires_at<now() THEN EXCLUDED.expires_at ELSE rate_limits.expires_at END
               RETURNING hits""",
            (digest, seconds),
        )
        if row["hits"] > limit:
            raise HTTPException(
                429, "请求过于频繁，请稍后重试", headers={"Retry-After": str(seconds)}
            )

    def audit(self, actor, action, target=None):
        self.execute(
            "INSERT INTO audit(actor_id,action,target) VALUES (%s,%s,%s)",
            (actor, action, target),
        )


class PostgresJobIndex:
    def __init__(self, db: Database):
        self.db = db

    def upsert(self, metadata, *, updated_at):
        self.db.execute(
            "INSERT INTO job_metadata VALUES (%s,%s) ON CONFLICT(id) DO UPDATE SET metadata=EXCLUDED.metadata",
            (metadata.id, Jsonb(asdict(metadata))),
        )

    def get(self, job_id):
        row = self.db.one("SELECT metadata FROM job_metadata WHERE id=%s", (job_id,))
        return row["metadata"] if row else None

    def delete(self, job_id):
        self.db.execute("DELETE FROM job_metadata WHERE id=%s", (job_id,))

    def ids(self):
        return [row["id"] for row in self.db.all("SELECT id FROM job_metadata")]
