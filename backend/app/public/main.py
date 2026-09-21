from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from urllib.parse import urlsplit

from ..jobs import JobManager, _persisted_request_payload
from ..schemas import InspectResponse, JobRequest, RepairRequest
from ..security import RequestBodyLimitMiddleware
from ..storage import JobMetadata, Storage, utc_now_iso
from .auth import Auth, password_matches
from .config import PublicConfig
from .db import Database, PostgresJobIndex
from .queue import Queue
from .worker import disk_percent


class Credentials(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(min_length=1, max_length=128)
    captcha: str = Field(default="", max_length=2048)


class Registration(Credentials):
    invitation: str = Field(min_length=1, max_length=128)
    accept_terms: bool


class EmailRequest(BaseModel):
    email: str = Field(max_length=254)
    captcha: str = Field(default="", max_length=2048)


class TokenRequest(BaseModel):
    token: str = Field(min_length=20, max_length=128)


class ResetRequest(TokenRequest):
    password: str = Field(min_length=12, max_length=128)


class PasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


class DeleteAccount(BaseModel):
    password: str = Field(min_length=1, max_length=128)


class InvitationRequest(BaseModel):
    count: int = Field(default=10, ge=1, le=100)


class UserUpdate(BaseModel):
    banned: bool | None = None
    daily_minutes: int | None = Field(default=None, ge=1, le=1440)


class SiteUpdate(BaseModel):
    registration_open: bool
    queue_open: bool


def create_app(config: PublicConfig | None = None) -> FastAPI:
    config = config or PublicConfig.from_env()
    db = Database(config.database_url)
    auth, queue = Auth(db, config), Queue(db, config)

    async def housekeeping():
        while True:
            try:
                await run_in_threadpool(clean_auth)
            except Exception:
                logging.getLogger(__name__).exception("account_cleanup_failed")
            await asyncio.sleep(60)

    def clean_auth():
        with db.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at<now()")
            conn.execute("DELETE FROM auth_tokens WHERE expires_at<now()")
            conn.execute(
                "DELETE FROM rate_limits WHERE expires_at<now()-interval '1 hour'"
            )
            conn.execute("DELETE FROM audit WHERE created_at<now()-interval '30 days'")
            conn.execute("DELETE FROM usage WHERE day<current_date-30")
            conn.execute(
                "DELETE FROM users WHERE (deleted_at IS NOT NULL OR (NOT verified AND created_at<now()-interval '48 hours')) AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.user_id=users.id)"
            )

    @asynccontextmanager
    async def lifespan(app):
        config.validate_api()
        db.open()
        if config.development:
            db.migrate()
        else:
            db.one("SELECT 1 FROM schema_migrations LIMIT 1")
        app.state.storage = Storage(
            config.storage_settings(), job_index=PostgresJobIndex(db), backfill=False
        )
        task = asyncio.create_task(housekeeping())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            db.close()

    app = FastAPI(
        title="题包转换公益站",
        version="0.7.0-public",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = config.storage_settings()
    app.state.db, app.state.auth, app.state.queue = db, auth, queue
    app.add_middleware(RequestBodyLimitMiddleware)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[
            urlsplit(config.origin).hostname,
            "localhost",
            "127.0.0.1",
            "testserver",
        ],
    )

    anonymous = {
        "/api/health/live",
        "/api/public/config",
        "/api/auth/register",
        "/api/auth/login",
        "/api/auth/verify",
        "/api/auth/resend",
        "/api/auth/forgot",
        "/api/auth/reset",
    }

    @app.middleware("http")
    async def protect(request: Request, call_next):
        upload_id = None
        try:
            if (
                request.method not in {"GET", "HEAD", "OPTIONS"}
                and request.headers.get("origin") != config.origin
            ):
                raise HTTPException(403, "仅允许本站发起操作")
            if request.url.path not in anonymous:
                request.state.user = await run_in_threadpool(auth.current, request)
            if request.url.path != "/api/health/live":
                ip = request.client.host if request.client else "unknown"
                await run_in_threadpool(db.rate_limit, f"http:{ip}", 600, 60)
            if request.method == "POST" and (
                request.url.path == "/api/inspect"
                or request.url.path.endswith("/repairs")
            ):
                if await run_in_threadpool(disk_percent, config.data_dir) >= 80:
                    raise HTTPException(507, "磁盘空间紧张，暂时停止上传")
                await run_in_threadpool(
                    db.rate_limit, f"upload:{request.state.user['id']}", 12, 60
                )
                # Reserve before Starlette parses/spools the multipart body.
                # This bounds temporary storage across both API processes.
                upload_id = await run_in_threadpool(
                    queue.create_upload, request.state.user["id"], "upload.zip"
                )
                request.state.upload_id = upload_id
            response = await call_next(request)
        except HTTPException as exc:
            response = JSONResponse(
                {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
            )
        finally:
            if upload_id:
                await run_in_threadpool(queue.abandon_upload, upload_id)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
            }
        )
        return response

    def storage() -> Storage:
        return app.state.storage

    def user(request):
        return request.state.user

    def admin(request):
        actor = user(request)
        if not actor["is_admin"]:
            raise HTTPException(403, "需要管理员权限")
        return actor

    def public_user(row):
        return {
            name: row[name]
            for name in (
                "id",
                "email",
                "is_admin",
                "verified",
                "banned",
                "daily_minutes",
                "created_at",
            )
        }

    def auth_limit(request, email, action, captcha):
        ip = request.client.host if request.client else "unknown"
        db.rate_limit(f"auth:{action}:ip:{ip}", 10, 600)
        db.rate_limit(f"auth:{action}:email:{email.strip().lower()}", 5, 600)
        auth.captcha(captcha, ip)

    def job_response(row):
        value = (
            JobManager(config.storage_settings(), storage())
            .response(row["id"])
            .model_dump(mode="json")
        )
        value.update(
            status="queued"
            if row["status"] in {"uploading", "uploaded"}
            else row["status"],
            error=row["error"],
            queue_position=queue.position(row),
            expires_at=row["expires_at"],
            billed_minutes=row["billed_minutes"],
            reserved_minutes=row["reserved_minutes"],
            filename=row["filename"],
            kind=row["kind"],
            lifecycle=row["status"],
            download_ready=row["status"] == "success"
            and storage().paths_for(row["id"]).result_path.is_file(),
        )
        return value

    @app.get("/api/health/live")
    def live():
        return {"status": "ok", "public_mode": True}

    @app.get("/api/public/config")
    def public_config():
        return {
            "turnstile_site_key": config.turnstile_site_key,
            "development": config.development,
            "contact_email": config.contact_email,
            **db.one("SELECT registration_open FROM site_settings WHERE id=1"),
        }

    @app.get("/api/health/ready")
    def ready(request: Request):
        admin(request)
        worker = db.one(
            "SELECT *,heartbeat_at>now()-interval '30 seconds' AS healthy FROM worker_status WHERE id='single'"
        )
        return JSONResponse(
            {"status": "ready" if worker and worker["healthy"] else "not_ready"},
            status_code=200 if worker and worker["healthy"] else 503,
        )

    @app.post("/api/auth/register", status_code=202)
    def register(payload: Registration, request: Request):
        if not payload.accept_terms:
            raise HTTPException(422, "请先阅读并同意服务条款与隐私说明")
        auth_limit(request, payload.email, "register", payload.captcha)
        auth.register(payload.email, payload.password, payload.invitation)
        return {
            "message": "如果邮箱与邀请码有效，您将收到验证邮件。已有账号请直接登录。"
        }

    @app.post("/api/auth/login")
    def login(payload: Credentials, request: Request, response: Response):
        auth_limit(request, payload.email, "login", payload.captcha)
        row = auth.login(payload.email, payload.password, response)
        return public_user(row)

    @app.post("/api/auth/verify")
    def verify(payload: TokenRequest):
        auth.consume_token(payload.token, "verify")
        return {"message": "邮箱验证成功，请登录"}

    @app.post("/api/auth/resend", status_code=202)
    def resend(payload: EmailRequest, request: Request):
        auth_limit(request, payload.email, "mail", payload.captcha)
        auth.request_email(payload.email, "verify")
        return {"message": "如账号符合条件，验证邮件将发送至该邮箱"}

    @app.post("/api/auth/forgot", status_code=202)
    def forgot(payload: EmailRequest, request: Request):
        auth_limit(request, payload.email, "mail", payload.captcha)
        auth.request_email(payload.email, "reset")
        return {"message": "如账号符合条件，重置邮件将发送至该邮箱"}

    @app.post("/api/auth/reset")
    def reset(payload: ResetRequest):
        auth.consume_token(payload.token, "reset", payload.password)
        return {"message": "密码已重置，请重新登录"}

    @app.get("/api/auth/me")
    @app.get("/api/account")
    def me(request: Request):
        return {**public_user(user(request)), "quota": queue.quota(user(request)["id"])}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response):
        auth.logout(request, response)
        return {"message": "已退出"}

    @app.post("/api/auth/password")
    def change_password(payload: PasswordRequest, request: Request, response: Response):
        auth.change_password(
            user(request), payload.current_password, payload.new_password
        )
        auth.logout(request, response)
        return {"message": "密码已修改，所有设备均已退出，请重新登录"}

    @app.delete("/api/account", status_code=202)
    def delete_account(payload: DeleteAccount, request: Request, response: Response):
        actor = user(request)
        if actor["is_admin"]:
            raise HTTPException(409, "管理员账号请先由运营方移交权限")
        if not password_matches(actor["password_hash"], payload.password):
            raise HTTPException(400, "密码错误")
        with db.connect() as conn:
            Queue.lock(conn)
            conn.execute(
                "UPDATE users SET banned=true,deleted_at=now() WHERE id=%s",
                (actor["id"],),
            )
            conn.execute("DELETE FROM sessions WHERE user_id=%s", (actor["id"],))
            conn.execute("DELETE FROM auth_tokens WHERE user_id=%s", (actor["id"],))
        for row in db.all("SELECT id FROM jobs WHERE user_id=%s", (actor["id"],)):
            queue.cancel(row["id"], delete=True)
        auth.logout(request, response)
        db.audit(actor["id"], "account_deleted")
        return {"message": "账号已停用，文件正在清理；安全审计记录最多保留 30 天"}

    @app.post("/api/inspect", status_code=202)
    async def inspect(request: Request, file: UploadFile = File(...)):
        filename = Path(file.filename or "").name[:255]
        if not filename.lower().endswith(".zip") or "\x00" in filename:
            raise HTTPException(400, "请上传 ZIP 题包")
        uid = user(request)["id"]
        job_id = request.state.upload_id
        await run_in_threadpool(
            db.execute, "UPDATE jobs SET filename=%s WHERE id=%s", (filename, job_id)
        )
        paths = storage().paths_for(job_id)
        size, checksum = 0, hashlib.sha256()
        try:
            paths.input_dir.mkdir(parents=True, mode=0o700)
            paths.work_dir.mkdir(mode=0o700)
            paths.output_dir.mkdir(mode=0o700)
            paths.logs_path.touch(mode=0o600)
            with paths.upload_path.open("xb") as dest:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > config.max_upload:
                        raise HTTPException(413, "单次上传最多 512 MiB")
                    checksum.update(chunk)
                    await run_in_threadpool(dest.write, chunk)
            if not size:
                raise HTTPException(400, "题包不能为空")
            await run_in_threadpool(
                storage().write_metadata,
                JobMetadata(
                    id=job_id,
                    filename=filename,
                    size=size,
                    status="queued",
                    created_at=utc_now_iso(),
                ),
            )
            await run_in_threadpool(
                queue.submit, job_id, uid, kind="inspect", size=size
            )
            return {
                "job_id": job_id,
                "status": "queued",
                "sha256": checksum.hexdigest(),
            }
        except Exception:
            await run_in_threadpool(queue.cancel, job_id, delete=True)
            raise
        finally:
            await file.close()

    @app.get("/api/inspections/{job_id}")
    def inspection(job_id: str, request: Request):
        row = queue.owned(job_id, user(request)["id"])
        if (
            row["status"] in {"uploading", "queued", "running"}
            and row["kind"] == "inspect"
        ):
            return {"job_id": job_id, "status": row["status"]}
        if row["kind"] == "inspect" and row["status"] != "uploaded":
            raise HTTPException(422, row["error"] or "题包检查失败")
        metadata = storage().read_metadata(job_id)
        return InspectResponse.model_validate(
            {**metadata.__dict__, "job_id": job_id}
        ).model_dump()

    def submit_request(payload: JobRequest, request: Request):
        uid = user(request)["id"]
        queue.owned(payload.job_id, uid)
        if payload.is_legacy_request:
            raise HTTPException(422, "公众版请使用 source_format / target_format")
        JobManager(config.storage_settings(), storage())._validate_request(payload)
        metadata = storage().read_metadata(payload.job_id)
        if payload.source_format == "auto" and metadata.detected_format:
            payload.source_format = metadata.detected_format
        if payload.source_format == payload.target_format:
            raise HTTPException(422, "输入和输出格式必须不同")
        persisted = _persisted_request_payload(payload)
        # The queue is the authoritative request. Worker writes the repair
        # replay file only after claiming it, preventing duplicate-submit races.
        queue.submit(payload.job_id, uid, kind="convert", payload=persisted)
        return job_response(queue.owned(payload.job_id, uid))

    @app.post("/api/jobs", status_code=202)
    def start(payload: JobRequest, request: Request):
        db.rate_limit(f"submit:{user(request)['id']}", 20, 60)
        return submit_request(payload, request)

    @app.get("/api/jobs")
    def jobs(request: Request, offset: int = 0):
        rows = db.all(
            "SELECT * FROM jobs WHERE user_id=%s AND NOT delete_requested AND expires_at>now() ORDER BY created_at DESC LIMIT 20 OFFSET %s",
            (user(request)["id"], max(0, offset)),
        )
        return {
            "items": [job_response(r) for r in rows if r["status"] != "uploading"],
            "quota": queue.quota(user(request)["id"]),
        }

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str, request: Request):
        return job_response(queue.owned(job_id, user(request)["id"]))

    @app.get("/api/jobs/{job_id}/events")
    async def events(job_id: str, request: Request):
        await run_in_threadpool(queue.owned, job_id, user(request)["id"])

        async def stream():
            cursor = request.query_params.get(
                "cursor", request.headers.get("last-event-id", "0:0")
            )
            try:
                offset = max(0, int(cursor.split(":")[-1]))
            except ValueError:
                offset = 0
            while not await request.is_disconnected():
                try:
                    await run_in_threadpool(auth.current, request)
                    row = await run_in_threadpool(
                        queue.owned, job_id, user(request)["id"]
                    )
                    value = await run_in_threadpool(job_response, row)
                    text, next_offset, reset = await run_in_threadpool(
                        storage().read_log_chunk, job_id, offset
                    )
                    event_id = f"0:{next_offset}"
                    yield f"id: {event_id}\nevent: job\ndata: {json.dumps(value, default=str)}\n\n"
                    if text or reset:
                        data = {
                            "text": text,
                            "offset": 0 if reset else offset,
                            "next_offset": next_offset,
                            "reset": reset,
                        }
                        yield f"id: {event_id}\nevent: logs\ndata: {json.dumps(data)}\n\n"
                    offset = next_offset
                    if value["report_ready"]:
                        report = await run_in_threadpool(storage().read_report, job_id)
                        yield f"event: report\ndata: {json.dumps(report)}\n\n"
                    if row["status"] in {"success", "failed", "cancelled", "uploaded"}:
                        if (
                            next_offset
                            >= storage().paths_for(job_id).logs_path.stat().st_size
                        ):
                            return
                        continue
                except HTTPException:
                    return
                await asyncio.sleep(2)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "X-Accel-Buffering": "no",
                "Cache-Control": "no-cache, no-transform",
            },
        )

    @app.get("/api/jobs/{job_id}/logs", response_class=PlainTextResponse)
    def logs(job_id: str, request: Request):
        queue.owned(job_id, user(request)["id"])
        return storage().read_logs(job_id)

    @app.get("/api/jobs/{job_id}/report")
    @app.get("/api/jobs/{job_id}/repairs")
    def report(job_id: str, request: Request):
        queue.owned(job_id, user(request)["id"])
        return storage().read_report(job_id)

    @app.post("/api/jobs/{job_id}/repairs", status_code=202)
    async def repairs(
        job_id: str,
        request: Request,
        plan: str = Form(...),
        files: list[UploadFile] | None = File(default=None),
    ):
        uid = user(request)["id"]
        await run_in_threadpool(queue.owned, job_id, uid)
        await run_in_threadpool(db.rate_limit, f"repair:{uid}", 5, 60)
        try:
            repair = RepairRequest.model_validate_json(plan)
        except ValidationError as exc:
            raise HTTPException(422, "修复计划不正确") from exc
        reserved_id = request.state.upload_id
        derived_id = None
        try:
            derived_id, payload = await storage().create_repair_job(
                job_id, repair, files or []
            )
            with db.connect() as conn:
                conn.execute(
                    "UPDATE jobs SET id=%s,status='uploaded',filename=%s WHERE id=%s",
                    (
                        derived_id,
                        storage().read_metadata(derived_id).filename,
                        reserved_id,
                    ),
                )
            return await run_in_threadpool(
                submit_request, JobRequest.model_validate(payload), request
            )
        except Exception:
            row = await run_in_threadpool(
                db.one,
                "SELECT id FROM jobs WHERE id IN (%s,%s)",
                (derived_id, reserved_id),
            )
            if row:
                await run_in_threadpool(queue.cancel, row["id"], delete=True)
            if derived_id and (not row or row["id"] != derived_id):
                await run_in_threadpool(storage().delete_job, derived_id)
            raise

    @app.get("/api/jobs/{job_id}/download")
    def download(job_id: str, request: Request):
        row = queue.owned(job_id, user(request)["id"])
        path = storage().paths_for(job_id).result_path
        if row["status"] != "success" or not path.is_file():
            raise HTTPException(409, "结果尚未生成")
        return FileResponse(
            path, filename=f"oj-package-{job_id}.zip", media_type="application/zip"
        )

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str, request: Request):
        queue.owned(job_id, user(request)["id"])
        queue.cancel(job_id)
        return {"id": job_id, "status": "cancelled", "deleted": False}

    @app.delete("/api/jobs/{job_id}", status_code=202)
    def delete_job(job_id: str, request: Request):
        queue.owned(job_id, user(request)["id"])
        queue.cancel(job_id, delete=True)
        return {"id": job_id, "status": "cancelled", "deleted": True}

    @app.get("/api/admin/users")
    def users(request: Request, offset: int = 0):
        admin(request)
        return [
            public_user(r)
            for r in db.all(
                "SELECT * FROM users ORDER BY created_at DESC LIMIT 100 OFFSET %s",
                (max(0, offset),),
            )
        ]

    @app.patch("/api/admin/users/{user_id}")
    def update_user(user_id: str, payload: UserUpdate, request: Request):
        actor = admin(request)
        row = db.one("SELECT * FROM users WHERE id=%s", (user_id,))
        if not row:
            raise HTTPException(404, "用户不存在")
        if row["is_admin"] and payload.banned:
            raise HTTPException(409, "不能封禁管理员")
        db.execute(
            "UPDATE users SET banned=COALESCE(%s,banned),daily_minutes=COALESCE(%s,daily_minutes) WHERE id=%s",
            (payload.banned, payload.daily_minutes, user_id),
        )
        if payload.banned:
            db.execute("DELETE FROM sessions WHERE user_id=%s", (user_id,))
            for row in db.all(
                "SELECT id FROM jobs WHERE user_id=%s AND status IN ('queued','running','uploading')",
                (user_id,),
            ):
                queue.cancel(row["id"])
        db.audit(actor["id"], "user_updated", user_id)
        return {"message": "已更新"}

    @app.get("/api/admin/invitations")
    def invitations(request: Request):
        admin(request)
        return db.all(
            "SELECT id,created_at,expires_at,revoked,used_at FROM invitations ORDER BY created_at DESC LIMIT 100"
        )

    @app.post("/api/admin/invitations")
    def new_invitations(payload: InvitationRequest, request: Request):
        actor = admin(request)
        result = auth.invitations(payload.count)
        db.audit(actor["id"], "invitations_created", str(payload.count))
        return {"codes": result}

    @app.delete("/api/admin/invitations/{invite_id}")
    def revoke_invitation(invite_id: str, request: Request):
        actor = admin(request)
        db.execute("UPDATE invitations SET revoked=true WHERE id=%s", (invite_id,))
        db.audit(actor["id"], "invitation_revoked", invite_id)
        return {"message": "已撤销"}

    @app.get("/api/admin/jobs")
    def all_jobs(request: Request, offset: int = 0):
        admin(request)
        return db.all(
            "SELECT id,user_id,status,kind,wine,created_at,error FROM jobs ORDER BY created_at DESC LIMIT 100 OFFSET %s",
            (max(0, offset),),
        )

    @app.post("/api/admin/jobs/{job_id}/cancel")
    def admin_cancel(job_id: str, request: Request):
        actor = admin(request)
        queue.cancel(job_id)
        db.audit(actor["id"], "job_cancelled", job_id)
        return {"message": "终止请求已提交"}

    @app.get("/api/admin/status")
    def admin_status(request: Request):
        admin(request)
        return {
            **db.one(
                "SELECT registration_open,queue_open FROM site_settings WHERE id=1"
            ),
            "worker": db.one("SELECT * FROM worker_status WHERE id='single'"),
            "audit": db.all("SELECT * FROM audit ORDER BY id DESC LIMIT 50"),
        }

    @app.patch("/api/admin/settings")
    def settings(payload: SiteUpdate, request: Request):
        actor = admin(request)
        db.execute(
            "UPDATE site_settings SET registration_open=%s,queue_open=%s WHERE id=1",
            (payload.registration_open, payload.queue_open),
        )
        db.audit(actor["id"], "site_settings_updated", json.dumps(payload.model_dump()))
        return payload

    return app
