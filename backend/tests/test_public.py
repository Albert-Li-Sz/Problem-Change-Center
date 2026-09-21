from __future__ import annotations

import io
import json
import os
import secrets
import subprocess
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql

from app.public.config import PublicConfig
from app.public.cli import provision
from app.public.db import Database
from app.public.queue import Queue
from app.public.auth import digest
from app.public.main import create_app
from app.public.worker import Worker, command, collect_output, container_name
from app.storage import JobMetadata, utc_now_iso


TEST_URL = os.getenv("PUBLIC_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="set PUBLIC_TEST_DATABASE_URL to a disposable PostgreSQL database",
)
PASSWORD = "test-password-long-enough"


@pytest.fixture
def site(tmp_path):
    schema = "p2h_test_" + uuid.uuid4().hex
    with psycopg.connect(TEST_URL) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    parts = urlsplit(TEST_URL)
    query = dict(parse_qsl(parts.query))
    query["options"] = f"-csearch_path={schema}"
    url = urlunsplit(parts._replace(query=urlencode(query)))
    config = PublicConfig(
        database_url=url,
        origin="http://testserver",
        data_dir=tmp_path,
        development=True,
        contact_email="operator@example.com",
    )
    app = create_app(config)
    try:
        with TestClient(app, headers={"Origin": config.origin}) as client:
            yield client, app, config
    finally:
        with psycopg.connect(TEST_URL) as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


def register(site, email="person@example.com", *, administrator=False):
    client, app, config = site
    code = app.state.auth.invitations(1)[0]
    response = client.post(
        "/api/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "invitation": code,
            "accept_terms": True,
        },
    )
    assert response.status_code == 202, response.text
    row = app.state.db.one("SELECT * FROM users WHERE email=%s", (email,))
    link = (config.data_dir / "dev-mail" / f"{row['id']}-verify.txt").read_text()
    token = link.split("=", 1)[1]
    assert client.post("/api/auth/verify", json={"token": token}).status_code == 200
    if administrator:
        app.state.db.execute("UPDATE users SET is_admin=true WHERE id=%s", (row["id"],))
    login = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    client.headers["X-CSRF-Token"] = client.cookies.get("p2h_csrf")
    return row


def make_job(site, uid, *, kind="inspect"):
    _, app, _ = site
    queue = app.state.queue
    job_id = queue.create_upload(uid, "test.zip")
    paths = app.state.storage.paths_for(job_id)
    paths.input_dir.mkdir(parents=True)
    paths.work_dir.mkdir()
    paths.output_dir.mkdir()
    paths.logs_path.touch()
    app.state.storage.write_metadata(
        JobMetadata(
            id=job_id,
            filename="test.zip",
            size=1,
            status="queued",
            created_at=utc_now_iso(),
        )
    )
    if kind == "convert":
        app.state.db.execute("UPDATE jobs SET status='uploaded' WHERE id=%s", (job_id,))
    payload = (
        {"job_id": job_id, "source_format": "hydro", "target_format": "icpc"}
        if kind == "convert"
        else None
    )
    queue.submit(job_id, uid, kind=kind, payload=payload, size=1)
    return job_id


def test_invitation_email_and_session(site):
    client, app, _ = site
    row = register(site)
    assert row["password_hash"].startswith("$argon2id$")
    invite = app.state.db.one("SELECT * FROM invitations")
    assert invite["used_by"] == row["id"]
    assert len(invite["token_hash"]) == 64
    assert client.get("/api/account").json()["quota"]["remaining"] == 120
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/account").status_code == 401


