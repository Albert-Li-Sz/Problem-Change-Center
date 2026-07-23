from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, PrivateAttr, StringConstraints, model_validator


JobStatus = Literal["queued", "running", "success", "failed", "cancelled"]
ProgressPhase = Literal[
    "validate_archive",
    "extract",
    "detect",
    "read",
    "validate_ir",
    "write",
    "validate_output",
    "package",
]
TimeoutKind = Literal["overall", "idle", "stage", "problem"]
MissingEnvPolicy = Literal["warn", "error"]
LegacyTargetFormat = Literal[
    "hydro",
    "domjudge",
    "hydro_to_domjudge",
    "domjudge_to_hydro",
    "hoj_to_hydro",
    "hydro_to_hoj",
    "hoj_to_domjudge",
]
SourceFormat = Literal[
    "auto",
    "polygon",
    "probhub",
    "hydro",
    "icpc",
    "hoj",
    "fps",
    "qduoj",
    "uoj",
    "dmoj",
    "generic",
]
WritableFormat = Literal["hydro", "icpc", "hoj", "fps", "qduoj", "uoj", "dmoj"]
LossPolicy = Literal["warn", "error"]
PackageScope = Literal["single", "multi", "unknown"]
PackageLayout = Literal[
    "directory", "contest", "workspace", "nested", "xml", "mixed", "unknown"
]
IcpcLicense = Literal[
    "unknown",
    "public domain",
    "cc0",
    "cc by",
    "cc by-sa",
    "educational",
    "permission",
]
CommandArgument = Annotated[
    str,
    StringConstraints(min_length=1, max_length=256, pattern=r"^[^\x00-\x1f\x7f]+$"),
]


class FormatCandidate(BaseModel):
    format: str
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)


class DetectedProblem(BaseModel):
    id: str = Field(min_length=1, max_length=256)
    path: str = Field(min_length=1, max_length=1024)


class InspectResponse(BaseModel):
    job_id: str
    filename: str
    size: int
    warnings: list[str] = Field(default_factory=list)
    detected_format: str | None = None
    format_candidates: list[FormatCandidate] = Field(default_factory=list)
    package_scope: PackageScope = "unknown"
    package_layout: PackageLayout = "unknown"
    problem_count: int | None = Field(default=None, ge=0)
    problems: list[DetectedProblem] = Field(default_factory=list)
    problems_truncated: bool = False
    supported_targets: list[WritableFormat] = Field(default_factory=list)


class PolygonOptions(BaseModel):
    run_doall: bool = False
    missing_env: MissingEnvPolicy = "warn"
    with_statement: bool = False
    with_attachments: bool = False
    validator_mode: Literal["auto", "default", "custom"] = "auto"


class HydroOptions(BaseModel):
    pid_start: str = Field(default="P1000", max_length=64)
    owner: int = Field(default=1, ge=1)
    tags: list[CommandArgument] = Field(default_factory=list, max_length=100)


class IcpcOptions(BaseModel):
    code_start: str = Field(default="A", max_length=16)
    color: str = Field(default="#000000", max_length=7)
    profile: Literal["legacy-icpc", "2025-09"] = "legacy-icpc"
    license: IcpcLicense = "unknown"
    rights_owner: str = Field(
        default="", max_length=256, pattern=r"^[^\x00-\x1f\x7f]*$"
    )


class FpsOptions(BaseModel):
    profile: Literal["hustoj-1.6", "qduoj-1.2"] = "hustoj-1.6"


class ConversionOptions(BaseModel):
    polygon: PolygonOptions = Field(default_factory=PolygonOptions)
    hydro: HydroOptions = Field(default_factory=HydroOptions)
    icpc: IcpcOptions = Field(default_factory=IcpcOptions)
    fps: FpsOptions = Field(default_factory=FpsOptions)


