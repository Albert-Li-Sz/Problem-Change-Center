from __future__ import annotations

from fastapi import FastAPI, File, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse

from .config import settings
from .jobs import JobManager
from .schemas import DeleteResponse, InspectResponse, JobRequest, JobResponse
from .storage import Storage


app = FastAPI(title="Polygon2Hydro Web UI", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

storage = Storage(settings)
job_manager = JobManager(settings, storage)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/inspect", response_model=InspectResponse)
async def inspect(file: UploadFile = File(...)) -> InspectResponse:
    return await storage.save_upload(file)


@app.post("/api/jobs", response_model=JobResponse)
def start_job(request: JobRequest) -> JobResponse:
    return job_manager.start(request)


@app.get("/api/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str) -> JobResponse:
    return job_manager.response(job_id)


@app.get("/api/jobs/{job_id}/logs", response_class=PlainTextResponse)
def get_logs(job_id: str) -> PlainTextResponse:
    return PlainTextResponse(storage.read_logs(job_id))


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str) -> FileResponse:
    response = job_manager.response(job_id)
    paths = storage.paths_for(job_id)
    if response.status != "success" or not paths.result_path.exists():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Download is not ready")
    return FileResponse(paths.result_path, filename=f"p2h-{job_id}.zip", media_type="application/zip")


@app.delete("/api/jobs/{job_id}", response_model=DeleteResponse)
def delete_job(job_id: str) -> DeleteResponse:
    return job_manager.cancel_or_delete(job_id)
