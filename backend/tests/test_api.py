from __future__ import annotations

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


PROXY_SECRET = "test-only-" * 4


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


def _production_settings(tmp_path: Path, **changes: object) -> Settings:
    values: dict[str, object] = {
        "deployment_mode": "production",
        "allowed_hosts": ("converter.example.com",),
        "allowed_origins": ("https://converter.example.com",),
        "trusted_proxy_secret": PROXY_SECRET,
        "rate_limit_requests_per_minute": 20,
        "rate_limit_uploads_per_minute": 5,
    }
    values.update(changes)
    return replace(_settings(tmp_path), **values)


def _proxy_headers(**extra: str) -> dict[str, str]:
    return {
        "host": "converter.example.com",
        "x-p2h-proxy-secret": PROXY_SECRET,
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


def test_production_requires_trusted_proxy_and_valid_host(tmp_path: Path) -> None:
    client = _client(_production_settings(tmp_path))

    live = client.get("/api/health/live", headers={"host": "converter.example.com"})
    bypass = client.get("/api/health", headers={"host": "converter.example.com"})
    bad_host = client.get(
        "/api/health",
        headers={
            "host": "attacker.example",
            "x-p2h-proxy-secret": PROXY_SECRET,
        },
    )
    trusted = client.get("/api/health", headers=_proxy_headers())

    assert live.status_code == 200
    assert bypass.status_code == 403
    assert bad_host.status_code == 400
    assert trusted.status_code == 200
    assert trusted.headers["cache-control"] == "no-store"
    assert trusted.headers["x-content-type-options"] == "nosniff"
    assert trusted.headers["x-frame-options"] == "DENY"
    assert trusted.headers["strict-transport-security"].startswith("max-age=")
    assert trusted.headers["content-security-policy"].startswith("default-src 'none'")
    assert len(trusted.headers["x-request-id"]) == 32


def test_production_blocks_cross_site_mutations(tmp_path: Path) -> None:
    client = _client(_production_settings(tmp_path))
    headers = _proxy_headers(
        origin="https://evil.example",
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


def test_production_rejects_malformed_referer_without_server_error(
    tmp_path: Path,
) -> None:
    client = _client(_production_settings(tmp_path))

    response = client.post(
        "/api/jobs",
        headers=_proxy_headers(referer="https://[invalid"),
        json={
            "job_id": "a" * 32,
            "source_format": "hydro",
            "target_format": "icpc",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Referer origin is not allowed"


def test_production_rate_limit_is_enforced(tmp_path: Path) -> None:
    settings = _production_settings(tmp_path, rate_limit_requests_per_minute=2)
    client = _client(settings)
    headers = _proxy_headers(**{"x-forwarded-for": "203.0.113.8"})

    first = client.get("/api/jobs/not-a-job", headers=headers)
    second = client.get("/api/jobs/not-a-job", headers=headers)
    limited = client.get("/api/jobs/not-a-job", headers=headers)

    assert first.status_code == 404
    assert second.status_code == 404
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) >= 1


def test_request_body_limit_rejects_before_upload_parsing(tmp_path: Path) -> None:
    settings = _production_settings(tmp_path, max_request_body_bytes=64)
    client = _client(settings)

    response = client.post(
        "/api/inspect",
        headers=_proxy_headers(origin="https://converter.example.com"),
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


def test_inspect_rejects_high_compression_ratio(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("huge.txt", b"\0" * (4 * 1024 * 1024))

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
