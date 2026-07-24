from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.jobs import JobManager
from app.main import app
from app.security import InMemoryRateLimiter
from app.storage import JobMetadata, Storage


ACCESS_KEY = "test-only-external-access-key"
ACCESS_SALT = bytes.fromhex("ab" * 16)
ACCESS_KEY_HASH = (
    "pbkdf2_sha256$600000$"
    f"{ACCESS_SALT.hex()}$"
    f"{hashlib.pbkdf2_hmac('sha256', ACCESS_KEY.encode(), ACCESS_SALT, 600_000).hex()}"
)


def _make_zip() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("problems/a/problem.xml", "<problem></problem>")
    return out.getvalue()


def _make_hydro_zip() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("P1000/problem.yaml", "title: Sum\n")
        archive.writestr("P1000/testdata/config.yaml", "time: 1000ms\nmemory: 256m\n")
        archive.writestr("P1000/testdata/1.in", "1 2\n")
        archive.writestr("P1000/testdata/1.out", "3\n")
    return out.getvalue()


def _make_polygon_zip() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("contest.xml", "<contest/>")
        archive.writestr("problems/sum/problem.xml", "<problem/>")
    return out.getvalue()


def _make_probhub_workspace_zip() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            ".probhub/workspace.yaml",
            "schema_version: 1\nproblems:\n  - id: L01\n    directory: L01\n",
        )
        archive.writestr(
            "L01/probhub.yaml",
            "schema_version: 1\nid: L01\nname: Sum\n",
        )
        archive.writestr("L01/data/sample/1.in", "1 2\n")
        archive.writestr("L01/data/sample/1.ans", "3\n")
    return out.getvalue()


def _make_probhub_export_zip() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("problem.yaml", "name: Sum\nlimits:\n  memory: 256\n")
        archive.writestr("domjudge-problem.ini", "timelimit='1'\n")
        archive.writestr("problem.pdf", b"%PDF-1.4\n%%EOF\n")
        archive.writestr("data/sample/1.in", "1 2\n")
        archive.writestr("data/sample/1.ans", "3\n")
        archive.writestr("data/secret/1.in", "4 5\n")
        archive.writestr("data/secret/1.ans", "9\n")
    return out.getvalue()


def _make_probhub_legacy_zip() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "legacy/meta.json",
            json.dumps(
                {
                    "problem": {
                        "display_name": "Legacy Sum",
                        "format": "markdown",
                    },
                    "statement": {
                        "description": "Add two integers.",
                        "input": "Two integers.",
                        "output": "Their sum.",
                    },
                }
            ),
        )
        archive.writestr("legacy/problem.zh.md", "## 题目描述\n\n计算和。\n")
        archive.writestr("legacy/data/sample/1.in", "1 2\n")
        archive.writestr("legacy/data/sample/1.ans", "3\n")
        archive.writestr("legacy/data/secret/2.in", "4 5\n")
        archive.writestr("legacy/data/secret/2.ans", "9\n")
        archive.writestr("legacy/std.cpp", "int main(){return 0;}\n")
        archive.writestr("legacy/validator.cpp", "int main(){return 0;}\n")
    return out.getvalue()


