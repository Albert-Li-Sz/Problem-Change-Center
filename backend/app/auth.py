from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import psycopg
from fastapi import HTTPException, status
from psycopg.rows import dict_row

from .config import Settings


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PBKDF2_ITERATIONS = 240_000
LOGGER = logging.getLogger(__name__)
ApprovalStatus = Literal["pending", "approved", "rejected"]
APPROVAL_STATUSES: set[str] = {"pending", "approved", "rejected"}


@dataclass(frozen=True)
class AuthenticatedUser:
    id: str
    email: str
    is_admin: bool
    daily_quota: int
    created_at: str
    approval_status: ApprovalStatus = "pending"
    reviewed_at: str | None = None
    reviewed_by: str | None = None
    email_verified_at: str | None = None
    daily_used: int = 0
    daily_pending: int = 0

    @property
    def remaining_today(self) -> int | None:
        if self.is_admin:
            return None
        return max(0, self.daily_quota - self.daily_used - self.daily_pending)


@dataclass(frozen=True)
class QuotaInfo:
    daily_quota: int
    daily_used: int
    daily_pending: int
    remaining_today: int | None


class AuthService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._schema_lock = threading.Lock()
        self._schema_ready = False
        self._bootstrap_lock = threading.Lock()
        self._bootstrap_ready = False

    def ensure_ready(self) -> None:
        self._ensure_schema()
        if self._bootstrap_ready:
            return
        with self._bootstrap_lock:
            if self._bootstrap_ready:
                return
            self.bootstrap_admin()
            self._bootstrap_ready = True

    def register(self, email: str, password: str) -> AuthenticatedUser:
        email = _normalize_email(email)
        _validate_password(password)
        self._ensure_schema()

        user_id = uuid.uuid4().hex
        password_hash = _hash_password(password)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM p2h_users WHERE email = %s", (email,))
                existing = cur.fetchone()
                if existing is not None:
                    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email is already registered")
                cur.execute(
                    """
                    INSERT INTO p2h_users (id, email, password_hash, is_admin, daily_quota, approval_status)
                    VALUES (%s, %s, %s, false, %s, 'pending')
                    """,
                    (user_id, email, password_hash, self.settings.default_daily_quota),
                )
        return self.get_user(user_id)

    def login(self, email: str, password: str) -> tuple[AuthenticatedUser, str]:
        email = _normalize_email(email)
        self._ensure_schema()
        row = self._fetch_user_row_by_email(email)
        if row is None or not _verify_password(password, str(row["password_hash"])):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
        if row["approval_status"] != "approved":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_approval_detail(str(row["approval_status"])))

        user = self._user_from_row(row)
        return user, self.create_token(user)

    def authenticate_header(self, authorization: str | None) -> AuthenticatedUser:
        if not authorization:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Authorization header")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Authorization header")
        return self.authenticate_token(token)

    def authenticate_token(self, token: str) -> AuthenticatedUser:
        self._ensure_schema()
        try:
            payload_b64, signature = token.split(".", 1)
            expected = _sign(payload_b64, self.settings.auth_secret_key)
            if not hmac.compare_digest(signature, expected):
                raise ValueError("bad signature")
            payload = json.loads(_b64decode(payload_b64).decode("utf-8"))
            expires_at = int(payload["exp"])
            user_id = str(payload["sub"])
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc

        if expires_at < int(datetime.now(timezone.utc).timestamp()):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has expired")
        user = self.get_user(user_id)
        if user.approval_status != "approved":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_approval_detail(user.approval_status))
        return user

    def create_token(self, user: AuthenticatedUser) -> str:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=self.settings.auth_token_ttl_seconds)
        payload = {
            "sub": user.id,
            "email": user.email,
            "exp": int(expires_at.timestamp()),
        }
        payload_b64 = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        return f"{payload_b64}.{_sign(payload_b64, self.settings.auth_secret_key)}"

    def get_user(self, user_id: str) -> AuthenticatedUser:
        self._ensure_schema()
        row = self._fetch_user_row_by_id(user_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown user")
        return self._user_from_row(row)

    def list_users(self) -> list[AuthenticatedUser]:
        self._ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT u.*, COALESCE(q.used, 0) AS daily_used, COALESCE(q.pending, 0) AS daily_pending
                    FROM p2h_users u
                    LEFT JOIN p2h_daily_usage q
                      ON q.user_id = u.id AND q.usage_date = CURRENT_DATE
                    ORDER BY u.created_at ASC
                    """
                )
                return [self._user_from_row(row) for row in cur.fetchall()]

    def update_user(
        self,
        actor: AuthenticatedUser,
        user_id: str,
        *,
        daily_quota: int | None,
        daily_used: int | None,
        is_admin: bool | None,
        approval_status: ApprovalStatus | None,
    ) -> AuthenticatedUser:
        self.require_admin(actor)
        self._ensure_schema()
        if daily_quota is not None and daily_quota < 0:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="daily_quota must be non-negative")
        if daily_used is not None and daily_used < 0:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="daily_used must be non-negative")
        if approval_status is not None and approval_status not in APPROVAL_STATUSES:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid approval status")

        updates: list[str] = []
        params: list[Any] = []
        if daily_quota is not None:
            updates.append("daily_quota = %s")
            params.append(daily_quota)
        if is_admin is not None:
            updates.append("is_admin = %s")
            params.append(is_admin)
        if approval_status is not None:
            updates.append("approval_status = %s")
            params.append(approval_status)
            updates.append("reviewed_at = now()")
            updates.append("reviewed_by = %s")
            params.append(actor.id)
        if not updates and daily_used is None:
            return self.get_user(user_id)

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, is_admin, approval_status FROM p2h_users WHERE id = %s FOR UPDATE", (user_id,))
                existing = cur.fetchone()
                if existing is None:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown user")
                current_is_admin = bool(existing["is_admin"])
                current_approval_status = str(existing["approval_status"])
                next_is_admin = current_is_admin if is_admin is None else is_admin
                next_approval_status = current_approval_status if approval_status is None else approval_status
                if current_is_admin and current_approval_status == "approved" and (
                    not next_is_admin or next_approval_status != "approved"
                ):
                    if self._approved_admin_count(cur) <= 1:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Cannot remove the last approved administrator",
                        )
                if updates:
                    updates.append("updated_at = now()")
                    params.append(user_id)
                    cur.execute(f"UPDATE p2h_users SET {', '.join(updates)} WHERE id = %s", params)
                if daily_used is not None:
                    cur.execute(
                        """
                        INSERT INTO p2h_daily_usage (user_id, usage_date, used, pending)
                        VALUES (%s, CURRENT_DATE, %s, 0)
                        ON CONFLICT (user_id, usage_date)
                        DO UPDATE SET used = EXCLUDED.used
                        """,
                        (user_id, daily_used),
                    )
        return self.get_user(user_id)

    def change_password(self, user: AuthenticatedUser, current_password: str, new_password: str) -> None:
        _validate_password(new_password)
        self._ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT password_hash FROM p2h_users WHERE id = %s FOR UPDATE", (user.id,))
                row = cur.fetchone()
                if row is None:
                    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown user")
                if not _verify_password(current_password, str(row["password_hash"])):
                    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Current password is invalid")
                cur.execute(
                    "UPDATE p2h_users SET password_hash = %s, updated_at = now() WHERE id = %s",
                    (_hash_password(new_password), user.id),
                )

    def reset_password(self, actor: AuthenticatedUser, user_id: str, new_password: str) -> None:
        self.require_admin(actor)
        _validate_password(new_password)
        self._ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM p2h_users WHERE id = %s", (user_id,))
                if cur.fetchone() is None:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown user")
                cur.execute(
                    "UPDATE p2h_users SET password_hash = %s, updated_at = now() WHERE id = %s",
                    (_hash_password(new_password), user_id),
                )

    def reserve_conversion(self, user: AuthenticatedUser) -> QuotaInfo:
        self._ensure_schema()
        if user.is_admin:
            return self.quota_for(user.id)

        with self._connect() as conn:
            with conn.cursor() as cur:
                self._ensure_usage_row(cur, user.id)
                cur.execute(
                    """
                    SELECT u.daily_quota, q.used, q.pending
                    FROM p2h_users u
                    JOIN p2h_daily_usage q ON q.user_id = u.id AND q.usage_date = CURRENT_DATE
                    WHERE u.id = %s
                    FOR UPDATE OF q
                    """,
                    (user.id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown user")
                quota = int(row["daily_quota"])
                used = int(row["used"])
                pending = int(row["pending"])
                if used + pending >= quota:
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail=f"Daily conversion quota exceeded ({quota}/day)",
                    )
                pending += 1
                cur.execute(
                    "UPDATE p2h_daily_usage SET pending = %s WHERE user_id = %s AND usage_date = CURRENT_DATE",
                    (pending, user.id),
                )
                return QuotaInfo(
                    daily_quota=quota,
                    daily_used=used,
                    daily_pending=pending,
                    remaining_today=max(0, quota - used - pending),
                )

    def complete_conversion(self, user_id: str | None, *, success: bool) -> None:
        if not user_id:
            return
        user = self.get_user(user_id)
        with self._connect() as conn:
            with conn.cursor() as cur:
                self._ensure_usage_row(cur, user_id)
                cur.execute(
                    """
                    SELECT used, pending
                    FROM p2h_daily_usage
                    WHERE user_id = %s AND usage_date = CURRENT_DATE
                    FOR UPDATE
                    """,
                    (user_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return
                used = int(row["used"])
                pending = max(0, int(row["pending"]) - 1)
                if success and not user.is_admin:
                    used += 1
                cur.execute(
                    """
                    UPDATE p2h_daily_usage
                    SET used = %s, pending = %s
                    WHERE user_id = %s AND usage_date = CURRENT_DATE
                    """,
                    (used, pending, user_id),
                )

    def quota_for(self, user_id: str) -> QuotaInfo:
        user = self.get_user(user_id)
        used, pending = self._daily_counts(user_id)
        remaining = None if user.is_admin else max(0, user.daily_quota - used - pending)
        return QuotaInfo(daily_quota=user.daily_quota, daily_used=used, daily_pending=pending, remaining_today=remaining)

    def require_admin(self, user: AuthenticatedUser) -> None:
        if not user.is_admin:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required")

    def bootstrap_admin(self) -> None:
        self._ensure_schema()
        email = self.settings.bootstrap_admin_email.strip()
        password = self.settings.bootstrap_admin_password
        if not email or not password:
            return
        email = _normalize_email(email)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM p2h_users WHERE email = %s", (email,))
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        """
                        INSERT INTO p2h_users (
                            id, email, password_hash, is_admin, daily_quota,
                            email_verified_at, approval_status, reviewed_at
                        )
                        VALUES (%s, %s, %s, true, %s, now(), 'approved', now())
                        """,
                        (uuid.uuid4().hex, email, _hash_password(password), self.settings.default_daily_quota),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE p2h_users
                        SET is_admin = true,
                            approval_status = 'approved',
                            email_verified_at = COALESCE(email_verified_at, now()),
                            reviewed_at = COALESCE(reviewed_at, now()),
                            reviewed_by = COALESCE(reviewed_by, id),
                            updated_at = now()
                        WHERE email = %s
                        """,
                        (email,),
                    )

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS p2h_users (
                            id text PRIMARY KEY,
                            email text UNIQUE NOT NULL,
                            password_hash text NOT NULL,
                            is_admin boolean NOT NULL DEFAULT false,
                            daily_quota integer NOT NULL DEFAULT 10,
                            email_verified_at timestamptz,
                            approval_status text NOT NULL DEFAULT 'pending',
                            reviewed_at timestamptz,
                            reviewed_by text,
                            created_at timestamptz NOT NULL DEFAULT now(),
                            updated_at timestamptz NOT NULL DEFAULT now()
                        )
                        """
                    )
                    cur.execute("ALTER TABLE p2h_users ADD COLUMN IF NOT EXISTS email_verified_at timestamptz")
                    cur.execute("ALTER TABLE p2h_users ADD COLUMN IF NOT EXISTS approval_status text")
                    cur.execute("ALTER TABLE p2h_users ADD COLUMN IF NOT EXISTS reviewed_at timestamptz")
                    cur.execute("ALTER TABLE p2h_users ADD COLUMN IF NOT EXISTS reviewed_by text")
                    cur.execute(
                        """
                        UPDATE p2h_users
                        SET approval_status = CASE
                            WHEN is_admin OR email_verified_at IS NOT NULL THEN 'approved'
                            ELSE 'pending'
                        END,
                            reviewed_at = CASE
                                WHEN is_admin OR email_verified_at IS NOT NULL THEN COALESCE(reviewed_at, email_verified_at, updated_at, created_at, now())
                                ELSE reviewed_at
                            END
                        WHERE approval_status IS NULL OR approval_status NOT IN ('pending', 'approved', 'rejected')
                        """
                    )
                    cur.execute(
                        """
                        UPDATE p2h_users
                        SET approval_status = 'approved',
                            reviewed_at = COALESCE(reviewed_at, email_verified_at, updated_at, created_at, now())
                        WHERE (is_admin OR email_verified_at IS NOT NULL) AND approval_status = 'pending'
                        """
                    )
                    cur.execute("ALTER TABLE p2h_users ALTER COLUMN approval_status SET DEFAULT 'pending'")
                    cur.execute("ALTER TABLE p2h_users ALTER COLUMN approval_status SET NOT NULL")
                    cur.execute(
                        """
                        DO $$
                        BEGIN
                            IF NOT EXISTS (
                                SELECT 1
                                FROM pg_constraint
                                WHERE conname = 'p2h_users_approval_status_check'
                            ) THEN
                                ALTER TABLE p2h_users
                                ADD CONSTRAINT p2h_users_approval_status_check
                                CHECK (approval_status IN ('pending', 'approved', 'rejected'));
                            END IF;
                        END
                        $$;
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS p2h_daily_usage (
                            user_id text NOT NULL REFERENCES p2h_users(id) ON DELETE CASCADE,
                            usage_date date NOT NULL,
                            used integer NOT NULL DEFAULT 0,
                            pending integer NOT NULL DEFAULT 0,
                            PRIMARY KEY (user_id, usage_date)
                        )
                        """
                    )
                    cur.execute("ALTER TABLE p2h_daily_usage ADD COLUMN IF NOT EXISTS pending integer NOT NULL DEFAULT 0")
            self._schema_ready = True

    def _connect(self) -> psycopg.Connection[Any]:
        return psycopg.connect(self.settings.database_url, row_factory=dict_row)

    def _fetch_user_row_by_email(self, email: str, cur: psycopg.Cursor[Any] | None = None) -> dict[str, Any] | None:
        query = """
            SELECT u.*, COALESCE(q.used, 0) AS daily_used, COALESCE(q.pending, 0) AS daily_pending
            FROM p2h_users u
            LEFT JOIN p2h_daily_usage q
              ON q.user_id = u.id AND q.usage_date = CURRENT_DATE
            WHERE u.email = %s
        """
        if cur is not None:
            cur.execute(query, (email,))
            return cur.fetchone()
        with self._connect() as conn:
            with conn.cursor() as owned_cur:
                owned_cur.execute(query, (email,))
                return owned_cur.fetchone()

    def _fetch_user_row_by_id(self, user_id: str, cur: psycopg.Cursor[Any] | None = None) -> dict[str, Any] | None:
        query = """
            SELECT u.*, COALESCE(q.used, 0) AS daily_used, COALESCE(q.pending, 0) AS daily_pending
            FROM p2h_users u
            LEFT JOIN p2h_daily_usage q
              ON q.user_id = u.id AND q.usage_date = CURRENT_DATE
            WHERE u.id = %s
        """
        if cur is not None:
            cur.execute(query, (user_id,))
            return cur.fetchone()
        with self._connect() as conn:
            with conn.cursor() as owned_cur:
                owned_cur.execute(query, (user_id,))
                return owned_cur.fetchone()

    def _daily_counts(self, user_id: str) -> tuple[int, int]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(used, 0) AS used, COALESCE(pending, 0) AS pending FROM p2h_daily_usage WHERE user_id = %s AND usage_date = CURRENT_DATE",
                    (user_id,),
                )
                row = cur.fetchone()
                if not row:
                    return 0, 0
                return int(row["used"]), int(row["pending"])

    def _ensure_usage_row(self, cur: psycopg.Cursor[Any], user_id: str) -> None:
        cur.execute(
            """
            INSERT INTO p2h_daily_usage (user_id, usage_date, used, pending)
            VALUES (%s, CURRENT_DATE, 0, 0)
            ON CONFLICT (user_id, usage_date) DO NOTHING
            """,
            (user_id,),
        )

    def _approved_admin_count(self, cur: psycopg.Cursor[Any]) -> int:
        cur.execute("SELECT count(*) AS count FROM p2h_users WHERE is_admin = true AND approval_status = 'approved'")
        return int(cur.fetchone()["count"])

    def _user_from_row(self, row: dict[str, Any] | None) -> AuthenticatedUser:
        if row is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown user")
        approval_status = str(row.get("approval_status") or "pending")
        if approval_status not in APPROVAL_STATUSES:
            approval_status = "pending"
        return AuthenticatedUser(
            id=str(row["id"]),
            email=str(row["email"]),
            is_admin=bool(row["is_admin"]),
            daily_quota=int(row["daily_quota"]),
            created_at=_iso(row["created_at"]),
            approval_status=approval_status,  # type: ignore[arg-type]
            reviewed_at=_iso(row["reviewed_at"]) if row.get("reviewed_at") else None,
            reviewed_by=str(row["reviewed_by"]) if row.get("reviewed_by") else None,
            email_verified_at=_iso(row["email_verified_at"]) if row.get("email_verified_at") else None,
            daily_used=int(row.get("daily_used") or 0),
            daily_pending=int(row.get("daily_pending") or 0),
        )


def _normalize_email(email: str) -> str:
    email = email.strip().lower()
    if not EMAIL_RE.fullmatch(email):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid email address")
    return email


def _validate_password(password: str) -> None:
    if len(password) < 8:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Password must be at least 8 characters")


def _approval_detail(approval_status: str) -> str:
    if approval_status == "pending":
        return "Account is pending administrator approval"
    if approval_status == "rejected":
        return "Account has been rejected"
    return "Account is not approved"


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _sign(payload_b64: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64encode(digest)


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)
