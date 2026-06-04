from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

from .config import Settings
from .schemas import JobRequest
from .storage import JobPaths


def container_name(job_id: str) -> str:
    return f"p2h-{job_id}"


def build_docker_command(settings: Settings, job_id: str, paths: JobPaths, request: JobRequest) -> list[str]:
    cmd = _base_docker_command(settings, job_id, paths)

    if request.target == "domjudge":
        _append_domjudge_args(cmd, request)
    else:
        _append_hydro_args(cmd, request)

    return cmd


def _base_docker_command(settings: Settings, job_id: str, paths: JobPaths) -> list[str]:
    return [
        settings.docker_bin,
        "run",
        "--rm",
        "--name",
        container_name(job_id),
        "--label",
        "app=p2h-web-ui",
        "--label",
        f"job_id={job_id}",
        "--network",
        "none",
        "--user",
        "10001:10001",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        str(settings.docker_pids_limit),
        "--memory",
        settings.docker_memory,
        "--cpus",
        settings.docker_cpus,
        "--tmpfs",
        f"/tmp:rw,noexec,nosuid,nodev,size={settings.docker_tmp_size}",
        "-e",
        "TMPDIR=/work",
        "-e",
        "XDG_CACHE_HOME=/work/.cache",
        "-v",
        f"{paths.input_dir.resolve()}:/input:ro",
        "-v",
        f"{paths.work_dir.resolve()}:/work:rw",
        "-v",
        f"{paths.output_dir.resolve()}:/output:rw",
        settings.runner_image,
    ]


def _append_hydro_args(cmd: list[str], request: JobRequest) -> None:
    cmd.extend(
        [
            "convert",
            "/input/contest.zip",
            "-o",
            "/output",
            "--pid-start",
            request.pid_start,
            "--owner",
            str(request.owner),
            "--missing-env",
            request.missing_env,
            "--verbose",
        ]
    )

    for tag in request.tags:
        cmd.extend(["--tag", tag])

    for slug in request.only:
        cmd.extend(["--only", slug])

    cmd.append("--run-doall" if request.run_doall else "--no-run-doall")


def _append_domjudge_args(cmd: list[str], request: JobRequest) -> None:
    cmd.extend(
        [
            "domjudge-convert",
            "/input/contest.zip",
            "-o",
            "/output",
            "--code-start",
            request.domjudge_code_start,
            "--color",
            request.domjudge_color,
            "--missing-env",
            request.missing_env,
            "--verbose",
        ]
    )

    for slug in request.only:
        cmd.extend(["--only", slug])

    if request.domjudge_default_validator:
        cmd.append("--default-validator")
    elif request.domjudge_auto_validator:
        cmd.append("--auto-validator")

    if request.domjudge_with_statement:
        cmd.append("--with-statement")
    if request.domjudge_with_attachments:
        cmd.append("--with-attachments")

    cmd.append("--run-doall" if request.run_doall else "--no-run-doall")


def stop_container(settings: Settings, job_id: str) -> None:
    subprocess.run(
        [settings.docker_bin, "rm", "-f", container_name(job_id)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def pack_output(output_dir: Path, result_path: Path) -> None:
    if result_path.exists():
        result_path.unlink()
    with zipfile.ZipFile(result_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(output_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(output_dir).as_posix())
