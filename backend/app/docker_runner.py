from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path, PurePosixPath

from .config import Settings
from .schemas import JobRequest
from .storage import JobPaths


def container_name(job_id: str) -> str:
    return f"p2h-{job_id}"


def build_docker_command(settings: Settings, job_id: str, paths: JobPaths, request: JobRequest) -> list[str]:
    runner_image = _runner_image_for_request(settings.runner_image, request)
    cmd = _base_docker_command(settings, job_id, paths, runner_image)

    if request.target == "hydro":
        _append_hydro_args(cmd, request)
    elif request.target == "domjudge":
        _append_domjudge_args(cmd, request)
    elif request.target == "hydro_to_domjudge":
        _append_hydro_to_domjudge_args(cmd, request)

    return cmd


def _base_docker_command(settings: Settings, job_id: str, paths: JobPaths, runner_image: str) -> list[str]:
    cmd = [
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
        runner_image,
    ]

    if _runner_requires_amd64(runner_image):
        cmd[2:2] = ["--platform", "linux/amd64"]

    return cmd


def _runner_image_for_request(image: str, request: JobRequest) -> str:
    if request.target != "hydro_to_domjudge":
        return image

    prefix, name, suffix = _split_image_name(image)
    if name.endswith("-wine"):
        return f"{prefix}{name.removesuffix('-wine')}{suffix}"
    return image


def _split_image_name(image: str) -> tuple[str, str, str]:
    name_with_suffix = image.rsplit("/", 1)[-1]
    prefix = image[: -len(name_with_suffix)]
    suffix = ""
    if "@" in name_with_suffix:
        name_with_suffix, digest = name_with_suffix.split("@", 1)
        suffix = f"@{digest}"
    elif ":" in name_with_suffix:
        name_with_suffix, tag = name_with_suffix.rsplit(":", 1)
        suffix = f":{tag}"
    return prefix, name_with_suffix, suffix


def _runner_requires_amd64(image: str) -> bool:
    image_name = image.rsplit("/", 1)[-1].split(":", 1)[0]
    return image_name.endswith("-wine")


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


def _append_hydro_to_domjudge_args(cmd: list[str], request: JobRequest) -> None:
    cmd.extend(
        [
            "hydro-to-domjudge",
            "/input/contest.zip",
            "-o",
            "/output",
            "--code-start",
            request.domjudge_code_start,
            "--color",
            request.domjudge_color,
            "--verbose",
        ]
    )

    for slug in request.only:
        cmd.extend(["--only", slug])


def stop_container(settings: Settings, job_id: str) -> None:
    subprocess.run(
        [settings.docker_bin, "rm", "-f", container_name(job_id)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def pack_output(output_dir: Path, result_path: Path, *, target: str = "archive") -> None:
    if result_path.exists():
        result_path.unlink()
    if target == "hydro":
        hydro_packages = sorted(path for path in output_dir.iterdir() if path.is_file() and path.suffix.lower() == ".zip")
        if hydro_packages:
            _pack_hydro_packages(hydro_packages, result_path)
            return

    _pack_directory(output_dir, result_path)


def _pack_directory(output_dir: Path, result_path: Path) -> None:
    with zipfile.ZipFile(result_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(output_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(output_dir).as_posix())


def _pack_hydro_packages(package_paths: list[Path], result_path: Path) -> None:
    seen: set[str] = set()
    with zipfile.ZipFile(result_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for package_path in package_paths:
            with zipfile.ZipFile(package_path) as package:
                for info in package.infolist():
                    if info.is_dir():
                        continue
                    name = _safe_zip_member_name(info.filename)
                    if name in seen:
                        raise ValueError(f"duplicate Hydro package member while merging: {name}")
                    seen.add(name)

                    target_info = zipfile.ZipInfo(filename=name, date_time=info.date_time)
                    target_info.comment = info.comment
                    target_info.external_attr = info.external_attr
                    target_info.compress_type = zipfile.ZIP_DEFLATED
                    archive.writestr(target_info, package.read(info))


def _safe_zip_member_name(name: str) -> str:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or not normalized or normalized.endswith("/"):
        raise ValueError(f"unsafe zip member path: {name}")
    return normalized
