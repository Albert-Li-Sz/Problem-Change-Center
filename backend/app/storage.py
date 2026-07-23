from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, UploadFile, status

from .config import Settings
from .format_detection import detect_zip_format, detected_format
from .schemas import InspectResponse, JobStatus


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class JobMetadata:
    id: str
    filename: str
    size: int
    status: JobStatus
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    error: str | None = None
    detected_format: str | None = None
    format_candidates: list[dict[str, object]] = field(default_factory=list)
    source_format: str | None = None
    target_format: str | None = None


class JobPaths:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.input_dir = root / "input"
        self.work_dir = root / "work"
        self.output_dir = root / "output"
        self.upload_path = self.input_dir / "contest.zip"
        self.logs_path = root / "logs.txt"
        self.result_path = root / "result.zip"
        self.report_path = root / "report.json"
        self.metadata_path = root / "metadata.json"


class Storage:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.jobs_dir = settings.data_dir / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._upload_lock = asyncio.Lock()
        self._log_lock = threading.Lock()

    def paths_for(self, job_id: str) -> JobPaths:
        if not _is_safe_job_id(job_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Unknown job"
            )
        return JobPaths(self.jobs_dir / job_id)

    async def save_upload(self, upload: UploadFile) -> InspectResponse:
        async with self._upload_lock:
            return await self._save_upload_locked(upload)

    async def _save_upload_locked(self, upload: UploadFile) -> InspectResponse:
        filename = Path(upload.filename or "").name
        if not filename.lower().endswith(".zip"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only .zip files are accepted",
            )

        if len(self.job_ids()) >= self.settings.max_stored_jobs:
            raise HTTPException(
                status_code=507,
                detail="Stored job limit reached; delete an existing job and retry",
            )
        existing_bytes = self.total_storage_bytes()
        if existing_bytes >= self.settings.max_storage_bytes:
            raise HTTPException(
                status_code=507,
                detail="Job storage limit reached; delete an existing job and retry",
            )

        job_id = uuid.uuid4().hex
        paths = self.paths_for(job_id)
        paths.input_dir.mkdir(parents=True, exist_ok=False)
        paths.work_dir.mkdir(parents=True, exist_ok=True)
        paths.output_dir.mkdir(parents=True, exist_ok=True)
        prepare_runner_mount_permissions(paths)
        paths.logs_path.write_text("", encoding="utf-8")

        size = 0
        try:
            with paths.upload_path.open("wb") as out:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.settings.max_upload_bytes:
                        raise HTTPException(
                            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail=f"Upload exceeds {self.settings.max_upload_bytes} bytes",
                        )
                    if existing_bytes + size > self.settings.max_storage_bytes:
                        raise HTTPException(
                            status_code=507,
                            detail="Job storage limit reached; delete an existing job and retry",
                        )
                    out.write(chunk)
        except HTTPException:
            shutil.rmtree(paths.root, ignore_errors=True)
            raise
        except Exception:
            shutil.rmtree(paths.root, ignore_errors=True)
            raise
        finally:
            await upload.close()

        if not zipfile.is_zipfile(paths.upload_path):
            shutil.rmtree(paths.root, ignore_errors=True)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded file is not a valid zip",
            )

        prepare_runner_mount_permissions(paths)

        try:
            candidates = detect_zip_format(paths.upload_path)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            shutil.rmtree(paths.root, ignore_errors=True)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsafe or invalid ZIP archive: {exc}",
            ) from exc
        candidate_data = [
            {
                "format": item.format,
                "confidence": item.confidence,
                "evidence": list(item.evidence),
            }
            for item in candidates
        ]
        detected = detected_format(candidates)
        metadata = JobMetadata(
            id=job_id,
            filename=filename,
            size=size,
            status="queued",
            created_at=utc_now_iso(),
            detected_format=detected,
            format_candidates=candidate_data,
        )
        self.write_metadata(metadata)
        return InspectResponse(
            job_id=job_id,
            filename=filename,
            size=size,
            warnings=[
                "Default safe mode will not execute doall.sh.",
                "If doall.sh is enabled, it runs only inside the restricted Docker runner.",
            ],
            detected_format=detected,
            format_candidates=candidate_data,
        )

    def capture_conversion_report(self, job_id: str) -> dict[str, object] | None:
        paths = self.paths_for(job_id)
        source = paths.output_dir / ".p2h-report.json"
        if source.is_symlink():
            raise ValueError("conversion report must not be a symbolic link")
        if not source.is_file():
            return None
        if source.stat().st_size > 1024 * 1024:
            raise ValueError("conversion report exceeds 1 MiB")
        try:
            report = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid conversion report: {exc}") from exc
        if (
            not isinstance(report, dict)
            or type(report.get("schema_version")) is not int
            or report.get("schema_version") != 1
        ):
            raise ValueError("invalid conversion report schema")
        for name in ("source_format", "target_format"):
            value = report.get(name)
            if not isinstance(value, str) or not value or len(value) > 32:
                raise ValueError(f"invalid conversion report {name}")
        problem_count = report.get("problem_count")
        if (
            type(problem_count) is not int
            or problem_count < 0
            or problem_count > 100_000
        ):
            raise ValueError("invalid conversion report problem_count")
        counts = report.get("counts")
        severities = {"warning", "loss", "fatal"}
        if (
            not isinstance(counts, dict)
            or set(counts) != severities
            or not all(
                type(counts[name]) is int and 0 <= counts[name] <= 100_000
                for name in severities
            )
        ):
            raise ValueError("invalid conversion report counts")
        issues = report.get("issues")
        artifacts = report.get("artifacts")
        if not isinstance(issues, list) or len(issues) > 100_000:
            raise ValueError("invalid conversion report issues")
        issue_counts = {name: 0 for name in severities}
        for issue in issues:
            if not isinstance(issue, dict) or set(issue) - {
                "severity",
                "code",
                "message",
                "problem",
                "field",
            }:
                raise ValueError("invalid conversion report issue")
            severity = issue.get("severity")
            code = issue.get("code")
            message = issue.get("message")
            if (
                severity not in severities
                or not isinstance(code, str)
                or not code
                or len(code) > 128
                or not isinstance(message, str)
                or not message
                or len(message) > 4096
            ):
                raise ValueError("invalid conversion report issue")
            for optional_name in ("problem", "field"):
                optional = issue.get(optional_name)
                if optional is not None and (
                    not isinstance(optional, str) or len(optional) > 1024
                ):
                    raise ValueError("invalid conversion report issue")
            issue_counts[str(severity)] += 1
        if any(counts[name] != issue_counts[name] for name in severities):
            raise ValueError("conversion report counts do not match issues")
        if (
            not isinstance(artifacts, list)
            or len(artifacts) > 100_000
            or not all(
                isinstance(item, str) and len(item) <= 1024 for item in artifacts
            )
        ):
            raise ValueError("invalid conversion report artifacts")
        source.replace(paths.report_path)
        return report

    def read_report(self, job_id: str) -> dict[str, object]:
        path = self.paths_for(job_id).report_path
        if path.is_symlink():
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Invalid stored report",
            )
        if not path.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Conversion report is not available",
            )
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Invalid stored report",
            ) from exc
        if not isinstance(value, dict):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Invalid stored report",
            )
        return value

    def read_metadata(self, job_id: str) -> JobMetadata:
        paths = self.paths_for(job_id)
        if not paths.metadata_path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Unknown job"
            )
        data = json.loads(paths.metadata_path.read_text(encoding="utf-8"))
        return JobMetadata(**data)

    def write_metadata(self, metadata: JobMetadata) -> None:
        paths = self.paths_for(metadata.id)
        paths.root.mkdir(parents=True, exist_ok=True)
        content = json.dumps(asdict(metadata), ensure_ascii=False, indent=2)
        tmp_path = paths.metadata_path.with_name(f"{paths.metadata_path.name}.tmp")
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(paths.metadata_path)

    def append_log(self, job_id: str, text: str) -> None:
        paths = self.paths_for(job_id)
        marker = b"\n[log truncated: size limit reached]\n"
        data = text.encode("utf-8", errors="replace")
        with self._log_lock:
            current_size = (
                paths.logs_path.stat().st_size if paths.logs_path.exists() else 0
            )
            remaining = self.settings.max_log_bytes - current_size
            if remaining <= 0:
                return
            if len(data) > remaining:
                keep = max(0, remaining - len(marker))
                data = data[:keep] + marker[: remaining - keep]
            with paths.logs_path.open("ab") as out:
                out.write(data)

    def read_logs(self, job_id: str) -> str:
        paths = self.paths_for(job_id)
        if not paths.logs_path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Unknown job"
            )
        return paths.logs_path.read_text(encoding="utf-8", errors="replace")

    def delete_job(self, job_id: str) -> None:
        paths = self.paths_for(job_id)
        if not paths.root.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Unknown job"
            )
        shutil.rmtree(paths.root)

    def job_ids(self) -> list[str]:
        return sorted(
            path.name
            for path in self.jobs_dir.iterdir()
            if path.is_dir() and _is_safe_job_id(path.name)
        )

    def total_storage_bytes(self) -> int:
        total = 0
        for root, directories, filenames in os.walk(self.jobs_dir, followlinks=False):
            directories[:] = [
                name for name in directories if not (Path(root) / name).is_symlink()
            ]
            for filename in filenames:
                try:
                    total += (Path(root) / filename).lstat().st_size
                except FileNotFoundError:
                    continue
        return total


def _is_safe_job_id(job_id: str) -> bool:
    return len(job_id) == 32 and all(ch in "0123456789abcdef" for ch in job_id)


def prepare_runner_mount_permissions(paths: JobPaths) -> None:
    # Docker runs the converter as fixed uid/gid 10001:10001. On Linux bind
    # mounts keep host ownership, so use mode bits instead of host chown.
    for directory in (paths.root, paths.input_dir):
        if directory.exists():
            directory.chmod(0o755)
    if paths.upload_path.exists():
        paths.upload_path.chmod(0o644)
    for directory in (paths.work_dir, paths.output_dir):
        if directory.exists():
            directory.chmod(0o777)
