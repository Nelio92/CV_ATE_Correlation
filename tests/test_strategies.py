from __future__ import annotations

import pandas as pd
import pytest

from cv_ate_correlation.models import CorrelationProfile, GuardBandProfile, RequirementRule
from cv_ate_correlation.correlation import correlate_frame


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
