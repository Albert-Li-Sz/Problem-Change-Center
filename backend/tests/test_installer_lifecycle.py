from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_UNIT = "oj-package-converter.service"
FRONTEND_UNIT = "oj-package-converter-frontend.service"


@dataclass
class Sandbox:
    root: Path
    home: Path
    units: Path
    systemctl_log: Path
    docker_log: Path
    env: dict[str, str]

    @property
    def config(self) -> Path:
        return self.root / ".env"

    def run(
        self, *args: str, input: str | None = None, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.root / "scripts" / "install.sh"), *args],
            cwd=cwd or self.root,
            env=self.env,
            input=input,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def systemctl_calls(self) -> list[str]:
        if not self.systemctl_log.exists():
            return []
        return self.systemctl_log.read_text(encoding="utf-8").splitlines()

    def docker_calls(self) -> list[str]:
        if not self.docker_log.exists():
            return []
        return self.docker_log.read_text(encoding="utf-8").splitlines()


def _executable(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _make_installed_layout(root: Path) -> None:
    uvicorn = root / "backend" / ".venv" / "bin" / "uvicorn"
    uvicorn.parent.mkdir(parents=True)
    _executable(uvicorn, "exit 0\n")
    (root / "frontend" / "dist").mkdir(parents=True)
    (root / "frontend" / "dist" / "index.html").write_text("<html></html>")
    (root / "frontend" / "node_modules" / "pkg").mkdir(parents=True)


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    root = tmp_path / "project"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / "scripts" / "install.sh", root / "scripts")
    shutil.copytree(PROJECT_ROOT / "deploy" / "systemd", root / "deploy" / "systemd")
    _make_installed_layout(root)

    home = tmp_path / "home"
    home.mkdir()
    units = tmp_path / "units"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl_log = tmp_path / "systemctl.log"
    docker_log = tmp_path / "docker.log"
    _executable(fake_bin / "sudo", 'exec "$@"\n')
    _executable(fake_bin / "systemctl", f'printf "%s\\n" "$*" >> "{systemctl_log}"\n')
    _executable(fake_bin / "node", "exit 0\n")
    _executable(
        fake_bin / "docker",
        f'printf "%s\\n" "$*" >> "{docker_log}"\n'
        'case "${1:-}" in info|pull) exit 0 ;; image) exit 0 ;; esac\n'
        "exit 1\n",
    )

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "P2H_SYSTEMD_UNIT_DIR": str(units),
        }
    )
    for name in ("P2H_PREBUILT_RUNNER_IMAGE", "P2H_INSTALL_BIND", "P2H_CONFIG_FILE"):
        env.pop(name, None)
    return Sandbox(root, home, units, systemctl_log, docker_log, env)


def _install(sandbox: Sandbox, *extra: str) -> subprocess.CompletedProcess[str]:
    return sandbox.run(
        "--non-interactive",
        "--skip-backend",
        "--skip-frontend",
        "--skip-runner",
        *extra,
    )


def test_install_with_systemd_renders_and_starts_units(sandbox: Sandbox) -> None:
    completed = _install(sandbox, "--lan", "--systemd")

    assert completed.returncode == 0, completed.stderr
    backend = (sandbox.units / BACKEND_UNIT).read_text(encoding="utf-8")
    frontend = (sandbox.units / FRONTEND_UNIT).read_text(encoding="utf-8")
    data_dir = sandbox.home / ".p2h-web-ui" / "backend_data"
    user = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()

    for unit in (backend, frontend):
        assert not re.search(r"@[A-Z_]+@", unit)
        assert f"User={user}" in unit
        assert f"EnvironmentFile={sandbox.config}" in unit
    assert f"WorkingDirectory={sandbox.root}/backend" in backend
    assert (
        f"ExecStart={sandbox.root}/backend/.venv/bin/uvicorn app.main:app "
        "--host ${P2H_BACKEND_HOST} --port 11451"
    ) in backend
    assert f"ReadWritePaths={data_dir}" in backend
    assert "SupplementaryGroups" not in backend
    assert "RemoveIPC" not in backend
    assert f"WorkingDirectory={sandbox.root}" in frontend
    assert f"{sandbox.root}/scripts/serve-frontend.mjs" in frontend
    assert data_dir.is_dir()
    assert "P2H_BACKEND_HOST=0.0.0.0\n" in sandbox.config.read_text()
    assert sandbox.systemctl_calls() == [
        "daemon-reload",
        f"enable {BACKEND_UNIT} {FRONTEND_UNIT}",
        f"restart {BACKEND_UNIT} {FRONTEND_UNIT}",
    ]
    assert "systemctl status" in completed.stdout
    assert "./install.sh update" in completed.stdout


