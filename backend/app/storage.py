from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import threading
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath

from fastapi import HTTPException, UploadFile, status

from .config import Settings
from .format_detection import inspect_zip_package
from .job_index import JobIndex
from .schemas import InspectResponse, JobStatus, RepairRequest


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
    package_scope: str = "unknown"
    package_layout: str = "unknown"
    problem_count: int | None = None
    problems: list[dict[str, str]] = field(default_factory=list)
    problems_truncated: bool = False
    supported_targets: list[str] = field(default_factory=list)
    source_format: str | None = None
    target_format: str | None = None
    progress: dict[str, object] | None = None
    timeout: dict[str, object] | None = None
    parent_job_id: str | None = None
    repair_revision: int = 0


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
        self.request_path = root / "request.json"
        self.repair_plan_path = self.input_dir / "repair-plan.json"
        self.supplements_dir = self.input_dir / "supplements"


class Storage:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.jobs_dir = settings.data_dir / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.job_index = JobIndex(settings.data_dir / "jobs.sqlite3")
        self._upload_lock = asyncio.Lock()
        self._log_lock = threading.Lock()
        self._metadata_lock = threading.RLock()
        self._change_condition = threading.Condition()
        self._change_revisions: dict[str, int] = {}
        self._backfill_job_index()

    def _backfill_job_index(self) -> None:
        existing_ids: set[str] = set()
        for root in self.jobs_dir.iterdir():
            if not root.is_dir() or not _is_safe_job_id(root.name):
                continue
            existing_ids.add(root.name)
            metadata_path = root / "metadata.json"
            if not metadata_path.is_file() or metadata_path.is_symlink():
                continue
            try:
                value = json.loads(metadata_path.read_text(encoding="utf-8"))
                metadata = JobMetadata(**value)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if metadata.id != root.name:
                continue
            self.job_index.upsert(metadata, updated_at=utc_now_iso())
        self.job_index.prune_missing(existing_ids)

    def paths_for(self, job_id: str) -> JobPaths:
        if not _is_safe_job_id(job_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Unknown job"
            )
        return JobPaths(self.jobs_dir / job_id)

    async def save_upload(self, upload: UploadFile) -> InspectResponse:
        async with self._upload_lock:
            return await self._save_upload_locked(upload)

    async def create_repair_job(
        self,
        parent_job_id: str,
        repair_request: RepairRequest,
        uploads: list[UploadFile],
    ) -> tuple[str, dict[str, object]]:
        try:
            async with self._upload_lock:
                return await self._create_repair_job_locked(
                    parent_job_id, repair_request, uploads
                )
        finally:
            for upload in uploads:
                await upload.close()

    async def _create_repair_job_locked(
        self,
        parent_job_id: str,
        repair_request: RepairRequest,
        uploads: list[UploadFile],
    ) -> tuple[str, dict[str, object]]:
        parent = self.read_metadata(parent_job_id)
        if parent.status != "failed":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Repairs can only be applied to a failed conversion",
            )
        report = self.read_report(parent_job_id)
        if report.get("schema_version") != 2 or not report.get("repair_ready"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The failed conversion has no repairable missing files",
            )
        raw_suggestions = report.get("repair_suggestions")
        if not isinstance(raw_suggestions, list):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Invalid stored repair suggestions",
            )
        suggestions = {
            str(item.get("id")): item
            for item in raw_suggestions
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        if len(self.job_ids()) >= self.settings.max_stored_jobs:
            raise HTTPException(
                status_code=507,
                detail="Stored job limit reached; delete an existing job and retry",
            )
        parent_size = self.paths_for(parent_job_id).upload_path.stat().st_size
        existing_bytes = self.total_storage_bytes()
        if existing_bytes + parent_size > self.settings.max_storage_bytes:
            raise HTTPException(
                status_code=507,
                detail="Job storage limit reached; delete an existing job and retry",
            )

        uploads_by_name: dict[str, UploadFile] = {}
        for upload in uploads:
            name = Path(upload.filename or "").name
            if (
                not name
                or name != upload.filename
                or any(character in name for character in ("/", "\\", "\x00"))
                or len(name) > 255
                or name in uploads_by_name
            ):
                for item in uploads:
                    await item.close()
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Repair upload filenames must be unique safe basenames",
                )
            uploads_by_name[name] = upload
        requested_uploads = {
            selection.upload_name
            for selection in repair_request.selections
            if selection.upload_name is not None
        }
        if requested_uploads != set(uploads_by_name):
            for item in uploads:
                await item.close()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Repair uploads do not match the confirmed repair plan",
            )

        job_id = uuid.uuid4().hex
        paths = self.paths_for(job_id)
        try:
            paths.input_dir.mkdir(parents=True, exist_ok=False)
            paths.work_dir.mkdir()
            paths.output_dir.mkdir()
            paths.logs_path.write_text("", encoding="utf-8")
            _copy_file_bounded(
                self.paths_for(parent_job_id).upload_path, paths.upload_path
            )
            source_hash = _sha256_file(paths.upload_path)
        except Exception:
            shutil.rmtree(paths.root, ignore_errors=True)
            raise
        applied_plan: list[dict[str, object]] = []
        total_upload_bytes = 0
        try:
            for index, selection in enumerate(repair_request.selections, start=1):
                suggestion = suggestions.get(selection.suggestion_id)
                if suggestion is None:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        detail="Repair plan references an unknown suggestion",
                    )
                entry: dict[str, object] = {
                    "suggestion_id": selection.suggestion_id,
                    "expected_path": suggestion.get("expected_path"),
                    "role": suggestion.get("role"),
                }
                if selection.candidate_path is not None:
                    candidates = suggestion.get("candidates")
                    candidate = (
                        next(
                            (
                                item
                                for item in candidates
                                if isinstance(item, dict)
                                and item.get("path") == selection.candidate_path
                            ),
                            None,
                        )
                        if isinstance(candidates, list)
                        else None
                    )
                    if candidate is None:
                        raise HTTPException(
                            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                            detail="Repair candidate was not offered by the converter",
                        )
                    entry.update(
                        {
                            "candidate_path": selection.candidate_path,
                            "strategy": candidate.get("strategy"),
                        }
                    )
                else:
                    upload_name = str(selection.upload_name)
                    upload = uploads_by_name[upload_name]
                    stored_name = f"{index:04d}.upload"
                    paths.supplements_dir.mkdir(exist_ok=True)
                    stored_path = paths.supplements_dir / stored_name
                    digest = hashlib.sha256()
                    size = 0
                    with stored_path.open("wb") as destination:
                        while chunk := await upload.read(1024 * 1024):
                            size += len(chunk)
                            total_upload_bytes += len(chunk)
                            if (
                                size > 256 * 1024 * 1024
                                or total_upload_bytes > self.settings.max_upload_bytes
                            ):
                                raise HTTPException(
                                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                                    detail="Repair uploads exceed the configured size limit",
                                )
                            if (
                                existing_bytes + parent_size + total_upload_bytes
                                > self.settings.max_storage_bytes
                            ):
                                raise HTTPException(
                                    status_code=507,
                                    detail="Job storage limit reached; delete an existing job and retry",
                                )
                            digest.update(chunk)
                            destination.write(chunk)
                    entry.update(
                        {
                            "upload_name": stored_name,
                            "upload_sha256": digest.hexdigest(),
                            "strategy": "upload",
                        }
                    )
                applied_plan.append(entry)
        except Exception:
            shutil.rmtree(paths.root, ignore_errors=True)
            raise
        finally:
            for upload in uploads:
                await upload.close()

        try:
            plan = {
                "schema_version": 1,
                "parent_job_id": parent_job_id,
                "source_sha256": source_hash,
                "selections": applied_plan,
            }
            paths.repair_plan_path.write_text(
                json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            request_payload = self.read_request(parent_job_id)
            request_payload["job_id"] = job_id
            metadata = JobMetadata(
                id=job_id,
                filename=parent.filename,
                size=paths.upload_path.stat().st_size,
                status="queued",
                created_at=utc_now_iso(),
                detected_format=parent.detected_format,
                format_candidates=parent.format_candidates,
                package_scope=parent.package_scope,
                package_layout=parent.package_layout,
                problem_count=parent.problem_count,
                problems=parent.problems,
                problems_truncated=parent.problems_truncated,
                supported_targets=parent.supported_targets,
                parent_job_id=parent_job_id,
                repair_revision=parent.repair_revision + 1,
            )
            self.write_metadata(metadata)
            prepare_runner_mount_permissions(paths)
            return job_id, request_payload
        except Exception:
            shutil.rmtree(paths.root, ignore_errors=True)
            raise

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
            inspection = inspect_zip_package(
                paths.upload_path,
                fallback_id=Path(filename).stem,
            )
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
            for item in inspection.candidates
        ]
        detected = inspection.detected_format
        problem_data = [
            {"id": problem.id, "path": problem.path} for problem in inspection.problems
        ]
        metadata = JobMetadata(
            id=job_id,
            filename=filename,
            size=size,
            status="queued",
            created_at=utc_now_iso(),
            detected_format=detected,
            format_candidates=candidate_data,
            package_scope=inspection.package_scope,
            package_layout=inspection.package_layout,
            problem_count=inspection.problem_count,
            problems=problem_data,
            problems_truncated=inspection.problems_truncated,
            supported_targets=list(inspection.supported_targets),
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
            package_scope=inspection.package_scope,  # type: ignore[arg-type]
            package_layout=inspection.package_layout,  # type: ignore[arg-type]
            problem_count=inspection.problem_count,
            problems=problem_data,
            problems_truncated=inspection.problems_truncated,
            supported_targets=list(inspection.supported_targets),  # type: ignore[arg-type]
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
            or report.get("schema_version") not in {1, 2}
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
                "context",
            }:
                raise ValueError("invalid conversion report issue")
            severity = issue.get("severity")
            code = issue.get("code")
            message = issue.get("message")
            if (
                severity not in severities
                or not isinstance(code, str)
                or not code
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
            context = issue.get("context", {})
            if (
                not isinstance(context, dict)
                or len(context) > 32
                or not all(
                    isinstance(key, str)
                    and 0 < len(key) <= 128
                    and isinstance(value, str)
                    and len(value) <= 1024
                    for key, value in context.items()
                )
            ):
                raise ValueError("invalid conversion report issue context")
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
        if report["schema_version"] == 2:
            _validate_report_v2(report)
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
        with self._metadata_lock:
            data = self.job_index.get(job_id)
            if data is not None:
                return JobMetadata(**data)
            if paths.metadata_path.is_file() and not paths.metadata_path.is_symlink():
                try:
                    data = json.loads(paths.metadata_path.read_text(encoding="utf-8"))
                    metadata = JobMetadata(**data)
                except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="Invalid stored job metadata",
                    ) from exc
                self.job_index.upsert(metadata, updated_at=utc_now_iso())
                return metadata
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Unknown job"
            )

    def write_metadata(self, metadata: JobMetadata) -> None:
        paths = self.paths_for(metadata.id)
        with self._metadata_lock:
            paths.root.mkdir(parents=True, exist_ok=True)
            content = json.dumps(asdict(metadata), ensure_ascii=False, indent=2)
            tmp_path = paths.metadata_path.with_name(f"{paths.metadata_path.name}.tmp")
            tmp_path.write_text(content, encoding="utf-8")
            tmp_path.replace(paths.metadata_path)
            self.job_index.upsert(metadata, updated_at=utc_now_iso())
        self.notify_change(metadata.id)

    def write_request(self, job_id: str, request: dict[str, object]) -> None:
        path = self.paths_for(job_id).request_path
        content = json.dumps(request, ensure_ascii=False, indent=2)
        if len(content.encode("utf-8")) > 1024 * 1024:
            raise ValueError("job request exceeds 1 MiB")
        temporary = path.with_name(f"{path.name}.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)

    def read_request(self, job_id: str) -> dict[str, object]:
        path = self.paths_for(job_id).request_path
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 1024 * 1024:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Original conversion request is not available",
            )
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Invalid stored conversion request",
            ) from exc
        if not isinstance(value, dict):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Invalid stored conversion request",
            )
        return value

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
        self.notify_change(job_id)

    def read_logs(self, job_id: str) -> str:
        paths = self.paths_for(job_id)
        if not paths.logs_path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Unknown job"
            )
        return paths.logs_path.read_text(encoding="utf-8", errors="replace")

    def delete_job(self, job_id: str) -> None:
        paths = self.paths_for(job_id)
        indexed = self.job_index.get(job_id) is not None
        if not paths.root.exists() and not indexed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Unknown job"
            )
        if paths.root.exists():
            shutil.rmtree(paths.root)
        self.job_index.delete(job_id)
        self.notify_change(job_id)

    def job_ids(self) -> list[str]:
        return self.job_index.ids()

    def notify_change(self, job_id: str) -> int:
        with self._change_condition:
            revision = self._change_revisions.get(job_id, 0) + 1
            self._change_revisions[job_id] = revision
            self._change_condition.notify_all()
            return revision

    def change_revision(self, job_id: str) -> int:
        with self._change_condition:
            return self._change_revisions.get(job_id, 0)

    def wait_for_change(
        self, job_id: str, after_revision: int, timeout: float
    ) -> int | None:
        deadline = time.monotonic() + timeout
        with self._change_condition:
            while self._change_revisions.get(job_id, 0) <= after_revision:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._change_condition.wait(timeout=remaining)
            return self._change_revisions[job_id]

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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_file_bounded(source_path: Path, destination_path: Path) -> None:
    with source_path.open("rb") as source, destination_path.open("wb") as destination:
        while chunk := source.read(1024 * 1024):
            destination.write(chunk)


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