@pytest.mark.skipif(
    os.getenv("PUBLIC_TEST_ROLES") != "1",
    reason="requires a disposable PostgreSQL cluster with CREATE DATABASE/ROLE",
)
def test_production_database_roles_separate_private_account_data(monkeypatch):
    name = "p2h_role_test_" + uuid.uuid4().hex
    roles = ("p2h_api", "p2h_worker")
    passwords = {role: secrets.token_urlsafe(32) for role in roles}
    with psycopg.connect(TEST_URL, autocommit=True) as owner:
        if owner.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", (list(roles),)
        ).fetchall():
            pytest.skip("Existing platform roles must not be changed by tests")
        owner.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        parts = urlsplit(TEST_URL)._replace(path="/" + name, query="")
        db = Database(urlunsplit(parts))
        db.open()
        peers = []
        try:
            db.migrate()
            monkeypatch.setenv("PUBLIC_API_DB_PASSWORD", passwords["p2h_api"])
            monkeypatch.setenv("PUBLIC_WORKER_DB_PASSWORD", passwords["p2h_worker"])
            provision(db)
            for role in roles:
                peer = Database(
                    psycopg.conninfo.make_conninfo(
                        urlunsplit(parts), user=role, password=passwords[role]
                    )
                )
                peer.open()
                peers.append(peer)
            api, worker = peers
            uid = uuid.uuid4().hex
            api.execute(
                "INSERT INTO users(id,email,password_hash,verified) VALUES (%s,%s,%s,true)",
                (uid, "role-test@example.org", "unused-test-hash"),
            )
            api.one("SELECT 1 FROM schema_migrations LIMIT 1")
            config = PublicConfig(database_url=urlunsplit(parts), development=True)
            queue = Queue(api, config)
            job = queue.create_upload(uid, "test.zip")
            queue.submit(job, uid, kind="inspect", size=1)
            worker_queue = Queue(worker, config)
            claimed = worker_queue.claim()
            assert claimed["id"] == job
            assert worker_queue.heartbeat(job, claimed["lease_token"])
            assert worker_queue.finish(job, claimed["lease_token"], "failed")
            for table in ("users", "sessions", "auth_tokens", "invitations", "audit"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    worker.one(
                        sql.SQL("SELECT * FROM {} LIMIT 1").format(
                            sql.Identifier(table)
                        )
                    )
        finally:
            for peer in peers:
                peer.close()
            db.close()
            owner.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )
            for role in roles:
                owner.execute(
                    sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role))
                )


def test_csrf_origin_admin_and_anonymous_boundaries(site):
    client, _, _ = site
    assert client.get("/api/jobs").status_code == 401
    assert (
        client.post(
            "/api/inspect", headers={"Origin": "https://attacker.example"}
        ).status_code
        == 403
    )
    register(site)
    assert (
        client.post("/api/auth/logout", headers={"X-CSRF-Token": "bad"}).status_code
        == 403
    )
    assert client.get("/api/admin/users").status_code == 403
    assert client.get("/api/account").status_code == 200


def test_cross_user_access_covers_every_artifact_endpoint(site):
    client, app, _ = site
    owner = register(site)
    job_id = make_job(site, owner["id"])
    register(site, "second@example.com")
    for suffix in ("", "/logs", "/report", "/repairs", "/events", "/download"):
        assert client.get(f"/api/jobs/{job_id}{suffix}").status_code == 404
    assert client.get(f"/api/inspections/{job_id}").status_code == 404
    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 404
    assert client.delete(f"/api/jobs/{job_id}").status_code == 404
    assert (
        client.post(f"/api/jobs/{job_id}/repairs", data={"plan": "{}"}).status_code
        == 404
    )
    assert client.get("/api/jobs").json()["items"] == []
    assert (
        app.state.db.one("SELECT status FROM jobs WHERE id=%s", (job_id,))["status"]
        == "queued"
    )


