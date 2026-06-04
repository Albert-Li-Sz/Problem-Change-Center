from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


JobStatus = Literal["queued", "running", "success", "failed", "cancelled"]
MissingEnvPolicy = Literal["warn", "error"]
TargetFormat = Literal["hydro", "domjudge", "hydro_to_domjudge"]
ApprovalStatus = Literal["pending", "approved", "rejected"]


class InspectResponse(BaseModel):
    job_id: str
    filename: str
    size: int
    warnings: list[str] = Field(default_factory=list)


class JobRequest(BaseModel):
    job_id: str
    target: TargetFormat = "hydro"
    pid_start: str = "P1000"
    owner: int = Field(default=1, ge=1)
    tags: list[str] = Field(default_factory=list)
    only: list[str] = Field(default_factory=list)
    run_doall: bool = False
    missing_env: MissingEnvPolicy = "warn"
    domjudge_code_start: str = "A"
    domjudge_color: str = "#000000"
    domjudge_with_statement: bool = False
    domjudge_with_attachments: bool = False
    domjudge_auto_validator: bool = True
    domjudge_default_validator: bool = False


class JobResponse(BaseModel):
    id: str
    user_id: str | None = None
    status: JobStatus
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    download_ready: bool = False
    error: str | None = None


class DeleteResponse(BaseModel):
    id: str
    status: JobStatus
    deleted: bool


class RegisterRequest(BaseModel):
    email: str
    password: str


class RegisterResponse(BaseModel):
    email: str
    approval_status: ApprovalStatus = "pending"


class LoginRequest(BaseModel):
    email: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class UserResponse(BaseModel):
    id: str
    email: str
    is_admin: bool
    approval_status: ApprovalStatus
    daily_quota: int
    daily_used: int
    daily_pending: int = 0
    remaining_today: int | None = None
    reviewed_at: str | None = None
    reviewed_by: str | None = None
    created_at: str


class AuthResponse(BaseModel):
    token: str
    user: UserResponse


class AdminUpdateUserRequest(BaseModel):
    daily_quota: int | None = Field(default=None, ge=0)
    daily_used: int | None = Field(default=None, ge=0)
    is_admin: bool | None = None
    approval_status: ApprovalStatus | None = None


class AdminResetPasswordRequest(BaseModel):
    new_password: str
