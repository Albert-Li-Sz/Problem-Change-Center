from __future__ import annotations

import re

# Docker is invoked with a fixed argv and shell=False.
import subprocess  # nosec B404
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from .config import Settings
from .schemas import JobRequest
from .storage import JobPaths


DEFAULT_MAX_ARCHIVE_ENTRIES = 50_000
DEFAULT_MAX_ARCHIVE_MEMBER_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_ARCHIVE_COMPRESSION_RATIO = 200.0
CONTAINER_TMP_PATH = "/tmp"  # nosec B108


def container_name(job_id: str) -> str:
    return f"p2h-{job_id}"


def build_docker_command(
    settings: Settings, job_id: str, paths: JobPaths, request: JobRequest
) -> list[str]:
    runner_image = _runner_image_for_request(settings.runner_image, request)
    cmd = _base_docker_command(settings, job_id, paths, runner_image)

    if not request.is_legacy_request:
        _append_package_convert_args(cmd, request)
    elif request.target == "hydro":
        _append_hydro_args(cmd, request)
    elif request.target == "domjudge":
        _append_domjudge_args(cmd, request)
    elif request.target == "hydro_to_domjudge":
        _append_hydro_to_domjudge_args(cmd, request)
    elif request.target == "domjudge_to_hydro":
        _append_domjudge_to_hydro_args(cmd, request)
    elif request.target == "hoj_to_hydro":
        _append_hoj_to_hydro_args(cmd, request)
    elif request.target == "hydro_to_hoj":
        _append_hydro_to_hoj_args(cmd, request)
    elif request.target == "hoj_to_domjudge":
        _append_hoj_to_domjudge_args(cmd, request)

    return cmd


def _base_docker_command(
    settings: Settings, job_id: str, paths: JobPaths, runner_image: str
) -> list[str]:
    is_wine_runner = _runner_requires_amd64(runner_image)
    max_archive_bytes = parse_size_bytes(settings.docker_work_size)
    max_archive_member_bytes = min(max_archive_bytes, DEFAULT_MAX_ARCHIVE_MEMBER_BYTES)
    archive_env_vars = [
        f"P2H_MAX_ARCHIVE_ENTRIES={DEFAULT_MAX_ARCHIVE_ENTRIES}",
        f"P2H_MAX_ARCHIVE_UNCOMPRESSED_BYTES={max_archive_bytes}",
        f"P2H_MAX_ARCHIVE_MEMBER_BYTES={max_archive_member_bytes}",
        f"P2H_MAX_ARCHIVE_COMPRESSION_RATIO={DEFAULT_MAX_ARCHIVE_COMPRESSION_RATIO:g}",
    ]
    env_vars = ["TMPDIR=/work", "XDG_CACHE_HOME=/work/.cache", *archive_env_vars]
    extra_tmpfs: list[str] = []
    if is_wine_runner:
        env_vars = [
            "TMPDIR=/home/app",
            "HOME=/home/app",
            "WINEPREFIX=/home/app/.wine",
            "XDG_CACHE_HOME=/home/app/.cache",
            *archive_env_vars,
        ]
        extra_tmpfs = [
            "--tmpfs",
            f"/home/app:rw,exec,nosuid,nodev,size={settings.docker_wine_home_size},uid=10001,gid=10001,mode=700",
        ]

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
        "--read-only",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "SYS_ADMIN",
        "--cap-add",
        "SETUID",
        "--cap-add",
        "SETGID",
        "--cap-add",
        "SETPCAP",
        "--cap-add",
        "DAC_OVERRIDE",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        str(_effective_pids_limit(settings, runner_image)),
        "--memory",
        settings.docker_memory,
        "--cpus",
        settings.docker_cpus,
        "--tmpfs",
        f"{CONTAINER_TMP_PATH}:rw,noexec,nosuid,nodev,size={settings.docker_tmp_size}",
        "--tmpfs",
        f"/work:rw,exec,nosuid,nodev,size={settings.docker_work_size},uid=10001,gid=10001,mode=700",
        "--tmpfs",
        f"/output:rw,noexec,nosuid,nodev,size={settings.docker_output_size},uid=10001,gid=10001,mode=700",
        *extra_tmpfs,
        *[part for env_var in env_vars for part in ("-e", env_var)],
        "-v",
        f"{paths.input_dir.resolve()}:/input:ro",
        "-v",
        f"{paths.output_dir.resolve()}:/result:rw",
        runner_image,
    ]

    if is_wine_runner:
        cmd[2:2] = ["--platform", "linux/amd64"]

    return cmd


