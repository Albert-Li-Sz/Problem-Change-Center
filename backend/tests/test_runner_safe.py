import importlib
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
RUNNER_DIR = ROOT / "runner"
sys.path.insert(0, str(RUNNER_DIR))
p2h_safe = importlib.import_module("p2h_safe")


def test_normalize_polygon_executable_bits_repairs_scripts_and_exes(tmp_path: Path) -> None:
    shell_script = tmp_path / "problems" / "sum" / "scripts" / "gen-input.sh"
    shebang_tool = tmp_path / "problems" / "sum" / "files" / "tool"
    windows_exe = tmp_path / "problems" / "sum" / "files" / "gen.exe"
    plain_text = tmp_path / "problems" / "sum" / "statement.txt"

    shell_script.parent.mkdir(parents=True)
    shell_script.write_text("#!/usr/bin/env bash\necho ok\n", encoding="utf-8")
    shebang_tool.parent.mkdir(parents=True)
    shebang_tool.write_text("#!/usr/bin/env python3\nprint('ok')\n", encoding="utf-8")
    windows_exe.write_bytes(b"MZ fake exe")
    plain_text.write_text("not executable", encoding="utf-8")

    for path in (shell_script, shebang_tool, windows_exe, plain_text):
        path.chmod(0o644)

    assert p2h_safe.normalize_polygon_executable_bits(tmp_path) == 3

    for path in (shell_script, shebang_tool, windows_exe):
        assert os.access(path, os.X_OK)
    assert not os.access(plain_text, os.X_OK)
    assert p2h_safe.normalize_polygon_executable_bits(tmp_path) == 0


def test_p2h_doall_patch_repairs_permissions_before_running(tmp_path: Path, monkeypatch) -> None:
    script = tmp_path / "problems" / "sum" / "doall.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env bash\necho ok\n", encoding="utf-8")
    script.chmod(0o644)

    p2h_module = types.ModuleType("p2h")
    p2h_module.__path__ = []  # type: ignore[attr-defined]
    convert_module = types.ModuleType("p2h.convert")
    calls: list[bool] = []

    def run_doall(work_root: Path, slugs: list[str], *, verbose: bool = False) -> int:
        calls.append(os.access(script, os.X_OK))
        return 0

    convert_module._run_doall_for_all = run_doall  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "p2h", p2h_module)
    monkeypatch.setitem(sys.modules, "p2h.convert", convert_module)

    patched_convert = p2h_safe._install_p2h_patches()
    assert patched_convert._run_doall_for_all(tmp_path, ["sum"], verbose=True) == 0
    assert calls == [True]


def test_p2h_doall_patch_turns_pause_reads_into_failures(tmp_path: Path, monkeypatch) -> None:
    p2h_module = types.ModuleType("p2h")
    p2h_module.__path__ = []  # type: ignore[attr-defined]
    convert_module = types.ModuleType("p2h.convert")

    def run_doall(work_root: Path, slugs: list[str], *, verbose: bool = False) -> None:
        subprocess.run(
            ["bash", "-c", "read || true; echo continued"],
            cwd=work_root,
            stdin=subprocess.DEVNULL,
            check=True,
        )

    convert_module._run_doall_for_all = run_doall  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "p2h", p2h_module)
    monkeypatch.setitem(sys.modules, "p2h.convert", convert_module)
    monkeypatch.delenv("BASH_ENV", raising=False)

    patched_convert = p2h_safe._install_p2h_patches()
    with pytest.raises(subprocess.CalledProcessError):
        patched_convert._run_doall_for_all(tmp_path, ["sum"], verbose=False)
    assert "BASH_ENV" not in os.environ
