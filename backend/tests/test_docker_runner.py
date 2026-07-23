import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from app.config import Settings

from app.docker_runner import build_docker_command, pack_output, parse_size_bytes
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
        request = JobRequest(
            job_id="a" * 32, pid_start="P1000", owner=1, run_doall=False
        )

        cmd = build_docker_command(_settings(root), request.job_id, paths, request)

    assert "--network" in cmd
    assert "none" in cmd
    assert "--user" not in cmd
    assert "--read-only" in cmd
    assert "--cap-drop" in cmd
    assert "ALL" in cmd
    cap_adds = [cmd[index + 1] for index, item in enumerate(cmd) if item == "--cap-add"]
    assert cap_adds == ["SYS_ADMIN", "SETUID", "SETGID", "SETPCAP", "DAC_OVERRIDE"]
    assert "--security-opt" in cmd
    assert "no-new-privileges:true" in cmd
    assert "--pids-limit" in cmd
    assert "--memory" in cmd
    assert "--cpus" in cmd
    assert f"{paths.work_dir.resolve()}:/work:rw" not in cmd
    assert "/work:rw,exec,nosuid,nodev,size=1g,uid=10001,gid=10001,mode=700" in cmd
    assert "/output:rw,noexec,nosuid,nodev,size=1g,uid=10001,gid=10001,mode=700" in cmd
    assert f"{paths.output_dir.resolve()}:/result:rw" in cmd
    assert f"{paths.output_dir.resolve()}:/output:rw" not in cmd
    assert "P2H_MAX_ARCHIVE_UNCOMPRESSED_BYTES=1073741824" in cmd
    assert "P2H_MAX_ARCHIVE_COMPRESSION_RATIO=200" in cmd
    assert "--no-run-doall" in cmd
    assert "--run-doall" not in cmd


def test_wine_runner_requests_amd64_platform() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        paths = JobPaths(root / "jobs" / ("a" * 32))
        paths.input_dir.mkdir(parents=True)
        paths.output_dir.mkdir(parents=True)
        request = JobRequest(job_id="a" * 32, pid_start="P1000", owner=1)

        cmd = build_docker_command(
            _settings_with_image(root, "p2h-runner-wine"),
            request.job_id,
            paths,
            request,
        )

    assert cmd[:5] == ["docker", "run", "--platform", "linux/amd64", "--rm"]
    assert cmd[cmd.index("--pids-limit") + 1] == "4096"
    assert "/home/app:rw,exec,nosuid,nodev,size=4g,uid=10001,gid=10001,mode=700" in cmd
    assert "TMPDIR=/home/app" in cmd
    assert "HOME=/home/app" in cmd
    assert "WINEPREFIX=/home/app/.wine" in cmd
    assert "XDG_CACHE_HOME=/home/app/.cache" in cmd
    assert "XDG_CACHE_HOME=/work/.cache" not in cmd


def test_wine_runner_digest_image_requests_amd64_platform() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        paths = JobPaths(root / "jobs" / ("a" * 32))
        paths.input_dir.mkdir(parents=True)
        paths.output_dir.mkdir(parents=True)
        request = JobRequest(job_id="a" * 32, pid_start="P1000", owner=1)

        cmd = build_docker_command(
            _settings_with_image(
                root, "registry.example.com/p2h-runner-wine@sha256:abcd"
            ),
            request.job_id,
            paths,
            request,
        )

    assert cmd[:5] == ["docker", "run", "--platform", "linux/amd64", "--rm"]
    assert cmd[cmd.index("--pids-limit") + 1] == "4096"
    assert "/home/app:rw,exec,nosuid,nodev,size=4g,uid=10001,gid=10001,mode=700" in cmd


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
    assert f"{paths.work_dir.resolve()}:/work:rw" not in cmd
    assert "/work:rw,exec,nosuid,nodev,size=1g,uid=10001,gid=10001,mode=700" in cmd
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

        cmd = build_docker_command(
            _settings_with_image(root, "p2h-runner-wine"),
            request.job_id,
            paths,
            request,
        )

    assert cmd[:2] == ["docker", "run"]
    assert "--platform" not in cmd
    assert "p2h-runner" in cmd
    assert "p2h-runner-wine" not in cmd
    assert "hydro-to-domjudge" in cmd


def test_domjudge_to_hydro_command_uses_bridge_args_and_normal_runner() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        paths = JobPaths(root / "jobs" / ("1" * 32))
        paths.input_dir.mkdir(parents=True)
        paths.output_dir.mkdir(parents=True)
        request = JobRequest(
            job_id="1" * 32,
            target="domjudge_to_hydro",
            pid_start="H200",
            owner=9,
            tags=["ICPC", "2026"],
            only=["b"],
            run_doall=True,
        )

        cmd = build_docker_command(
            _settings_with_image(root, "p2h-runner-wine"),
            request.job_id,
            paths,
            request,
        )

    assert cmd[:2] == ["docker", "run"]
    assert "--platform" not in cmd
    assert "p2h-runner" in cmd
    assert "p2h-runner-wine" not in cmd
    assert "domjudge-to-hydro" in cmd
    assert "--pid-start" in cmd and "H200" in cmd
    assert "--owner" in cmd and "9" in cmd
    assert cmd.count("--tag") == 2
    assert "--only" in cmd and "b" in cmd
    assert "--run-doall" not in cmd
    assert "--no-run-doall" not in cmd


