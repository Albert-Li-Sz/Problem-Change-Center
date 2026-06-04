from __future__ import annotations

import os
import uuid
from pathlib import Path

import psycopg
import pytest
from fastapi import HTTPException

from app.auth import AuthService, _hash_password
from app.config import Settings


TEST_DATABASE_URL = os.getenv("P2H_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="P2H_TEST_DATABASE_URL is not set")


def _settings(*, bootstrap_email: str = "", bootstrap_password: str = "") -> Settings:
    assert TEST_DATABASE_URL is not None
    return Settings(
        data_dir=Path("/tmp/p2h-auth-tests"),
        docker_bin="docker",
        runner_image="p2h-runner",
        max_upload_bytes=1024,
        job_timeout_seconds=10,
        job_ttl_seconds=3600,
        docker_memory="1g",
        docker_cpus="2",
        docker_pids_limit=256,
        docker_tmp_size="512m",
        docker_work_size="1g",
        database_url=TEST_DATABASE_URL,
        auth_secret_key="test-secret",
        default_daily_quota=2,
        bootstrap_admin_email=bootstrap_email,
        bootstrap_admin_password=bootstrap_password,
    )


def _clean_tables(database_url: str) -> None:
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS p2h_email_verifications")
            cur.execute("DROP TABLE IF EXISTS p2h_daily_usage")
            cur.execute("DROP TABLE IF EXISTS p2h_users")


def test_register_approval_passwords_admin_and_quota() -> None:
    assert TEST_DATABASE_URL is not None
    _clean_tables(TEST_DATABASE_URL)

    admin_email = "admin@example.com"
    service = AuthService(_settings(bootstrap_email=admin_email, bootstrap_password="initialpass"))
    service.ensure_ready()

    admin, admin_token = service.login(admin_email, "initialpass")
    assert admin_token
    assert admin.is_admin is True
    assert admin.approval_status == "approved"

    with pytest.raises(HTTPException) as wrong_current_password:
        service.change_password(admin, "wrongpass", "changedpass")
    assert wrong_current_password.value.status_code == 401

    service.change_password(admin, "initialpass", "changedpass")
    restarted = AuthService(_settings(bootstrap_email=admin_email, bootstrap_password="initialpass"))
    restarted.ensure_ready()
    with pytest.raises(HTTPException) as old_bootstrap_password:
        restarted.login(admin_email, "initialpass")
    assert old_bootstrap_password.value.status_code == 401
    admin, _ = restarted.login(admin_email, "changedpass")

    email = f"{uuid.uuid4().hex}@example.com"
    user = restarted.register(email, "password123")
    assert user.email == email
    assert user.approval_status == "pending"

    with pytest.raises(HTTPException) as pending_login:
        restarted.login(email, "password123")
    assert pending_login.value.status_code == 403

    rejected = restarted.update_user(
        admin,
        user.id,
        daily_quota=None,
        daily_used=None,
        is_admin=None,
        approval_status="rejected",
    )
    assert rejected.approval_status == "rejected"
    with pytest.raises(HTTPException) as rejected_login:
        restarted.login(email, "password123")
    assert rejected_login.value.status_code == 403

    approved = restarted.update_user(
        admin,
        user.id,
        daily_quota=None,
        daily_used=None,
        is_admin=None,
        approval_status="approved",
    )
    assert approved.approval_status == "approved"
    logged_in, user_token = restarted.login(email, "password123")
    assert user_token
    assert restarted.authenticate_token(user_token).id == logged_in.id

    hold = restarted.reserve_conversion(logged_in)
    assert hold.daily_pending == 1
    assert hold.remaining_today == 1
    logged_in = restarted.get_user(logged_in.id)
    restarted.reserve_conversion(logged_in)
    logged_in = restarted.get_user(logged_in.id)
    with pytest.raises(HTTPException) as quota_exceeded:
        restarted.reserve_conversion(logged_in)
    assert quota_exceeded.value.status_code == 429

    restarted.complete_conversion(logged_in.id, success=True)
    after_success = restarted.get_user(logged_in.id)
    assert after_success.daily_used == 1
    assert after_success.daily_pending == 1

    restarted.complete_conversion(logged_in.id, success=False)
    after_failure = restarted.get_user(logged_in.id)
    assert after_failure.daily_used == 1
    assert after_failure.daily_pending == 0

    with pytest.raises(HTTPException) as not_admin:
        restarted.require_admin(after_failure)
    assert not_admin.value.status_code == 403

    updated = restarted.update_user(
        admin,
        after_failure.id,
        daily_quota=5,
        daily_used=0,
        is_admin=True,
        approval_status="approved",
    )
    assert updated.daily_quota == 5
    assert updated.daily_used == 0
    assert updated.is_admin is True

    restarted.reset_password(admin, updated.id, "resetpass1")
    with pytest.raises(HTTPException) as old_user_password:
        restarted.login(email, "password123")
    assert old_user_password.value.status_code == 401
    reset_user, _ = restarted.login(email, "resetpass1")
    assert reset_user.id == updated.id


def test_cannot_remove_last_approved_admin() -> None:
    assert TEST_DATABASE_URL is not None
    _clean_tables(TEST_DATABASE_URL)

    service = AuthService(_settings(bootstrap_email="admin@example.com", bootstrap_password="initialpass"))
    service.ensure_ready()
    admin, _ = service.login("admin@example.com", "initialpass")

    with pytest.raises(HTTPException) as demote_last_admin:
        service.update_user(
            admin,
            admin.id,
            daily_quota=None,
            daily_used=None,
            is_admin=False,
            approval_status=None,
        )
    assert demote_last_admin.value.status_code == 400

    with pytest.raises(HTTPException) as reject_last_admin:
        service.update_user(
            admin,
            admin.id,
            daily_quota=None,
            daily_used=None,
            is_admin=None,
            approval_status="rejected",
        )
    assert reject_last_admin.value.status_code == 400


def test_legacy_verified_users_migrate_to_approved() -> None:
    assert TEST_DATABASE_URL is not None
    _clean_tables(TEST_DATABASE_URL)

    verified_id = uuid.uuid4().hex
    admin_id = uuid.uuid4().hex
    pending_id = uuid.uuid4().hex
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE p2h_users (
                    id text PRIMARY KEY,
                    email text UNIQUE NOT NULL,
                    password_hash text NOT NULL,
                    is_admin boolean NOT NULL DEFAULT false,
                    daily_quota integer NOT NULL DEFAULT 10,
                    email_verified_at timestamptz,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute(
                """
                INSERT INTO p2h_users (id, email, password_hash, is_admin, email_verified_at)
                VALUES
                    (%s, %s, %s, false, now()),
                    (%s, %s, %s, true, NULL),
                    (%s, %s, %s, false, NULL)
                """,
                (
                    verified_id,
                    "verified@example.com",
                    _hash_password("password123"),
                    admin_id,
                    "legacy-admin@example.com",
                    _hash_password("password123"),
                    pending_id,
                    "pending@example.com",
                    _hash_password("password123"),
                ),
            )

    service = AuthService(_settings())
    service.ensure_ready()
    users = {user.email: user for user in service.list_users()}

    assert users["verified@example.com"].approval_status == "approved"
    assert users["legacy-admin@example.com"].approval_status == "approved"
    assert users["pending@example.com"].approval_status == "pending"
