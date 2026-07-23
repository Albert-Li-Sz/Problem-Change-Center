from __future__ import annotations

import re
import selectors
import shutil

# The argv is built without a shell by docker_runner.
import subprocess  # nosec B404
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import HTTPException, status

from .config import Settings
from .docker_runner import (
    build_docker_command,
    pack_output,
    parse_size_bytes,
    stop_container,
)
from .schemas import DeleteResponse, JobRequest, JobResponse
from .storage import Storage, prepare_runner_mount_permissions, utc_now_iso


_PID_RE = re.compile(r"^[A-Za-z]+[0-9]+$")
_DOMJUDGE_CODE_RE = re.compile(r"^[A-Za-z]+$")
_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_TARGETS = {
    "hydro",
    "domjudge",
    "hydro_to_domjudge",
    "domjudge_to_hydro",
    "hoj_to_hydro",
    "hydro_to_hoj",
    "hoj_to_domjudge",
}


@dataclass
class RuntimeJob:
    process: subprocess.Popen[str] | None = None
    thread: threading.Thread | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    delete_when_finished: bool = False


class JobManager:
    def __init__(self, settings: Settings, storage: Storage) -> None:
        self.settings = settings
        self.storage = storage
        self._lock = threading.RLock()
        self._runtime: dict[str, RuntimeJob] = {}

    def start(self, request: JobRequest) -> JobResponse:
        self.cleanup_expired()
        self._validate_request(request)
        metadata = self.storage.read_metadata(request.job_id)
        if (
            not request.is_legacy_request
            and request.source_format == "auto"
            and metadata.detected_format
        ):
            request.source_format = metadata.detected_format  # type: ignore[assignment]
        elif (
            not request.is_legacy_request
            and request.source_format != "auto"
            and metadata.detected_format
            and request.source_format != metadata.detected_format
            and request.source_format != "generic"
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"source_format does not match inspected package ({metadata.detected_format})",
            )
        if (
            not request.is_legacy_request
            and request.source_format != "auto"
            and request.source_format == request.effective_target_format
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="source_format and target_format must differ",
            )
        with self._lock:
            runtime = self._runtime.get(request.job_id)
            if runtime and runtime.thread and runtime.thread.is_alive():
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Job is already running",
                )
            active_jobs = sum(
                1
                for item in self._runtime.values()
                if item.thread is not None and item.thread.is_alive()
            )
            if active_jobs >= self.settings.max_concurrent_jobs:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"At most {self.settings.max_concurrent_jobs} conversion jobs may run concurrently",
                )
            if metadata.status not in {"queued", "failed", "cancelled"}:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Job cannot be started again",
                )

            paths = self.storage.paths_for(request.job_id)
            shutil.rmtree(paths.work_dir, ignore_errors=True)
            shutil.rmtree(paths.output_dir, ignore_errors=True)
            paths.work_dir.mkdir(parents=True, exist_ok=True)
            paths.output_dir.mkdir(parents=True, exist_ok=True)
            prepare_runner_mount_permissions(paths)
            paths.logs_path.write_text("", encoding="utf-8")
            if paths.result_path.exists():
                paths.result_path.unlink()
            if paths.report_path.exists():
                paths.report_path.unlink()

            metadata.status = "queued"
            metadata.started_at = None
            metadata.finished_at = None
            metadata.exit_code = None
            metadata.error = None
            metadata.source_format = request.source_format
            metadata.target_format = request.effective_target_format
            self.storage.write_metadata(metadata)
            queued_response = JobResponse(
                id=metadata.id,
                status=metadata.status,
                created_at=metadata.created_at,
                started_at=metadata.started_at,
                finished_at=metadata.finished_at,
                exit_code=metadata.exit_code,
                download_ready=False,
                error=metadata.error,
                source_format=metadata.source_format,
                target_format=metadata.target_format,
            )

            runtime = RuntimeJob()
            thread = threading.Thread(
                target=self._run_job_with_cleanup, args=(request,), daemon=True
            )
            runtime.thread = thread
            self._runtime[request.job_id] = runtime
            thread.start()

        return queued_response

    def response(self, job_id: str) -> JobResponse:
        metadata = self.storage.read_metadata(job_id)
        paths = self.storage.paths_for(job_id)
        report_counts: dict[str, int] = {}
        if paths.report_path.is_file():
            report = self.storage.read_report(job_id)
            raw_counts = report.get("counts")
            if isinstance(raw_counts, dict):
                report_counts = {
                    name: int(raw_counts.get(name, 0))
                    for name in ("warning", "loss", "fatal")
                    if isinstance(raw_counts.get(name, 0), int)
                }
        return JobResponse(
            id=metadata.id,
            status=metadata.status,
            created_at=metadata.created_at,
            started_at=metadata.started_at,
            finished_at=metadata.finished_at,
            exit_code=metadata.exit_code,
            download_ready=metadata.status == "success" and paths.result_path.exists(),
            error=metadata.error,
            source_format=metadata.source_format,
            target_format=metadata.target_format,
            report_ready=paths.report_path.is_file(),
            report_counts=report_counts,
        )

    def cancel_or_delete(self, job_id: str) -> DeleteResponse:
        with self._lock:
            metadata = self.storage.read_metadata(job_id)
            runtime = self._runtime.get(job_id)
            if runtime and runtime.thread and runtime.thread.is_alive():
                runtime.cancel_event.set()
                runtime.delete_when_finished = True
                stop_container(self.settings, job_id)
                if metadata.status in {"queued", "running"}:
                    metadata.status = "cancelled"
                    metadata.finished_at = utc_now_iso()
                    metadata.error = "Cancelled by user"
                    self.storage.write_metadata(metadata)
                paths = self.storage.paths_for(job_id)
                if paths.result_path.exists():
                    paths.result_path.unlink()
                return DeleteResponse(id=job_id, status="cancelled", deleted=False)

            self.storage.delete_job(job_id)
            self._runtime.pop(job_id, None)
            return DeleteResponse(id=job_id, status=metadata.status, deleted=True)

    def _run_job_with_cleanup(self, request: JobRequest) -> None:
        try:
            self._run_job(request)
        finally:
            with self._lock:
                runtime = self._runtime.get(request.job_id)
                delete_when_finished = bool(
                    runtime is not None and runtime.delete_when_finished
                )
                if delete_when_finished:
                    self._runtime.pop(request.job_id, None)
                    try:
                        self.storage.delete_job(request.job_id)
                    except HTTPException as exc:
                        if exc.status_code != status.HTTP_404_NOT_FOUND:
                            raise

    def _run_job(self, request: JobRequest) -> None:
        paths = self.storage.paths_for(request.job_id)
        with self._lock:
            runtime = self._runtime[request.job_id]
            if runtime.cancel_event.is_set():
                return
            metadata = self.storage.read_metadata(request.job_id)
            if metadata.status == "cancelled":
                return
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
            with self._lock:
                runtime = self._runtime[request.job_id]
                if runtime.cancel_event.is_set():
                    return
                # shell=False is used and every request-derived argument has been validated.
                process = subprocess.Popen(  # nosec B603
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                runtime.process = process

            exit_code = self._stream_process_output(process, request.job_id, cmd)
            with self._lock:
                metadata = self.storage.read_metadata(request.job_id)
                runtime = self._runtime[request.job_id]
                if runtime.cancel_event.is_set() or metadata.status == "cancelled":
                    metadata.exit_code = exit_code
                    self.storage.write_metadata(metadata)
                    return

                metadata.exit_code = exit_code
                self.storage.write_metadata(metadata)

            report = self.storage.capture_conversion_report(request.job_id)
            if report:
                metadata = self.storage.read_metadata(request.job_id)
                if isinstance(report.get("source_format"), str):
                    metadata.source_format = str(report["source_format"])
                if isinstance(report.get("target_format"), str):
                    metadata.target_format = str(report["target_format"])
                self.storage.write_metadata(metadata)

            if exit_code != 0:
                with self._lock:
                    metadata = self.storage.read_metadata(request.job_id)
                    metadata.status = "failed"
                    metadata.finished_at = utc_now_iso()
                    metadata.error = f"Runner exited with code {exit_code}"
                    self.storage.write_metadata(metadata)
                return

            if not request.is_legacy_request:
                if report is None:
                    raise ValueError("converter did not produce a conversion report")
                if report.get("target_format") != request.effective_target_format:
                    raise ValueError(
                        "conversion report target does not match the request"
                    )
                problem_count = report.get("problem_count")
                if type(problem_count) is not int or problem_count < 1:
                    raise ValueError("converter did not produce any problem packages")
            pack_output(
                paths.output_dir,
                paths.result_path,
                target=request.effective_target_format,
                max_uncompressed_bytes=parse_size_bytes(
                    self.settings.docker_output_size
                ),
            )
            with self._lock:
                metadata = self.storage.read_metadata(request.job_id)
                runtime = self._runtime[request.job_id]
                if runtime.cancel_event.is_set() or metadata.status == "cancelled":
                    if paths.result_path.exists():
                        paths.result_path.unlink()
                    return
                metadata.status = "success"
                metadata.finished_at = utc_now_iso()
                metadata.error = None
                self.storage.write_metadata(metadata)
        except subprocess.TimeoutExpired:
            stop_container(self.settings, request.job_id)
            if process is not None:
                process.kill()
            with self._lock:
                metadata = self.storage.read_metadata(request.job_id)
                runtime = self._runtime[request.job_id]
                if runtime.cancel_event.is_set() or metadata.status == "cancelled":
                    return
                metadata.status = "failed"
                metadata.finished_at = utc_now_iso()
                metadata.error = (
                    f"Job timed out after {self.settings.job_timeout_seconds} seconds"
                )
                self.storage.append_log(request.job_id, metadata.error + "\n")
                self.storage.write_metadata(metadata)
        except Exception as exc:
            stop_container(self.settings, request.job_id)
            with self._lock:
                metadata = self.storage.read_metadata(request.job_id)
                runtime = self._runtime[request.job_id]
                if runtime.cancel_event.is_set() or metadata.status == "cancelled":
                    if paths.result_path.exists():
                        paths.result_path.unlink()
                    return
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

    def cleanup_expired(self, *, now: datetime | None = None) -> int:
        current_time = now or datetime.now(timezone.utc)
        removed = 0
        with self._lock:
            for job_id in self.storage.job_ids():
                runtime = self._runtime.get(job_id)
                if runtime and runtime.thread and runtime.thread.is_alive():
                    continue
                try:
                    metadata = self.storage.read_metadata(job_id)
                    reference_text = metadata.finished_at or metadata.created_at
                    reference_time = datetime.fromisoformat(reference_text)
                    if reference_time.tzinfo is None:
                        reference_time = reference_time.replace(tzinfo=timezone.utc)
                except (OSError, ValueError, TypeError, HTTPException):
                    continue
                if (
                    current_time - reference_time
                ).total_seconds() < self.settings.job_ttl_seconds:
                    continue
                self.storage.delete_job(job_id)
                self._runtime.pop(job_id, None)
                removed += 1
        return removed

    def _stream_process_output(
        self, process: subprocess.Popen[str], job_id: str, cmd: list[str]
    ) -> int:
        if process.stdout is None:
            raise RuntimeError("Runner stdout pipe is unavailable")
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
                    raise subprocess.TimeoutExpired(
                        cmd, self.settings.job_timeout_seconds
                    )
        finally:
            selector.close()

    def _validate_request(self, request: JobRequest) -> None:
        if request.target not in _TARGETS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"target must be one of: {', '.join(sorted(_TARGETS))}",
            )
        hydro_pid = (
            request.pid_start
            if request.is_legacy_request
            else request.options.hydro.pid_start
        )
        hydro_owner = (
            request.owner if request.is_legacy_request else request.options.hydro.owner
        )
        icpc_code = (
            request.domjudge_code_start
            if request.is_legacy_request
            else request.options.icpc.code_start
        )
        icpc_color = (
            request.domjudge_color
            if request.is_legacy_request
            else request.options.icpc.color
        )
        if request.effective_target_format == "hydro" and not _PID_RE.fullmatch(
            hydro_pid
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="pid_start must look like P1000",
            )
        if hydro_owner < 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="owner must be positive",
            )
        if request.missing_env not in {"warn", "error"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="missing_env must be warn or error",
            )
        request.tags[:] = [item.strip() for item in request.tags if item.strip()]
        request.options.hydro.tags[:] = [
            item.strip() for item in request.options.hydro.tags if item.strip()
        ]
        request.only[:] = [item.strip() for item in request.only if item.strip()]
        icpc_code = icpc_code.strip().upper()
        icpc_color = icpc_color.strip()
        if request.is_legacy_request:
            request.domjudge_code_start = icpc_code
            request.domjudge_color = icpc_color
        else:
            request.options.icpc.code_start = icpc_code
            request.options.icpc.color = icpc_color
        if request.effective_target_format == "icpc":
            if not _DOMJUDGE_CODE_RE.fullmatch(icpc_code):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="domjudge_code_start must contain letters only, for example A",
                )
            if not _HEX_COLOR_RE.fullmatch(icpc_color):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="domjudge_color must be in #RRGGBB format",
                )
            license_name = request.options.icpc.license
            rights_owner = request.options.icpc.rights_owner.strip()
            if (
                license_name
                in {
                    "cc0",
                    "cc by",
                    "cc by-sa",
                    "educational",
                    "permission",
                }
                and not rights_owner
            ):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="rights_owner is required for the selected ICPC package license",
                )
            if license_name == "public domain" and rights_owner:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="rights_owner must be empty for a public-domain ICPC package",
                )
            request.options.icpc.rights_owner = rights_owner
        if request.target == "domjudge":
            if request.domjudge_auto_validator and request.domjudge_default_validator:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="domjudge_auto_validator and domjudge_default_validator cannot both be enabled",
                )
        if not request.is_legacy_request:
            if (
                request.source_format != "auto"
                and request.source_format == request.effective_target_format
            ):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="source_format and target_format must differ",
                )
            if (
                request.source_format == "polygon"
                and request.options.polygon.validator_mode
                not in {
                    "auto",
                    "default",
                    "custom",
                }
            ):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="invalid Polygon validator mode",
                )


def _quote_command(cmd: list[str]) -> str:
    import shlex

    return " ".join(shlex.quote(part) for part in cmd)
