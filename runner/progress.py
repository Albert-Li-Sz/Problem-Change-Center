from __future__ import annotations

import json
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Literal, TextIO


PROGRESS_PREFIX = "P2H_EVENT "
ProgressFormat = Literal["text", "jsonl", "none"]


class ProgressDeadlineExceeded(RuntimeError):
    def __init__(
        self,
        kind: Literal["overall", "idle", "stage", "problem"],
        limit_seconds: int,
        phase: str | None,
        problem: str | None,
    ) -> None:
        self.kind = kind
        self.limit_seconds = limit_seconds
        self.phase = phase
        self.problem = problem
        super().__init__(
            f"{kind} timeout after {limit_seconds} seconds"
            f" (phase={phase or '-'}, problem={problem or '-'})"
        )


class ProgressReporter:
    def __init__(
        self,
        *,
        output_format: ProgressFormat = "text",
        stream: TextIO | None = None,
        heartbeat_seconds: float = 15.0,
        total_timeout_seconds: int | None = None,
        idle_timeout_seconds: int | None = None,
        stage_timeout_seconds: int | None = None,
        problem_timeout_seconds: int | None = None,
    ) -> None:
        self.output_format = output_format
        self.stream = stream or sys.stdout
        self.heartbeat_seconds = heartbeat_seconds
        self.total_timeout_seconds = total_timeout_seconds
        self.idle_timeout_seconds = idle_timeout_seconds
        self.stage_timeout_seconds = stage_timeout_seconds
        self.problem_timeout_seconds = problem_timeout_seconds
        self._lock = threading.Lock()
        self._sequence = 0
        self._phase: str | None = None
        self._problem: str | None = None
        self._current: int | None = None
        self._total: int | None = None
        self._unit: str | None = None
        self._detail: str | None = None
        self._phase_started_monotonic: float | None = None
        self._problem_started_monotonic: float | None = None
        self._started_monotonic = time.monotonic()
        self._last_activity_monotonic = self._started_monotonic
        self._stopped = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._previous_alarm_handler: Any = None
        self._previous_alarm_timer: tuple[float, float] | None = None

    def __enter__(self) -> "ProgressReporter":
        self._install_deadline_timer()
        if self.output_format != "none" and self.heartbeat_seconds > 0:
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop,
                name="p2h-progress-heartbeat",
                daemon=True,
            )
            self._heartbeat_thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._stopped.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=1)
        self._restore_deadline_timer()

    def phase(
        self,
        phase: str,
        *,
        detail: str,
        current: int | None = None,
        total: int | None = None,
        unit: str | None = None,
        problem: str | None = None,
    ) -> None:
        self._phase = phase
        self._set_problem(problem)
        self._current = current
        self._total = total
        self._unit = unit
        self._detail = detail
        self._phase_started_monotonic = time.monotonic()
        self._emit("phase_started")

    def update(
        self,
        *,
        current: int | None = None,
        total: int | None = None,
        unit: str | None = None,
        problem: str | None = None,
        detail: str | None = None,
    ) -> None:
        if current is not None:
            self._current = current
        if total is not None:
            self._total = total
        if unit is not None:
            self._unit = unit
        self._set_problem(problem)
        if detail is not None:
            self._detail = detail
        self._emit("progress")

    def complete(self, *, detail: str | None = None) -> None:
        if detail is not None:
            self._detail = detail
        if self._total is not None:
            self._current = self._total
        self._emit("phase_completed")

    def _heartbeat_loop(self) -> None:
        while not self._stopped.wait(self.heartbeat_seconds):
            if self._phase is not None:
                self._emit("heartbeat")

    def _emit(self, event: str) -> None:
        if event != "heartbeat":
            self._last_activity_monotonic = time.monotonic()
        if self.output_format == "none":
            return
        with self._lock:
            self._sequence += 1
            payload: dict[str, Any] = {
                "schema_version": 1,
                "sequence": self._sequence,
                "event": event,
                "phase": self._phase,
                "current": self._current,
                "total": self._total,
                "unit": self._unit,
                "problem": self._problem,
                "detail": self._detail,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "phase_elapsed_seconds": round(
                    time.monotonic() - self._phase_started_monotonic, 3
                )
                if self._phase_started_monotonic is not None
                else None,
            }
            if self.output_format == "jsonl":
                line = PROGRESS_PREFIX + json.dumps(
                    payload, ensure_ascii=False, separators=(",", ":")
                )
            else:
                progress = ""
                if self._current is not None:
                    progress = f" {self._current}"
                    if self._total is not None:
                        progress += f"/{self._total}"
                    if self._unit:
                        progress += f" {self._unit}"
                problem = f" [{self._problem}]" if self._problem else ""
                line = (
                    f"progress: {event} phase={self._phase}{problem}{progress}"
                    f" detail={self._detail or ''}"
                )
            print(line, file=self.stream, flush=True)

    def _set_problem(self, problem: str | None) -> None:
        if problem != self._problem:
            self._problem_started_monotonic = (
                time.monotonic() if problem is not None else None
            )
        self._problem = problem

    def _install_deadline_timer(self) -> None:
        limits = (
            self.total_timeout_seconds,
            self.idle_timeout_seconds,
            self.stage_timeout_seconds,
            self.problem_timeout_seconds,
        )
        if not any(limit is not None for limit in limits):
            return
        if threading.current_thread() is not threading.main_thread() or not hasattr(
            signal, "setitimer"
        ):
            raise RuntimeError(
                "direct CLI timeout enforcement requires a main-thread POSIX runtime"
            )
        self._previous_alarm_handler = signal.getsignal(signal.SIGALRM)
        self._previous_alarm_timer = signal.getitimer(signal.ITIMER_REAL)
        signal.signal(signal.SIGALRM, self._deadline_alarm)
        signal.setitimer(signal.ITIMER_REAL, 0.25, 0.25)

    def _restore_deadline_timer(self) -> None:
        if self._previous_alarm_timer is None:
            return
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, self._previous_alarm_handler)
        signal.setitimer(signal.ITIMER_REAL, *self._previous_alarm_timer)
        self._previous_alarm_timer = None

    def _deadline_alarm(self, _signum: int, _frame: Any) -> None:
        now = time.monotonic()
        checks = (
            (
                "overall",
                self.total_timeout_seconds,
                self._started_monotonic,
            ),
            (
                "idle",
                self.idle_timeout_seconds,
                self._last_activity_monotonic,
            ),
            (
                "stage",
                self.stage_timeout_seconds,
                self._phase_started_monotonic,
            ),
            (
                "problem",
                self.problem_timeout_seconds,
                self._problem_started_monotonic,
            ),
        )
        for kind, limit, started in checks:
            if limit is not None and started is not None and now - started >= limit:
                raise ProgressDeadlineExceeded(
                    kind,
                    limit,
                    self._phase,
                    self._problem,  # type: ignore[arg-type]
                )
