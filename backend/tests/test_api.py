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
from app.storage import JobMetadata, Storage


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
    app.state.storage = Storage(settings)
    app.state.job_manager = JobManager(settings, app.state.storage)

    import app.main as main_module

    main_module.storage = app.state.storage
    main_module.job_manager = app.state.job_manager
    return TestClient(app)


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
