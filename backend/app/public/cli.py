"""Operator-only maintenance; never exposed through the public API."""

from __future__ import annotations

import argparse
import getpass
import os
import uuid

from psycopg import sql

from .auth import Auth, email_address, password_hash
from .config import PublicConfig
from .db import Database


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("migrate")
    commands.add_parser("provision")
    administrator = commands.add_parser("admin")
    administrator.add_argument("email")
    invites = commands.add_parser("invite")
    invites.add_argument("--count", type=int, default=10)
    args = parser.parse_args()
    config = PublicConfig.from_env()
    db = Database(config.database_url)
    db.open()
    try:
        if args.action in {"migrate", "provision"}:
            db.migrate()
        if args.action == "provision":
            provision(db)
        elif args.action == "admin":
            email = email_address(args.email)
            # A real terminal prompt keeps credentials out of argv and shell history.
            password = getpass.getpass("Administrator password (12+ characters): ")
            if password != getpass.getpass("Repeat password: "):
                raise SystemExit("Passwords differ")
            hashed = password_hash(password)
            with db.connect() as conn:
                row = conn.execute(
                    "INSERT INTO users(id,email,password_hash,verified,is_admin) VALUES (%s,%s,%s,true,true) ON CONFLICT(email) DO UPDATE SET password_hash=EXCLUDED.password_hash,verified=true,is_admin=true,banned=false RETURNING id",
                    (uuid.uuid4().hex, email, hashed),
                ).fetchone()
                conn.execute("DELETE FROM sessions WHERE user_id=%s", (row["id"],))
            print("Administrator created/updated; existing sessions revoked.")
        elif args.action == "invite":
            if not 1 <= args.count <= 100:
                raise SystemExit("count must be 1..100")
            for code in Auth(db, config).invitations(args.count):
                print(code)
    finally:
        db.close()


def provision(db: Database):
    with db.connect() as conn:
        conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        for name, env in (
            ("p2h_api", "PUBLIC_API_DB_PASSWORD"),
            ("p2h_worker", "PUBLIC_WORKER_DB_PASSWORD"),
        ):
            password = os.environ[env]
            if len(password) < 24:
                raise ValueError(f"{env} must contain at least 24 characters")
            if not conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname=%s", (name,)
            ).fetchone():
                conn.execute(
                    sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(name))
                )
            conn.execute(
                sql.SQL("ALTER ROLE {} WITH PASSWORD {}").format(
                    sql.Identifier(name), sql.Literal(password)
                )
            )
            conn.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                    sql.Identifier(name)
                )
            )
        conn.execute(
            "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO p2h_api"
        )
        conn.execute("GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO p2h_api")
        conn.execute(
            "GRANT SELECT,INSERT,UPDATE,DELETE ON jobs,job_metadata,usage,worker_status TO p2h_worker"
        )
        conn.execute("GRANT SELECT ON user_limits,site_settings TO p2h_worker")
        conn.execute(
            "REVOKE ALL ON users,sessions,auth_tokens,invitations,rate_limits,audit FROM p2h_worker"
        )


if __name__ == "__main__":
    main()