class JobRequest(BaseModel):
    _legacy_request: bool = PrivateAttr(default=True)

    job_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    target: LegacyTargetFormat = "hydro"
    source_format: SourceFormat = "auto"
    target_format: WritableFormat | None = None
    loss_policy: LossPolicy = "warn"
    options: ConversionOptions = Field(default_factory=ConversionOptions)
    pid_start: str = Field(default="P1000", max_length=64)
    owner: int = Field(default=1, ge=1)
    tags: list[CommandArgument] = Field(default_factory=list, max_length=100)
    only: list[CommandArgument] = Field(default_factory=list, max_length=100)
    run_doall: bool = False
    missing_env: MissingEnvPolicy = "warn"
    domjudge_code_start: str = Field(default="A", max_length=16)
    domjudge_color: str = Field(default="#000000", max_length=7)
    domjudge_with_statement: bool = False
    domjudge_with_attachments: bool = False
    domjudge_auto_validator: bool = True
    domjudge_default_validator: bool = False

    @model_validator(mode="after")
    def resolve_conversion(self) -> "JobRequest":
        fields = self.model_fields_set
        legacy_supplied = "target" in fields
        matrix_supplied = (
            "source_format" in fields
            or "target_format" in fields
            or "loss_policy" in fields
            or "options" in fields
        )
        if legacy_supplied and matrix_supplied:
            raise ValueError(
                "legacy target cannot be combined with source_format, target_format, or options"
            )
        self._legacy_request = not matrix_supplied
        if self._legacy_request:
            source, target = LEGACY_TARGET_MAP[self.target]
            self.source_format = source  # type: ignore[assignment]
            self.target_format = target  # type: ignore[assignment]
        elif self.target_format is None:
            raise ValueError("target_format is required for matrix conversion requests")
        return self

    @property
    def is_legacy_request(self) -> bool:
        return self._legacy_request

    @property
    def effective_target_format(self) -> str:
        if self.target_format is None:
            raise ValueError("target_format was not resolved")
        return self.target_format


LEGACY_TARGET_MAP: dict[str, tuple[str, str]] = {
    "hydro": ("polygon", "hydro"),
    "domjudge": ("polygon", "icpc"),
    "hydro_to_domjudge": ("hydro", "icpc"),
    "domjudge_to_hydro": ("icpc", "hydro"),
    "hoj_to_hydro": ("hoj", "hydro"),
    "hydro_to_hoj": ("hydro", "hoj"),
    "hoj_to_domjudge": ("hoj", "icpc"),
}


class ReportCounts(BaseModel):
    warning: int = 0
    loss: int = 0
    fatal: int = 0


class JobProgress(BaseModel):
    phase: ProgressPhase
    current: int | None = Field(default=None, ge=0)
    total: int | None = Field(default=None, ge=0)
    unit: str | None = Field(default=None, max_length=32)
    problem: str | None = Field(default=None, max_length=256)
    detail: str | None = Field(default=None, max_length=1024)
    started_at: str
    last_activity_at: str


class JobTimeout(BaseModel):
    kind: TimeoutKind
    limit_seconds: int = Field(ge=1)
    phase: ProgressPhase | None = None
    problem: str | None = Field(default=None, max_length=256)


class JobResponse(BaseModel):
    id: str
    status: JobStatus
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    download_ready: bool = False
    error: str | None = None
    source_format: str | None = None
    target_format: str | None = None
    report_ready: bool = False
    report_counts: ReportCounts = Field(default_factory=ReportCounts)
    progress: JobProgress | None = None
    timeout: JobTimeout | None = None


class DeleteResponse(BaseModel):
    id: str
    status: JobStatus
    deleted: bool


class RepairChoice(BaseModel):
    suggestion_id: str = Field(min_length=24, max_length=24, pattern=r"^[0-9a-f]{24}$")
    candidate_path: str | None = Field(default=None, min_length=1, max_length=1024)
    upload_name: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def choose_one_source(self) -> "RepairChoice":
        if (self.candidate_path is None) == (self.upload_name is None):
            raise ValueError(
                "repair choice must select exactly one candidate_path or upload_name"
            )
        return self


class RepairRequest(BaseModel):
    selections: list[RepairChoice] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_suggestions(self) -> "RepairRequest":
        identifiers = [selection.suggestion_id for selection in self.selections]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("repair suggestion cannot be selected more than once")
        upload_names = [
            selection.upload_name
            for selection in self.selections
            if selection.upload_name is not None
        ]
        if len(upload_names) != len(set(upload_names)):
            raise ValueError("repair upload names must be unique")
        return self