def _make_archive(files: dict[str, str | bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return out.getvalue()


def _make_nested_icpc_zip() -> bytes:
    def problem(code: str) -> bytes:
        return _make_archive(
            {
                "problem.yaml": f"name: {code}\n",
                "domjudge-problem.ini": f"short-name = {code}\n",
                "problem_statement/problem.en.pdf": b"%PDF-1.4\n%%EOF\n",
                "data/secret/1.in": "1\n",
                "data/secret/1.ans": "1\n",
            }
        )

    return _make_archive({"A.zip": problem("A"), "B.zip": problem("B")})


def _hoj_document(problem_id: str, title: str) -> str:
    return json.dumps(
        {"problem": {"problemId": problem_id, "title": title}},
        ensure_ascii=False,
    )


def _make_hoj_problem_zip(problem_id: str, title: str, value: str) -> bytes:
    stem = f"problem_{problem_id}"
    return _make_archive(
        {
            f"{stem}.json": _hoj_document(problem_id, title),
            f"{stem}/1.in": f"{value}\n",
            f"{stem}/1.out": f"{value}\n",
        }
    )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        docker_bin="docker",
        runner_image="p2h-runner",
        max_upload_bytes=1024 * 1024,
        job_timeout_seconds=10,
        job_ttl_seconds=3600,
        docker_memory="1g",
        docker_cpus="2",
        docker_pids_limit=1024,
        docker_wine_pids_limit=4096,
        docker_wine_home_size="4g",
        docker_tmp_size="512m",
        docker_work_size="1g",
    )


def _client(settings: Settings) -> TestClient:
    app.dependency_overrides.clear()
    app.state.settings = settings
    app.state.storage = Storage(settings)
    app.state.job_manager = JobManager(settings, app.state.storage)
    app.state.rate_limiter = InMemoryRateLimiter()
    return TestClient(app)


def _external_settings(tmp_path: Path, **changes: object) -> Settings:
    values: dict[str, object] = {
        "deployment_mode": "external",
        "allowed_hosts": ("converter.example.com",),
        "allowed_origins": ("http://converter.example.com:11452",),
        "access_key_hash": ACCESS_KEY_HASH,
        "rate_limit_requests_per_minute": 20,
        "rate_limit_uploads_per_minute": 5,
    }
    values.update(changes)
    return replace(_settings(tmp_path), **values)


def _access_headers(**extra: str) -> dict[str, str]:
    return {
        "host": "converter.example.com",
        "x-p2h-access-key": ACCESS_KEY,
        **extra,
    }


def test_inspect_accepts_zip_and_rejects_non_zip(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings)
    ok = client.post(
        "/api/inspect", files={"file": ("contest.zip", _make_zip(), "application/zip")}
    )
    assert ok.status_code == 200
    assert ok.json()["filename"] == "contest.zip"
    paths = app.state.storage.paths_for(ok.json()["job_id"])
    assert stat.S_IMODE(paths.root.stat().st_mode) == 0o755
    assert stat.S_IMODE(paths.input_dir.stat().st_mode) == 0o755
    assert stat.S_IMODE(paths.upload_path.stat().st_mode) == 0o644
    assert stat.S_IMODE(paths.work_dir.stat().st_mode) == 0o777
    assert stat.S_IMODE(paths.output_dir.stat().st_mode) == 0o777

    bad_ext = client.post(
        "/api/inspect", files={"file": ("contest.txt", b"x", "text/plain")}
    )
    assert bad_ext.status_code == 400

    bad_zip = client.post(
        "/api/inspect", files={"file": ("contest.zip", b"x", "application/zip")}
    )
    assert bad_zip.status_code == 400


def test_inspect_reports_unique_format_candidate(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))

    response = client.post(
        "/api/inspect",
        files={"file": ("hydro.zip", _make_hydro_zip(), "application/zip")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["detected_format"] == "hydro"
    assert payload["format_candidates"][0] == {
        "format": "hydro",
        "confidence": 0.98,
        "evidence": ["problem.yaml", "testdata/config.yaml"],
    }


def test_polygon_problem_xml_is_not_misdetected_as_fps(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))

    response = client.post(
        "/api/inspect",
        files={"file": ("polygon.zip", _make_polygon_zip(), "application/zip")},
    )

    assert response.status_code == 200
    assert response.json()["detected_format"] == "polygon"
    assert [item["format"] for item in response.json()["format_candidates"]] == [
        "polygon"
    ]


@pytest.mark.parametrize(
    ("filename", "archive", "evidence"),
    [
        (
            "workspace.zip",
            _make_probhub_workspace_zip(),
            [".probhub/workspace.yaml", "probhub.yaml"],
        ),
        (
            "legacy.zip",
            _make_probhub_legacy_zip(),
            [
                "meta.json",
                "data/sample and data/secret",
                "legacy statement and sources",
            ],
        ),
    ],
)
def test_inspect_detects_probhub_workspace_and_legacy(
    tmp_path: Path, filename: str, archive: bytes, evidence: list[str]
) -> None:
    client = _client(_settings(tmp_path))

    response = client.post(
        "/api/inspect",
        files={"file": (filename, archive, "application/zip")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["detected_format"] == "probhub"
    assert payload["format_candidates"][0] == {
        "format": "probhub",
        "confidence": {
            "workspace.zip": 0.995,
            "legacy.zip": 0.97,
        }[filename],
        "evidence": evidence,
    }


@pytest.mark.parametrize("with_samples", [True, False])
def test_inspect_keeps_root_pdf_icpc_and_probhub_candidates(
    tmp_path: Path,
    with_samples: bool,
) -> None:
    client = _client(_settings(tmp_path))
    files: dict[str, str | bytes] = {
        "problem.yaml": "name: Sum\n",
        "domjudge-problem.ini": "timelimit='1'\n",
        "problem.pdf": b"%PDF-1.4\n%%EOF\n",
        "data/secret/1.in": "1\n",
        "data/secret/1.ans": "1\n",
    }
    if with_samples:
        files.update(
            {
                "data/sample/1.in": "1\n",
                "data/sample/1.ans": "1\n",
            }
        )

    response = client.post(
        "/api/inspect",
        files={"file": ("root-pdf.zip", _make_archive(files), "application/zip")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["detected_format"] is None
    assert [candidate["format"] for candidate in payload["format_candidates"][:2]] == [
        "icpc",
        "probhub",
    ]
    assert all(
        candidate["confidence"] == 0.98
        for candidate in payload["format_candidates"][:2]
    )
    assert payload["package_scope"] == "single"
    assert payload["package_layout"] == "directory"
    assert payload["problem_count"] == 1
    assert payload["problems"] == [{"id": "root-pdf", "path": "."}]
    assert set(payload["supported_targets"]) == {
        "hydro",
        "icpc",
        "hoj",
        "fps",
        "qduoj",
        "uoj",
        "dmoj",
    }


@pytest.mark.parametrize(
    (
        "filename",
        "archive",
        "expected_format",
        "expected_scope",
        "expected_layout",
        "expected_count",
        "expected_ids",
    ),
    [
        (
            "single-polygon.zip",
            _make_archive(
                {
                    "problem.xml": "<problem/>",
                    "tests/1": "1\n",
                    "tests/1.a": "1\n",
                }
            ),
            "polygon",
            "single",
            "directory",
            1,
            ["single-polygon"],
        ),
        (
            "contest.zip",
            _make_archive(
                {
                    "contest.xml": "<contest/>",
                    "problems/a/problem.xml": "<problem/>",
                    "problems/b/problem.xml": "<problem/>",
                }
            ),
            "polygon",
            "multi",
            "contest",
            2,
            ["a", "b"],
        ),
        (
            "probhub-workspace.zip",
            _make_archive(
                {
                    ".probhub/workspace.yaml": (
                        "schema_version: 1\nproblems:\n"
                        "  - id: A\n    directory: tasks/one\n"
                        "  - id: B\n    directory: tasks/two\n"
                    ),
                    "tasks/one/probhub.yaml": "schema_version: 1\nid: A\n",
                    "tasks/two/probhub.yaml": "schema_version: 1\nid: B\n",
                    "tasks/one/data/sample/1.in": "1\n",
                    "tasks/one/data/sample/1.ans": "1\n",
                    "tasks/two/data/sample/1.in": "2\n",
                    "tasks/two/data/sample/1.ans": "2\n",
                }
            ),
            "probhub",
            "multi",
            "workspace",
            2,
            ["A", "B"],
        ),
        (
            "hydro.zip",
            _make_archive(
                {
                    "directories/one/problem.yaml": "title: A\npid: P1000\n",
                    "directories/one/testdata/config.yaml": "cases: []\n",
                    "directories/two/problem.yaml": "title: B\npid: P1001\n",
                    "directories/two/testdata/config.yaml": "cases: []\n",
                }
            ),
            "hydro",
            "multi",
            "directory",
            2,
            ["P1000", "P1001"],
        ),
        (
            "icpc-nested.zip",
            _make_nested_icpc_zip(),
            "icpc",
            "multi",
            "nested",
            2,
            ["A", "B"],
        ),
        (
            "hoj.zip",
            _make_archive(
                {
                    "problem_1.json": '{"problem": {"problemId": 1001}}',
                    "problem_1/1.in": "1\n",
                    "problem_1/1.out": "1\n",
                    "problem_2.json": '{"problem": {"problemId": 1002}}',
                    "problem_2/1.in": "2\n",
                    "problem_2/1.out": "2\n",
                }
            ),
            "hoj",
            "multi",
            "directory",
            2,
            ["1001", "1002"],
        ),
        (
            "fps.zip",
            _make_archive(
                {
                    "problem.xml": (
                        "<fps version='1.6'>"
                        "<item><remote_id>A</remote_id><title>One</title></item>"
                        "<item><remote_id>B</remote_id><title>Two</title></item>"
                        "</fps>"
                    )
                }
            ),
            "fps",
            "multi",
            "xml",
            2,
            ["A", "B"],
        ),
        (
            "qduoj.zip",
            _make_archive(
                {
                    "1/problem.json": '{"display_id": "Q-A"}',
                    "1/testcase/1.in": "1\n",
                    "1/testcase/1.out": "1\n",
                    "2/problem.json": '{"display_id": "Q-B"}',
                    "2/testcase/1.in": "2\n",
                    "2/testcase/1.out": "2\n",
                }
            ),
            "qduoj",
            "multi",
            "directory",
            2,
            ["Q-A", "Q-B"],
        ),
        (
            "uoj.zip",
            _make_archive(
                {
                    "a/problem.conf": "n_tests 1\n",
                    "b/problem.conf": "n_tests 1\n",
                }
            ),
            "uoj",
            "multi",
            "directory",
            2,
            ["a", "b"],
        ),
        (
            "dmoj.zip",
            _make_archive(
                {
                    "a/init.yml": "test_cases: []\n",
                    "b/init.yml": "test_cases: []\n",
                }
            ),
            "dmoj",
            "multi",
            "directory",
            2,
            ["a", "b"],
        ),
        (
            "generic.zip",
            _make_archive(
                {
                    "a/1.in": "1\n",
                    "a/1.out": "1\n",
                    "b/1.in": "2\n",
                    "b/1.ans": "2\n",
                }
            ),
            None,
            "multi",
            "directory",
            2,
            ["a", "b"],
        ),
    ],
)
def test_inspect_classifies_platform_package_scope_and_layout(
    tmp_path: Path,
    filename: str,
    archive: bytes,
    expected_format: str | None,
    expected_scope: str,
    expected_layout: str,
    expected_count: int,
    expected_ids: list[str],
) -> None:
    client = _client(_settings(tmp_path))

    response = client.post(
        "/api/inspect",
        files={"file": (filename, archive, "application/zip")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["detected_format"] == expected_format
    assert payload["package_scope"] == expected_scope
    assert payload["package_layout"] == expected_layout
    assert payload["problem_count"] == expected_count
    assert [problem["id"] for problem in payload["problems"]] == expected_ids
    assert payload["problems_truncated"] is False
    if filename == "icpc-nested.zip":
        assert [problem["path"] for problem in payload["problems"]] == [
            "A.zip",
            "B.zip",
        ]
    expected_targets = {"hydro", "icpc", "hoj", "fps", "qduoj", "uoj", "dmoj"}
    if expected_format in expected_targets:
        expected_targets.remove(expected_format)
    assert set(payload["supported_targets"]) == expected_targets


def test_inspect_merges_expanded_and_nested_hoj_problems(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))
    first_document = _hoj_document("P1001", "First")
    archive = _make_archive(
        {
            "export/A/problem_P1001.json": first_document,
            "export/A/problem_P1001/1.in": "1\n",
            "export/A/problem_P1001/1.out": "1\n",
            "export/A.zip": _make_hoj_problem_zip("P1001", "First", "1"),
            "export/B.zip": _make_hoj_problem_zip("P1002", "Second", "2"),
        }
    )

    response = client.post(
        "/api/inspect",
        files={"file": ("hoj-contest.zip", archive, "application/zip")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["detected_format"] == "hoj"
    assert payload["package_scope"] == "multi"
    assert payload["package_layout"] == "nested"
    assert payload["problem_count"] == 2
    assert payload["problems"] == [
        {"id": "P1001", "path": "export/A/problem_P1001"},
        {"id": "P1002", "path": "export/B.zip!/problem_P1002"},
    ]


def test_inspect_does_not_merge_conflicting_hoj_problem_ids(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))
    archive = _make_archive(
        {
            "export/A/problem_P1001.json": _hoj_document("P1001", "Original"),
            "export/A/problem_P1001/1.in": "1\n",
            "export/A/problem_P1001/1.out": "1\n",
            "export/A.zip": _make_hoj_problem_zip("P1001", "Conflicting", "1"),
            "export/B.zip": _make_hoj_problem_zip("P1002", "Second", "2"),
        }
    )

    response = client.post(
        "/api/inspect",
        files={"file": ("conflicting-hoj.zip", archive, "application/zip")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["detected_format"] == "hoj"
    assert payload["package_scope"] == "single"
    assert payload["package_layout"] == "directory"
    assert payload["problem_count"] == 1
    assert payload["problems"] == [{"id": "P1001", "path": "export/A/problem_P1001"}]


def test_external_mode_requires_access_key_and_valid_host(tmp_path: Path) -> None:
    client = _client(_external_settings(tmp_path))

    live = client.get("/api/health/live", headers={"host": "converter.example.com"})
    missing = client.get("/api/health", headers={"host": "converter.example.com"})
    wrong = client.get(
        "/api/health",
        headers={
            "host": "converter.example.com",
            "x-p2h-access-key": "wrong-key",
        },
    )
    bad_host = client.get(
        "/api/health",
        headers={
            "host": "attacker.example",
            "x-p2h-access-key": ACCESS_KEY,
        },
    )
    authenticated = client.get("/api/health", headers=_access_headers())

    assert live.status_code == 200
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert wrong.json()["detail"] == "Invalid or missing access key"
    assert bad_host.status_code == 400
    assert authenticated.status_code == 200
    assert authenticated.headers["cache-control"] == "no-store"
    assert authenticated.headers["x-content-type-options"] == "nosniff"
    assert authenticated.headers["x-frame-options"] == "DENY"
    assert "strict-transport-security" not in authenticated.headers
    assert authenticated.headers["cross-origin-resource-policy"] == "same-site"
    assert authenticated.headers["content-security-policy"].startswith(
        "default-src 'none'"
    )
    assert len(authenticated.headers["x-request-id"]) == 32


@pytest.mark.parametrize(
    "path",
    [
        "/api/health/ready",
        "/metrics",
        f"/api/jobs/{'a' * 32}/events",
        f"/api/jobs/{'a' * 32}/download",
    ],
)
def test_external_mode_protects_non_live_endpoints(tmp_path: Path, path: str) -> None:
    client = _client(_external_settings(tmp_path))

    assert (
        client.get(path, headers={"host": "converter.example.com"}).status_code == 401
    )


def test_external_mode_allows_authenticated_cors_preflight(tmp_path: Path) -> None:
    # The module-level CORS middleware is initialized with the test process's
    # default origin; app.state settings can still exercise external auth here.
    origin = "http://localhost:11452"
    client = _client(_external_settings(tmp_path, allowed_origins=(origin,)))

    response = client.options(
        "/api/inspect",
        headers={
            "host": "converter.example.com",
            "origin": origin,
            "access-control-request-method": "POST",
            "access-control-request-headers": "x-p2h-access-key",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert "X-P2H-Access-Key" in response.headers["access-control-allow-headers"]


def test_external_mode_exposes_authentication_errors_to_allowed_origins(
    tmp_path: Path,
) -> None:
    origin = "http://localhost:11452"
    client = _client(_external_settings(tmp_path, allowed_origins=(origin,)))

    response = client.get(
        "/api/health",
        headers={"host": "converter.example.com", "origin": origin},
    )

    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == origin
    assert response.json() == {"detail": "Invalid or missing access key"}


def test_external_mode_blocks_cross_site_mutations(tmp_path: Path) -> None:
    client = _client(_external_settings(tmp_path))
    headers = _access_headers(
        origin="http://evil.example:11452",
        **{"sec-fetch-site": "cross-site"},
    )

    response = client.post(
        "/api/jobs",
        headers=headers,
        json={
            "job_id": "a" * 32,
            "source_format": "hydro",
            "target_format": "icpc",
        },
    )

    assert response.status_code == 403
    assert "Cross-site" in response.json()["detail"]


def test_external_mode_rejects_malformed_referer_without_server_error(
    tmp_path: Path,
) -> None:
    client = _client(_external_settings(tmp_path))

    response = client.post(
        "/api/jobs",
        headers=_access_headers(referer="http://[invalid"),
        json={
            "job_id": "a" * 32,
            "source_format": "hydro",
            "target_format": "icpc",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Referer origin is not allowed"


def test_external_mode_rate_limit_is_enforced(tmp_path: Path) -> None:
    settings = _external_settings(tmp_path, rate_limit_requests_per_minute=2)
    client = _client(settings)
    headers = _access_headers()

    first = client.get("/api/jobs/not-a-job", headers=headers)
    second = client.get("/api/jobs/not-a-job", headers=headers)
    limited = client.get("/api/jobs/not-a-job", headers=headers)

    assert first.status_code == 404
    assert second.status_code == 404
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) >= 1


def test_external_mode_limits_auth_failures_without_locking_out_valid_key(
    tmp_path: Path,
) -> None:
    settings = _external_settings(tmp_path, rate_limit_auth_failures_per_minute=2)
    client = _client(settings)
    invalid_headers = {
        "host": "converter.example.com",
        "x-p2h-access-key": "wrong-key",
    }

    assert client.get("/api/health", headers=invalid_headers).status_code == 401
    assert client.get("/api/health", headers=invalid_headers).status_code == 401
    limited = client.get("/api/health", headers=invalid_headers)

    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) >= 1
    assert client.get("/api/health", headers=_access_headers()).status_code == 200


def test_request_body_limit_rejects_before_upload_parsing(tmp_path: Path) -> None:
    settings = _external_settings(tmp_path, max_request_body_bytes=64)
    client = _client(settings)

    response = client.post(
        "/api/inspect",
        headers=_access_headers(origin="http://converter.example.com:11452"),
        files={"file": ("large.zip", b"x" * 128, "application/zip")},
    )

    assert response.status_code == 413
    assert "64-byte limit" in response.json()["detail"]


@pytest.mark.parametrize(
    ("member_names", "expected"),
    [
        (["../escape.in", "escape.out"], "unsafe zip member path"),
        (["data/1.in", "data\\1.in"], "duplicate zip member path"),
    ],
)
def test_inspect_rejects_unsafe_member_names(
    tmp_path: Path, member_names: list[str], expected: str
) -> None:
    client = _client(_settings(tmp_path))
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        for index, name in enumerate(member_names):
            archive.writestr(name, f"{index}\n")

    response = client.post(
        "/api/inspect",
        files={"file": ("unsafe.zip", payload.getvalue(), "application/zip")},
    )

    assert response.status_code == 400
    assert expected in response.json()["detail"]
    assert list((_settings(tmp_path).data_dir / "jobs").iterdir()) == []


def test_inspect_allows_small_high_compression_ratio_testdata(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))
    payload = io.BytesIO()
    content = (b"1000000000\n" * 1_000_002)[:11_000_017]
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("problem_1292/18.in", content)

    response = client.post(
        "/api/inspect",
        files={"file": ("testdata.zip", payload.getvalue(), "application/zip")},
    )

    assert response.status_code == 200


def test_inspect_rejects_large_high_compression_ratio(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("huge.txt", b"\0" * (32 * 1024 * 1024))

    response = client.post(
        "/api/inspect",
        files={"file": ("bomb.zip", payload.getvalue(), "application/zip")},
    )

    assert response.status_code == 400
    assert "compression ratio limit" in response.json()["detail"]


def test_job_request_rejects_legacy_and_matrix_fields_together(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))

    response = client.post(
        "/api/jobs",
        json={
            "job_id": "c" * 32,
            "target": "hydro",
            "source_format": "fps",
            "target_format": "hydro",
        },
    )

    assert response.status_code == 422

    loss_policy_response = client.post(
        "/api/jobs",
        json={
            "job_id": "c" * 32,
            "target": "hydro",
            "loss_policy": "error",
        },
    )
    assert loss_policy_response.status_code == 422


def test_auto_detected_source_cannot_equal_target(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))
    inspected = client.post(
        "/api/inspect",
        files={"file": ("hydro.zip", _make_hydro_zip(), "application/zip")},
    )

    response = client.post(
        "/api/jobs",
        json={
            "job_id": inspected.json()["job_id"],
            "source_format": "auto",
            "target_format": "hydro",
        },
    )

    assert response.status_code == 422
    assert "must differ" in response.json()["detail"]


def test_report_endpoint_returns_stored_conversion_report(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))
    job_id = "d" * 32
    app.state.storage.write_metadata(
        JobMetadata(
            job_id, "fps.zip", 1, "success", datetime.now(timezone.utc).isoformat()
        )
    )
    report = {
        "schema_version": 1,
        "source_format": "fps",
        "target_format": "hydro",
        "counts": {"warning": 1, "loss": 0, "fatal": 0},
        "issues": [],
        "problems": [],
    }
    paths = app.state.storage.paths_for(job_id)
    paths.report_path.write_text(json.dumps(report), encoding="utf-8")

    response = client.get(f"/api/jobs/{job_id}/report")

    assert response.status_code == 200
    assert response.json() == report
    status_response = client.get(f"/api/jobs/{job_id}")
    assert status_response.json()["report_ready"] is True
    assert status_response.json()["report_counts"] == {
        "warning": 1,
        "loss": 0,
        "fatal": 0,
    }


def test_terminal_job_event_stream_sends_job_logs_and_report(
    tmp_path: Path,
) -> None:
    client = _client(_settings(tmp_path))
    job_id = "e" * 32
    app.state.storage.write_metadata(
        JobMetadata(
            job_id,
            "fps.zip",
            1,
            "success",
            datetime.now(timezone.utc).isoformat(),
            finished_at=datetime.now(timezone.utc).isoformat(),
            source_format="fps",
            target_format="hydro",
        )
    )
    paths = app.state.storage.paths_for(job_id)
    paths.logs_path.write_text("conversion complete\n", encoding="utf-8")
    paths.result_path.write_bytes(b"zip")
    report = {
        "schema_version": 1,
        "source_format": "fps",
        "target_format": "hydro",
        "problem_count": 1,
        "counts": {"warning": 0, "loss": 0, "fatal": 0},
        "issues": [],
        "artifacts": ["P1000.zip"],
    }
    paths.report_path.write_text(json.dumps(report), encoding="utf-8")

    response = client.get(f"/api/jobs/{job_id}/events")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    assert "retry: 2000" in response.text
    assert "event: job" in response.text
    assert '"status":"success"' in response.text
    assert "event: logs" in response.text
    assert "conversion complete" in response.text
    assert "event: report" in response.text
    assert '"problem_count":1' in response.text


def test_job_event_stream_rejects_unknown_job(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))

    response = client.get(f"/api/jobs/{'f' * 32}/events")

    assert response.status_code == 404


def test_job_event_stream_resumes_logs_from_cursor(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))
    job_id = "7" * 32
    app.state.storage.write_metadata(
        JobMetadata(
            job_id,
            "fps.zip",
            1,
            "success",
            datetime.now(timezone.utc).isoformat(),
            finished_at=datetime.now(timezone.utc).isoformat(),
            source_format="fps",
            target_format="hydro",
        )
    )
    paths = app.state.storage.paths_for(job_id)
    paths.logs_path.write_text("first\nsecond\n", encoding="utf-8")

    response = client.get(
        f"/api/jobs/{job_id}/events?cursor=0:6",
        headers={"Last-Event-ID": "0:0"},
    )

    assert response.status_code == 200
    assert '"text":"second\\n"' in response.text
    assert '"offset":6' in response.text
    assert '"next_offset":13' in response.text
    assert '"text":"first\\nsecond\\n"' not in response.text


def test_metrics_endpoint_exposes_bounded_runtime_metrics(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))
    client.get("/api/health")
    client.get("/not-a-real-route-one")
    client.get("/not-a-real-route-two")

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert "p2h_http_requests_total" in response.text
    assert 'route="/api/health"' in response.text
    assert 'route="__unmatched__"' in response.text
    assert "not-a-real-route" not in response.text
    assert "p2h_jobs_active 0" in response.text
    assert "p2h_storage_bytes" in response.text


def test_terminal_event_stream_drains_large_logs_in_chunks(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))
    job_id = "8" * 32
    app.state.storage.write_metadata(
        JobMetadata(
            job_id,
            "large.zip",
            1,
            "success",
            datetime.now(timezone.utc).isoformat(),
            finished_at=datetime.now(timezone.utc).isoformat(),
            source_format="hydro",
            target_format="icpc",
        )
    )
    paths = app.state.storage.paths_for(job_id)
    paths.logs_path.write_text("a" * (300 * 1024), encoding="utf-8")

    response = client.get(f"/api/jobs/{job_id}/events")

    assert response.status_code == 200
    assert response.text.count("event: logs") == 2
    assert f'"next_offset":{300 * 1024}' in response.text


def test_inspect_enforces_stored_job_and_total_storage_limits(tmp_path: Path) -> None:
    one_job_settings = replace(_settings(tmp_path), max_stored_jobs=1)
    client = _client(one_job_settings)
    first = client.post(
        "/api/inspect", files={"file": ("first.zip", _make_zip(), "application/zip")}
    )
    assert first.status_code == 200
    second = client.post(
        "/api/inspect", files={"file": ("second.zip", _make_zip(), "application/zip")}
    )
    assert second.status_code == 507

    storage_settings = replace(
        _settings(tmp_path / "bytes"),
        max_storage_bytes=len(_make_zip()) - 1,
    )
    storage_client = _client(storage_settings)
    too_large = storage_client.post(
        "/api/inspect",
        files={"file": ("large.zip", _make_zip(), "application/zip")},
    )
    assert too_large.status_code == 507
    assert list((storage_settings.data_dir / "jobs").iterdir()) == []


def test_api_request_removes_expired_job_before_serving_it(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), job_ttl_seconds=60)
    client = _client(settings)
    job_id = "a" * 32
    app.state.storage.write_metadata(
        JobMetadata(
            job_id,
            "contest.zip",
            1,
            "queued",
            (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat(),
        )
    )

    response = client.get(f"/api/jobs/{job_id}")

    assert response.status_code == 404
    assert not app.state.storage.paths_for(job_id).root.exists()


def test_job_request_rejects_oversized_or_control_character_arguments(
    tmp_path: Path,
) -> None:
    client = _client(_settings(tmp_path))
    job_id = "b" * 32

    too_many = client.post(
        "/api/jobs",
        json={"job_id": job_id, "tags": [f"tag-{index}" for index in range(101)]},
    )
    control_character = client.post(
        "/api/jobs",
        json={"job_id": job_id, "only": ["safe\nforged-log-line"]},
    )
    oversized = client.post(
        "/api/jobs",
        json={"job_id": job_id, "tags": ["x" * 257]},
    )

    assert too_many.status_code == 422
    assert control_character.status_code == 422
    assert oversized.status_code == 422


def test_confirmed_repair_creates_immutable_derived_job_and_cleans_finalize_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(_settings(tmp_path))
    storage = app.state.storage
    parent_id = "c" * 32
    parent_paths = storage.paths_for(parent_id)
    parent_paths.input_dir.mkdir(parents=True)
    parent_paths.work_dir.mkdir()
    parent_paths.output_dir.mkdir()
    parent_paths.logs_path.write_text("", encoding="utf-8")
    original = _make_hydro_zip()
    parent_paths.upload_path.write_bytes(original)
    storage.write_metadata(
        JobMetadata(
            id=parent_id,
            filename="hydro.zip",
            size=len(original),
            status="failed",
            created_at=datetime.now(timezone.utc).isoformat(),
            detected_format="hydro",
            source_format="hydro",
            target_format="hoj",
        )
    )
    storage.write_request(
        parent_id,
        {
            "job_id": parent_id,
            "source_format": "hydro",
            "target_format": "hoj",
            "loss_policy": "warn",
            "only": [],
            "options": {},
        },
    )
    suggestion_id = "b" * 24
    report = {
        "schema_version": 2,
        "source_format": "hydro",
        "target_format": "hoj",
        "problem_count": 1,
        "counts": {"warning": 0, "loss": 0, "fatal": 1},
        "issues": [
            {
                "severity": "fatal",
                "code": "hydro-missing-test-output",
                "message": "missing output",
                "problem": "P1000",
                "field": "cases",
                "context": {
                    "expected_path": "P1000/testdata/1.ans",
                    "role": "output",
                },
            }
        ],
        "artifacts": [],
        "repair_ready": True,
        "repair_suggestions": [
            {
                "id": suggestion_id,
                "issue_code": "hydro-missing-test-output",
                "expected_path": "P1000/testdata/1.ans",
                "role": "output",
                "problem": "P1000",
                "candidates": [
                    {
                        "path": "P1000/testdata/1.out",
                        "strategy": "extension-alias",
                        "confidence": 0.95,
                    }
                ],
                "requires_upload": False,
            }
        ],
        "applied_repairs": [],
        "source_semantic_digest": None,
    }
    parent_paths.report_path.write_text(json.dumps(report), encoding="utf-8")
    manager = app.state.job_manager
    monkeypatch.setattr(
        manager, "start", lambda request: manager.response(request.job_id)
    )

    response = client.post(
        f"/api/jobs/{parent_id}/repairs",
        data={
            "plan": json.dumps(
                {
                    "selections": [
                        {
                            "suggestion_id": suggestion_id,
                            "candidate_path": "P1000/testdata/1.out",
                        }
                    ]
                }
            )
        },
    )

    assert response.status_code == 200
    derived_id = response.json()["id"]
    assert derived_id != parent_id
    derived_paths = storage.paths_for(derived_id)
    assert parent_paths.upload_path.read_bytes() == original
    assert derived_paths.upload_path.read_bytes() == original
    plan = json.loads(derived_paths.repair_plan_path.read_text())
    assert plan["parent_job_id"] == parent_id
    assert plan["selections"][0]["expected_path"] == "P1000/testdata/1.ans"
    assert storage.read_metadata(derived_id).parent_job_id == parent_id

    storage.delete_job(derived_id)

    def fail_read_request(_job_id: str) -> dict[str, object]:
        raise RuntimeError("simulated stored request failure")

    monkeypatch.setattr(storage, "read_request", fail_read_request)
    with pytest.raises(RuntimeError, match="simulated stored request failure"):
        client.post(
            f"/api/jobs/{parent_id}/repairs",
            data={
                "plan": json.dumps(
                    {
                        "selections": [
                            {
                                "suggestion_id": suggestion_id,
                                "candidate_path": "P1000/testdata/1.out",
                            }
                        ]
                    }
                )
            },
        )
    assert storage.job_ids() == [parent_id]


def test_repair_rejects_unoffered_candidate(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))
    storage = app.state.storage
    job_id = "d" * 32
    paths = storage.paths_for(job_id)
    paths.input_dir.mkdir(parents=True)
    paths.work_dir.mkdir()
    paths.output_dir.mkdir()
    paths.logs_path.write_text("", encoding="utf-8")
    paths.upload_path.write_bytes(_make_hydro_zip())
    storage.write_metadata(
        JobMetadata(
            id=job_id,
            filename="hydro.zip",
            size=1,
            status="failed",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
    )
    paths.report_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "repair_ready": True,
                "repair_suggestions": [
                    {
                        "id": "e" * 24,
                        "expected_path": "1.ans",
                        "role": "output",
                        "candidates": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    response = client.post(
        f"/api/jobs/{job_id}/repairs",
        data={
            "plan": json.dumps(
                {
                    "selections": [
                        {
                            "suggestion_id": "e" * 24,
                            "candidate_path": "../escape",
                        }
                    ]
                }
            )
        },
    )

    assert response.status_code == 422


def test_cancel_endpoint_keeps_diagnostics_and_removes_partial_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(_settings(tmp_path))
    storage = app.state.storage
    job_id = "f" * 32
    paths = storage.paths_for(job_id)
    paths.input_dir.mkdir(parents=True)
    paths.work_dir.mkdir()
    paths.output_dir.mkdir()
    paths.logs_path.write_text("conversion started\n", encoding="utf-8")
    paths.result_path.write_bytes(b"partial")
    storage.write_metadata(
        JobMetadata(
            id=job_id,
            filename="hydro.zip",
            size=1,
            status="running",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
    )
    monkeypatch.setattr("app.jobs.stop_container", lambda *_args: None)

    response = client.post(f"/api/jobs/{job_id}/cancel")

    assert response.status_code == 200
    assert response.json() == {
        "id": job_id,
        "status": "cancelled",
        "deleted": False,
    }
    assert paths.root.is_dir()
    assert paths.logs_path.read_text(encoding="utf-8") == "conversion started\n"
    assert not paths.result_path.exists()
    assert storage.read_metadata(job_id).status == "cancelled"