def test_install_with_systemd_requires_backend_environment(sandbox: Sandbox) -> None:
    shutil.rmtree(sandbox.root / "backend" / ".venv")

    completed = _install(sandbox, "--systemd")

    assert completed.returncode == 1
    assert "backend/.venv is missing" in completed.stderr
    assert sandbox.systemctl_calls() == []


def test_interactive_install_offers_systemd(sandbox: Sandbox) -> None:
    completed = sandbox.run(
        "--interactive",
        "--skip-backend",
        "--skip-frontend",
        input="1\nn\n3\ny\n",
    )

    assert completed.returncode == 0, completed.stderr
    assert "Install systemd services" in completed.stderr
    assert (sandbox.units / BACKEND_UNIT).is_file()
    assert "P2H_BACKEND_HOST=127.0.0.1\n" in sandbox.config.read_text()


def test_reinstall_keeps_config_and_refreshes_own_services(sandbox: Sandbox) -> None:
    assert _install(sandbox, "--lan", "--systemd").returncode == 0
    sandbox.systemctl_log.unlink()

    completed = _install(sandbox)

    assert completed.returncode == 0, completed.stderr
    assert "Keeping existing configuration" in completed.stdout
    assert "P2H_BACKEND_HOST=0.0.0.0\n" in sandbox.config.read_text()
    assert f"restart {BACKEND_UNIT} {FRONTEND_UNIT}" in sandbox.systemctl_calls()


def test_services_of_another_checkout_are_left_alone(sandbox: Sandbox) -> None:
    sandbox.units.mkdir()
    foreign = "[Service]\nWorkingDirectory=/srv/other/backend\n"
    (sandbox.units / BACKEND_UNIT).write_text(foreign, encoding="utf-8")

    installed = _install(sandbox)
    removed = sandbox.run("uninstall", "--yes")

    assert installed.returncode == 0, installed.stderr
    assert removed.returncode == 0, removed.stderr
    assert "another checkout" in removed.stderr
    assert (sandbox.units / BACKEND_UNIT).read_text(encoding="utf-8") == foreign
    assert sandbox.systemctl_calls() == []


def test_update_requires_existing_configuration(sandbox: Sandbox) -> None:
    completed = sandbox.run("update", "--no-pull", "--skip-backend", "--skip-frontend")

    assert completed.returncode == 1
    assert "run ./install.sh first" in completed.stderr


def test_update_pulls_configured_runner_and_restarts_services(
    sandbox: Sandbox,
) -> None:
    image = "registry.example:5000/team/p2h-runner-wine:v1"
    installed = _install(sandbox, "--systemd")
    assert installed.returncode == 0, installed.stderr
    config = sandbox.config.read_text(encoding="utf-8")
    sandbox.config.write_text(
        config.replace("P2H_RUNNER_IMAGE=p2h-runner\n", f"P2H_RUNNER_IMAGE={image}\n"),
        encoding="utf-8",
    )
    sandbox.systemctl_log.unlink()

    completed = sandbox.run("update", "--no-pull", "--skip-backend", "--skip-frontend")

    assert completed.returncode == 0, completed.stderr
    assert "Update complete" in completed.stdout
    assert f"pull --platform linux/amd64 {image}" in sandbox.docker_calls()
    assert "pull registry.example:5000/team/p2h-runner:v1" in sandbox.docker_calls()
    assert sandbox.config.read_text(encoding="utf-8").count(image) == 1
    assert sandbox.systemctl_calls() == [
        "daemon-reload",
        f"enable {BACKEND_UNIT} {FRONTEND_UNIT}",
        f"restart {BACKEND_UNIT} {FRONTEND_UNIT}",
    ]


