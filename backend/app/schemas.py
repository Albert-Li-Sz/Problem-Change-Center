from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


JobStatus = Literal["queued", "running", "success", "failed", "cancelled"]
MissingEnvPolicy = Literal["warn", "error"]
TargetFormat = Literal["hydro", "domjudge", "hydro_to_domjudge"]


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