def _runner_image_for_request(image: str, request: JobRequest) -> str:
    bridge_targets = {
        "hydro_to_domjudge",
        "domjudge_to_hydro",
        "hoj_to_hydro",
        "hydro_to_hoj",
        "hoj_to_domjudge",
    }
    requires_polygon_runtime = (
        request.target not in bridge_targets
        if request.is_legacy_request
        else request.source_format in {"polygon", "auto"}
    )
    if requires_polygon_runtime:
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
    image_name = image.rsplit("/", 1)[-1]
    if "@" in image_name:
        image_name = image_name.split("@", 1)[0]
    elif ":" in image_name:
        image_name = image_name.rsplit(":", 1)[0]
    return image_name.endswith("-wine")


def _effective_pids_limit(settings: Settings, runner_image: str) -> int:
    if (
        _runner_requires_amd64(runner_image)
        and settings.docker_wine_pids_limit is not None
    ):
        return settings.docker_wine_pids_limit
    return settings.docker_pids_limit


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


def _append_domjudge_to_hydro_args(cmd: list[str], request: JobRequest) -> None:
    cmd.extend(
        [
            "domjudge-to-hydro",
            "/input/contest.zip",
            "-o",
            "/output",
            "--pid-start",
            request.pid_start,
            "--owner",
            str(request.owner),
            "--verbose",
        ]
    )

    for tag in request.tags:
        cmd.extend(["--tag", tag])

    for slug in request.only:
        cmd.extend(["--only", slug])


def _append_hoj_to_hydro_args(cmd: list[str], request: JobRequest) -> None:
    cmd.extend(
        [
            "hoj-to-hydro",
            "/input/contest.zip",
            "-o",
            "/output",
            "--pid-start",
            request.pid_start,
            "--owner",
            str(request.owner),
            "--verbose",
        ]
    )
    for tag in request.tags:
        cmd.extend(["--tag", tag])
    for slug in request.only:
        cmd.extend(["--only", slug])


def _append_hydro_to_hoj_args(cmd: list[str], request: JobRequest) -> None:
    cmd.extend(["hydro-to-hoj", "/input/contest.zip", "-o", "/output", "--verbose"])
    for slug in request.only:
        cmd.extend(["--only", slug])


