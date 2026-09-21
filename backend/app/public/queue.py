from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from .config import PublicConfig
from .db import Database


class Queue:
    def __init__(self, db: Database, config: PublicConfig):
        self.db, self.config = db, config

    @staticmethod
    def lock(conn):
        # Serializes the short admission/claim/accounting transactions only.
        conn.execute("SELECT pg_advisory_xact_lock(728602)")

    def owned(self, job_id: str, user_id: str):
        row = self.db.one(
            "SELECT * FROM jobs WHERE id=%s AND user_id=%s AND NOT delete_requested AND expires_at>now()",
            (job_id, user_id),
        )
        if not row:
            raise HTTPException(404, "任务不存在或已过期")
        return row

    def create_upload(self, user_id: str, filename: str):
        job_id = uuid.uuid4().hex
        with self.db.connect() as conn:
            self.lock(conn)
            limits = conn.execute(
                "SELECT * FROM user_limits WHERE id=%s", (user_id,)
            ).fetchone()
            if not limits or limits["banned"]:
                raise HTTPException(403, "账号不可用")
            count = conn.execute(
                "SELECT count(*) AS n FROM jobs WHERE user_id=%s AND NOT delete_requested",
                (user_id,),
            ).fetchone()["n"]
            uploading = conn.execute(
                "SELECT count(*) AS n FROM jobs WHERE user_id=%s AND status='uploading'",
                (user_id,),
            ).fetchone()["n"]
            if count >= 20 or uploading >= 2:
                raise HTTPException(
                    429, "最多保留 20 个任务，同时最多上传 2 个文件；请先删除旧任务"
                )
            if (
                conn.execute(
                    "SELECT count(*) AS n FROM jobs WHERE status='uploading'"
                ).fetchone()["n"]
                >= 2
            ):
                raise HTTPException(429, "上传通道繁忙，请稍后重试")
            conn.execute(
                "INSERT INTO jobs(id,user_id,filename) VALUES (%s,%s,%s)",
                (job_id, user_id, filename),
            )
        return job_id

    def abandon_upload(self, job_id: str):
        """Release admission if parsing failed or the client disconnected."""
        with self.db.connect() as conn:
            self.lock(conn)
            row = conn.execute(
                "SELECT * FROM jobs WHERE id=%s AND status='uploading' FOR UPDATE",
                (job_id,),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE jobs SET delete_requested=true WHERE id=%s", (job_id,)
                )
                self.settle(conn, row, status="cancelled", error="上传未完成")

    def submit(self, job_id: str, user_id: str, *, kind: str, payload=None, size=0):
        minutes = 1 if kind == "inspect" else 30
        wine = bool(
            kind == "convert"
            and payload
            and payload.get("source_format") in {"auto", "polygon"}
        )
        with self.db.connect() as conn:
            self.lock(conn)
            if not conn.execute(
                "SELECT queue_open FROM site_settings WHERE id=1"
            ).fetchone()["queue_open"]:
                raise HTTPException(503, "任务队列暂时暂停")
            row = conn.execute(
                "SELECT * FROM jobs WHERE id=%s AND user_id=%s FOR UPDATE",
                (job_id, user_id),
            ).fetchone()
            expected = "uploading" if kind == "inspect" else "uploaded"
            if not row or row["delete_requested"]:
                raise HTTPException(404, "任务不存在")
            if row["status"] != expected:
                raise HTTPException(409, "任务已提交或尚未完成检查")
            limits = conn.execute(
                "SELECT * FROM user_limits WHERE id=%s", (user_id,)
            ).fetchone()
            if not limits or limits["banned"] or not limits["verified"]:
                raise HTTPException(403, "账号不可用")
            waiting = conn.execute(
                "SELECT count(*) AS n FROM jobs WHERE user_id=%s AND status='queued'",
                (user_id,),
            ).fetchone()["n"]
            if waiting >= 5:
                raise HTTPException(429, "最多同时排队 5 个任务")
            day = conn.execute(
                "SELECT (now() AT TIME ZONE 'UTC')::date AS day"
            ).fetchone()["day"]
            conn.execute(
                "INSERT INTO usage(user_id,day) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (user_id, day),
            )
            usage = conn.execute(
                "SELECT * FROM usage WHERE user_id=%s AND day=%s FOR UPDATE",
                (user_id, day),
            ).fetchone()
            if usage["used"] + usage["reserved"] + minutes > limits["daily_minutes"]:
                raise HTTPException(
                    429,
                    f"剩余额度不足；本次需要预留 {minutes} 分钟，结束后退回未使用部分",
                )
            conn.execute(
                "UPDATE usage SET reserved=reserved+%s WHERE user_id=%s AND day=%s",
                (minutes, user_id, day),
            )
            conn.execute(
                """UPDATE jobs SET kind=%s,status='queued',request=%s,wine=%s,quota_date=%s,
                   reserved_minutes=%s,started_at=NULL,finished_at=NULL,cancel_requested=false,error=NULL,
                   size=CASE WHEN %s>0 THEN %s ELSE size END WHERE id=%s""",
                (kind, Jsonb(payload), wine, day, minutes, size, size, job_id),
            )

    def claim(self):
        with self.db.connect() as conn:
            self.lock(conn)
            if not conn.execute(
                "SELECT queue_open FROM site_settings WHERE id=1"
            ).fetchone()["queue_open"]:
                return None
            active = conn.execute(
                "SELECT count(*) AS n,count(*) FILTER (WHERE wine) AS wine FROM jobs WHERE status='running'"
            ).fetchone()
            if active["n"] >= self.config.max_concurrent:
                return None
            row = conn.execute(
                """SELECT j.* FROM jobs j JOIN user_limits u ON u.id=j.user_id
                   WHERE j.status='queued' AND NOT j.cancel_requested AND NOT j.delete_requested
                     AND j.expires_at>now() AND u.verified AND NOT u.banned
                     AND (NOT j.wine OR %s < %s)
                     AND (SELECT count(*) FROM jobs r WHERE r.user_id=j.user_id AND r.status='running') < 2
                   ORDER BY j.created_at FOR UPDATE OF j SKIP LOCKED LIMIT 1""",
                (active["wine"], self.config.max_wine),
            ).fetchone()
            if not row:
                return None
            token = uuid.uuid4().hex
            return conn.execute(
                """UPDATE jobs SET status='running',started_at=now(),lease_token=%s,
                   lease_until=now()+interval '30 seconds' WHERE id=%s RETURNING *""",
                (token, row["id"]),
            ).fetchone()

    def heartbeat(self, job_id: str, token: str):
        return self.db.one(
            """UPDATE jobs SET lease_until=now()+interval '30 seconds'
               WHERE id=%s AND lease_token=%s AND status='running' RETURNING cancel_requested,delete_requested""",
            (job_id, token),
        )

    @staticmethod
    def settle(conn, row, *, status, error=None):
        elapsed = 0
        if row["started_at"]:
            elapsed = max(
                1,
                math.ceil(
                    (datetime.now(timezone.utc) - row["started_at"]).total_seconds()
                    / 60
                ),
            )
        billed = min(row["reserved_minutes"], elapsed)
        if row["reserved_minutes"]:
            conn.execute(
                "UPDATE usage SET reserved=GREATEST(0,reserved-%s),used=used+%s WHERE user_id=%s AND day=%s",
                (row["reserved_minutes"], billed, row["user_id"], row["quota_date"]),
            )
        conn.execute(
            """UPDATE jobs SET status=%s,error=%s,finished_at=now(),expires_at=now()+interval '24 hours',
               reserved_minutes=0,billed_minutes=billed_minutes+%s,lease_until=NULL WHERE id=%s""",
            (status, error, billed, row["id"]),
        )

    def finish(self, job_id: str, token: str, status: str, error=None):
        with self.db.connect() as conn:
            self.lock(conn)
            row = conn.execute(
                "SELECT * FROM jobs WHERE id=%s AND lease_token=%s AND status='running' FOR UPDATE",
                (job_id, token),
            ).fetchone()
            if not row:
                return False
            if row["cancel_requested"]:
                status, error = "cancelled", "任务已取消"
            self.settle(conn, row, status=status, error=error)
            return True

    def cancel(self, job_id: str, *, delete=False):
        with self.db.connect() as conn:
            self.lock(conn)
            row = conn.execute(
                "SELECT * FROM jobs WHERE id=%s FOR UPDATE", (job_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "任务不存在")
            conn.execute(
                "UPDATE jobs SET cancel_requested=true,delete_requested=delete_requested OR %s WHERE id=%s",
                (delete, job_id),
            )
            if row["status"] in {"queued", "uploading", "uploaded"}:
                self.settle(conn, row, status="cancelled", error="任务已取消")

    def quota(self, user_id: str):
        row = self.db.one(
            """SELECT u.daily_minutes,COALESCE(q.used,0) AS used,COALESCE(q.reserved,0) AS reserved
               FROM user_limits u LEFT JOIN usage q ON q.user_id=u.id AND q.day=(now() AT TIME ZONE 'UTC')::date
               WHERE u.id=%s""",
            (user_id,),
        )
        return {
            **row,
            "remaining": max(0, row["daily_minutes"] - row["used"] - row["reserved"]),
            "reset_timezone": "UTC",
        }

    def position(self, row):
        if row["status"] != "queued":
            return None
        return self.db.one(
            "SELECT count(*)+1 AS n FROM jobs WHERE status='queued' AND created_at<%s",
            (row["created_at"],),
        )["n"]
