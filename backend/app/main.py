from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import ValidationError

from .config import settings
from .jobs import JobManager
from .schemas import (
    DeleteResponse,
    InspectResponse,
    JobRequest,
    JobResponse,
    RepairRequest,
)
from .security import (
    InMemoryRateLimiter,
    RequestBodyLimitMiddleware,
    enforce_request_security,
    readiness_status,
)
from .storage import Storage


@asynccontextmanager
async def lifespan(application: FastAPI):  # type: ignore[no-untyped-def]
    manager = application.state.job_manager
    manager.recover_interrupted()
    try:
        yield
    finally:
        manager.shutdown()


app = FastAPI(
    title="OJ Package Converter",
    version="0.6.0",
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.allowed_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Accept", "Content-Type", "X-Request-ID"],
)
app.add_middleware(RequestBodyLimitMiddleware)

storage = Storage(settings)
job_manager = JobManager(settings, storage)
app.state.settings = settings
app.state.storage = storage
app.state.job_manager = job_manager
app.state.rate_limiter = InMemoryRateLimiter()


@app.middleware("http")
async def cleanup_expired_jobs(request, call_next):  # type: ignore[no-untyped-def]
    if not request.url.path.startswith("/api/health"):
        request.app.state.job_manager.cleanup_expired()
    return await call_next(request)


@app.middleware("http")
async def request_security(request, call_next):  # type: ignore[no-untyped-def]
    return await enforce_request_security(request, call_next)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/health/live")
def health_live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/health/ready", response_model=None)
def health_ready() -> JSONResponse:
    ready, checks = readiness_status(app.state.settings)
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "not_ready", "checks": checks},
    )


@app.post("/api/inspect", response_model=InspectResponse)
async def inspect(file: UploadFile = File(...)) -> InspectResponse:
    return await app.state.storage.save_upload(file)


@app.post("/api/jobs", response_model=JobResponse)
def start_job(request: JobRequest) -> JobResponse:
    return app.state.job_manager.start(request)


@app.get("/api/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str) -> JobResponse:
    return app.state.job_manager.response(job_id)


@app.get("/api/jobs/{job_id}/logs", response_class=PlainTextResponse)
def get_logs(job_id: str) -> PlainTextResponse:
    return PlainTextResponse(app.state.storage.read_logs(job_id))


@app.get("/api/jobs/{job_id}/report")
def get_report(job_id: str) -> dict[str, object]:
    return app.state.storage.read_report(job_id)


@app.get("/api/jobs/{job_id}/repairs")
def get_repairs(job_id: str) -> dict[str, object]:
    report = app.state.storage.read_report(job_id)
    return {
        "repair_ready": bool(report.get("repair_ready")),
        "repair_suggestions": report.get("repair_suggestions", []),
        "applied_repairs": report.get("applied_repairs", []),
    }


@app.post("/api/jobs/{job_id}/repairs", response_model=JobResponse)
async def apply_repairs(
    job_id: str,
    plan: str = Form(...),
    files: list[UploadFile] | None = File(default=None),
) -> JobResponse:
    try:
        repair_request = RepairRequest.model_validate_json(plan)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=exc.errors(),
        ) from exc
    derived_job_id, request_payload = await app.state.storage.create_repair_job(
        job_id, repair_request, files or []
    )
    try:
        request = JobRequest.model_validate(request_payload)
    except ValidationError as exc:
        app.state.storage.delete_job(derived_job_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stored conversion request cannot be replayed",
        ) from exc
    try:
        return app.state.job_manager.start(request)
    except Exception:
        app.state.storage.delete_job(derived_job_id)
        raise


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str) -> FileResponse:
    response = app.state.job_manager.response(job_id)
    paths = app.state.storage.paths_for(job_id)
    if response.status != "success" or not paths.result_path.exists():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Download is not ready"
        )
    return FileResponse(
        paths.result_path,
        filename=f"oj-package-convert-{job_id}.zip",
        media_type="application/zip",
    )


@app.delete("/api/jobs/{job_id}", response_model=DeleteResponse)
def delete_job(job_id: str) -> DeleteResponse:
    return app.state.job_manager.cancel_or_delete(job_id)


@app.post("/api/jobs/{job_id}/cancel", response_model=DeleteResponse)
def cancel_job(job_id: str) -> DeleteResponse:
    return app.state.job_manager.cancel(job_id)
