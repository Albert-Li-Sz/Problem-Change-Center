from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("mode", "variable", "value"),
    [
        ("--backend-only", "P2H_BACKEND_PORT", "8000"),
        ("--frontend-only", "P2H_FRONTEND_PORT", "5173"),
    ],
)
def test_start_script_rejects_nonstandard_deployment_ports(
    tmp_path: Path, mode: str, variable: str, value: str
) -> None:
    project_root = tmp_path / "project"
    scripts_dir = project_root / "scripts"
    scripts_dir.mkdir(parents=True)
    start_script = scripts_dir / "start.sh"
    shutil.copy2(PROJECT_ROOT / "scripts" / "start.sh", start_script)
    (project_root / ".env").write_text(
        "P2H_BACKEND_HOST=127.0.0.1\nP2H_FRONTEND_HOST=127.0.0.1\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment[variable] = value

    completed = subprocess.run(
        ["bash", str(start_script), mode],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 1
    assert "ports are fixed at backend 11451 and frontend 11452" in completed.stderr


def test_start_script_reports_missing_runner_image(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    scripts_dir = project_root / "scripts"
    fake_bin = tmp_path / "bin"
    scripts_dir.mkdir(parents=True)
    fake_bin.mkdir()
    start_script = scripts_dir / "start.sh"
    shutil.copy2(PROJECT_ROOT / "scripts" / "start.sh", start_script)
    (project_root / ".env").write_text(
        "P2H_RUNNER_IMAGE=missing-runner\n", encoding="utf-8"
    )
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/usr/bin/env bash
if [[ "${1:-}" == "info" ]]; then exit 0; fi
if [[ "${1:-}" == "image" && "${2:-}" == "inspect" ]]; then exit 1; fi
exit 1
""",
        encoding="utf-8",
    )
    fake_docker.chmod(fake_docker.stat().st_mode | 0o100)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"

    completed = subprocess.run(
        ["bash", str(start_script), "--backend-only"],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 1
    assert "configured runner image missing-runner is missing" in completed.stderr
    assert "docker compose --profile runner build runner" in completed.stderr


def test_start_script_accepts_local_oci_index_image(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    scripts_dir = project_root / "scripts"
    fake_bin = tmp_path / "bin"
    scripts_dir.mkdir(parents=True)
    fake_bin.mkdir()
    start_script = scripts_dir / "start.sh"
    shutil.copy2(PROJECT_ROOT / "scripts" / "start.sh", start_script)
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/usr/bin/env bash
if [[ "${1:-}" == "info" ]]; then exit 0; fi
if [[ "${1:-}" == "image" && "${2:-}" == "inspect" ]]; then exit 1; fi
if [[ "${1:-}" == "image" && "${2:-}" == "ls" ]]; then printf 'oci-index-id\n'; exit 0; fi
exit 1
""",
        encoding="utf-8",
    )
    fake_docker.chmod(fake_docker.stat().st_mode | 0o100)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"

    completed = subprocess.run(
        ["bash", str(start_script), "--backend-only"],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 1
    assert "backend/.venv is missing" in completed.stderr
    assert "configured runner image" not in completed.stderr


def test_installer_rejects_unsupported_node_version(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    scripts_dir = project_root / "scripts"
    frontend_dir = project_root / "frontend"
    fake_bin = tmp_path / "bin"
    scripts_dir.mkdir(parents=True)
    frontend_dir.mkdir()
    fake_bin.mkdir()
    install_script = scripts_dir / "install.sh"
    shutil.copy2(PROJECT_ROOT / "scripts" / "install.sh", install_script)
    fake_node = fake_bin / "node"
    fake_node.write_text(
        """#!/usr/bin/env bash
if [[ "${1:-}" == "-p" ]]; then printf '18.20.0\\n'; exit 0; fi
if [[ "${1:-}" == "-e" ]]; then exit 1; fi
exit 0
""",
        encoding="utf-8",
    )
    fake_node.chmod(fake_node.stat().st_mode | 0o100)
    fake_npm = fake_bin / "npm"
    fake_npm.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_npm.chmod(fake_npm.stat().st_mode | 0o100)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"

    completed = subprocess.run(
        [
            "bash",
            str(install_script),
            "--skip-backend",
            "--skip-runner",
            "--no-frontend-build",
        ],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 1
    assert "Node.js 20.19+, 22.12+, or 24+ is required" in completed.stderr
    assert "found 18.20.0" in completed.stderr


def test_installer_pulls_and_configures_prebuilt_runner(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    scripts_dir = project_root / "scripts"
    fake_bin = tmp_path / "bin"
    scripts_dir.mkdir(parents=True)
    fake_bin.mkdir()
    install_script = scripts_dir / "install.sh"
    shutil.copy2(PROJECT_ROOT / "scripts" / "install.sh", install_script)
    pulled = tmp_path / "pulled.txt"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        f"""#!/usr/bin/env bash
if [[ "${{1:-}}" == "info" ]]; then exit 0; fi
if [[ "${{1:-}}" == "pull" ]]; then printf '%s' "${{2:-}}" > "{pulled}"; exit 0; fi
exit 1
""",
        encoding="utf-8",
    )
    fake_docker.chmod(fake_docker.stat().st_mode | 0o100)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
    image = "ghcr.io/albert-li-sz/p2h-runner@sha256:" + "a" * 64

    completed = subprocess.run(
        [
            "bash",
            str(install_script),
            "--skip-backend",
            "--skip-frontend",
            "--runner-image",
            image,
        ],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0
    assert pulled.read_text(encoding="utf-8") == image
    assert f"P2H_RUNNER_IMAGE={image}\n" in (project_root / ".env").read_text(
        encoding="utf-8"
    )
    assert "P2H_BACKEND_PORT=11451\n" in (project_root / ".env").read_text(
        encoding="utf-8"
    )
    assert "P2H_FRONTEND_PORT=11452\n" in (project_root / ".env").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("options", "answers", "images"),
    [
        (
            ["--interactive", "--no-systemd"],
            "1\ny\n1\n",
            [
                "ghcr.io/albert-li-sz/p2h-runner-wine:main",
                "ghcr.io/albert-li-sz/p2h-runner:main",
            ],
        ),
        (
            ["--non-interactive", "--no-wine", "--runner-source", "prebuilt"],
            "",
            ["ghcr.io/albert-li-sz/p2h-runner:main"],
        ),
        (
            [
                "--non-interactive",
                "--runner-image",
                "registry.example:5000/team/runner-wine:test-wine",
            ],
            "",
            [
                "registry.example:5000/team/runner-wine:test-wine",
                "registry.example:5000/team/runner:test-wine",
            ],
        ),
        (
            [
                "--non-interactive",
                "--runner-image",
                "ghcr.io/example/runner-wine@sha256:" + "a" * 64,
            ],
            "",
            ["ghcr.io/example/runner-wine@sha256:" + "a" * 64],
        ),
    ],
)
def test_installer_pulls_current_namespace_and_companion_runner(
    tmp_path: Path, options: list[str], answers: str, images: list[str]
) -> None:
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / "scripts/install.sh", project / "scripts/install.sh")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    pulled = tmp_path / "pulled.txt"
    calls = tmp_path / "docker-calls.txt"
    docker = fake_bin / "docker"
    docker.write_text(
        f"#!/usr/bin/env bash\n"
        'if [[ "${1:-}" == info ]]; then exit 0; fi\n'
        f'if [[ "${{1:-}}" == pull ]]; then printf "%s\\n" "${{@: -1}}" >> "{pulled}"; printf "%s\\n" "$*" >> "{calls}"; exit 0; fi\n'
        "exit 1\n"
    )
    docker.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env["PATH"]
    env.pop("P2H_PREBUILT_RUNNER_IMAGE", None)

    result = subprocess.run(
        [
            "bash",
            str(project / "scripts/install.sh"),
            "--skip-backend",
            "--skip-frontend",
            *options,
        ],
        cwd=project,
        env=env,
        input=answers,
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert pulled.read_text().splitlines() == images
    if "runner-wine" in images[0]:
        assert (
            calls.read_text().splitlines()[0].startswith("pull --platform linux/amd64 ")
        )
    assert f"P2H_RUNNER_IMAGE={images[0]}\n" in (project / ".env").read_text()


@pytest.mark.parametrize(
    ("options", "bind"),
    [
        ([], "127.0.0.1"),
        (["--local"], "127.0.0.1"),
        (["--lan"], "0.0.0.0"),  # nosec B104
        (["--bind", "0.0.0.0"], "0.0.0.0"),  # nosec B104
    ],
)
def test_installer_writes_bind_address_without_access_control(
    tmp_path: Path, options: list[str], bind: str
) -> None:
    project_root = tmp_path / "project"
    scripts_dir = project_root / "scripts"
    scripts_dir.mkdir(parents=True)
    install_script = scripts_dir / "install.sh"
    shutil.copy2(PROJECT_ROOT / "scripts" / "install.sh", install_script)
    config_file = project_root / "generated.env"

    completed = subprocess.run(
        [
            "bash",
            str(install_script),
            "--non-interactive",
            *options,
            "--skip-backend",
            "--skip-frontend",
            "--skip-runner",
            "--config",
            str(config_file),
        ],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    config = config_file.read_text(encoding="utf-8")
    assert f"P2H_BACKEND_HOST={bind}\n" in config
    assert f"P2H_FRONTEND_HOST={bind}\n" in config
    assert "P2H_BACKEND_PORT=11451" in config
    assert "P2H_FRONTEND_PORT=11452" in config
    assert "ACCESS_KEY" not in config
    assert "DEPLOYMENT_MODE" not in config
    assert "RATE_LIMIT" not in config
    assert stat.S_IMODE(config_file.stat().st_mode) == 0o600
    assert ("trusted LAN" in completed.stderr) == (bind == "0.0.0.0")  # nosec B104


def test_installer_rejects_unsupported_bind_address(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            "bash",
            str(PROJECT_ROOT / "scripts" / "install.sh"),
            "--non-interactive",
            "--bind",
            "192.168.1.10",
            "--skip-backend",
            "--skip-frontend",
            "--skip-runner",
            "--config",
            str(tmp_path / "rejected.env"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode != 0
    assert "127.0.0.1 or 0.0.0.0" in completed.stderr
    assert not (tmp_path / "rejected.env").exists()


def test_interactive_installer_collects_bind_and_wine_choices(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    scripts_dir = project_root / "scripts"
    scripts_dir.mkdir(parents=True)
    install_script = scripts_dir / "install.sh"
    shutil.copy2(PROJECT_ROOT / "scripts" / "install.sh", install_script)
    config_file = project_root / "interactive.env"

    completed = subprocess.run(
        [
            "bash",
            str(install_script),
            "--interactive",
            "--no-systemd",
            "--skip-backend",
            "--skip-frontend",
            "--config",
            str(config_file),
        ],
        cwd=project_root,
        input="2\ny\n3\n",
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    config = config_file.read_text(encoding="utf-8")
    assert "P2H_BACKEND_HOST=0.0.0.0" in config
    assert "P2H_FRONTEND_HOST=0.0.0.0" in config
    assert "P2H_RUNNER_IMAGE=p2h-runner-wine" in config
    assert "ACCESS_KEY" not in config
