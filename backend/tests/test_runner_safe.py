import importlib
import os
import subprocess
import sys
import types
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
RUNNER_DIR = ROOT / "runner"
sys.path.insert(0, str(RUNNER_DIR))
p2h_safe = importlib.import_module("p2h_safe")


def test_normalize_polygon_executable_bits_repairs_scripts_and_exes(
    tmp_path: Path,
) -> None:
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


def test_normalize_polygon_testdata_line_endings_only_changes_selected_tests(
    tmp_path: Path,
) -> None:
    selected_tests = tmp_path / "problems" / "sum" / "tests"
    selected_tests.mkdir(parents=True)
    (selected_tests / "01").write_bytes(b"1 2\r\n3 4\r\n")
    (selected_tests / "01.a").write_bytes(b"3\r\n7\r")
    (selected_tests / "already-lf").write_bytes(b"unchanged\n")

    source = tmp_path / "problems" / "sum" / "files" / "generator-source.txt"
    source.parent.mkdir()
    source.write_bytes(b"keep\r\nsource\r\n")

    other_tests = tmp_path / "problems" / "other" / "tests"
    other_tests.mkdir(parents=True)
    (other_tests / "01").write_bytes(b"keep\r\nother\r\n")

    assert p2h_safe.normalize_polygon_testdata_line_endings(tmp_path, ["sum"]) == 2
    assert (selected_tests / "01").read_bytes() == b"1 2\n3 4\n"
    assert (selected_tests / "01.a").read_bytes() == b"3\n7\r"
    assert (selected_tests / "already-lf").read_bytes() == b"unchanged\n"
    assert source.read_bytes() == b"keep\r\nsource\r\n"
    assert (other_tests / "01").read_bytes() == b"keep\r\nother\r\n"
    assert p2h_safe.normalize_polygon_testdata_line_endings(tmp_path, ["sum"]) == 0


def test_normalize_crlf_file_handles_a_pair_split_across_chunks(tmp_path: Path) -> None:
    testdata = tmp_path / "01"
    testdata.write_bytes(b"abc\r\ndef\r\n")

    assert p2h_safe._normalize_crlf_file(testdata, chunk_size=4) is True
    assert testdata.read_bytes() == b"abc\ndef\n"


def test_collect_tools_ignores_problem_relative_native_executables(
    tmp_path: Path,
) -> None:
    script = tmp_path / "doall.sh"
    script.write_text(
        "files/gen.exe 42\n"
        "solutions/std.exe < tests/01 > tests/01.a\n"
        "./check.exe tests/01 tests/01.a tests/01.a\n",
        encoding="utf-8",
    )
    tools: set[str] = set()

    p2h_safe.collect_tools_from_script(script, tools)

    assert tools == set()


