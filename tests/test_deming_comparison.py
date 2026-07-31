from __future__ import annotations

import math

import pandas as pd

from cv_ate_correlation.correlation import CorrelationResult
from cv_ate_correlation.deming_comparison import add_deming_comparison, deming_fit


def test_equal_variance_deming_fit_is_symmetric_and_minimizes_orthogonal_error() -> None:
    ate = pd.Series([0.2, 0.8, 2.2, 2.7, 4.3, 4.8])
    lab = pd.Series([0.0, 2.3, 3.7, 6.5, 7.9, 10.4])

    fit = deming_fit(ate, lab)
    reverse = deming_fit(lab, ate)
    ols_slope = float(((ate - ate.mean()) * (lab - lab.mean())).sum()) / float(
        ((ate - ate.mean()) ** 2).sum()
    )
    ols_intercept = float(lab.mean()) - ols_slope * float(ate.mean())
    deming_residual = lab - (fit.slope * ate + fit.intercept)
    ols_residual = lab - (ols_slope * ate + ols_intercept)
    deming_orthogonal = float((deming_residual.pow(2).mean() / (1 + fit.slope**2)) ** 0.5)
    ols_orthogonal = float((ols_residual.pow(2).mean() / (1 + ols_slope**2)) ** 0.5)

    assert fit.status == "Available"
    assert math.isclose(fit.slope * reverse.slope, 1.0, rel_tol=1e-12)
    assert deming_orthogonal <= ols_orthogonal


def test_deming_comparison_adds_predictions_without_changing_existing_models() -> None:
    summary = pd.DataFrame({
        "LinearSlope": [1.8],
        "LinearIntercept": [0.1],
        "MedianDelta": [2.0],
    })
    details = pd.DataFrame({
        "GroupIndex": [0, 0, 0, 0],
        "ReferenceValue": [1.0, 3.1, 5.2, 7.0],
        "CandidateValue": [0.0, 1.0, 2.0, 3.0],
        "LinearCorrectedCandidate": [0.1, 1.9, 3.7, 5.5],
        "LinearResidual": [0.9, 1.2, 1.5, 1.5],
        "MedianDeltasCorrectedCandidate": [2.0, 3.0, 4.0, 5.0],
        "MedianDeltasResidual": [-1.0, 0.1, 1.2, 2.0],
    })

    compared = add_deming_comparison(CorrelationResult(summary, details))

    assert compared.summary.loc[0, "DemingErrorVarianceRatio"] == 1.0
    assert compared.summary.loc[0, "DemingStatus"] == "Available"
    assert compared.details["DemingCorrectedCandidate"].notna().all()
    assert compared.details["DemingResidual"].notna().all()
    pd.testing.assert_series_equal(
        compared.details["LinearCorrectedCandidate"],
        details["LinearCorrectedCandidate"],
    )