def test_one_invitation_cannot_be_consumed_twice(site):
    _, app, _ = site
    code = app.state.auth.invitations(1)[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(
            pool.map(
                lambda email: app.state.auth.register(email, PASSWORD, code),
                ["a@example.com", "b@example.com"],
            )
        )
    assert app.state.db.one("SELECT count(*) AS n FROM users")["n"] == 1


def test_password_reset_revokes_session_and_token_is_one_use(site):
    client, app, config = site
    row = register(site)
    assert (
        client.post("/api/auth/forgot", json={"email": row["email"]}).status_code == 202
    )
    token = (
        (config.data_dir / "dev-mail" / f"{row['id']}-reset.txt")
        .read_text()
        .split("=", 1)[1]
    )
    payload = {"token": token, "password": "new-password-long-enough"}
    assert client.post("/api/auth/reset", json=payload).status_code == 200
    assert client.get("/api/account").status_code == 401
    assert client.post("/api/auth/reset", json=payload).status_code == 400


def test_quota_concurrent_reservations_are_atomic(site):
    _, app, _ = site
    user = register(site)
    app.state.db.execute("UPDATE users SET daily_minutes=30 WHERE id=%s", (user["id"],))

    def submit(_):
        try:
            return make_job(site, user["id"], kind="convert")
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    assert sum(isinstance(r, str) for r in results) == 1
    assert app.state.queue.quota(user["id"])["reserved"] == 30
    successful = next(r for r in results if isinstance(r, str))
    app.state.queue.cancel(successful)
    assert app.state.queue.quota(user["id"])["remaining"] == 30


def test_claim_is_unique_and_enforces_user_concurrency(site):
    _, app, _ = site
    row = register(site)
    for _ in range(4):
        make_job(site, row["id"])
    with ThreadPoolExecutor(max_workers=4) as pool:
        claimed = [
            r for r in pool.map(lambda _: app.state.queue.claim(), range(4)) if r
        ]
    assert len(claimed) == 2
    assert len({r["id"] for r in claimed}) == 2
    for r in claimed:
        app.state.queue.finish(r["id"], r["lease_token"], "failed", "fixture")
        assert not app.state.queue.finish(r["id"], r["lease_token"], "success")
    assert app.state.queue.quota(row["id"])["used"] == 2


def test_account_delete_cancels_jobs_and_revokes_sessions(site):
    client, app, _ = site
    row = register(site)
    job_id = make_job(site, row["id"])
    response = client.request("DELETE", "/api/account", json={"password": PASSWORD})
    assert response.status_code == 202
    assert client.get("/api/account").status_code == 401
    assert app.state.db.one("SELECT delete_requested FROM jobs WHERE id=%s", (job_id,))[
        "delete_requested"
    ]


def test_admin_controls_and_audit(site):
    client, app, _ = site
    register(site, administrator=True)
    assert (
        len(client.post("/api/admin/invitations", json={"count": 3}).json()["codes"])
        == 3
    )
    assert (
        client.patch(
            "/api/admin/settings",
            json={"registration_open": False, "queue_open": False},
        ).status_code
        == 200
    )
    assert len(app.state.db.all("SELECT * FROM audit")) == 2
    assert client.get("/api/admin/status").json()["queue_open"] is False


def test_public_docker_has_no_host_output_or_privileges(site):
    _, app, config = site
    user = register(site)
    job_id = make_job(site, user["id"])
    row = app.state.queue.claim()
    argv = command(config, row, app.state.storage)
    assert (
        "SYS_ADMIN" not in argv
        and "--privileged" not in argv
        and "--cap-add" not in argv
    )
    assert argv[argv.index("--network") + 1] == "none"
    assert "10001:10001" in argv
    assert len([v for v in argv if v.startswith("type=bind")]) == 1
    assert argv[argv.index("--mount") + 1].endswith("/input,readonly")
    assert job_id in " ".join(argv)


def test_cross_midnight_settlement_uses_reserved_day(site):
    _, app, _ = site
    user = register(site)
    job_id = make_job(site, user["id"], kind="convert")
    row = app.state.queue.claim()
    app.state.db.execute("UPDATE usage SET day=day-1 WHERE user_id=%s", (user["id"],))
    app.state.db.execute(
        "UPDATE jobs SET quota_date=quota_date-1,started_at=now()-interval '70 seconds' WHERE id=%s",
        (job_id,),
    )
    assert app.state.queue.finish(job_id, row["lease_token"], "failed")
    yesterday = app.state.db.one("SELECT * FROM usage WHERE user_id=%s", (user["id"],))
    assert yesterday["used"] == 2 and yesterday["reserved"] == 0
    assert app.state.queue.quota(user["id"])["remaining"] == 120


def test_recover_expired_lease_with_missing_container(site, monkeypatch):
    _, app, config = site
    user = register(site)
    job_id = make_job(site, user["id"])
    row = app.state.queue.claim()
    app.state.db.execute(
        "UPDATE jobs SET lease_until=now()-interval '1 second' WHERE id=%s", (job_id,)
    )
    monkeypatch.setattr(
        "app.public.worker.docker",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, b""),
    )
    Worker(config, app.state.db).maintenance()
    assert (
        app.state.db.one("SELECT status FROM jobs WHERE id=%s", (job_id,))["status"]
        == "failed"
    )
    assert app.state.queue.heartbeat(job_id, row["lease_token"]) is None
    assert app.state.queue.quota(user["id"])["reserved"] == 0


def test_expired_files_and_metadata_are_removed(site):
    _, app, config = site
    user = register(site)
    job_id = make_job(site, user["id"])
    app.state.db.execute(
        "UPDATE jobs SET expires_at=now()-interval '1 second' WHERE id=%s", (job_id,)
    )
    Worker(config, app.state.db).maintenance()
    assert not app.state.storage.paths_for(job_id).root.exists()
    assert app.state.db.one("SELECT id FROM jobs WHERE id=%s", (job_id,)) is None
    assert app.state.queue.quota(user["id"])["reserved"] == 0


def test_disk_cutoff_and_upload_size_are_enforced(site, monkeypatch):
    client, _, _ = site
    register(site)
    monkeypatch.setattr("app.public.main.disk_percent", lambda _: 81)
    response = client.post("/api/inspect", files={"file": ("a.zip", b"1234")})
    assert response.status_code == 507
    response = client.post(
        "/api/inspect", content=b"x", headers={"Content-Length": str(1024**3)}
    )
    assert response.status_code in {413, 507}


