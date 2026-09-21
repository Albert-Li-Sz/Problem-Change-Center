from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import selectors
import shutil
import signal
import subprocess  # nosec B404
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

from ..docker_runner import conversion_arguments, pack_output
from ..jobs import JobManager
from ..schemas import InspectResponse, JobRequest
from ..storage import Storage, utc_now_iso
from .config import PublicConfig
from .db import Database, PostgresJobIndex
from .queue import Queue


LOGGER = logging.getLogger(__name__)
OUTPUT_LIMIT = 1024**3
STOP = threading.Event()
# A private, bounded container mount, not a shared host temporary path.
TMP_MOUNT = "/tmp:rw,noexec,nosuid,nodev,size=256m,uid=10001,gid=10001,mode=700"  # nosec B108


def disk_percent(path: Path) -> int:
    usage = shutil.disk_usage(path)
    return round(usage.used * 100 / usage.total)


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, timeout=30, **kwargs)  # nosec B603 B607


def container_name(row):
    return f"p2h-public-{row['id']}-{row['lease_token'][:8]}"


def instance_label(config: PublicConfig):
    return (
        "p2h.instance=" + hashlib.sha256(str(config.data_dir).encode()).hexdigest()[:16]
    )


def command(config: PublicConfig, row, storage: Storage) -> list[str]:
    paths = storage.paths_for(row["id"])
    wine = row["wine"]
    image = config.wine_image if wine else config.runner_image
    memory = "4g" if wine else "2g"
    home_size = "2g" if wine else "1g"
    cmd = [
        "docker",
        "run",
        "--name",
        container_name(row),
        "--runtime",
        "runc",
        "--label",
        "app=p2h-public",
        "--label",
        instance_label(config),
        "--network",
        "none",
        "--read-only",
        "--user",
        "10001:10001",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--log-driver",
        "none",
        "--pids-limit",
        "1024" if wine else "512",
        "--memory",
        memory,
        "--memory-swap",
        memory,
        "--cpus",
        "2",
        "--ulimit",
        "nofile=1024:1024",
        "--ulimit",
        "fsize=1073741824:1073741824",
        "--tmpfs",
        TMP_MOUNT,
        "--tmpfs",
        "/work:rw,exec,nosuid,nodev,size=2g,uid=10001,gid=10001,mode=700",
        "--tmpfs",
        "/output:rw,noexec,nosuid,nodev,size=1g,uid=10001,gid=10001,mode=700",
        "--tmpfs",
        f"/home/app:rw,exec,nosuid,nodev,size={home_size},uid=10001,gid=10001,mode=700",
        "--mount",
        f"type=bind,src={paths.input_dir},dst=/input,readonly",
        "-e",
        "HOME=/home/app",
        "-e",
        "WINEPREFIX=/home/app/.wine",
        "-e",
        "TMPDIR=/work",
        "-e",
        "P2H_MAX_ARCHIVE_UNCOMPRESSED_BYTES=1073741824",
        "-e",
        "P2H_MAX_ARCHIVE_ENTRIES=50000",
        "-e",
        "P2H_MAX_ARCHIVE_MEMBER_BYTES=268435456",
    ]
    if wine:
        # Debian Wine 10 frees TMPDIR's environment pointer if this directory
        # is absent (Debian #1110936). A private runtime mount avoids that path.
        cmd += [
            "--platform",
            "linux/amd64",
            "--tmpfs",
            "/run/user/10001:rw,noexec,nosuid,nodev,size=16m,uid=10001,gid=10001,mode=700",
            "-e",
            "XDG_RUNTIME_DIR=/run/user/10001",
            "-e",
            "WINEDEBUG=-all,err+all",
        ]
    cmd += ["--entrypoint", "python", image, "-u", "/opt/public_runner.py"]
    if row["kind"] == "inspect":
        return cmd + ["inspect"]
    request = JobRequest.model_validate(row["request"])
    return cmd + conversion_arguments(config.storage_settings(), paths, request)


