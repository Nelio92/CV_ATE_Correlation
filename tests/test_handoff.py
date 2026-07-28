from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from openpyxl import load_workbook

from cv_ate_correlation.handoff import (
    MANIFEST_SHEET,
    REPEAT_INDEX,
    REQUEST_ID,
    REQUEST_SHEET,
    create_measurement_request,
    import_measurement_results,
)
from cv_ate_correlation.models import CorrelationProfile


PROFILE = CorrelationProfile(
    name="Generic voltage campaign",
    strategy="median_offset",
    reference_column="CV_Value",
    candidate_column="ATE_Value",
    group_by=("Parameter", "Temperature"),
)


def _source() -> pd.DataFrame:
    return pd.DataFrame({
        "DUT": [1, 1, 2],
        "Parameter": ["VOUT", "VOUT", "VOUT"],
        "Temperature": [25, 25, 25],
        "Unit": ["V", "V", "V"],
        "Test Value": [1.00, 1.01, 0.99],
        "Low": [0.9, 0.9, 0.9],
        "High": [1.1, 1.1, 1.1],
    })


def _fill_returned(path: Path, values: list[float]) -> None:
    workbook = load_workbook(path)
    sheet = workbook[REQUEST_SHEET]
    reference_column = next(cell.column for cell in sheet[1] if cell.value == PROFILE.reference_column)
    for row, value in enumerate(values, start=2):
        sheet.cell(row=row, column=reference_column, value=value)
    workbook.save(path)


def test_protected_request_round_trip_is_one_to_one(tmp_path: Path) -> None:
    request_path, manifest_path = tmp_path / "request.xlsx", tmp_path / "manifest.xlsx"
    request, manifest = create_measurement_request(_source(), PROFILE, request_path, manifest_path)

    assert list(request[REPEAT_INDEX]) == [1, 2, 1]
    assert not request[REQUEST_ID].is_unique
    assert not request.duplicated([REQUEST_ID, REPEAT_INDEX]).any()
    assert "Test Value" not in request.columns
    assert "Low" not in request.columns
    assert "High" not in request.columns
    assert list(manifest[PROFILE.candidate_column]) == [1.00, 1.01, 0.99]

    workbook = load_workbook(request_path)
    assert workbook[REQUEST_SHEET].protection.sheet
    assert workbook["_Metadata"].sheet_state == "veryHidden"
    reference_column = next(cell.column for cell in workbook[REQUEST_SHEET][1] if cell.value == PROFILE.reference_column)
    assert not workbook[REQUEST_SHEET].cell(row=2, column=reference_column).protection.locked
    assert workbook[REQUEST_SHEET].cell(row=2, column=1).protection.locked

    _fill_returned(request_path, [1.02, 1.03, 1.00])
    aligned = import_measurement_results(request_path, manifest_path, PROFILE)
    assert list(aligned[PROFILE.reference_column]) == [1.02, 1.03, 1.00]
    assert list(aligned[PROFILE.candidate_column]) == [1.00, 1.01, 0.99]


def test_duplicate_returned_request_key_is_rejected(tmp_path: Path) -> None:
    request_path, manifest_path = tmp_path / "request.xlsx", tmp_path / "manifest.xlsx"
    create_measurement_request(_source(), PROFILE, request_path, manifest_path)
    _fill_returned(request_path, [1.02, 1.03, 1.00])

    workbook = load_workbook(request_path)
    sheet = workbook[REQUEST_SHEET]
    sheet.cell(row=3, column=1, value=sheet.cell(row=2, column=1).value)
    sheet.cell(row=3, column=2, value=sheet.cell(row=2, column=2).value)
    workbook.save(request_path)

    with pytest.raises(ValueError, match="duplicate measurement request keys"):
        import_measurement_results(request_path, manifest_path, PROFILE)


def test_manifest_contains_internal_sheet(tmp_path: Path) -> None:
    request_path, manifest_path = tmp_path / "request.xlsx", tmp_path / "manifest.xlsx"
    create_measurement_request(_source(), PROFILE, request_path, manifest_path)
    assert MANIFEST_SHEET in pd.ExcelFile(manifest_path).sheet_names