def test_update_pulls_source_then_reruns_new_installer(
    sandbox: Sandbox, tmp_path: Path
) -> None:
    def git(*args: str, cwd: Path) -> None:
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
        )

    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "--initial-branch=main", str(origin)],
        check=True,
    )
    (sandbox.root / ".gitignore").write_text(
        ".env\nbackend/.venv/\nfrontend/dist/\nfrontend/node_modules/\n"
    )
    git("init", "-q", "-b", "main", cwd=sandbox.root)
    git("add", ".", cwd=sandbox.root)
    git("commit", "-q", "-m", "initial", cwd=sandbox.root)
    git("remote", "add", "origin", str(origin), cwd=sandbox.root)
    git("push", "-q", "-u", "origin", "main", cwd=sandbox.root)
    assert _install(sandbox).returncode == 0

    other = tmp_path / "other"
    git("clone", "-q", str(origin), str(other), cwd=tmp_path)
    (other / "NEW_FILE").write_text("new\n")
    git("add", "NEW_FILE", cwd=other)
    git("commit", "-q", "-m", "upstream change", cwd=other)
    git("push", "-q", "origin", "main", cwd=other)

    completed = sandbox.run(
        "update", "--skip-backend", "--skip-frontend", "--skip-runner"
    )

    assert completed.returncode == 0, completed.stderr
    assert (sandbox.root / "NEW_FILE").is_file()
    assert "upstream change" in completed.stdout
    assert "Continuing with the updated installer" in completed.stdout
    assert "Update complete" in completed.stdout

    (sandbox.root / "NEW_FILE").write_text("local edit\n")
    dirty = sandbox.run("update", "--skip-backend", "--skip-frontend", "--skip-runner")
    assert dirty.returncode == 1
    assert "uncommitted changes" in dirty.stderr


def test_uninstall_requires_confirmation_without_terminal(sandbox: Sandbox) -> None:
    assert _install(sandbox, "--systemd").returncode == 0

    completed = sandbox.run("uninstall")

    assert completed.returncode == 1
    assert "--yes" in completed.stderr
    assert (sandbox.root / "backend" / ".venv").is_dir()
    assert (sandbox.units / BACKEND_UNIT).is_file()


def test_uninstall_removes_services_and_build_output_but_keeps_data(
    sandbox: Sandbox,
) -> None:
    assert _install(sandbox, "--systemd").returncode == 0
    data_dir = sandbox.home / ".p2h-web-ui" / "backend_data"
    (data_dir / "jobs").mkdir(parents=True)
    sandbox.systemctl_log.unlink()

    completed = sandbox.run("uninstall", "--yes")

    assert completed.returncode == 0, completed.stderr
    assert sandbox.systemctl_calls() == [
        f"disable --now {BACKEND_UNIT} {FRONTEND_UNIT}",
        "daemon-reload",
    ]
    assert not (sandbox.units / BACKEND_UNIT).exists()
    assert not (sandbox.units / FRONTEND_UNIT).exists()
    assert not (sandbox.root / "backend" / ".venv").exists()
    assert not (sandbox.root / "frontend" / "node_modules").exists()
    assert not (sandbox.root / "frontend" / "dist").exists()
    assert sandbox.config.is_file()
    assert (data_dir / "jobs").is_dir()
    assert (sandbox.root / "scripts" / "install.sh").is_file()


def test_uninstall_purge_deletes_config_data_and_images(sandbox: Sandbox) -> None:
    assert _install(sandbox).returncode == 0
    data_dir = sandbox.home / ".p2h-web-ui" / "backend_data"
    (data_dir / "jobs").mkdir(parents=True)

    completed = sandbox.run("uninstall", "--purge", "--remove-images", "--yes")

    assert completed.returncode == 0, completed.stderr
    assert not sandbox.config.exists()
    assert not data_dir.exists()
    assert "image rm p2h-runner" in sandbox.docker_calls()
    assert "image rm p2h-runner-wine" in sandbox.docker_calls()


@pytest.mark.parametrize("data_dir", ["~", "/"])
def test_uninstall_purge_refuses_unsafe_data_directory(
    sandbox: Sandbox, data_dir: str
) -> None:
    assert _install(sandbox).returncode == 0
    config = sandbox.config.read_text(encoding="utf-8")
    sandbox.config.write_text(
        config.replace(
            "P2H_DATA_DIR=~/.p2h-web-ui/backend_data\n", f"P2H_DATA_DIR={data_dir}\n"
        ),
        encoding="utf-8",
    )

    completed = sandbox.run("uninstall", "--purge", "--yes")

    assert completed.returncode == 1
    assert "refusing to delete unsafe data directory" in completed.stderr
    assert sandbox.home.is_dir()
    assert sandbox.config.is_file()


def test_lifecycle_options_are_scoped_to_their_command(sandbox: Sandbox) -> None:
    assert (
        "only valid with: ./install.sh uninstall" in _install(sandbox, "--purge").stderr
    )
    assert (
        "only valid with: ./install.sh update" in _install(sandbox, "--no-pull").stderr
    )
    assert sandbox.run("uninstall", "--systemd").returncode == 1
