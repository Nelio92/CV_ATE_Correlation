"""Windowed executable entry point with frozen-build diagnostics."""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from . import __version__

SMOKE_TEST_ARGUMENT = "--smoke-test"


def startup_error_log_path() -> Path:
    """Return the per-user startup error log used by the windowed executable."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    root = Path(base) if base else Path.home()
    return root / "CorreLaTE" / "logs" / "startup-error.log"


def _write_startup_error(error: BaseException) -> Path:
    path = startup_error_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    details = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    path.write_text(
        "\n".join((
            f"Timestamp UTC: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            f"CorreLaTE version: {__version__}",
            f"Python: {sys.version}",
            f"Executable: {sys.executable}",
            f"Frozen: {bool(getattr(sys, 'frozen', False))}",
            "",
            details,
        )),
        encoding="utf-8",
    )
    return path


def _show_startup_error(error: BaseException, log_path: Path) -> None:
    message = (
        "CorreLaTE could not start.\n\n"
        f"{type(error).__name__}: {error}\n\n"
        f"Diagnostic log:\n{log_path}"
    )
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, message, "CorreLaTE startup error", 0x10)
    except Exception:
        # A windowed executable has no console, but this remains useful when the
        # launcher is executed from a development shell on a non-Windows host.
        print(message, file=sys.stderr)


def run_frozen_smoke_test() -> None:
    """Exercise dependencies and assets that are most vulnerable to freezer omissions."""
    import tkinter as tk

    import pandas as pd
    from matplotlib.figure import Figure
    from PIL import features

    from cv_ate_correlation.gui import logo_asset_path
    from cv_ate_correlation.profiles_8188 import builtin_profile_ids

    if not logo_asset_path(64).is_file() or not logo_asset_path(256).is_file():
        raise RuntimeError("Packaged Signal Bloom logo assets are missing")
    if not builtin_profile_ids():
        raise RuntimeError("No built-in correlation profiles were loaded")
    if not features.check("webp"):
        raise RuntimeError("The packaged Pillow build has no WebP support")

    root = tk.Tk()
    root.withdraw()
    try:
        if not root.tk.eval("info patchlevel"):
            raise RuntimeError("The packaged Tcl/Tk runtime did not initialize")
        logo = tk.PhotoImage(file=logo_asset_path(64))
        if logo.width() != 64 or logo.height() != 64:
            raise RuntimeError("The packaged Tk runtime could not decode the logo PNG")
        root.update_idletasks()
    finally:
        root.destroy()

    with tempfile.TemporaryDirectory(prefix="correlate-smoke-") as temporary:
        root = Path(temporary)
        workbook = root / "smoke.xlsx"
        frame = pd.DataFrame({"Test Number": [1], "Test Value": [2.5]})
        frame.to_excel(workbook, index=False, engine="openpyxl")
        restored = pd.read_excel(workbook, engine="openpyxl")
        if restored.to_dict(orient="records") != frame.to_dict(orient="records"):
            raise RuntimeError("Packaged Excel read/write round trip changed the data")

        figure = Figure(figsize=(2.0, 1.5))
        axis = figure.subplots()
        axis.scatter([1.0, 2.0], [2.0, 3.0])
        plot = root / "smoke.webp"
        figure.savefig(plot, format="webp", dpi=40)
        if not plot.is_file() or plot.stat().st_size == 0:
            raise RuntimeError("Packaged Matplotlib/Pillow WebP rendering failed")


def main(argv: Sequence[str] | None = None) -> int:
    """Launch the desktop application or execute the noninteractive frozen smoke test."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    smoke_test = arguments == [SMOKE_TEST_ARGUMENT]
    try:
        if smoke_test:
            run_frozen_smoke_test()
        else:
            if arguments:
                raise ValueError(f"Unknown executable argument(s): {' '.join(arguments)}")
            from cv_ate_correlation.gui import launch

            launch()
        return 0
    except Exception as error:
        log_path = _write_startup_error(error)
        if not smoke_test:
            _show_startup_error(error, log_path)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