def test_upload_admission_precedes_multipart_parsing_and_releases_on_error(site):
    client, app, _ = site
    owner = register(site)
    first = app.state.queue.create_upload(owner["id"], "one.zip")
    second = app.state.queue.create_upload(owner["id"], "two.zip")
    # Even an invalid multipart body must be rejected by admission first.
    response = client.post(
        "/api/inspect",
        content=b"broken",
        headers={"Content-Type": "multipart/form-data"},
    )
    assert response.status_code == 429
    app.state.queue.abandon_upload(first)
    app.state.queue.abandon_upload(second)
    assert client.post("/api/inspect", json={}).status_code == 422
    assert (
        app.state.db.one("SELECT count(*) AS n FROM jobs WHERE status='uploading'")["n"]
        == 0
    )


def test_sse_replays_all_terminal_log_chunks(site):
    client, app, _ = site
    user = register(site)
    job_id = make_job(site, user["id"])
    row = app.state.queue.claim()
    app.state.storage.append_log(job_id, "x" * 300000 + "TAIL_MARKER")
    app.state.queue.finish(job_id, row["lease_token"], "failed", "fixture")
    response = client.get(f"/api/jobs/{job_id}/events")
    assert "TAIL_MARKER" in response.text
    assert response.text.count("event: logs") == 2


def test_duplicate_submit_cannot_overwrite_request(site):
    client, app, _ = site
    user = register(site)
    job_id = make_job(site, user["id"], kind="convert")
    response = client.post(
        "/api/jobs",
        json={"job_id": job_id, "source_format": "hydro", "target_format": "fps"},
    )
    assert response.status_code == 409
    assert (
        app.state.db.one("SELECT request FROM jobs WHERE id=%s", (job_id,))["request"][
            "target_format"
        ]
        == "icpc"
    )


def test_repair_inherits_owner_and_immutable_input(site):
    client, app, _ = site
    user = register(site)
    job_id = make_job(site, user["id"], kind="convert")
    paths = app.state.storage.paths_for(job_id)
    paths.upload_path.write_bytes(b"test-original")
    payload = {"job_id": job_id, "source_format": "hydro", "target_format": "icpc"}
    app.state.storage.write_request(job_id, payload)
    metadata = app.state.storage.read_metadata(job_id)
    metadata.status = "failed"
    app.state.storage.write_metadata(metadata)
    paths.report_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "repair_ready": True,
                "repair_suggestions": [
                    {
                        "id": "a" * 24,
                        "expected_path": "tests/1.ans",
                        "role": "output",
                        "candidates": [
                            {"path": "tests/1.out", "strategy": "extension-alias"}
                        ],
                    }
                ],
            }
        )
    )
    claimed = app.state.queue.claim()
    app.state.queue.finish(job_id, claimed["lease_token"], "failed")
    response = client.post(
        f"/api/jobs/{job_id}/repairs",
        data={
            "plan": json.dumps(
                {
                    "selections": [
                        {"suggestion_id": "a" * 24, "candidate_path": "tests/1.out"}
                    ]
                }
            )
        },
    )
    assert response.status_code == 202, response.text
    derived = response.json()["id"]
    assert derived != job_id
    assert app.state.queue.owned(derived, user["id"])["status"] == "queued"
    assert (
        app.state.storage.paths_for(derived).upload_path.read_bytes()
        == b"test-original"
    )
    assert app.state.storage.read_metadata(derived).parent_job_id == job_id


def test_one_hundred_users_and_global_claim_limits(site):
    client, app, _ = site
    users = [uuid.uuid4().hex for _ in range(100)]
    with app.state.db.connect() as conn:
        for number, uid in enumerate(users):
            conn.execute(
                "INSERT INTO users(id,email,password_hash,verified) VALUES (%s,%s,'test-only-unused',true)",
                (uid, f"load{number}@example.com"),
            )
            conn.execute(
                "INSERT INTO sessions VALUES (%s,%s,%s,now()+interval '1 hour')",
                (digest(uid), uid, digest(uid)),
            )

    def account(uid):
        return client.get(
            "/api/account", headers={"Cookie": f"p2h_session={uid}; p2h_csrf={uid}"}
        ).status_code

    with ThreadPoolExecutor(max_workers=10) as pool:
        assert list(pool.map(account, users)) == [200] * 100
    for uid in users[:15]:
        make_job(site, uid)
    with ThreadPoolExecutor(max_workers=15) as pool:
        claims = list(pool.map(lambda _: app.state.queue.claim(), range(15)))
    assert len([row for row in claims if row]) == 10
    assert len({row["id"] for row in claims if row}) == 10