def test_native_polygon_doall_rebuilds_source_backed_wine_commands(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    problem = tmp_path / "problems" / "sum"
    files = problem / "files"
    scripts = problem / "scripts"
    solutions = problem / "solutions"
    validator_tests = files / "tests" / "validator-tests"
    for directory in (files, scripts, solutions, validator_tests):
        directory.mkdir(parents=True, exist_ok=True)

    (files / "gen.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    (files / "check.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    (solutions / "std.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    (files / "gen.exe").write_bytes(b"MZ generator")
    (problem / "check.exe").write_bytes(b"MZ checker")
    (solutions / "std.exe").write_bytes(b"MZ solution")
    (validator_tests / "01").write_bytes(b"1 2\r\n")
    (problem / "problem.xml").write_text(
        """\
<problem>
  <files>
    <executables>
      <executable>
        <source path="files/gen.cpp" type="cpp.gcc14-64-msys2-g++23"/>
        <binary path="files/gen.exe" type="exe.win32"/>
      </executable>
    </executables>
  </files>
  <assets>
    <checker>
      <source path="files/check.cpp" type="cpp.g++17"/>
      <binary path="check.exe" type="exe.win32"/>
    </checker>
    <solutions>
      <solution tag="main">
        <source path="solutions/std.cpp" type="cpp.g++17"/>
        <binary path="solutions/std.exe" type="exe.win32"/>
      </solution>
    </solutions>
  </assets>
</problem>
""",
        encoding="utf-8",
    )
    (problem / "doall.sh").write_text(
        "#!/usr/bin/env bash\n"
        'scripts/generate.sh "wine files/gen.exe 42"\n'
        "wine solutions/std.exe < tests/01 > tests/01.a\n"
        "wine check.exe tests/01 tests/01.a tests/01.a\n",
        encoding="utf-8",
    )
    (scripts / "generate.sh").write_text(
        "#!/usr/bin/env bash\n"
        'eval "$1" > tests/01\n'
        "wine files/towin.exe tests/01 | cat >/dev/null\n",
        encoding="utf-8",
    )

    commands: list[list[str]] = []

    def fake_compile(command: list[str], **_kwargs) -> subprocess.CompletedProcess:
        commands.append(command)
        output = Path(command[command.index("-o") + 1])
        output.write_bytes(b"\x7fELF native")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(p2h_safe.subprocess, "run", fake_compile)

    assert p2h_safe.prepare_native_polygon_doall(tmp_path, ["sum"], verbose=True) == 3
    progress = capsys.readouterr().out
    assert "[sum] native compile 1/3:" in progress
    assert "[sum] native compile complete: 3 executable(s)" in progress
    assert len(commands) == 3
    assert (files / "gen.exe").read_bytes().startswith(b"\x7fELF")
    assert (problem / "check.exe").read_bytes().startswith(b"\x7fELF")
    assert (solutions / "std.exe").read_bytes().startswith(b"\x7fELF")
    assert (files / "towin.exe").read_text(encoding="utf-8").startswith("#!")

    doall = (problem / "doall.sh").read_text(encoding="utf-8")
    helper = (scripts / "generate.sh").read_text(encoding="utf-8")
    assert "\nset -e\n" in doall
    assert "wine " not in doall
    assert "wine " not in helper
    assert '"files/gen.exe 42"' in doall
    assert "./check.exe" in doall
    assert "files/towin.exe" in helper

    assert p2h_safe.normalize_polygon_doall_inputs(tmp_path, ["sum"]) == 1
    assert (validator_tests / "01").read_bytes() == b"1 2\n"


def test_native_polygon_doall_keeps_wine_when_source_is_missing(
    tmp_path: Path,
) -> None:
    problem = tmp_path / "problems" / "sum"
    problem.mkdir(parents=True)
    doall = problem / "doall.sh"
    doall.write_text(
        "#!/usr/bin/env bash\nwine files/binary-only.exe\n", encoding="utf-8"
    )
    (problem / "problem.xml").write_text("<problem/>", encoding="utf-8")

    assert p2h_safe.prepare_native_polygon_doall(tmp_path, ["sum"]) == 0
    assert "wine files/binary-only.exe" in doall.read_text(encoding="utf-8")


def test_polygon_doall_fail_fast_stops_after_first_failed_helper(
    tmp_path: Path,
) -> None:
    problem = tmp_path / "problems" / "sum"
    scripts = problem / "scripts"
    scripts.mkdir(parents=True)
    doall = problem / "doall.sh"
    doall.write_text(
        "#!/usr/bin/env bash\nbash scripts/fail.sh\ntouch should-not-exist\n",
        encoding="utf-8",
    )
    (scripts / "fail.sh").write_text("#!/usr/bin/env bash\nexit 7\n", encoding="utf-8")

    p2h_safe._make_polygon_doall_fail_fast(problem)
    result = subprocess.run(["bash", "doall.sh"], cwd=problem, check=False)

    assert result.returncode == 7
    assert not (problem / "should-not-exist").exists()


def test_polygon_wine_preflight_reports_emulation_failure(
    tmp_path: Path, monkeypatch
) -> None:
    problem = tmp_path / "problems" / "sum"
    problem.mkdir(parents=True)
    (problem / "doall.sh").write_text(
        "#!/usr/bin/env bash\nwine files/tool.exe\n", encoding="utf-8"
    )
    monkeypatch.setattr(p2h_safe.shutil, "which", lambda _name: "/usr/bin/wine")
    monkeypatch.setattr(
        p2h_safe.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["wine"], 134, "", "free(): invalid pointer\nqemu: signal 6"
        ),
    )

    with pytest.raises(RuntimeError, match="Wine runtime preflight failed"):
        p2h_safe.ensure_polygon_wine_is_usable(tmp_path, ["sum"])


def test_checker_language_auto_is_added_to_hydro_config() -> None:
    config = """\
type: default
checker_type: testlib
checker:
  file: check.cpp
time: 1000ms
"""

    patched = p2h_safe.ensure_checker_lang_auto(config)

    assert "checker:\n  file: check.cpp\n  lang: auto\n" in patched
    assert patched.endswith("\n")


def test_checker_language_auto_is_not_duplicated() -> None:
    config = """\
type: default
checker_type: testlib
checker:
  file: check.cpp
  lang: auto
"""

    patched = p2h_safe.ensure_checker_lang_auto(config)

    assert patched.count("lang: auto") == 1


def test_checker_language_auto_treats_language_as_existing() -> None:
    config = """\
checker:
  file: check.cpp
  language: cpp
"""

    patched = p2h_safe.ensure_checker_lang_auto(config)

    assert "lang: auto" not in patched
    assert patched.count("language: cpp") == 1


def test_hydro_writer_patch_adds_checker_language(monkeypatch) -> None:
    p2h_module = types.ModuleType("p2h")
    p2h_module.__path__ = []  # type: ignore[attr-defined]
    writer_module = types.ModuleType("p2h.hydro_writer")

    def build_config_yaml() -> str:
        return "checker:\n  file: check.cpp\n"

    writer_module._build_config_yaml = build_config_yaml  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "p2h", p2h_module)
    monkeypatch.setitem(sys.modules, "p2h.hydro_writer", writer_module)

    p2h_safe._install_hydro_writer_patches()

    assert (
        writer_module._build_config_yaml()
        == "checker:\n  file: check.cpp\n  lang: auto\n"
    )  # type: ignore[attr-defined]


def test_p2h_doall_patch_repairs_permissions_before_running(
    tmp_path: Path, monkeypatch
) -> None:
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


def test_p2h_extract_patch_rejects_zip_bomb_before_upstream_extraction(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "bomb.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("problems/sum/huge.bin", b"\0" * (32 * 1024 * 1024))

    p2h_module = types.ModuleType("p2h")
    p2h_module.__path__ = []  # type: ignore[attr-defined]
    convert_module = types.ModuleType("p2h.convert")
    upstream_called = False

    def safe_extract_contest_zip(_source: Path, _target: Path) -> list[str]:
        nonlocal upstream_called
        upstream_called = True
        return []

    convert_module._safe_extract_contest_zip = safe_extract_contest_zip  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "p2h", p2h_module)
    monkeypatch.setitem(sys.modules, "p2h.convert", convert_module)

    patched_convert = p2h_safe._install_p2h_patches()
    with pytest.raises(ValueError, match="compression ratio limit"):
        patched_convert._safe_extract_contest_zip(source, tmp_path / "output")
    assert upstream_called is False


def test_p2h_doall_patch_normalizes_generated_testdata(
    tmp_path: Path, monkeypatch
) -> None:
    tests_dir = tmp_path / "problems" / "sum" / "tests"
    tests_dir.mkdir(parents=True)

    p2h_module = types.ModuleType("p2h")
    p2h_module.__path__ = []  # type: ignore[attr-defined]
    convert_module = types.ModuleType("p2h.convert")

    def run_doall(work_root: Path, slugs: list[str], *, verbose: bool = False) -> str:
        (tests_dir / "01").write_bytes(b"input\r\n")
        (tests_dir / "01.a").write_bytes(b"answer\r\n")
        return "done"

    convert_module._run_doall_for_all = run_doall  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "p2h", p2h_module)
    monkeypatch.setitem(sys.modules, "p2h.convert", convert_module)

    patched_convert = p2h_safe._install_p2h_patches()
    assert patched_convert._run_doall_for_all(tmp_path, ["sum"], verbose=True) == "done"
    assert (tests_dir / "01").read_bytes() == b"input\n"
    assert (tests_dir / "01.a").read_bytes() == b"answer\n"


def test_p2h_doall_patch_turns_pause_reads_into_failures(
    tmp_path: Path, monkeypatch
) -> None:
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


def test_p2h_doall_patch_turns_read_r_pause_into_failure(
    tmp_path: Path, monkeypatch
) -> None:
    p2h_module = types.ModuleType("p2h")
    p2h_module.__path__ = []  # type: ignore[attr-defined]
    convert_module = types.ModuleType("p2h.convert")

    def run_doall(work_root: Path, slugs: list[str], *, verbose: bool = False) -> None:
        subprocess.run(
            ["bash", "-c", "read -r || true; echo continued"],
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
