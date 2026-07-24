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
    image = "ghcr.io/lmtinsuzhou/p2h-runner@sha256:" + "a" * 64

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


def test_installer_hashes_external_access_key_without_persisting_plaintext(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    scripts_dir = project_root / "scripts"
    scripts_dir.mkdir(parents=True)
    install_script = scripts_dir / "install.sh"
    shutil.copy2(PROJECT_ROOT / "scripts" / "install.sh", install_script)
    config_file = project_root / "external.env"
    access_key = "correct-horse-battery-staple"
    environment = os.environ.copy()
    environment["P2H_ACCESS_KEY"] = access_key

    completed = subprocess.run(
        [
            "bash",
            str(install_script),
            "--non-interactive",
            "--external",
            "--site-address",
            "converter.example.com",
            "--skip-backend",
            "--skip-frontend",
            "--skip-runner",
            "--config",
            str(config_file),
        ],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    config = config_file.read_text(encoding="utf-8")
    assert access_key not in config
    assert "P2H_DEPLOYMENT_MODE=external" in config
    assert "P2H_ALLOWED_ORIGINS=http://converter.example.com:11452" in config
    assert "P2H_BACKEND_PORT=11451" in config
    assert "P2H_FRONTEND_PORT=11452" in config
    assert "P2H_ACCESS_KEY_HASH='pbkdf2_sha256$600000$" in config
    assert stat.S_IMODE(config_file.stat().st_mode) == 0o600


def test_interactive_installer_collects_deployment_wine_and_key_choices(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    scripts_dir = project_root / "scripts"
    scripts_dir.mkdir(parents=True)
    install_script = scripts_dir / "install.sh"
    shutil.copy2(PROJECT_ROOT / "scripts" / "install.sh", install_script)
    config_file = project_root / "interactive.env"
    access_key = "interactive-access-key"

    completed = subprocess.run(
        [
            "bash",
            str(install_script),
            "--interactive",
            "--skip-backend",
            "--skip-frontend",
            "--config",
            str(config_file),
        ],
        cwd=project_root,
        input=f"2\ny\n3\nconverter.example.com\n{access_key}\n{access_key}\n",
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    config = config_file.read_text(encoding="utf-8")
    assert "P2H_DEPLOYMENT_MODE=external" in config
    assert "P2H_RUNNER_IMAGE=p2h-runner-wine" in config
    assert "P2H_ACCESS_KEY_HASH='pbkdf2_sha256$600000$" in config
    assert access_key not in config
