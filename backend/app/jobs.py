from __future__ import annotations

import queue
import re
import selectors
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass

from fastapi import HTTPException, status

from .auth import AuthService, AuthenticatedUser
from .config import Settings
from .docker_runner import build_docker_command, pack_output, stop_container
from .schemas import DeleteResponse, JobRequest, JobResponse
from .storage import JobMetadata, Storage, utc_now_iso


_PID_RE = re.compile(r"^[A-Za-z]+[0-9]+$")
_DOMJUDGE_CODE_RE = re.compile(r"^[A-Za-z]+$")
_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_TARGETS = {"hydro", "domjudge", "hydro_to_domjudge"}


@dataclass
class RuntimeJob:
    request: JobRequest
    process: subprocess.Popen[str] | None = None
    cancel_event: threading.Event | None = None


class JobManager:
    def __init__(self, settings: Settings, storage: Storage, auth: AuthService | None = None) -> None:
        self.settings = settings
        self.storage = storage
        self.auth = auth
        self._lock = threading.RLock()
        self._runtime: dict[str, RuntimeJob] = {}
        self._queue: queue.Queue[tuple[JobRequest, threading.Event]] = queue.Queue()
        self._workers: list[threading.Thread] = []
        self._started = False
        self._start_workers()

    def start(self, request: JobRequest, user: AuthenticatedUser | None = None) -> JobResponse:
        self._validate_request(request)
        metadata = self.storage.read_metadata(request.job_id)
        self.ensure_access(metadata, user)
        with self._lock:
            runtime = self._runtime.get(request.job_id)
            if runtime is not None:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Job is already queued or running")
            if metadata.status not in {"queued", "failed", "cancelled"}:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Job cannot be started again")

            paths = self.storage.paths_for(request.job_id)
            if not paths.upload_path.exists():
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Uploaded package has been cleaned; upload it again to start a new conversion",
                )

            if self.auth is not None and user is not None and not user.is_admin and not metadata.quota_reserved:
                self.auth.reserve_conversion(user)
                metadata.quota_reserved = True
                metadata.quota_finalized = False

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
            metadata.run_requested = True
            metadata.started_at = None
            metadata.finished_at = None
            metadata.exit_code = None
            metadata.error = None
            self.storage.write_metadata(metadata)

            cancel_event = threading.Event()
            self._runtime[request.job_id] = RuntimeJob(request=request, cancel_event=cancel_event)
            self._queue.put((request, cancel_event))

        return self.response(request.job_id, user)

    def response(self, job_id: str, user: AuthenticatedUser | None = None) -> JobResponse:
        metadata = self.storage.read_metadata(job_id)
        self.ensure_access(metadata, user)
        paths = self.storage.paths_for(job_id)
        return JobResponse(
            id=metadata.id,
            user_id=metadata.user_id,
            status=metadata.status,
            created_at=metadata.created_at,
            started_at=metadata.started_at,
            finished_at=metadata.finished_at,
            exit_code=metadata.exit_code,
            download_ready=metadata.status == "success" and paths.result_path.exists(),
            error=metadata.error,
        )

    def cancel_or_delete(self, job_id: str, user: AuthenticatedUser | None = None) -> DeleteResponse:
        metadata = self.storage.read_metadata(job_id)
        self.ensure_access(metadata, user)
        with self._lock:
            metadata = self.storage.read_metadata(job_id)
            self.ensure_access(metadata, user)
            runtime = self._runtime.get(job_id)
            if runtime is not None and metadata.status in {"success", "failed", "cancelled"}:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Job is finishing; retry shortly",
                )
            was_queued = metadata.status == "queued"
            was_running = metadata.status == "running"
            is_active = was_running or (was_queued and (runtime is not None or metadata.run_requested))
            if is_active:
                if runtime and runtime.cancel_event:
                    runtime.cancel_event.set()
                if was_running or (runtime is not None and runtime.process is not None):
                    stop_container(self.settings, job_id)
                metadata.status = "cancelled"
                metadata.finished_at = utc_now_iso()
                metadata.error = "Cancelled by user"
                self.storage.write_metadata(metadata)
                if runtime is not None and runtime.process is None and was_queued:
                    self._runtime.pop(job_id, None)
                    self._finalize_quota(metadata, success=False)
                    self.storage.cleanup_runtime_files(job_id)
                elif runtime is None:
                    self._finalize_quota(metadata, success=False)
                    self.storage.cleanup_runtime_files(job_id)
                return DeleteResponse(id=job_id, status="cancelled", deleted=False)

            self.storage.delete_job(job_id)
            self._runtime.pop(job_id, None)
            return DeleteResponse(id=job_id, status=metadata.status, deleted=True)

    def recover_interrupted_jobs(self) -> None:
        for metadata in self.storage.iter_metadata():
            if metadata.status not in {"queued", "running"}:
                continue
            if metadata.status == "queued" and not metadata.run_requested and not metadata.quota_reserved:
                continue
            metadata.status = "cancelled"
            metadata.finished_at = utc_now_iso()
            metadata.error = "Cancelled after backend restart"
            self.storage.write_metadata(metadata)
            self._finalize_quota(metadata, success=False)
            self.storage.cleanup_runtime_files(metadata.id)

    def _start_workers(self) -> None:
        if self._started:
            return
        self._started = True
        for index in range(max(1, self.settings.max_concurrent_jobs)):
            worker = threading.Thread(target=self._worker_loop, name=f"p2h-worker-{index + 1}", daemon=True)
            worker.start()
            self._workers.append(worker)

    def _worker_loop(self) -> None:
        while True:
            request, cancel_event = self._queue.get()
            try:
                self._run_job(request, cancel_event)
            finally:
                self._queue.task_done()

    def _run_job(self, request: JobRequest, cancel_event: threading.Event) -> None:
        paths = self.storage.paths_for(request.job_id)
        process: subprocess.Popen[str] | None = None
        terminal_metadata: JobMetadata | None = None

        try:
            with self._lock:
                if cancel_event.is_set():
                    terminal_metadata = self._mark_cancelled_before_start(request.job_id)
                    return

                metadata = self.storage.read_metadata(request.job_id)
                if metadata.status == "cancelled":
                    terminal_metadata = metadata
                    return
                metadata.status = "running"
                metadata.started_at = utc_now_iso()
                metadata.finished_at = None
                metadata.exit_code = None
                metadata.error = None
                self.storage.write_metadata(metadata)

            cmd = build_docker_command(self.settings, request.job_id, paths, request)
            self.storage.append_log(request.job_id, "$ " + _quote_command(cmd) + "\n")
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            with self._lock:
                runtime = self._runtime.get(request.job_id)
                if runtime:
                    runtime.process = process
                if cancel_event.is_set():
                    stop_container(self.settings, request.job_id)

            exit_code = self._stream_process_output(process, request.job_id, cmd)
            metadata = self.storage.read_metadata(request.job_id)
            if metadata.status == "cancelled":
                metadata.exit_code = exit_code
                self.storage.write_metadata(metadata)
                terminal_metadata = metadata
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
            terminal_metadata = metadata
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
            terminal_metadata = metadata
        except Exception as exc:
            stop_container(self.settings, request.job_id)
            metadata = self.storage.read_metadata(request.job_id)
            metadata.status = "failed"
            metadata.finished_at = utc_now_iso()
            metadata.error = str(exc)
            self.storage.append_log(request.job_id, f"backend error: {exc}\n")
            self.storage.write_metadata(metadata)
            terminal_metadata = metadata
        finally:
            try:
                metadata = terminal_metadata or self.storage.read_metadata(request.job_id)
                if metadata.status in {"success", "failed", "cancelled"}:
                    self._finalize_quota(metadata, success=metadata.status == "success")
                    self.storage.cleanup_runtime_files(request.job_id)
                    self.storage.cleanup_expired_jobs()
            except HTTPException:
                pass
            finally:
                with self._lock:
                    self._runtime.pop(request.job_id, None)

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
        if request.target not in _TARGETS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="target must be hydro, domjudge, or hydro_to_domjudge",
            )
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
        if request.target in {"domjudge", "hydro_to_domjudge"}:
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
        if request.target == "domjudge":
            if request.domjudge_auto_validator and request.domjudge_default_validator:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="domjudge_auto_validator and domjudge_default_validator cannot both be enabled",
                )

    def _mark_cancelled_before_start(self, job_id: str) -> JobMetadata:
        metadata = self.storage.read_metadata(job_id)
        if metadata.status == "cancelled":
            return metadata
        metadata.status = "cancelled"
        metadata.finished_at = utc_now_iso()
        metadata.error = "Cancelled before runner container started"
        self.storage.write_metadata(metadata)
        return metadata

    def _finalize_quota(self, metadata: JobMetadata, *, success: bool) -> None:
        metadata.run_requested = False
        if metadata.quota_finalized:
            self.storage.write_metadata(metadata)
            return
        if metadata.quota_reserved and self.auth is not None:
            self.auth.complete_conversion(metadata.user_id, success=success)
        metadata.quota_finalized = True
        self.storage.write_metadata(metadata)

    def ensure_access(self, metadata: JobMetadata, user: AuthenticatedUser | None) -> None:
        if user is None or user.is_admin:
            return
        if metadata.user_id != user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown job")


def _quote_command(cmd: list[str]) -> str:
    import shlex

    return " ".join(shlex.quote(part) for part in cmd)