def test_hoj_to_hydro_command_uses_hydro_args_and_normal_runner() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        paths = JobPaths(root / "jobs" / ("2" * 32))
        paths.input_dir.mkdir(parents=True)
        paths.output_dir.mkdir(parents=True)
        request = JobRequest(
            job_id="2" * 32,
            target="hoj_to_hydro",
            pid_start="H300",
            owner=6,
            tags=["HOJ", "2026"],
            only=["hoj-1000"],
            run_doall=True,
        )

        cmd = build_docker_command(
            _settings_with_image(root, "p2h-runner-wine"),
            request.job_id,
            paths,
            request,
        )

    assert "--platform" not in cmd
    assert "p2h-runner" in cmd and "p2h-runner-wine" not in cmd
    assert "hoj-to-hydro" in cmd
    assert "--pid-start" in cmd and "H300" in cmd
    assert "--owner" in cmd and "6" in cmd
    assert cmd.count("--tag") == 2
    assert "--only" in cmd and "hoj-1000" in cmd
    assert "--run-doall" not in cmd and "--no-run-doall" not in cmd


def test_hydro_to_hoj_command_uses_only_bridge_args() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        paths = JobPaths(root / "jobs" / ("3" * 32))
        paths.input_dir.mkdir(parents=True)
        paths.output_dir.mkdir(parents=True)
        request = JobRequest(
            job_id="3" * 32, target="hydro_to_hoj", only=["P1000"], run_doall=True
        )

        cmd = build_docker_command(_settings(root), request.job_id, paths, request)

    assert "hydro-to-hoj" in cmd
    assert "--only" in cmd and "P1000" in cmd
    assert "--pid-start" not in cmd
    assert "--owner" not in cmd
    assert "--color" not in cmd
    assert "--run-doall" not in cmd and "--no-run-doall" not in cmd


def test_hoj_to_domjudge_command_uses_domjudge_output_args() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        paths = JobPaths(root / "jobs" / ("4" * 32))
        paths.input_dir.mkdir(parents=True)
        paths.output_dir.mkdir(parents=True)
        request = JobRequest(
            job_id="4" * 32,
            target="hoj_to_domjudge",
            domjudge_code_start="D",
            domjudge_color="#123ABC",
            only=["hoj-1000"],
        )

        cmd = build_docker_command(_settings(root), request.job_id, paths, request)

    assert "hoj-to-domjudge" in cmd
    assert "--code-start" in cmd and "D" in cmd
    assert "--color" in cmd and "#123ABC" in cmd
    assert "--only" in cmd and "hoj-1000" in cmd
    assert "--auto-validator" not in cmd and "--default-validator" not in cmd


def test_matrix_command_passes_format_profiles_and_uses_normal_runner() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        paths = JobPaths(root / "jobs" / ("5" * 32))
        paths.input_dir.mkdir(parents=True)
        paths.output_dir.mkdir(parents=True)
        request = JobRequest(
            job_id="5" * 32,
            source_format="fps",
            target_format="icpc",
            loss_policy="error",
            only=["sum"],
            options={
                "hydro": {"pid_start": "H200", "owner": 9, "tags": ["ICPC"]},
                "icpc": {
                    "code_start": "C",
                    "color": "#123ABC",
                    "profile": "2025-09",
                    "license": "cc by-sa",
                    "rights_owner": "ICPC",
                },
                "fps": {"profile": "qduoj-1.2"},
                "polygon": {
                    "run_doall": False,
                    "missing_env": "error",
                    "with_statement": True,
                    "with_attachments": True,
                    "validator_mode": "default",
                },
            },
        )

        cmd = build_docker_command(
            _settings_with_image(root, "p2h-runner-wine"),
            request.job_id,
            paths,
            request,
        )

    assert cmd[:2] == ["docker", "run"]
    assert "--platform" not in cmd
    assert "p2h-runner" in cmd and "p2h-runner-wine" not in cmd
    assert "package-convert" in cmd
    for option, value in (
        ("--source-format", "fps"),
        ("--target-format", "icpc"),
        ("--loss-policy", "error"),
        ("--pid-start", "H200"),
        ("--owner", "9"),
        ("--code-start", "C"),
        ("--color", "#123ABC"),
        ("--icpc-profile", "2025-09"),
        ("--icpc-license", "cc by-sa"),
        ("--icpc-rights-owner", "ICPC"),
        ("--fps-profile", "qduoj-1.2"),
        ("--missing-env", "error"),
        ("--validator-mode", "default"),
    ):
        assert cmd[cmd.index(option) + 1] == value
    assert "--only" in cmd and "sum" in cmd
    assert "--tag" in cmd and "ICPC" in cmd
    assert "--with-statement" in cmd
    assert "--with-attachments" in cmd
    assert "--no-run-doall" in cmd


