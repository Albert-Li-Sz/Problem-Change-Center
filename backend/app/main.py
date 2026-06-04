from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse

from .auth import AuthService, AuthenticatedUser
from .config import settings
from .jobs import JobManager
from .schemas import (
    AdminUpdateUserRequest,
    AdminResetPasswordRequest,
    AuthResponse,
    ChangePasswordRequest,
    DeleteResponse,
    InspectResponse,
    JobRequest,
    JobResponse,
    LoginRequest,
    RegisterResponse,
    RegisterRequest,
    UserResponse,
)
from .storage import Storage


app = FastAPI(title="Polygon Converter Web UI", version="0.2.0")
LOGGER = logging.getLogger(__name__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

storage = Storage(settings)
auth_service = AuthService(settings)
job_manager = JobManager(settings, storage, auth_service)


@app.on_event("startup")
def startup_cleanup() -> None:
    storage.cleanup_expired_jobs()
    try:
        auth_service.ensure_ready()
        job_manager.recover_interrupted_jobs()
    except Exception as exc:
        LOGGER.warning("Startup auth/job recovery skipped: %s", exc)


def user_response(user: AuthenticatedUser) -> UserResponse:
    return UserResponse(
        id=user.id,
        email=user.email,
        is_admin=user.is_admin,
        approval_status=user.approval_status,
        daily_quota=user.daily_quota,
        daily_used=user.daily_used,
        daily_pending=user.daily_pending,
        remaining_today=user.remaining_today,
        reviewed_at=user.reviewed_at,
        reviewed_by=user.reviewed_by,
        created_at=user.created_at,
    )


def current_user(authorization: str | None = Header(default=None)) -> AuthenticatedUser:
    auth_service.ensure_ready()
    return auth_service.authenticate_header(authorization)


def current_admin(user: AuthenticatedUser = Depends(current_user)) -> AuthenticatedUser:
    auth_service.require_admin(user)
    return user


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/auth/register", response_model=RegisterResponse)
def register(request: RegisterRequest) -> RegisterResponse:
    auth_service.ensure_ready()
    user = auth_service.register(request.email, request.password)
    return RegisterResponse(email=user.email, approval_status=user.approval_status)


@app.post("/api/auth/login", response_model=AuthResponse)
def login(request: LoginRequest) -> AuthResponse:
    auth_service.ensure_ready()
    user, token = auth_service.login(request.email, request.password)
    return AuthResponse(token=token, user=user_response(user))


@app.get("/api/auth/me", response_model=UserResponse)
def me(user: AuthenticatedUser = Depends(current_user)) -> UserResponse:
    return user_response(user)


@app.post("/api/auth/password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(request: ChangePasswordRequest, user: AuthenticatedUser = Depends(current_user)) -> None:
    auth_service.change_password(user, request.current_password, request.new_password)


@app.get("/api/admin/users", response_model=list[UserResponse])
def list_users(_: AuthenticatedUser = Depends(current_admin)) -> list[UserResponse]:
    return [user_response(user) for user in auth_service.list_users()]


@app.patch("/api/admin/users/{user_id}", response_model=UserResponse)
def update_user(user_id: str, request: AdminUpdateUserRequest, admin: AuthenticatedUser = Depends(current_admin)) -> UserResponse:
    user = auth_service.update_user(
        admin,
        user_id,
        daily_quota=request.daily_quota,
        daily_used=request.daily_used,
        is_admin=request.is_admin,
        approval_status=request.approval_status,
    )
    return user_response(user)


@app.post("/api/admin/users/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT)
def reset_user_password(
    user_id: str,
    request: AdminResetPasswordRequest,
    admin: AuthenticatedUser = Depends(current_admin),
) -> None:
    auth_service.reset_password(admin, user_id, request.new_password)


@app.post("/api/inspect", response_model=InspectResponse)
async def inspect(file: UploadFile = File(...), user: AuthenticatedUser = Depends(current_user)) -> InspectResponse:
    return await storage.save_upload(file, user)


@app.post("/api/jobs", response_model=JobResponse)
def start_job(request: JobRequest, user: AuthenticatedUser = Depends(current_user)) -> JobResponse:
    return job_manager.start(request, user)


@app.get("/api/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str, user: AuthenticatedUser = Depends(current_user)) -> JobResponse:
    return job_manager.response(job_id, user)


@app.get("/api/jobs/{job_id}/logs", response_class=PlainTextResponse)
def get_logs(job_id: str, user: AuthenticatedUser = Depends(current_user)) -> PlainTextResponse:
    metadata = storage.read_metadata(job_id)
    job_manager.ensure_access(metadata, user)
    return PlainTextResponse(storage.read_logs(job_id))


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str, user: AuthenticatedUser = Depends(current_user)) -> FileResponse:
    response = job_manager.response(job_id, user)
    paths = storage.paths_for(job_id)
    if response.status != "success" or not paths.result_path.exists():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Download is not ready")
    return FileResponse(paths.result_path, filename=f"polygon-convert-{job_id}.zip", media_type="application/zip")


@app.delete("/api/jobs/{job_id}", response_model=DeleteResponse)
def delete_job(job_id: str, user: AuthenticatedUser = Depends(current_user)) -> DeleteResponse:
    return job_manager.cancel_or_delete(job_id, user)
