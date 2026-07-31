from __future__ import annotations

from pathlib import Path

from cv_ate_correlation import exe_entry


def test_executable_smoke_test_exercises_frozen_runtime_dependencies() -> None:
    exe_entry.run_frozen_smoke_test()


def test_executable_main_smoke_argument_returns_success(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(exe_entry, "run_frozen_smoke_test", lambda: calls.append("smoke"))

    assert exe_entry.main([exe_entry.SMOKE_TEST_ARGUMENT]) == 0
    assert calls == ["smoke"]


def test_executable_main_logs_smoke_failures_without_opening_dialog(
    tmp_path: Path,
    monkeypatch,
) -> None:
    log = tmp_path / "startup-error.log"

    def fail() -> None:
        raise RuntimeError("frozen smoke failure")

    monkeypatch.setattr(exe_entry, "run_frozen_smoke_test", fail)
    monkeypatch.setattr(exe_entry, "startup_error_log_path", lambda: log)
    monkeypatch.setattr(
        exe_entry,
        "_show_startup_error",
        lambda error, path: (_ for _ in ()).throw(AssertionError("dialog must not open")),
    )

    assert exe_entry.main([exe_entry.SMOKE_TEST_ARGUMENT]) == 1
    assert "frozen smoke failure" in log.read_text(encoding="utf-8")


def test_packaging_files_reference_the_windowed_launcher_and_assets() -> None:
    project_root = Path(__file__).resolve().parents[1]
    spec = (project_root / "correlate_pyinstaller.spec").read_text(encoding="utf-8")
    build = (project_root / "build_correlate_exe.ps1").read_text(encoding="utf-8")
    configuration = (project_root / "pyproject.toml").read_text(encoding="utf-8")
    workflow = (project_root / ".github/workflows/correlate-windows-release.yml").read_text(
        encoding="utf-8"
    )

    assert 'ENTRY_SCRIPT = PROJECT_DIR / "run_correlate.py"' in spec
    assert 'name="CorreLaTE"' in spec
    assert 'console=False' in spec
    assert 'icon=str(ICON)' in spec
    assert '"cv_ate_correlation/assets"' in spec
    assert "--smoke-test" in build
    assert 'Replace("{{VERSION}}", $applicationVersion)' in build
    assert 'Join-Path $scriptRoot ".venv/Scripts/python.exe"' in build
    assert '[project.gui-scripts]' in configuration
    assert 'correlate = "cv_ate_correlation.exe_entry:main"' in configuration
    assert '"Pillow>=10"' in configuration
    assert "./build_correlate_exe.ps1 -Clean" in workflow
    assert "release_packages/**/*" in workflow
