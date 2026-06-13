from pathlib import Path
from tempfile import TemporaryDirectory

from app.config import Settings
import zipfile

from app.docker_runner import build_docker_command, pack_output
from app.schemas import JobRequest
from app.storage import JobPaths


def _settings(root: Path) -> Settings:
    return Settings(
        data_dir=root,
        docker_bin="docker",
        runner_image="p2h-runner",
        max_upload_bytes=1024,
        job_timeout_seconds=10,
        job_ttl_seconds=3600,
        docker_memory="1g",
        docker_cpus="2",
        docker_pids_limit=1024,
        docker_wine_pids_limit=4096,
        docker_wine_home_size="4g",
        docker_tmp_size="512m",
        docker_work_size="1g",
    )


def _settings_with_image(root: Path, image: str) -> Settings:
    settings = _settings(root)
    return Settings(
        data_dir=settings.data_dir,
        docker_bin=settings.docker_bin,
        runner_image=image,
        max_upload_bytes=settings.max_upload_bytes,
        job_timeout_seconds=settings.job_timeout_seconds,
        job_ttl_seconds=settings.job_ttl_seconds,
        docker_memory=settings.docker_memory,
        docker_cpus=settings.docker_cpus,
        docker_pids_limit=settings.docker_pids_limit,
        docker_wine_pids_limit=settings.docker_wine_pids_limit,
        docker_wine_home_size=settings.docker_wine_home_size,
        docker_tmp_size=settings.docker_tmp_size,
        docker_work_size=settings.docker_work_size,
    )


def test_docker_command_includes_security_flags_and_safe_mode() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        paths = JobPaths(root / "jobs" / ("a" * 32))
        paths.input_dir.mkdir(parents=True)
        paths.output_dir.mkdir(parents=True)
        request = JobRequest(job_id="a" * 32, pid_start="P1000", owner=1, run_doall=False)

        cmd = build_docker_command(_settings(root), request.job_id, paths, request)

    assert "--network" in cmd
    assert "none" in cmd
    assert "--user" in cmd
    assert "10001:10001" in cmd
    assert "--read-only" in cmd
    assert "--cap-drop" in cmd
    assert "ALL" in cmd
    assert "--security-opt" in cmd
    assert "no-new-privileges:true" in cmd
    assert "--pids-limit" in cmd
    assert "--memory" in cmd
    assert "--cpus" in cmd
    assert f"{paths.work_dir.resolve()}:/work:rw" in cmd
    assert "--no-run-doall" in cmd
    assert "--run-doall" not in cmd


def test_wine_runner_requests_amd64_platform() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        paths = JobPaths(root / "jobs" / ("a" * 32))
        paths.input_dir.mkdir(parents=True)
        paths.output_dir.mkdir(parents=True)
        request = JobRequest(job_id="a" * 32, pid_start="P1000", owner=1)

        cmd = build_docker_command(_settings_with_image(root, "p2h-runner-wine"), request.job_id, paths, request)

    assert cmd[:5] == ["docker", "run", "--platform", "linux/amd64", "--rm"]
    assert cmd[cmd.index("--pids-limit") + 1] == "4096"
    assert "/home/app:rw,exec,nosuid,nodev,size=4g,uid=10001,gid=10001,mode=700" in cmd
    assert "TMPDIR=/home/app" in cmd
    assert "HOME=/home/app" in cmd
    assert "WINEPREFIX=/home/app/.wine" in cmd
    assert "XDG_CACHE_HOME=/home/app/.cache" in cmd
    assert "XDG_CACHE_HOME=/work/.cache" not in cmd


def test_docker_command_allows_explicit_doall_and_passes_lists() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        paths = JobPaths(root / "jobs" / ("b" * 32))
        paths.input_dir.mkdir(parents=True)
        paths.output_dir.mkdir(parents=True)
        request = JobRequest(
            job_id="b" * 32,
            pid_start="ABC001",
            owner=7,
            tags=["校赛", "2026"],
            only=["a", "buy-cpu"],
            run_doall=True,
            missing_env="error",
        )

        cmd = build_docker_command(_settings(root), request.job_id, paths, request)

    assert "--run-doall" in cmd
    assert "--no-run-doall" not in cmd
    assert cmd.count("--tag") == 2
    assert "校赛" in cmd
    assert "2026" in cmd
    assert cmd.count("--only") == 2
    assert "a" in cmd
    assert "buy-cpu" in cmd
    assert "error" in cmd


