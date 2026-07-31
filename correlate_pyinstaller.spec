# -*- mode: python ; coding: utf-8 -*-
from __future__ import annotations

import re
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, copy_metadata

PROJECT_DIR = Path.cwd().resolve()
SOURCE_DIR = PROJECT_DIR / "src"
ENTRY_SCRIPT = PROJECT_DIR / "run_correlate.py"
ASSET_DIR = SOURCE_DIR / "cv_ate_correlation" / "assets"
ICON = ASSET_DIR / "correlate-signal-bloom.ico"
BUILD_DIR = PROJECT_DIR / "build_pyinstaller"

configuration = tomllib.loads((PROJECT_DIR / "pyproject.toml").read_text(encoding="utf-8"))
VERSION = str(configuration["project"]["version"])
version_parts = [int(value) for value in re.findall(r"\d+", VERSION)[:4]]
version_parts.extend([0] * (4 - len(version_parts)))
version_tuple = tuple(version_parts)
VERSION_RESOURCE = BUILD_DIR / "correlate-version-info.txt"
BUILD_DIR.mkdir(parents=True, exist_ok=True)
VERSION_RESOURCE.write_text(
    f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={version_tuple!r},
    prodvers={version_tuple!r},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', 'CorreLaTE Team'),
         StringStruct('FileDescription', 'CorreLaTE: ATE-to-Lab Correlation'),
         StringStruct('FileVersion', '{VERSION}'),
         StringStruct('InternalName', 'CorreLaTE'),
         StringStruct('LegalCopyright', 'Copyright (c) 2026 Wandji Lionel Wilfried'),
         StringStruct('OriginalFilename', 'CorreLaTE.exe'),
         StringStruct('ProductName', 'CorreLaTE'),
         StringStruct('ProductVersion', '{VERSION}')])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""",
    encoding="utf-8",
)


def include_runtime_module(module_name: str) -> bool:
    if ".tests" in module_name or module_name.endswith(".tests"):
        return False
    if module_name.startswith(("matplotlib.sphinxext", "matplotlib.testing")):
        return False
    if module_name.startswith(
        (
            "matplotlib.backends._backend_gtk",
            "matplotlib.backends.backend_gtk",
            "matplotlib.backends.backend_macosx",
            "matplotlib.backends.backend_qt",
            "matplotlib.backends.backend_webagg",
            "matplotlib.backends.backend_wx",
            "matplotlib.backends.qt_compat",
            "matplotlib.backends.qt_editor",
        )
    ):
        return False
    if module_name.startswith("matplotlib.backends.backend_"):
        return module_name in {
            "matplotlib.backends.backend_agg",
            "matplotlib.backends.backend_mixed",
        }
    return True


datas: list[tuple[str, str]] = []
binaries: list[tuple[str, str]] = []
hiddenimports: list[str] = []

for package_name in ("matplotlib", "openpyxl", "PIL"):
    package_datas, package_binaries, package_hiddenimports = collect_all(
        package_name,
        filter_submodules=include_runtime_module,
    )
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

for distribution_name in (
    "cv-ate-correlation",
    "pandas",
    "numpy",
    "matplotlib",
    "openpyxl",
    "Pillow",
):
    datas += copy_metadata(distribution_name)

for asset in sorted(ASSET_DIR.iterdir()):
    if asset.is_file():
        datas.append((str(asset), "cv_ate_correlation/assets"))

hiddenimports += [
    "matplotlib.backends.backend_agg",
    "numpy._core._exceptions",
    "numpy._core._methods",
    "numpy._core._multiarray_umath",
    "openpyxl.cell._writer",
    "PIL.WebPImagePlugin",
]

block_cipher = None

a = Analysis(
    [str(ENTRY_SCRIPT)],
    pathex=[str(SOURCE_DIR), str(PROJECT_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={"matplotlib": {"backends": ["Agg"]}},
    runtime_hooks=[],
    excludes=[
        "hypothesis",
        "IPython",
        "jupyter",
        "notebook",
        "pytest",
        "scipy",
        "sklearn",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="CorreLaTE",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="x86_64",
    icon=str(ICON),
    version=str(VERSION_RESOURCE),
    codesign_identity=None,
    entitlements_file=None,
)
