from __future__ import annotations

import struct
from pathlib import Path

import pytest

from cv_ate_correlation.gui import LOGO_ASSET_SIZES, logo_asset_path


@pytest.mark.parametrize("size", LOGO_ASSET_SIZES)
def test_packaged_logo_png_is_rgba_and_has_expected_dimensions(size: int) -> None:
    data = logo_asset_path(size).read_bytes()

    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", data[16:24]) == (size, size)
    assert data[25] == 6  # PNG color type 6 is RGBA.


def test_branding_assets_are_declared_as_package_data() -> None:
    project_root = Path(__file__).resolve().parents[1]
    configuration = (project_root / "pyproject.toml").read_text(encoding="utf-8")
    assets = logo_asset_path().parent

    assert '[tool.setuptools.package-data]' in configuration
    assert '"assets/*.png"' in configuration
    assert '"assets/*.svg"' in configuration
    assert '"assets/*.ico"' in configuration
    assert (assets / "correlate-signal-bloom.svg").is_file()
    assert (assets / "correlate-signal-bloom.ico").read_bytes().startswith(b"\x00\x00\x01\x00")