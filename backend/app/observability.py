from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def structured_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not any(getattr(handler, "_p2h_json", False) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        handler._p2h_json = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
        logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger


class Metrics:
    """Small in-process Prometheus registry for the single-node runtime."""

    _DURATION_BUCKETS = (0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 15.0, 60.0, 300.0)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._http_requests: dict[tuple[str, str, int], int] = defaultdict(int)
        self._http_duration_count: dict[tuple[str, str], int] = defaultdict(int)
        self._http_duration_sum: dict[tuple[str, str], float] = defaultdict(float)
        self._http_duration_buckets: dict[tuple[str, str, float], int] = defaultdict(
            int
        )
        self._jobs: dict[tuple[str, str, str], int] = defaultdict(int)
        self._job_duration_count: dict[tuple[str, str], int] = defaultdict(int)
        self._job_duration_sum: dict[tuple[str, str], float] = defaultdict(float)
        self._job_losses: dict[tuple[str, str], int] = defaultdict(int)
        self._active_jobs = 0

    def observe_http(
        self, method: str, route: str, status_code: int, duration_seconds: float
    ) -> None:
        key = (method, route)
        with self._lock:
            self._http_requests[(method, route, status_code)] += 1
            self._http_duration_count[key] += 1
            self._http_duration_sum[key] += duration_seconds
            for bucket in self._DURATION_BUCKETS:
                if duration_seconds <= bucket:
                    self._http_duration_buckets[(method, route, bucket)] += 1

    def job_started(self) -> None:
        with self._lock:
            self._active_jobs += 1

    def job_finished(
        self,
        *,
        status: str,
        source_format: str | None,
        target_format: str | None,
        duration_seconds: float,
        losses: int = 0,
    ) -> None:
        source = source_format or "unknown"
        target = target_format or "unknown"
        with self._lock:
            self._active_jobs = max(0, self._active_jobs - 1)
            self._jobs[(status, source, target)] += 1
            self._job_duration_count[(source, target)] += 1
            self._job_duration_sum[(source, target)] += duration_seconds
            self._job_losses[(source, target)] += max(0, losses)

    def render(self, *, storage_bytes: int) -> str:
        with self._lock:
            http_requests = dict(self._http_requests)
            duration_count = dict(self._http_duration_count)
            duration_sum = dict(self._http_duration_sum)
            duration_buckets = dict(self._http_duration_buckets)
            jobs = dict(self._jobs)
            job_duration_count = dict(self._job_duration_count)
            job_duration_sum = dict(self._job_duration_sum)
            job_losses = dict(self._job_losses)
            active_jobs = self._active_jobs

        lines = [
            "# HELP p2h_http_requests_total HTTP requests handled by the API.",
            "# TYPE p2h_http_requests_total counter",
        ]
        for (method, route, status_code), value in sorted(http_requests.items()):
            lines.append(
                "p2h_http_requests_total"
                f'{{method="{_escape(method)}",route="{_escape(route)}",status="{status_code}"}} {value}'
            )
        lines.extend(
            (
                "# HELP p2h_http_request_duration_seconds API request latency.",
                "# TYPE p2h_http_request_duration_seconds histogram",
            )
        )
        for method, route in sorted(duration_count):
            labels = f'method="{_escape(method)}",route="{_escape(route)}"'
            for bucket in self._DURATION_BUCKETS:
                value = duration_buckets.get((method, route, bucket), 0)
                lines.append(
                    f'p2h_http_request_duration_seconds_bucket{{{labels},le="{bucket:g}"}} {value}'
                )
            lines.append(
                f'p2h_http_request_duration_seconds_bucket{{{labels},le="+Inf"}} '
                f"{duration_count[(method, route)]}"
            )
            lines.append(
                f"p2h_http_request_duration_seconds_count{{{labels}}} {duration_count[(method, route)]}"
            )
            lines.append(
                f"p2h_http_request_duration_seconds_sum{{{labels}}} {duration_sum[(method, route)]:.9f}"
            )
        lines.extend(
            (
                "# HELP p2h_jobs_active Currently queued or running conversion jobs.",
                "# TYPE p2h_jobs_active gauge",
                f"p2h_jobs_active {active_jobs}",
                "# HELP p2h_jobs_total Completed conversion jobs.",
                "# TYPE p2h_jobs_total counter",
            )
        )
        for (status, source, target), value in sorted(jobs.items()):
            lines.append(
                "p2h_jobs_total"
                f'{{source="{_escape(source)}",status="{_escape(status)}",target="{_escape(target)}"}} {value}'
            )
        lines.extend(
            (
                "# HELP p2h_job_duration_seconds Conversion job duration.",
                "# TYPE p2h_job_duration_seconds summary",
            )
        )
        for source, target in sorted(job_duration_count):
            labels = f'source="{_escape(source)}",target="{_escape(target)}"'
            lines.append(
                f"p2h_job_duration_seconds_count{{{labels}}} {job_duration_count[(source, target)]}"
            )
            lines.append(
                f"p2h_job_duration_seconds_sum{{{labels}}} {job_duration_sum[(source, target)]:.9f}"
            )
        lines.extend(
            (
                "# HELP p2h_conversion_losses_total Declared lossy semantic mappings.",
                "# TYPE p2h_conversion_losses_total counter",
            )
        )
        for (source, target), value in sorted(job_losses.items()):
            lines.append(
                "p2h_conversion_losses_total"
                f'{{source="{_escape(source)}",target="{_escape(target)}"}} {value}'
            )
        lines.extend(
            (
                "# HELP p2h_storage_bytes Bytes used by retained job data.",
                "# TYPE p2h_storage_bytes gauge",
                f"p2h_storage_bytes {max(0, storage_bytes)}",
            )
        )
        return "\n".join(lines) + "\n"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
