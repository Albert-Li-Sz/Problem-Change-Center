"""Real API + Worker for Playwright, using a disposable PostgreSQL schema."""

from __future__ import annotations

import json
import os
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
import uvicorn
from psycopg import sql

from app.public.auth import password_hash
from app.public.config import PublicConfig
from app.public.main import create_app
from app.public.worker import STOP, Worker


def main():
    database_url = os.environ["PUBLIC_TEST_DATABASE_URL"]
    schema = "p2h_browser_" + uuid.uuid4().hex
    root = Path(__file__).resolve().parents[2] / ".public-dev" / "browser"
    data = root / schema
    data.mkdir(parents=True)
    with psycopg.connect(database_url) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    parts = urlsplit(database_url)
    query = dict(parse_qsl(parts.query))
    query["options"] = f"-csearch_path={schema}"
    config = PublicConfig(
        database_url=urlunsplit(parts._replace(query=urlencode(query))),
        data_dir=data,
        origin="http://127.0.0.1:4180",
        development=True,
        contact_email="contact@example.org",
    )
    app = create_app(config)
    lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def test_lifespan(application):
        async with lifespan(application):
            db = app.state.db
            admin_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO users(id,email,password_hash,verified,is_admin) VALUES (%s,%s,%s,true,true)",
                (
                    admin_id,
                    "admin@example.com",
                    password_hash("browser-test-password-2026"),
                ),
            )
            seed = {
                "invitation": app.state.auth.invitations(1)[0],
                "data_dir": str(data),
            }
            (root / "seed.json").write_text(json.dumps(seed))
            worker = Worker(config, db)
            thread = threading.Thread(target=worker.serve, daemon=True)
            thread.start()
            try:
                yield
            finally:
                STOP.set()
                thread.join(timeout=45)
        with psycopg.connect(database_url) as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )

    app.router.lifespan_context = test_lifespan
    uvicorn.run(app, host="127.0.0.1", port=8780, log_level="warning")


if __name__ == "__main__":
    main()
