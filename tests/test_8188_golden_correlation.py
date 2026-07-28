from __future__ import annotations

import pandas as pd
from pandas.testing import assert_frame_equal

from conftest import DATA_ROOT
from cv_ate_correlation.correlation import attach_covariate, correlate_frame
from cv_ate_correlation.profiles_8188 import get_correlation_profile


def _compare(actual: pd.DataFrame, expected: pd.DataFrame, keys: list[str], mapping: dict[str, str]) -> None:
    expected = expected.dropna(subset=keys).copy()
    expected = expected[keys + list(mapping)].rename(columns=mapping)
    actual = actual[keys + list(mapping.values())].copy()
    expected = expected.sort_values(keys).reset_index(drop=True)
    actual = actual.sort_values(keys).reset_index(drop=True)
    assert_frame_equal(actual, expected, check_dtype=False, rtol=1e-11, atol=1e-11)


def test_dpll_matches_golden_summary() -> None:
    source = DATA_ROOT / "TE_Data_Extraction" / "ATE_Extracted_DPLL_PN_Data.xlsx"
    golden = pd.read_excel(
        DATA_ROOT / "Outputs" / "CV_ATE_Correlation_DPLL_PN_FE_BE.xlsx", sheet_name="Delta_Summary"
    )
    profile = get_correlation_profile("ctrx8188-dpll")
    parts = []
    for sheet in ("FE_PN", "BE_PN"):
        result = correlate_frame(pd.read_excel(source, sheet_name=sheet), profile).summary
        result.insert(0, "DataSheet", sheet)
        parts.append(result)
    actual = pd.concat(parts, ignore_index=True)
    keys = ["DataSheet", "Test Number", "Voltage corner", "Frequency_GHz", "Temperature"]
    _compare(actual, golden, keys, {
        "N": "Count", "AvgDelta(CV-ATE)": "CorrelationFactor", "StdDelta(CV-ATE)": "ResidualStd",
        "WC_GuardBand": "MaxAbsResidual", "ATE_High_New": "AdjustedUpperLimit",
        "ATE_High_WC": "WorstCaseUpperLimit",
    })


def _run_power(input_name: str, sheet: str, profile_name: str, kf_name: str, kf_sheet: str) -> pd.DataFrame:
    profile = get_correlation_profile(profile_name)
    source = DATA_ROOT / "TE_Data_Extraction" / input_name
    frame = pd.read_excel(source, sheet_name=sheet)
    lookup = pd.read_excel(DATA_ROOT / "TE_Data_Extraction" / kf_name, sheet_name=kf_sheet)
    return correlate_frame(attach_covariate(frame, lookup, profile), profile).summary


def test_txlo_matches_golden_factors_and_guardbands() -> None:
    actual = _run_power(
        "ATE_Extracted_LO_Power_Data.xlsx", "FE_Filtered", "ctrx8188-txlo",
        "ATE_Extracted_Kf_Data_DoE.xlsx", "FE",
    )
    golden = pd.read_excel(
        DATA_ROOT / "Outputs" / "CV_ATE_Correlation_TXLO_Power_FE.xlsx", sheet_name="Correlation_Summary"
    ).dropna(how="all")
    keys = ["Test Number", "Voltage corner", "Frequency_GHz", "Temperature", "LO IDAC"]
    _compare(actual, golden, keys, {
        "N": "Count", "MedianDelta(CV-ATE)": "CorrelationFactor", "R2_OffsetModel": "R2",
        "ResidualStd(Delta)": "ResidualStd",
        "ResidualMax(Delta)": "ResidualMax", "ResidualMin(Delta)": "ResidualMin", "CorrMean": "CorrectedMean",
        "CorrStd": "CorrectedStd", "Corr_Low": "AdjustedLowerLimit", "Corr_High": "AdjustedUpperLimit",
        "Phys_alpha": "CovariateSlope", "Phys_beta": "CovariateIntercept", "R2_Physics": "CovariateR2",
        "ResidualStd_Physics": "CovariateResidualStd", "Kf_N_Present": "CovariateCountPresent",
        "Kf_N_Missing": "CovariateCountMissing", "Kf_Unique": "CovariateUnique", "Kf_Min": "CovariateMin",
        "Kf_Max": "CovariateMax", "Kf_Mean": "CovariateMean", "CorrMean_Physics": "CovariateCorrectedMean",
        "CorrStd_Physics": "CovariateCorrectedStd", "ResidualMax_Physics": "CovariateResidualMax",
        "ResidualMin_Physics": "CovariateResidualMin", "Corr_Low_Physics": "CovariateAdjustedLowerLimit",
        "Corr_High_Physics": "CovariateAdjustedUpperLimit",
    })


def test_txpa_fe_and_be_match_golden_factors_and_guardbands() -> None:
    for sheet, kf_sheet, output_name in (
        ("FE_Filtered", "KF_FE", "CV_ATE_Correlation_TXPA_Power_FE.xlsx"),
        ("BE_Filtered", "KF_BE", "CV_ATE_Correlation_TXPA_Power_BE.xlsx"),
    ):
        actual = _run_power(
            "ATE_Extracted_PA_Power_Data_DoE.xlsx", sheet, "ctrx8188-txpa",
            "ATE_Extracted_PA_Power_Data_DoE.xlsx", kf_sheet,
        )
        golden = pd.read_excel(DATA_ROOT / "Outputs" / output_name, sheet_name="Correlation_Summary").dropna(how="all")
        keys = ["LUT value", "Voltage corner", "Frequency_GHz", "Temperature", "PA Channel"]
        _compare(actual, golden, keys, {
            "N": "Count", "MedianDelta(CV-ATE)": "CorrelationFactor", "R2_OffsetModel": "R2",
            "ResidualStd(Delta)": "ResidualStd",
            "ResidualMax(Delta)": "ResidualMax", "ResidualMin(Delta)": "ResidualMin", "CorrMean": "CorrectedMean",
            "CorrStd": "CorrectedStd", "Corr_Low": "AdjustedLowerLimit", "Corr_High": "AdjustedUpperLimit",
            "Phys_alpha": "CovariateSlope", "Phys_beta": "CovariateIntercept", "R2_Physics": "CovariateR2",
            "ResidualStd_Physics": "CovariateResidualStd", "Kf_N_Present": "CovariateCountPresent",
            "Kf_N_Missing": "CovariateCountMissing", "Kf_Unique": "CovariateUnique", "Kf_Min": "CovariateMin",
            "Kf_Max": "CovariateMax", "Kf_Mean": "CovariateMean", "CorrMean_Physics": "CovariateCorrectedMean",
            "CorrStd_Physics": "CovariateCorrectedStd", "ResidualMax_Physics": "CovariateResidualMax",
            "ResidualMin_Physics": "CovariateResidualMin", "Corr_Low_Physics": "CovariateAdjustedLowerLimit",
            "Corr_High_Physics": "CovariateAdjustedUpperLimit",
        })
