from __future__ import annotations

import hashlib
import hmac
import re
import shutil

# Subprocess calls below use fixed argv and an administrator-configured executable.
import subprocess  # nosec B404
import tempfile
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request, Response, status
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import Settings


_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_RATE_LIMIT_EXEMPT_PATHS = {"/api/health/live"}
_AUTH_EXEMPT_PATHS = {"/api/health/live"}


class RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    """Enforce a byte limit for fixed-length and streamed HTTP request bodies."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        settings = _scope_settings(scope)
        limit = settings.max_request_body_bytes
        content_lengths = [
            value
            for name, value in scope.get("headers", [])
            if name.lower() == b"content-length"
        ]
        if len(content_lengths) > 1:
            await _json_asgi_response(
                scope,
                receive,
                send,
                status.HTTP_400_BAD_REQUEST,
                "Multiple Content-Length headers are not allowed",
            )
            return
        if content_lengths:
            try:
                declared_length = int(content_lengths[0].decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                await _json_asgi_response(
                    scope,
                    receive,
                    send,
                    status.HTTP_400_BAD_REQUEST,
                    "Invalid Content-Length header",
                )
                return
            if declared_length < 0:
                await _json_asgi_response(
                    scope,
                    receive,
                    send,
                    status.HTTP_400_BAD_REQUEST,
                    "Invalid Content-Length header",
                )
                return
            if declared_length > limit:
                await _json_asgi_response(
                    scope,
                    receive,
                    send,
                    status.HTTP_413_CONTENT_TOO_LARGE,
                    f"Request body exceeds the configured {limit}-byte limit",
                )
                return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise RequestBodyTooLarge
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except RequestBodyTooLarge:
            if response_started:
                raise
            await _json_asgi_response(
                scope,
                receive,
                send,
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"Request body exceeds the configured {limit}-byte limit",
            )


@dataclass
class _RateBucket:
    window_started: float
    count: int
    last_seen: float


class InMemoryRateLimiter:
    """Small bounded fixed-window limiter for the single production worker."""

    def __init__(self, *, max_clients: int = 10_000) -> None:
        self.max_clients = max_clients
        self._lock = threading.Lock()
        self._buckets: dict[tuple[str, str], _RateBucket] = {}

    def check(
        self,
        identity: str,
        category: str,
        limit: int,
        *,
        now: float | None = None,
    ) -> tuple[bool, int]:
        current = time.monotonic() if now is None else now
        key = (identity, category)
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None or current - bucket.window_started >= 60:
                self._buckets[key] = _RateBucket(current, 1, current)
                self._prune(current)
                return True, 0
            bucket.last_seen = current
            if bucket.count >= limit:
                retry_after = max(1, int(60 - (current - bucket.window_started)) + 1)
                return False, retry_after
            bucket.count += 1
            return True, 0

    def _prune(self, now: float) -> None:
        if len(self._buckets) <= self.max_clients:
            return
        expired = [
            key
            for key, bucket in self._buckets.items()
            if now - bucket.last_seen >= 120
        ]
        for key in expired:
            self._buckets.pop(key, None)
        if len(self._buckets) <= self.max_clients:
            return
        excess = len(self._buckets) - self.max_clients
        oldest = sorted(self._buckets, key=lambda key: self._buckets[key].last_seen)[
            :excess
        ]
        for key in oldest:
            self._buckets.pop(key, None)


async def enforce_request_security(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    settings = _request_settings(request)
    request_id = _request_id(request)
    request.state.request_id = request_id

    host = _request_hostname(request)
    if host is None or not _host_allowed(host, settings.allowed_hosts):
        return _secured_error(
            settings,
            request_id,
            request.url.path,
            status.HTTP_400_BAD_REQUEST,
            "Untrusted Host header",
        )

    if (
        settings.is_external
        and request.method != "OPTIONS"
        and request.url.path not in _AUTH_EXEMPT_PATHS
        and not _valid_access_key(request, settings)
    ):
        limiter = _request_rate_limiter(request)
        allowed, retry_after = limiter.check(
            _client_identity(request),
            "auth_failure",
            settings.rate_limit_auth_failures_per_minute,
        )
        if not allowed:
            response = _secured_error(
                settings,
                request_id,
                request.url.path,
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many invalid access key attempts",
            )
            response.headers["Retry-After"] = str(retry_after)
            return response
        response = _secured_error(
            settings,
            request_id,
            request.url.path,
            status.HTTP_401_UNAUTHORIZED,
            "Invalid or missing access key",
        )
        response.headers["WWW-Authenticate"] = "P2HAccessKey"
        return response

    if settings.is_external and request.method in _UNSAFE_METHODS:
        origin_error = _unsafe_origin_error(request, settings)
        if origin_error is not None:
            return _secured_error(
                settings,
                request_id,
                request.url.path,
                status.HTTP_403_FORBIDDEN,
                origin_error,
            )

    if settings.is_external and request.url.path not in _RATE_LIMIT_EXEMPT_PATHS:
        limiter = _request_rate_limiter(request)
        identity = _client_identity(request)
        category = (
            "upload"
            if request.method == "POST"
            and (
                request.url.path == "/api/inspect"
                or request.url.path.endswith("/repairs")
            )
            else "request"
        )
        limit = (
            settings.rate_limit_uploads_per_minute
            if category == "upload"
            else settings.rate_limit_requests_per_minute
        )
        allowed, retry_after = limiter.check(identity, category, limit)
        if not allowed:
            response = _secured_error(
                settings,
                request_id,
                request.url.path,
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Rate limit exceeded",
            )
            response.headers["Retry-After"] = str(retry_after)
            return response

    response = await call_next(request)
    _apply_security_headers(response, settings, request_id, request.url.path)
    return response


def readiness_status(settings: Settings) -> tuple[bool, dict[str, str]]:
    checks: dict[str, str] = {}
    jobs_dir = settings.data_dir / "jobs"
    try:
        jobs_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=".readiness-", dir=jobs_dir, delete=True
        ) as probe:
            probe.write(b"ok")
            probe.flush()
        checks["storage"] = "ok"
    except OSError:
        checks["storage"] = "unavailable"

    executable = (
        settings.docker_bin
        if Path(settings.docker_bin).is_absolute()
        else shutil.which(settings.docker_bin)
    )
    if not executable:
        checks["docker"] = "unavailable"
        checks["runner_image"] = "unchecked"
        return False, checks

    try:
        daemon = subprocess.run(  # nosec B603
            [executable, "info", "--format", "{{.ServerVersion}}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=settings.readiness_timeout_seconds,
        )
        checks["docker"] = "ok" if daemon.returncode == 0 else "unavailable"
    except (OSError, subprocess.TimeoutExpired):
        checks["docker"] = "unavailable"

    if checks["docker"] != "ok":
        checks["runner_image"] = "unchecked"
        return False, checks

    try:
        image = subprocess.run(  # nosec B603
            [executable, "image", "inspect", settings.runner_image],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=settings.readiness_timeout_seconds,
        )
        checks["runner_image"] = "ok" if image.returncode == 0 else "missing"
    except (OSError, subprocess.TimeoutExpired):
        checks["runner_image"] = "unavailable"
    return all(value == "ok" for value in checks.values()), checks


def _request_settings(request: Request) -> Settings:
    value = getattr(request.app.state, "settings", None)
    if isinstance(value, Settings):
        return value
    raise RuntimeError("Application settings are unavailable")


def _scope_settings(scope: Scope) -> Settings:
    application = scope.get("app")
    value = getattr(getattr(application, "state", None), "settings", None)
    if isinstance(value, Settings):
        return value
    raise RuntimeError("Application settings are unavailable")


def _request_id(request: Request) -> str:
    supplied = request.headers.get("x-request-id", "")
    if _REQUEST_ID_RE.fullmatch(supplied):
        return supplied
    return uuid.uuid4().hex


def _request_hostname(request: Request) -> str | None:
    raw_host = request.headers.get("host")
    if not raw_host or len(raw_host) > 255:
        return None
    try:
        parsed = urlsplit(f"//{raw_host}")
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return None
        _ = parsed.port
        return parsed.hostname
    except ValueError:
        return None


def _host_allowed(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    normalized = host.rstrip(".").lower()
    return any(
        normalized == candidate.rstrip(".").lower() for candidate in allowed_hosts
    )


def _valid_access_key(request: Request, settings: Settings) -> bool:
    encoded = settings.access_key_hash
    if not encoded:
        return False
    try:
        algorithm, iterations_text, salt_hex, expected_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        supplied = request.headers.get("x-p2h-access-key", "")
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            supplied.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations_text),
        ).hex()
    except (UnicodeError, ValueError):
        return False
    return hmac.compare_digest(digest, expected_hex)


def _unsafe_origin_error(request: Request, settings: Settings) -> str | None:
    fetch_site = request.headers.get("sec-fetch-site", "").lower()
    if fetch_site == "cross-site":
        return "Cross-site state-changing requests are not allowed"
    origin = request.headers.get("origin")
    if origin is not None:
        if origin == "null" or origin not in settings.allowed_origins:
            return "Origin is not allowed"
        return None
    referer = request.headers.get("referer")
    if referer:
        try:
            parsed = urlsplit(referer)
            _ = parsed.port
        except ValueError:
            return "Referer origin is not allowed"
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return "Referer origin is not allowed"
        referer_origin = f"{parsed.scheme}://{parsed.netloc}"
        if referer_origin not in settings.allowed_origins:
            return "Referer origin is not allowed"
    return None


def _client_identity(request: Request) -> str:
    if request.client is not None:
        return request.client.host
    return "unknown"


def _request_rate_limiter(request: Request) -> InMemoryRateLimiter:
    limiter = getattr(request.app.state, "rate_limiter", None)
    if not isinstance(limiter, InMemoryRateLimiter):
        limiter = InMemoryRateLimiter()
        request.app.state.rate_limiter = limiter
    return limiter


def _secured_error(
    settings: Settings,
    request_id: str,
    path: str,
    status_code: int,
    detail: str,
) -> JSONResponse:
    response = JSONResponse(status_code=status_code, content={"detail": detail})
    _apply_security_headers(response, settings, request_id, path)
    return response


def _apply_security_headers(
    response: Response,
    settings: Settings,
    request_id: str,
    path: str,
) -> None:
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    )
    response.headers["Cross-Origin-Resource-Policy"] = "same-site"
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    if settings.is_external:
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'none'"
        )


async def _json_asgi_response(
    scope: Scope,
    receive: Receive,
    send: Send,
    status_code: int,
    detail: str,
) -> None:
    response = JSONResponse(status_code=status_code, content={"detail": detail})
    await response(scope, receive, send)