def test_domjudge_command_uses_secure_runner_and_domjudge_args() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        paths = JobPaths(root / "jobs" / ("c" * 32))
        paths.input_dir.mkdir(parents=True)
        paths.output_dir.mkdir(parents=True)
        request = JobRequest(
            job_id="c" * 32,
            target="domjudge",
            only=["a", "buy-cpu"],
            run_doall=True,
            missing_env="error",
            domjudge_code_start="C",
            domjudge_color="#FF00AA",
            domjudge_with_statement=True,
            domjudge_with_attachments=True,
        )

        cmd = build_docker_command(_settings(root), request.job_id, paths, request)

    assert "--network" in cmd
    assert "none" in cmd
    assert "--read-only" in cmd
    assert f"{paths.work_dir.resolve()}:/work:rw" in cmd
    assert "domjudge-convert" in cmd
    assert "--pid-start" not in cmd
    assert "--owner" not in cmd
    assert "--code-start" in cmd
    assert "C" in cmd
    assert "--color" in cmd
    assert "#FF00AA" in cmd
    assert cmd.count("--only") == 2
    assert "--auto-validator" in cmd
    assert "--with-statement" in cmd
    assert "--with-attachments" in cmd
    assert "--run-doall" in cmd
    assert "--no-run-doall" not in cmd


def test_domjudge_command_can_force_default_validator() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        paths = JobPaths(root / "jobs" / ("d" * 32))
        paths.input_dir.mkdir(parents=True)
        paths.output_dir.mkdir(parents=True)
        request = JobRequest(
            job_id="d" * 32,
            target="domjudge",
            domjudge_auto_validator=False,
            domjudge_default_validator=True,
        )

        cmd = build_docker_command(_settings(root), request.job_id, paths, request)

    assert "--default-validator" in cmd
    assert "--auto-validator" not in cmd


def test_hydro_to_domjudge_command_uses_bridge_args() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        paths = JobPaths(root / "jobs" / ("e" * 32))
        paths.input_dir.mkdir(parents=True)
        paths.output_dir.mkdir(parents=True)
        request = JobRequest(
            job_id="e" * 32,
            target="hydro_to_domjudge",
            only=["sum"],
            domjudge_code_start="B",
            domjudge_color="#00AA11",
            run_doall=True,
        )

        cmd = build_docker_command(_settings(root), request.job_id, paths, request)

    assert "hydro-to-domjudge" in cmd
    assert "domjudge-convert" not in cmd
    assert "convert" not in cmd
    assert "--code-start" in cmd
    assert "B" in cmd
    assert "--color" in cmd
    assert "#00AA11" in cmd
    assert "--only" in cmd
    assert "sum" in cmd
    assert "--run-doall" not in cmd
    assert "--no-run-doall" not in cmd
    assert "--missing-env" not in cmd


def test_hydro_to_domjudge_uses_normal_runner_when_wine_is_configured() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        paths = JobPaths(root / "jobs" / ("f" * 32))
        paths.input_dir.mkdir(parents=True)
        paths.output_dir.mkdir(parents=True)
        request = JobRequest(
            job_id="f" * 32,
            target="hydro_to_domjudge",
        )

        cmd = build_docker_command(_settings_with_image(root, "p2h-runner-wine"), request.job_id, paths, request)

    assert cmd[:2] == ["docker", "run"]
    assert "--platform" not in cmd
    assert "p2h-runner" in cmd
    assert "p2h-runner-wine" not in cmd
    assert "hydro-to-domjudge" in cmd


def test_pack_output_merges_hydro_problem_packages() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        output_dir = root / "output"
        output_dir.mkdir()
        result_path = root / "result.zip"

        with zipfile.ZipFile(output_dir / "a.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("P1000/problem.yaml", "title: A\n")
        with zipfile.ZipFile(output_dir / "b.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("P1001/problem.yaml", "title: B\n")

        pack_output(output_dir, result_path, target="hydro")

        with zipfile.ZipFile(result_path) as archive:
            assert archive.namelist() == ["P1000/problem.yaml", "P1001/problem.yaml"]


def test_pack_output_keeps_non_hydro_packages_nested() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        output_dir = root / "output"
        output_dir.mkdir()
        result_path = root / "result.zip"
        (output_dir / "a.zip").write_bytes(b"not a nested package")

        pack_output(output_dir, result_path, target="domjudge")

        with zipfile.ZipFile(result_path) as archive:
            assert archive.namelist() == ["a.zip"]
