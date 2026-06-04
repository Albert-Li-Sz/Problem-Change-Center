from __future__ import annotations

import re
import selectors
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass

from fastapi import HTTPException, status

from .config import Settings
from .docker_runner import build_docker_command, pack_output, stop_container
from .schemas import DeleteResponse, JobRequest, JobResponse
from .storage import JobMetadata, Storage, utc_now_iso


_PID_RE = re.compile(r"^[A-Za-z]+[0-9]+$")
_DOMJUDGE_CODE_RE = re.compile(r"^[A-Za-z]+$")
_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


@dataclass
class RuntimeJob:
    process: subprocess.Popen[str] | None = None
    thread: threading.Thread | None = None


class JobManager:
    def __init__(self, settings: Settings, storage: Storage) -> None:
        self.settings = settings
        self.storage = storage
        self._lock = threading.RLock()
        self._runtime: dict[str, RuntimeJob] = {}

    def start(self, request: JobRequest) -> JobResponse:
        self._validate_request(request)
        metadata = self.storage.read_metadata(request.job_id)
        with self._lock:
            runtime = self._runtime.get(request.job_id)
            if runtime and runtime.thread and runtime.thread.is_alive():
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Job is already running")
            if metadata.status not in {"queued", "failed", "cancelled"}:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Job cannot be started again")

            paths = self.storage.paths_for(request.job_id)
            shutil.rmtree(paths.work_dir, ignore_errors=True)
            shutil.rmtree(paths.output_dir, ignore_errors=True)
            paths.work_dir.mkdir(parents=True, exist_ok=True)
            paths.output_dir.mkdir(parents=True, exist_ok=True)
            paths.work_dir.chmod(0o777)
            paths.output_dir.chmod(0o777)
            paths.logs_path.write_text("", encoding="utf-8")
            if paths.result_path.exists():
                paths.result_path.unlink()

            metadata.status = "queued"
            metadata.started_at = None
            metadata.finished_at = None
            metadata.exit_code = None
            metadata.error = None
            self.storage.write_metadata(metadata)

            thread = threading.Thread(target=self._run_job, args=(request,), daemon=True)
            self._runtime[request.job_id] = RuntimeJob(thread=thread)
            thread.start()

        return self.response(request.job_id)

    def response(self, job_id: str) -> JobResponse:
        metadata = self.storage.read_metadata(job_id)
        paths = self.storage.paths_for(job_id)
        return JobResponse(
            id=metadata.id,
            status=metadata.status,
            created_at=metadata.created_at,
            started_at=metadata.started_at,
            finished_at=metadata.finished_at,
            exit_code=metadata.exit_code,
            download_ready=metadata.status == "success" and paths.result_path.exists(),
            error=metadata.error,
        )

    def cancel_or_delete(self, job_id: str) -> DeleteResponse:
        metadata = self.storage.read_metadata(job_id)
        with self._lock:
            runtime = self._runtime.get(job_id)
            if metadata.status == "running" or (runtime and runtime.thread and runtime.thread.is_alive()):
                stop_container(self.settings, job_id)
                metadata.status = "cancelled"
                metadata.finished_at = utc_now_iso()
                metadata.error = "Cancelled by user"
                self.storage.write_metadata(metadata)
                return DeleteResponse(id=job_id, status="cancelled", deleted=False)

            self.storage.delete_job(job_id)
            self._runtime.pop(job_id, None)
            return DeleteResponse(id=job_id, status=metadata.status, deleted=True)

    def _run_job(self, request: JobRequest) -> None:
        paths = self.storage.paths_for(request.job_id)
        metadata = self.storage.read_metadata(request.job_id)
        metadata.status = "running"
        metadata.started_at = utc_now_iso()
        metadata.finished_at = None
        metadata.exit_code = None
        metadata.error = None
        self.storage.write_metadata(metadata)

        cmd = build_docker_command(self.settings, request.job_id, paths, request)
        self.storage.append_log(request.job_id, "$ " + _quote_command(cmd) + "\n")

        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            with self._lock:
                self._runtime.setdefault(request.job_id, RuntimeJob()).process = process

            exit_code = self._stream_process_output(process, request.job_id, cmd)
            metadata = self.storage.read_metadata(request.job_id)
            if metadata.status == "cancelled":
                metadata.exit_code = exit_code
                self.storage.write_metadata(metadata)
                return

            metadata.exit_code = exit_code
            metadata.finished_at = utc_now_iso()
            if exit_code == 0:
                pack_output(paths.output_dir, paths.result_path)
                metadata.status = "success"
            else:
                metadata.status = "failed"
                metadata.error = f"Runner exited with code {exit_code}"
            self.storage.write_metadata(metadata)
        except subprocess.TimeoutExpired:
            stop_container(self.settings, request.job_id)
            if process is not None:
                process.kill()
            metadata = self.storage.read_metadata(request.job_id)
            metadata.status = "failed"
            metadata.finished_at = utc_now_iso()
            metadata.error = f"Job timed out after {self.settings.job_timeout_seconds} seconds"
            self.storage.append_log(request.job_id, metadata.error + "\n")
            self.storage.write_metadata(metadata)
        except Exception as exc:
            stop_container(self.settings, request.job_id)
            metadata = self.storage.read_metadata(request.job_id)
            metadata.status = "failed"
            metadata.finished_at = utc_now_iso()
            metadata.error = str(exc)
            self.storage.append_log(request.job_id, f"backend error: {exc}\n")
            self.storage.write_metadata(metadata)
        finally:
            with self._lock:
                runtime = self._runtime.get(request.job_id)
                if runtime:
                    runtime.process = None

    def _stream_process_output(self, process: subprocess.Popen[str], job_id: str, cmd: list[str]) -> int:
        assert process.stdout is not None
        deadline = time.monotonic() + self.settings.job_timeout_seconds
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            while True:
                for key, _ in selector.select(timeout=0.2):
                    line = key.fileobj.readline()
                    if line:
                        self.storage.append_log(job_id, line)

                if process.poll() is not None:
                    remainder = process.stdout.read()
                    if remainder:
                        self.storage.append_log(job_id, remainder)
                    return process.returncode if process.returncode is not None else 0

                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(cmd, self.settings.job_timeout_seconds)
        finally:
            selector.close()

    def _validate_request(self, request: JobRequest) -> None:
        if request.target not in {"hydro", "domjudge"}:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="target must be hydro or domjudge")
        if request.target == "hydro" and not _PID_RE.fullmatch(request.pid_start):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="pid_start must look like P1000")
        if request.owner < 1:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="owner must be positive")
        if request.missing_env not in {"warn", "error"}:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="missing_env must be warn or error")
        request.tags[:] = [item.strip() for item in request.tags if item.strip()]
        request.only[:] = [item.strip() for item in request.only if item.strip()]
        request.domjudge_code_start = request.domjudge_code_start.strip().upper()
        request.domjudge_color = request.domjudge_color.strip()
        if request.target == "domjudge":
            if not _DOMJUDGE_CODE_RE.fullmatch(request.domjudge_code_start):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="domjudge_code_start must contain letters only, for example A",
                )
            if not _HEX_COLOR_RE.fullmatch(request.domjudge_color):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="domjudge_color must be in #RRGGBB format",
                )
            if request.domjudge_auto_validator and request.domjudge_default_validator:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="domjudge_auto_validator and domjudge_default_validator cannot both be enabled",
                )


def _quote_command(cmd: list[str]) -> str:
    import shlex

    return " ".join(shlex.quote(part) for part in cmd)