def test_wine_pool_limit(site):
    _, app, _ = site
    owners = [register(site, f"wine{i}@example.com") for i in range(3)]
    for owner in owners:
        for _ in range(2):
            make_job(site, owner["id"], kind="convert")
    app.state.db.execute("UPDATE jobs SET wine=true")
    claims = [app.state.queue.claim() for _ in range(6)]
    assert len([row for row in claims if row]) == 3


@pytest.mark.skipif(
    os.getenv("PUBLIC_TEST_DOCKER") != "1", reason="requires public runner images"
)
@pytest.mark.parametrize("attack", ["none", "symlink", "background"])
def test_real_container_output_boundary(site, attack):
    _, app, config = site
    user = register(site)
    job_id = make_job(site, user["id"])
    row = app.state.queue.claim()
    argv = command(config, row, app.state.storage)
    image_index = argv.index(config.runner_image)
    argv = argv[: image_index + 1] + [
        "-u",
        "-c",
        (
            "import os,socket,subprocess,time; from pathlib import Path; "
            "assert os.getuid()==10001; "
            "assert 'CapEff:\\t0000000000000000' in Path('/proc/self/status').read_text(); "
            "assert not Path('/var/run/docker.sock').exists(); "
            "Path('/output/ok.txt').write_text('safe-output'); "
            + (
                "Path('/output/link').symlink_to('/etc/passwd'); "
                if attack == "symlink"
                else ""
            )
            + (
                "subprocess.Popen(['python','-c',\"import time; from pathlib import Path; time.sleep(1); Path('/output/bad.txt').write_text('late')\"]); "
                if attack == "background"
                else ""
            )
            + "print('READY',flush=True); time.sleep(90)"
        ),
    ]
    process = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        assert process.stdout.readline().strip() == "READY"
        target = app.state.storage.paths_for(job_id).output_dir
        if attack == "symlink":
            with pytest.raises((ValueError, __import__("tarfile").ReadError)):
                collect_output(container_name(row), target)
        else:
            collect_output(container_name(row), target)
            assert (target / "ok.txt").read_text() == "safe-output"
            assert not (target / "bad.txt").exists()
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container_name(row)],
            check=False,
            capture_output=True,
        )
        process.wait(timeout=10)


@pytest.mark.skipif(
    os.getenv("PUBLIC_TEST_NATIVE_WINE") != "1", reason="native x86_64 acceptance test"
)
def test_native_wine_under_public_restrictions(site):
    _, app, config = site
    user = register(site)
    make_job(site, user["id"], kind="convert")
    row = app.state.queue.claim()
    row["wine"] = True
    argv = command(config, row, app.state.storage)
    image_index = argv.index(config.wine_image)
    argv[argv.index("--entrypoint") + 1] = "wine"
    argv = argv[: image_index + 1] + ["cmd", "/c", "echo", "public-wine-ok"]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stderr
        assert "public-wine-ok" in result.stdout
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container_name(row)],
            check=False,
            capture_output=True,
        )


@pytest.mark.skipif(
    os.getenv("PUBLIC_TEST_DOCKER") != "1",
    reason="requires locally built public runner",
)
def test_real_docker_inspect_and_convert(site):
    client, app, config = site
    register(site)
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("problem.yaml", "pid: P1000\ntitle: Addition\ntag: []\n")
        archive.writestr("problem.md", "# Addition\nAdd two integers.\n")
        archive.writestr("testdata/config.yaml", "time: 1s\nmemory: 256m\n")
        archive.writestr("testdata/1.in", "1 2\n")
        archive.writestr("testdata/1.out", "3\n")
    response = client.post(
        "/api/inspect",
        files={"file": ("test.zip", package.getvalue(), "application/zip")},
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    worker = Worker(config, app.state.db)
    worker.run_job(app.state.queue.claim())
    response = client.get(f"/api/inspections/{job_id}")
    assert response.status_code == 200, response.text
    assert response.json()["detected_format"] == "hydro"
    response = client.post(
        "/api/jobs",
        json={"job_id": job_id, "source_format": "hydro", "target_format": "icpc"},
    )
    assert response.status_code == 202, response.text
    worker.run_job(app.state.queue.claim())
    response = client.get(f"/api/jobs/{job_id}")
    assert response.json()["status"] == "success", client.get(
        f"/api/jobs/{job_id}/logs"
    ).text
    assert client.get(f"/api/jobs/{job_id}/download").status_code == 200
    assert client.get(f"/api/jobs/{job_id}/report").json()["target_format"] == "icpc"