def collect_output(name: str, output: Path, *, check=None):
    """Read Docker's archive without extractall, links, devices or trusted modes."""
    if check is not None:
        check()
    output.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [
            "docker",
            "exec",
            "--user",
            "10001:10001",
            name,
            "python",
            "-I",
            "/opt/public_export.py",
        ],  # nosec B603 B607
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    timer = threading.Timer(60, process.kill)
    timer.start()
    stopped = threading.Event()
    errors = []

    def keepalive():
        # Reads can block while Docker prepares a large archive. Keep renewing
        # the lease and honour cancellation even when no output is arriving.
        while not stopped.wait(2):
            try:
                if check is not None:
                    check()
            except Exception as exc:
                errors.append(exc)
                process.kill()
                return

    watcher = threading.Thread(target=keepalive, daemon=True)
    watcher.start()
    total = 0
    seen: set[str] = set()
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            for count, member in enumerate(archive, start=1):
                relative = PurePosixPath(member.name)
                parts = tuple(p for p in relative.parts if p != ".")
                if not parts:
                    continue
                if (
                    relative.is_absolute()
                    or ".." in parts
                    or "\\" in member.name
                    or len(member.name) > 1024
                    or count > 50000
                ):
                    raise ValueError("Unsafe or excessive output entries")
                if (
                    not (member.isdir() or member.isfile())
                    or member.islnk()
                    or member.issym()
                ):
                    raise ValueError("Output links and special files are not allowed")
                target = output.joinpath(*parts)
                key = "/".join(parts)
                if key in seen:
                    raise ValueError("Duplicate output entry")
                seen.add(key)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    continue
                total += member.size
                if member.size < 0 or total > OUTPUT_LIMIT:
                    raise ValueError("Output exceeds 1 GiB")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("Invalid output archive")
                with source, target.open("xb") as dest:
                    remaining = member.size
                    while remaining:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError("Truncated output")
                        dest.write(chunk)
                        remaining -= len(chunk)
        if process.wait(timeout=10) != 0:
            raise ValueError("Failed to retrieve container output")
    finally:
        stopped.set()
        timer.cancel()
        if process.poll() is None:
            process.kill()
        process.wait()
        if process.stdout:
            process.stdout.close()
        watcher.join(timeout=7)
        if errors:
            raise errors[0]


