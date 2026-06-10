from __future__ import annotations

import json
import shutil
import uuid
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, UploadFile, status

from .config import Settings
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


class JobPaths:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.input_dir = root / "input"
        self.work_dir = root / "work"
        self.output_dir = root / "output"
        self.upload_path = self.input_dir / "contest.zip"
        self.logs_path = root / "logs.txt"
        self.result_path = root / "result.zip"
        self.metadata_path = root / "metadata.json"


class Storage:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.jobs_dir = settings.data_dir / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

    def paths_for(self, job_id: str) -> JobPaths:
        if not _is_safe_job_id(job_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown job")
        return JobPaths(self.jobs_dir / job_id)

    async def save_upload(self, upload: UploadFile) -> InspectResponse:
        filename = Path(upload.filename or "").name
        if not filename.lower().endswith(".zip"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only .zip files are accepted")

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
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is not a valid zip")

        prepare_runner_mount_permissions(paths)

        metadata = JobMetadata(
            id=job_id,
            filename=filename,
            size=size,
            status="queued",
            created_at=utc_now_iso(),
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
        )

    def read_metadata(self, job_id: str) -> JobMetadata:
        paths = self.paths_for(job_id)
        if not paths.metadata_path.exists():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown job")
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
        with paths.logs_path.open("a", encoding="utf-8", errors="replace") as out:
            out.write(text)

    def read_logs(self, job_id: str) -> str:
        paths = self.paths_for(job_id)
        if not paths.logs_path.exists():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown job")
        return paths.logs_path.read_text(encoding="utf-8", errors="replace")

    def delete_job(self, job_id: str) -> None:
        paths = self.paths_for(job_id)
        if not paths.root.exists():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown job")
        shutil.rmtree(paths.root)


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