def _validate_report_v2(report: dict[str, object]) -> None:
    repair_ready = report.get("repair_ready")
    suggestions = report.get("repair_suggestions")
    applied = report.get("applied_repairs")
    digest = report.get("source_semantic_digest")
    if (
        not isinstance(repair_ready, bool)
        or not isinstance(suggestions, list)
        or len(suggestions) > 1000
        or not isinstance(applied, list)
        or len(applied) > 1000
        or (
            digest is not None
            and (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            )
        )
    ):
        raise ValueError("invalid conversion report v2 fields")
    if repair_ready != bool(suggestions):
        raise ValueError("conversion report repair_ready does not match suggestions")
    suggestion_ids: set[str] = set()
    expected_paths: set[str] = set()
    allowed_roles = {
        "input",
        "output",
        "sample-input",
        "sample-output",
        "checker",
        "interactor",
        "validator",
        "solution",
        "statement-pdf",
        "attachment",
    }
    expected_confidence = {
        "case-only": 1.0,
        "extension-alias": 0.95,
        "unique-basename": 0.85,
    }
    for suggestion in suggestions:
        if not isinstance(suggestion, dict) or set(suggestion) != {
            "id",
            "issue_code",
            "expected_path",
            "role",
            "problem",
            "candidates",
            "requires_upload",
        }:
            raise ValueError("invalid conversion report repair suggestion")
        if (
            not isinstance(suggestion["id"], str)
            or len(suggestion["id"]) != 24
            or any(
                character not in "0123456789abcdef" for character in suggestion["id"]
            )
            or not isinstance(suggestion["issue_code"], str)
            or not suggestion["issue_code"]
            or len(suggestion["issue_code"]) > 128
            or not isinstance(suggestion["expected_path"], str)
            or not _is_safe_archive_path(suggestion["expected_path"])
            or not isinstance(suggestion["role"], str)
            or suggestion["role"] not in allowed_roles
            or (
                suggestion["problem"] is not None
                and (
                    not isinstance(suggestion["problem"], str)
                    or len(suggestion["problem"]) > 256
                )
            )
            or not isinstance(suggestion["requires_upload"], bool)
            or not isinstance(suggestion["candidates"], list)
            or len(suggestion["candidates"]) > 100
        ):
            raise ValueError("invalid conversion report repair suggestion")
        if (
            suggestion["id"] in suggestion_ids
            or suggestion["expected_path"].casefold() in expected_paths
        ):
            raise ValueError("duplicate conversion report repair suggestion")
        suggestion_ids.add(suggestion["id"])
        expected_paths.add(suggestion["expected_path"].casefold())
        candidate_paths: set[str] = set()
        for candidate in suggestion["candidates"]:
            if (
                not isinstance(candidate, dict)
                or set(candidate) != {"path", "strategy", "confidence"}
                or not isinstance(candidate["path"], str)
                or not _is_safe_archive_path(candidate["path"])
                or candidate["strategy"]
                not in {"case-only", "extension-alias", "unique-basename"}
                or type(candidate["confidence"]) not in {int, float}
                or float(candidate["confidence"])
                != expected_confidence[candidate["strategy"]]
            ):
                raise ValueError("invalid conversion report repair candidate")
            candidate_key = candidate["path"].casefold()
            if candidate_key in candidate_paths:
                raise ValueError("duplicate conversion report repair candidate")
            candidate_paths.add(candidate_key)
    for repair in applied:
        if (
            not isinstance(repair, dict)
            or len(repair) > 16
            or not all(
                isinstance(key, str)
                and 0 < len(key) <= 128
                and isinstance(value, str)
                and len(value) <= 1024
                for key, value in repair.items()
            )
        ):
            raise ValueError("invalid conversion report applied repair")


def _is_safe_archive_path(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > 1024:
        return False
    if "\\" in value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and "." not in path.parts
        and all(part for part in path.parts)
    )