def test_pack_output_merges_hydro_problem_packages() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        output_dir = root / "output"
        output_dir.mkdir()
        result_path = root / "result.zip"

        with zipfile.ZipFile(
            output_dir / "a.zip", "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.writestr("P1000/problem.yaml", "title: A\n")
        with zipfile.ZipFile(
            output_dir / "b.zip", "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
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


def test_pack_output_merges_domjudge_to_hydro_problem_packages() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        output_dir = root / "output"
        output_dir.mkdir()
        result_path = root / "result.zip"

        with zipfile.ZipFile(
            output_dir / "a.zip", "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.writestr("P1000/problem.yaml", "title: A\n")
        with zipfile.ZipFile(
            output_dir / "b.zip", "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.writestr("P1001/problem.yaml", "title: B\n")

        pack_output(output_dir, result_path, target="domjudge_to_hydro")

        with zipfile.ZipFile(result_path) as archive:
            assert archive.namelist() == ["P1000/problem.yaml", "P1001/problem.yaml"]


def test_pack_output_merges_hoj_to_hydro_problem_packages() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        output_dir = root / "output"
        output_dir.mkdir()
        result_path = root / "result.zip"

        with zipfile.ZipFile(
            output_dir / "a.zip", "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.writestr("H100/problem.yaml", "title: A\n")
        with zipfile.ZipFile(
            output_dir / "b.zip", "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.writestr("H101/problem.yaml", "title: B\n")

        pack_output(output_dir, result_path, target="hoj_to_hydro")

        with zipfile.ZipFile(result_path) as archive:
            assert archive.namelist() == ["H100/problem.yaml", "H101/problem.yaml"]


def test_pack_output_keeps_hydro_to_hoj_files_at_archive_root() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        output_dir = root / "output"
        (output_dir / "problem_p1000").mkdir(parents=True)
        (output_dir / "problem_p1000.json").write_text(
            '{"problem": {}}\n', encoding="utf-8"
        )
        (output_dir / "problem_p1000" / "1.in").write_text("1\n", encoding="utf-8")
        result_path = root / "result.zip"

        pack_output(output_dir, result_path, target="hydro_to_hoj")

        with zipfile.ZipFile(result_path) as archive:
            assert set(archive.namelist()) == {
                "problem_p1000.json",
                "problem_p1000/1.in",
            }


def test_pack_output_allows_highly_compressible_generated_test_data() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        output_dir = root / "output"
        output_dir.mkdir()
        result_path = root / "result.zip"
        with zipfile.ZipFile(
            output_dir / "bomb.zip", "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.writestr("P1000/huge.bin", b"\0" * (4 * 1024 * 1024))

        pack_output(output_dir, result_path, target="hydro")

        with zipfile.ZipFile(result_path) as archive:
            info = archive.getinfo("P1000/huge.bin")
            assert info.file_size == 4 * 1024 * 1024
            assert info.file_size / info.compress_size > 200


def test_pack_output_still_rejects_generated_data_over_size_limit() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        output_dir = root / "output"
        output_dir.mkdir()
        result_path = root / "result.zip"
        with zipfile.ZipFile(
            output_dir / "large.zip", "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.writestr("P1000/large.in", b"0" * 4096)

        with pytest.raises(ValueError, match="uncompressed size limit"):
            pack_output(
                output_dir,
                result_path,
                target="hydro",
                max_uncompressed_bytes=1024,
            )

        assert not result_path.exists()


def test_pack_output_rejects_symlink_without_reading_its_target() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        output_dir = root / "output"
        output_dir.mkdir()
        secret = root / "host-secret.txt"
        secret.write_text("must not be archived", encoding="utf-8")
        (output_dir / "leak.txt").symlink_to(secret)
        result_path = root / "result.zip"

        with pytest.raises(ValueError, match="output symlinks are not supported"):
            pack_output(output_dir, result_path, target="domjudge")

        assert not result_path.exists()


def test_pack_output_rejects_empty_converter_output() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        output_dir = root / "output"
        output_dir.mkdir()
        result_path = root / "result.zip"

        with pytest.raises(ValueError, match="produced no output files"):
            pack_output(output_dir, result_path, target="qduoj")

        assert not result_path.exists()


@pytest.mark.parametrize(
    ("value", "expected"), [("1g", 1024**3), ("512MiB", 512 * 1024**2), ("1.5k", 1536)]
)
def test_parse_size_bytes(value: str, expected: int) -> None:
    assert parse_size_bytes(value) == expected
