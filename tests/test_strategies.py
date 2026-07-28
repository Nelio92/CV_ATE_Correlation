from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from cv_ate_correlation.models import (
    CorrelationProfile,
    GuardBandProfile,
    RequirementRule,
    TestPolicy as CorrelationTestPolicy,
    TestSelector as ProfileTestSelector,
)
from cv_ate_correlation.correlation import correlate_frame
from cv_ate_correlation.reporting import write_excel_report


def test_mean_delta_and_worst_case_upper_limit() -> None:
    frame = pd.DataFrame({
        "group": ["A"] * 5,
        "reference": [2, 3, 4, 5, 6],
        "ate": [1, 1, 3, 3, 5],
        "high": [10] * 5,
    })
    profile = CorrelationProfile(
        name="generic", strategy="mean_delta", reference_column="reference", candidate_column="ate",
        group_by=("group",), minimum_points=5, upper_limit_column="high",
        guard_band=GuardBandProfile(kind="shifted_upper_limit"),
    )
    row = correlate_frame(frame, profile).summary.iloc[0]
    assert row["CorrelationFactor"] == pytest.approx(1.4)
    assert row["MaxAbsResidual"] == pytest.approx(0.6)
    assert row["AdjustedUpperLimit"] == pytest.approx(8.6)
    assert row["WorstCaseUpperLimit"] == pytest.approx(8.0)


def test_median_offset_sigma_and_requirement_rule() -> None:
    frame = pd.DataFrame({
        "group": ["A"] * 5, "mode": ["special"] * 5,
        "reference": [10, 11, 12, 13, 20], "ate": [9, 10, 11, 12, 12],
    })
    profile = CorrelationProfile(
        name="generic", strategy="median_offset", reference_column="reference", candidate_column="ate",
        group_by=("group", "mode"), minimum_points=5,
        guard_band=GuardBandProfile(kind="distribution_sigma", rules=(RequirementRule(
            when={"mode": ("special",)}, lower=0, upper=20,
            lower_residual="maximum", upper_residual="minimum",
        ),)),
    )
    row = correlate_frame(frame, profile).summary.iloc[0]
    assert row["CorrelationFactor"] == pytest.approx(1.0)
    assert row["ResidualMax"] == pytest.approx(7.0)
    assert row["ResidualMin"] == pytest.approx(0.0)
    assert row["AdjustedLowerLimit"] == pytest.approx(7.0)
    assert row["AdjustedUpperLimit"] == pytest.approx(20.0)


def test_different_test_sets_use_different_strategies_and_guard_bands(tmp_path: Path) -> None:
    frame = pd.DataFrame({
        "group": ["A"] * 10,
        "Test Number": [101] * 5 + [202] * 5,
        "Test Name": ["Mean test"] * 5 + ["Median test"] * 5,
        "reference": [2, 3, 4, 5, 9, 2, 3, 4, 5, 9],
        "ate": [1, 2, 3, 4, 5, 1, 2, 3, 4, 5],
        "high": [10] * 10,
    })
    profile = CorrelationProfile(
        name="mixed policies",
        strategy="mean_delta",
        reference_column="reference",
        candidate_column="ate",
        group_by=("group",),
        minimum_points=5,
        upper_limit_column="high",
        guard_band=GuardBandProfile(kind="shifted_upper_limit"),
        test_policies=(
            CorrelationTestPolicy(
                "Mean policy",
                ProfileTestSelector(exact=(101,)),
                "mean_delta",
                GuardBandProfile(kind="shifted_upper_limit"),
            ),
            CorrelationTestPolicy(
                "Median policy",
                ProfileTestSelector(exact=(202,)),
                "median_offset",
                GuardBandProfile(kind="distribution_sigma", sigma_multiplier=4),
            ),
        ),
    )

    result = correlate_frame(frame, profile)
    summary = result.summary.set_index("TestSet")

    assert summary.loc["Mean policy", "CorrelationFactor"] == pytest.approx(1.6)
    assert summary.loc["Mean policy", "CorrelationStrategy"] == "mean_delta"
    assert summary.loc["Mean policy", "GuardBandPolicy"] == "shifted_upper_limit"
    assert summary.loc["Median policy", "CorrelationFactor"] == pytest.approx(1.0)
    assert summary.loc["Median policy", "CorrelationStrategy"] == "median_offset"
    assert summary.loc["Median policy", "GuardBandPolicy"] == "distribution_sigma"
    report = tmp_path / "mixed-policies.xlsx"
    write_excel_report(result, profile, report)
    for sheet in ("Correlation_Factors", "Guard_Bands"):
        columns = pd.read_excel(report, sheet_name=sheet).columns
        assert {"TestSet", "CorrelationStrategy", "GuardBandPolicy"}.issubset(columns)


def test_overlapping_test_set_policies_are_rejected_at_runtime() -> None:
    frame = pd.DataFrame({
        "group": ["A"] * 2,
        "Test Number": [101] * 2,
        "Test Name": ["Leakage"] * 2,
        "reference": [2, 3],
        "ate": [1, 2],
    })
    profile = CorrelationProfile(
        name="overlap",
        strategy="mean_delta",
        reference_column="reference",
        candidate_column="ate",
        group_by=("group",),
        minimum_points=1,
        test_policies=(
            CorrelationTestPolicy(
                "By number",
                ProfileTestSelector(exact=(101,)),
                "mean_delta",
                GuardBandProfile(kind="distribution_sigma"),
            ),
            CorrelationTestPolicy(
                "By name",
                ProfileTestSelector(name_contains=("Leakage",)),
                "median_offset",
                GuardBandProfile(kind="shifted_upper_limit"),
            ),
        ),
    )

    with pytest.raises(ValueError, match="Multiple test-set policies"):
        correlate_frame(frame, profile)
