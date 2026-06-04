from pathlib import Path
from tempfile import TemporaryDirectory

from app.config import Settings
from app.docker_runner import build_docker_command
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
        docker_pids_limit=256,
        docker_tmp_size="512m",
        docker_work_size="1g",
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