class Worker:
    def __init__(self, config: PublicConfig, db: Database):
        self.config, self.db = config, db
        self.queue = Queue(db, config)
        self.storage = Storage(
            config.storage_settings(), job_index=PostgresJobIndex(db), backfill=False
        )

    def run_job(self, row):
        job_id, name = row["id"], container_name(row)
        paths = self.storage.paths_for(job_id)
        process = None
        deadline = time.monotonic() + (60 if row["kind"] == "inspect" else 1800)
        metadata = self.storage.read_metadata(job_id)
        metadata.status, metadata.started_at = "running", utc_now_iso()
        self.storage.write_metadata(metadata)
        outcome, error = "failed", None
        try:
            if row["kind"] == "convert":
                self.storage.write_request(job_id, row["request"])
            # Public input mounts expose only this task and no writable host path.
            paths.input_dir.chmod(0o755)
            for path in paths.input_dir.rglob("*"):
                path.chmod(0o755 if path.is_dir() else 0o644)
            process = subprocess.Popen(
                command(self.config, row, self.storage),  # nosec B603
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            pending = ""
            next_beat = 0.0
            next_progress = 0.0
            progress_manager = JobManager(self.config.storage_settings(), self.storage)
            code = None
            try:
                while code is None:
                    now = time.monotonic()
                    if STOP.is_set() or now >= deadline:
                        raise RuntimeError(
                            "Worker 正在关闭"
                            if STOP.is_set()
                            else "任务超过运行时间上限"
                        )
                    if now >= next_beat:
                        lease = self.queue.heartbeat(job_id, row["lease_token"])
                        if (
                            not lease
                            or lease["cancel_requested"]
                            or lease["delete_requested"]
                        ):
                            raise RuntimeError("任务已取消或执行租约失效")
                        next_beat = now + 2
                    events = selector.select(timeout=0.5)
                    for key, _ in events:
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            raise RuntimeError("Docker runner 意外退出")
                        text = chunk.decode("utf-8", errors="replace")
                        self.storage.append_log(job_id, text)
                        pending = (pending + text)[-131072:]
                        if now >= next_progress:
                            lines = pending.splitlines()
                            for line in reversed(
                                lines[:-1] if not pending.endswith("\n") else lines
                            ):
                                if line.startswith("P2H_EVENT ") and len(line) <= 8192:
                                    progress_manager._handle_progress_line(job_id, line)
                                    next_progress = now + 0.5
                                    break
                        match = re.search(
                            r"(?:^|\n)P2H_PUBLIC_DONE (-?\d+)\r?\n", pending
                        )
                        if match:
                            code = int(match[1])
                    if process.poll() is not None and code is None:
                        raise RuntimeError("Docker runner 启动失败，请联系管理员")
            finally:
                selector.close()

            # Docker's archive endpoint cannot read tmpfs. The read-only exporter
            # terminates descendants and streams a bounded archive instead.
            def export_check():
                if STOP.is_set() or time.monotonic() >= deadline:
                    raise RuntimeError("输出导出超时或任务停止")
                current_lease = self.queue.heartbeat(job_id, row["lease_token"])
                if not current_lease or current_lease["cancel_requested"]:
                    raise RuntimeError("任务已取消或执行租约失效")

            collect_output(name, paths.output_dir, check=export_check)
            lease = self.queue.heartbeat(job_id, row["lease_token"])
            if not lease or lease["cancel_requested"]:
                raise RuntimeError("任务已取消")
            metadata.exit_code = code
            if row["kind"] == "inspect":
                if code != 0:
                    raise ValueError("题包检查失败，请查看日志")
                path = paths.output_dir / "inspection.json"
                if path.stat().st_size > 1024 * 1024:
                    raise ValueError("Inspection response too large")
                data = json.loads(path.read_text())
                data["format_candidates"] = data.pop("candidates")
                result = InspectResponse.model_validate(
                    {
                        "job_id": job_id,
                        "filename": row["filename"],
                        "size": row["size"],
                        **data,
                    }
                )
                for field in (
                    "detected_format",
                    "format_candidates",
                    "package_scope",
                    "package_layout",
                    "problem_count",
                    "problems",
                    "problems_truncated",
                    "supported_targets",
                ):
                    setattr(metadata, field, result.model_dump()[field])
                outcome = "uploaded"
            else:
                report = self.storage.capture_conversion_report(job_id)
                if code != 0:
                    raise ValueError(f"转换失败（退出码 {code}），请查看日志与报告")
                request = JobRequest.model_validate(row["request"])
                if (
                    not report
                    or report.get("target_format") != request.effective_target_format
                    or report.get("problem_count", 0) < 1
                ):
                    raise ValueError("转换器未生成有效报告")
                metadata.source_format = str(report["source_format"])
                metadata.target_format = str(report["target_format"])

                def progress(current, total, detail):
                    nonlocal next_beat
                    now = time.monotonic()
                    if now >= deadline or STOP.is_set():
                        raise RuntimeError("打包超时或任务停止")
                    if now >= next_beat:
                        lease = self.queue.heartbeat(job_id, row["lease_token"])
                        if not lease or lease["cancel_requested"]:
                            raise RuntimeError("任务已取消")
                        next_beat = now + 2
                        progress_manager._set_progress(
                            job_id,
                            phase="package",
                            detail=detail[:1024],
                            current=current,
                            total=total,
                            unit="files",
                        )

                pack_output(
                    paths.output_dir,
                    paths.result_path,
                    target=request.effective_target_format,
                    max_uncompressed_bytes=OUTPUT_LIMIT,
                    progress_callback=progress,
                )
                outcome = "success"
        except Exception as exc:
            error = str(exc)[:2048]
            self.storage.append_log(job_id, f"\n{error}\n")
        finally:
            try:
                docker(
                    "rm",
                    "-f",
                    name,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except (subprocess.SubprocessError, OSError):
                LOGGER.exception("container_cleanup_failed")
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
                if process.stdout:
                    process.stdout.close()
            # Publish artifacts and metadata before exposing the terminal queue
            # state to API readers. Queue state remains authoritative for cancel.
            metadata.status = "queued" if outcome == "uploaded" else outcome
            metadata.progress = self.storage.read_metadata(job_id).progress
            metadata.error = error
            metadata.finished_at = utc_now_iso()
            self.storage.write_metadata(metadata)
            if self.queue.finish(job_id, row["lease_token"], outcome, error):
                row = self.db.one("SELECT * FROM jobs WHERE id=%s", (job_id,))
                metadata.status = (
                    "queued" if row["status"] == "uploaded" else row["status"]
                )
                metadata.error = row["error"]
                metadata.finished_at = utc_now_iso()
                self.storage.write_metadata(metadata)
                if row["status"] not in {"uploaded", "success"}:
                    paths.result_path.unlink(missing_ok=True)
                shutil.rmtree(paths.output_dir, ignore_errors=True)
                paths.output_dir.mkdir(mode=0o700)

    def reap_finished_containers(self):
        # A temporary daemon outage may have prevented run_job's final rm.
        # Recheck each named lease before removal, so a freshly claimed task
        # cannot be mistaken for an orphan by a stale snapshot.
        containers = docker(
            "ps",
            "-a",
            "--filter",
            "label=app=p2h-public",
            "--filter",
            "label=" + instance_label(self.config),
            "--format",
            "{{.Names}}",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        for name in containers.stdout.decode().splitlines():
            match = re.fullmatch(r"p2h-public-([0-9a-f]{32})-([0-9a-f]{8})", name)
            if not match:
                continue
            if not self.db.one(
                "SELECT id FROM jobs WHERE id=%s AND status='running' AND left(lease_token,8)=%s",
                match.groups(),
            ):
                docker(
                    "rm",
                    "-f",
                    name,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

    def maintenance(self):
        try:
            self.reap_finished_containers()
        except (subprocess.SubprocessError, OSError):
            LOGGER.warning("orphan_container_scan_unavailable")
        for row in self.db.all(
            "SELECT * FROM jobs WHERE status='running' AND lease_until<now()"
        ):
            try:
                name = container_name(row)
                found = docker(
                    "ps",
                    "-aq",
                    "--filter",
                    f"name=^{name}$",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                if found.stdout.strip():
                    docker(
                        "rm",
                        "-f",
                        name,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
            except subprocess.SubprocessError:
                continue
            self.queue.finish(
                row["id"], row["lease_token"], "failed", "Worker 中断；请重新上传任务"
            )
        for row in self.db.all(
            "SELECT id FROM jobs WHERE status<>'running' AND (delete_requested OR expires_at<now() OR (status='uploading' AND created_at<now()-interval '1 hour'))"
        ):
            self.queue.cancel(row["id"], delete=True)
            try:
                self.storage.delete_job(row["id"])
            except Exception:
                if self.storage.paths_for(row["id"]).root.exists():
                    LOGGER.exception("artifact_cleanup_failed")
                    continue
            self.db.execute(
                "DELETE FROM jobs WHERE id=%s AND status<>'running'", (row["id"],)
            )

    def serve(self):
        futures = set()
        next_maintenance = 0.0
        with ThreadPoolExecutor(max_workers=self.config.max_concurrent) as pool:
            while not STOP.is_set():
                for future in tuple(futures):
                    if future.done():
                        futures.remove(future)
                        try:
                            future.result()
                        except Exception:
                            LOGGER.exception("job_execution_failed_before_settlement")
                try:
                    percent = disk_percent(self.config.data_dir)
                    self.db.execute(
                        """INSERT INTO worker_status(id,active,disk_percent) VALUES ('single',%s,%s)
                        ON CONFLICT(id) DO UPDATE SET heartbeat_at=now(),active=EXCLUDED.active,disk_percent=EXCLUDED.disk_percent""",
                        (len(futures), percent),
                    )
                    if time.monotonic() >= next_maintenance:
                        self.maintenance()
                        next_maintenance = time.monotonic() + 15
                    row = (
                        self.queue.claim()
                        if percent < 90 and len(futures) < self.config.max_concurrent
                        else None
                    )
                    if row:
                        futures.add(pool.submit(self.run_job, row))
                        continue
                except Exception:
                    LOGGER.exception("worker_loop_failed")
                STOP.wait(1)


def main():
    logging.basicConfig(level=logging.INFO)
    config = PublicConfig.from_env()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(config.database_url)
    db.open()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: STOP.set())
    try:
        Worker(config, db).serve()
    finally:
        db.close()


if __name__ == "__main__":
    main()
