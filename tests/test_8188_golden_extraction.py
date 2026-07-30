from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from conftest import DATA_ROOT, PROJECT_ROOT
from cv_ate_correlation.correlation import attach_covariate_from_test_rows
from cv_ate_correlation.extraction import LegacyWideTeCsvAdapter
from cv_ate_correlation.profiles_8188 import get_correlation_profile, get_extraction_profile


@pytest.mark.slow
@pytest.mark.parametrize(("profile_name", "manifest_name", "golden_name"), [
    ("ctrx8188-dpll", "CTRX8188_CV_TE_Correlation_Chip_IDs_DPLL_PN.xlsx", "ATE_Extracted_DPLL_PN_Data.xlsx"),
    ("ctrx8188-kf", "CTRX8188_CV_TE_Correlation_Chip_IDs_LO_Power.xlsx", "ATE_Extracted_Kf_Data_DoE.xlsx"),
    ("ctrx8188-txlo", "CTRX8188_CV_TE_Correlation_Chip_IDs_LO_Power.xlsx", "ATE_Extracted_LO_Power_Data.xlsx"),
    ("ctrx8188-txpa", "CTRX8188_CV_TE_Correlation_Chip_IDs_PA_Power_DoE.xlsx", "ATE_Extracted_PA_Power_Data_DoE.xlsx"),
])
def test_full_raw_extraction_matches_golden(profile_name: str, manifest_name: str, golden_name: str) -> None:
    extraction_root = DATA_ROOT / "TE_Data_Extraction"
    actual = LegacyWideTeCsvAdapter().extract(
        DATA_ROOT / "Raw_Data_TE", extraction_root / manifest_name, get_extraction_profile(profile_name)
    )
    expected = pd.read_excel(extraction_root / golden_name, sheet_name="Extracted_Data")
    if profile_name in {"ctrx8188-txlo", "ctrx8188-txpa"}:
        actual = attach_covariate_from_test_rows(actual, get_correlation_profile(profile_name))
        expected = expected.loc[
            ~pd.to_numeric(expected["Test Number"], errors="coerce").isin((52046, 52084, 52094))
        ].copy()
        assert pd.to_numeric(actual["Kf"], errors="coerce").notna().all()
    for column in actual.select_dtypes(include="object"):
        actual[column] = actual[column].mask(actual[column] == "", np.nan)
    expected["Temperature"] = pd.to_numeric(
        expected["Temperature"].replace({"Cold": "-40", "Ambient": "25", "Hot": "135"})
    )
    keys = [
        "Insertion Type", "Wafer", "X", "Y", "Temperature",
        "Voltage corner", "Frequency_GHz", "Test Number", "Test Name",
    ]
    compare_columns = [column for column in expected.columns if column in actual.columns]
    sort_columns = keys + [column for column in compare_columns if column not in keys]
    actual = actual.sort_values(sort_columns).reset_index(drop=True)
    expected = expected.sort_values(sort_columns).reset_index(drop=True)
    if profile_name == "ctrx8188-dpll":
        joined = actual.merge(
            expected, on=keys + ["DoE split"], suffixes=("_raw", "_golden"), validate="one_to_one"
        )
        changed = joined[(joined["Test Value_raw"] - joined["Test Value_golden"]).abs() > 1e-12]
        assert set(zip(changed["Wafer"], changed["X"], changed["Y"], changed["Temperature"], changed["Test Number"])) == {
            (15, 14, 6, 135, number) for number in (52004, 52006, 52007, 52009, 52064, 52065, 52104, 52105)
        }
        compare_columns.remove("Test Value")
    assert_frame_equal(
        actual[compare_columns], expected[compare_columns], check_dtype=False, rtol=1e-12, atol=1e-12
    )


@pytest.mark.slow
def test_dpll_streaming_adapter_matches_fresh_legacy_script(tmp_path: Path) -> None:
    extraction_root = DATA_ROOT / "TE_Data_Extraction"
    manifest = extraction_root / "CTRX8188_CV_TE_Correlation_Chip_IDs_DPLL_PN.xlsx"
    legacy_output = tmp_path / "legacy_dpll.xlsx"
    subprocess.run([
        sys.executable, str(PROJECT_ROOT / "Tests_Data_Extractor_Flat.py"),
        "--input-folder", str(DATA_ROOT / "Raw_Data_TE"),
        "--output-xlsx", str(legacy_output),
        "--chips-file", str(manifest),
        "--tests", "52004-52009,52047,52064,52065,52095,52104,52105",
    ], check=True)
    actual = LegacyWideTeCsvAdapter().extract(
        DATA_ROOT / "Raw_Data_TE", manifest, get_extraction_profile("ctrx8188-dpll")
    )
    expected = pd.read_excel(legacy_output, sheet_name="Extracted_Data")
    expected["Temperature"] = pd.to_numeric(expected["Temperature"])
    expected["DoE split"] = expected["DoE split"].replace({"TT": "POR"})
    columns = list(actual.columns)
    assert_frame_equal(
        actual.sort_values(columns).reset_index(drop=True),
        expected[columns].sort_values(columns).reset_index(drop=True),
        check_dtype=False,
    )