def _append_hoj_to_domjudge_args(cmd: list[str], request: JobRequest) -> None:
    cmd.extend(
        [
            "hoj-to-domjudge",
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


def _append_package_convert_args(cmd: list[str], request: JobRequest) -> None:
    cmd.extend(
        [
            "package-convert",
            "/input/contest.zip",
            "-o",
            "/output",
            "--source-format",
            request.source_format,
            "--target-format",
            request.effective_target_format,
            "--loss-policy",
            request.loss_policy,
            "--pid-start",
            request.options.hydro.pid_start,
            "--owner",
            str(request.options.hydro.owner),
            "--code-start",
            request.options.icpc.code_start,
            "--color",
            request.options.icpc.color,
            "--icpc-profile",
            request.options.icpc.profile,
            "--icpc-license",
            request.options.icpc.license,
            "--fps-profile",
            request.options.fps.profile,
            "--missing-env",
            request.options.polygon.missing_env,
            "--validator-mode",
            request.options.polygon.validator_mode,
        ]
    )
    if request.options.icpc.rights_owner:
        cmd.extend(["--icpc-rights-owner", request.options.icpc.rights_owner])
    for slug in request.only:
        cmd.extend(["--only", slug])
    for tag in request.options.hydro.tags:
        cmd.extend(["--tag", tag])
    if request.options.polygon.with_statement:
        cmd.append("--with-statement")
    if request.options.polygon.with_attachments:
        cmd.append("--with-attachments")
    cmd.append("--run-doall" if request.options.polygon.run_doall else "--no-run-doall")


def stop_container(settings: Settings, job_id: str) -> None:
    # The job id is validated and the Docker binary is administrator-configured.
    subprocess.run(  # nosec B603
        [settings.docker_bin, "rm", "-f", container_name(job_id)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def pack_output(
    output_dir: Path,
    result_path: Path,
    *,
    target: str = "archive",
    max_uncompressed_bytes: int = 1024 * 1024 * 1024,
) -> None:
    if result_path.exists():
        result_path.unlink()
    try:
        _reject_output_symlinks(output_dir)
        if target in {"hydro", "domjudge_to_hydro", "hoj_to_hydro"}:
            hydro_packages = sorted(
                path
                for path in output_dir.iterdir()
                if path.is_file() and path.suffix.lower() == ".zip"
            )
            if hydro_packages:
                _pack_hydro_packages(
                    hydro_packages,
                    result_path,
                    max_uncompressed_bytes=max_uncompressed_bytes,
                )
                return

        _pack_directory(
            output_dir, result_path, max_uncompressed_bytes=max_uncompressed_bytes
        )
    except Exception:
        if result_path.exists():
            result_path.unlink()
        raise


def _pack_directory(
    output_dir: Path, result_path: Path, *, max_uncompressed_bytes: int
) -> None:
    files = [path for path in sorted(output_dir.rglob("*")) if path.is_file()]
    if not files:
        raise ValueError("converter produced no output files")
    _validate_output_sizes(
        (
            (path.relative_to(output_dir).as_posix(), path.stat().st_size)
            for path in files
        ),
        max_uncompressed_bytes=max_uncompressed_bytes,
    )
    with zipfile.ZipFile(result_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(output_dir).as_posix())


def _reject_output_symlinks(output_dir: Path) -> None:
    for path in sorted(output_dir.rglob("*")):
        if path.is_symlink():
            relative = path.relative_to(output_dir).as_posix()
            raise ValueError(f"output symlinks are not supported: {relative}")


def _pack_hydro_packages(
    package_paths: list[Path],
    result_path: Path,
    *,
    max_uncompressed_bytes: int,
) -> None:
    seen: set[str] = set()
    total_bytes = 0
    total_entries = 0
    with zipfile.ZipFile(result_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for package_path in package_paths:
            with zipfile.ZipFile(package_path) as package:
                for info in package.infolist():
                    if info.is_dir():
                        continue
                    name = _safe_zip_member_name(info.filename)
                    if name in seen:
                        raise ValueError(
                            f"duplicate Hydro package member while merging: {name}"
                        )
                    seen.add(name)
                    total_entries += 1
                    total_bytes += info.file_size
                    _validate_archive_member(
                        name,
                        file_size=info.file_size,
                        compress_size=info.compress_size,
                        total_entries=total_entries,
                        total_bytes=total_bytes,
                        max_uncompressed_bytes=max_uncompressed_bytes,
                        # These archives were produced inside the size-limited
                        # runner. Contest data such as all-zero arrays can
                        # legitimately compress far beyond the upload ratio
                        # limit, so enforce declared/member/total sizes while
                        # streaming instead of rejecting by ratio alone.
                        check_ratio=False,
                    )

                    target_info = zipfile.ZipInfo(
                        filename=name, date_time=info.date_time
                    )
                    target_info.comment = info.comment
                    target_info.external_attr = info.external_attr
                    target_info.compress_type = zipfile.ZIP_DEFLATED
                    written = 0
                    with (
                        package.open(info) as source,
                        archive.open(target_info, "w", force_zip64=True) as target,
                    ):
                        while chunk := source.read(1024 * 1024):
                            written += len(chunk)
                            if (
                                written > info.file_size
                                or written > DEFAULT_MAX_ARCHIVE_MEMBER_BYTES
                            ):
                                raise ValueError(
                                    f"archive member expanded beyond declared size: {name}"
                                )
                            target.write(chunk)


def _validate_output_sizes(
    entries: Iterable[tuple[str, int]], *, max_uncompressed_bytes: int
) -> None:
    total_bytes = 0
    for total_entries, (name, file_size) in enumerate(entries, start=1):
        total_bytes += file_size
        _validate_archive_member(
            name,
            file_size=file_size,
            compress_size=file_size,
            total_entries=total_entries,
            total_bytes=total_bytes,
            max_uncompressed_bytes=max_uncompressed_bytes,
            check_ratio=False,
        )


def _validate_archive_member(
    name: str,
    *,
    file_size: int,
    compress_size: int,
    total_entries: int,
    total_bytes: int,
    max_uncompressed_bytes: int,
    check_ratio: bool = True,
) -> None:
    if total_entries > DEFAULT_MAX_ARCHIVE_ENTRIES:
        raise ValueError(
            f"output exceeds archive entry limit ({DEFAULT_MAX_ARCHIVE_ENTRIES})"
        )
    if file_size > min(max_uncompressed_bytes, DEFAULT_MAX_ARCHIVE_MEMBER_BYTES):
        raise ValueError(f"archive member exceeds uncompressed size limit: {name}")
    if total_bytes > max_uncompressed_bytes:
        raise ValueError(
            f"output exceeds uncompressed size limit ({max_uncompressed_bytes} bytes)"
        )
    if (
        check_ratio
        and file_size / max(compress_size, 1) > DEFAULT_MAX_ARCHIVE_COMPRESSION_RATIO
    ):
        raise ValueError(f"archive member exceeds compression ratio limit: {name}")


def parse_size_bytes(value: str) -> int:
    match = re.fullmatch(
        r"\s*([0-9]+(?:\.[0-9]+)?)\s*([kmgt]?)(?:i?b)?\s*", value, flags=re.IGNORECASE
    )
    if match is None:
        raise ValueError(f"invalid size value: {value}")
    multipliers = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
    size = int(float(match.group(1)) * multipliers[match.group(2).lower()])
    if size <= 0:
        raise ValueError(f"size must be positive: {value}")
    return size


def _safe_zip_member_name(name: str) -> str:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or ".." in path.parts
        or not normalized
        or normalized.endswith("/")
    ):
        raise ValueError(f"unsafe zip member path: {name}")
    return normalized
