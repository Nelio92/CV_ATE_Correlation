from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from cv_ate_correlation.correlation import correlate_frame
from cv_ate_correlation.html_report import write_html_report
from cv_ate_correlation.models import CorrelationProfile
from cv_ate_correlation.outliers import (
    MAD_SCALE_FACTOR,
    OUTLIER_FLAGGED,
    OUTLIER_REVIEW_STATUS,
    OUTLIER_ROW_ID,
    analyze_outliers,
    attach_outlier_audit,
    finalize_outlier_review,
)
from cv_ate_correlation.reporting import write_excel_report


def _review_profile(*, minimum_points: int = 5) -> CorrelationProfile:
    return CorrelationProfile(
        name="outlier review",
        strategy="Linear",
        reference_column="Lab",
        candidate_column="ATE",
        group_by=("Test Number", "Corner"),
        detail_key_columns=("DUT Nr", "Wafer", "X", "Y"),
        minimum_points=minimum_points,
    )


def _three_signal_frame() -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    definitions = {
        101: (
            "Lab anomaly",
            [1, 2, 3, 4, 5, 6, 100],
            [1, 2, 3, 4, 5, 6, 7],
        ),
        102: (
            "ATE anomaly",
            [1, 2, 3, 4, 5, 6, 7],
            [1, 2, 3, 4, 5, 6, 100],
        ),
        103: (
            "Paired-only anomaly",
            [0, 10, 20, 30, 40, 50, 60],
            [0, 10, 20, 40, 40, 50, 60],
        ),
    }
    for test_number, (test_name, lab_values, ate_values) in definitions.items():
        for dut, (lab, ate) in enumerate(zip(lab_values, ate_values), start=1):
            rows.append({
                "DUT Nr": dut,
                "Wafer": 1,
                "X": dut,
                "Y": test_number - 100,
                "Test Number": test_number,
                "Test Name": test_name,
                "Corner": "VNOM",
                "DoE split": "CHAR",
                "Insertion": "S1",
                "Temperature": 25,
                "Lab": lab,
                "ATE": ate,
            })
    return pd.DataFrame(rows)


def test_scaled_mad_flags_lab_ate_and_paired_anomalies_per_test_population() -> None:
    analysis = analyze_outliers(_three_signal_frame(), _review_profile(), threshold=6)

    assert MAD_SCALE_FACTOR == pytest.approx(1.4826)
    assert analysis.flagged_count == 3
    findings = analysis.findings.set_index("Test Number")
    assert "Lab/CV" in findings.loc[101, "OutlierFlaggedSeries"]
    assert "ATE/TE" in findings.loc[102, "OutlierFlaggedSeries"]
    assert findings.loc[103, "OutlierFlaggedSeries"] == "Paired"
    assert "Paired disagreement" in findings.loc[103, "OutlierReviewGuidance"]
    assert set(findings["Corner"]) == {"VNOM"}
    assert set(findings["Insertion"]) == {"S1"}
    assert set(findings["DoE split"]) == {"CHAR"}
    assert findings["OutlierPopulation"].nunique() == 3


def test_zero_mad_deviation_is_reported_as_an_indeterminate_review_candidate() -> None:
    frame = pd.DataFrame({
        "DUT Nr": range(1, 7),
        "Test Number": [101] * 6,
        "Test Name": ["Constant series"] * 6,
        "Corner": ["VMIN"] * 6,
        "Lab": [10, 10, 10, 10, 10, 20],
        "ATE": [9, 9, 9, 9, 9, 9],
    })

    analysis = analyze_outliers(frame, _review_profile(), threshold=6)

    assert analysis.flagged_count == 1
    finding = analysis.findings.iloc[0]
    assert finding["LabMAD"] == pytest.approx(0.0)
    assert finding["LabRobustScore"] == float("inf")
    assert "MAD=0" in finding["LabMADStatus"]


def test_default_review_retains_all_rows_and_explicit_filtering_is_audited() -> None:
    frame = _three_signal_frame()
    profile = _review_profile()
    analysis = analyze_outliers(frame, profile)

    retained, retained_review = finalize_outlier_review(analysis, profile)
    excluded_id = int(analysis.findings.iloc[0][OUTLIER_ROW_ID])
    filtered, filtered_review = finalize_outlier_review(analysis, profile, [excluded_id])

    assert len(retained) == len(frame)
    assert retained_review.excluded_count == 0
    assert retained.loc[retained[OUTLIER_FLAGGED], OUTLIER_REVIEW_STATUS].eq("Flagged – retained").all()
    assert len(filtered) == len(frame) - 1
    assert filtered_review.excluded_count == 1
    audit = filtered_review.audit_frame()
    assert audit["ReviewStatus"].eq("Excluded").sum() == 1
    assert audit["MADThreshold"].eq(6.0).all()


def test_selected_exclusions_cannot_reduce_a_correlation_population_below_minimum() -> None:
    frame = _three_signal_frame()
    profile = _review_profile(minimum_points=7)
    analysis = analyze_outliers(frame, profile)
    selected = analysis.findings.loc[
        analysis.findings["Test Number"].eq(101), OUTLIER_ROW_ID
    ].astype(int).tolist()

    with pytest.raises(ValueError, match="below the minimum of 7 points"):
        finalize_outlier_review(analysis, profile, selected)


def test_outlier_audit_is_written_to_excel_and_html(tmp_path: Path) -> None:
    frame = _three_signal_frame()
    profile = _review_profile()
    analysis = analyze_outliers(frame, profile)
    excluded_id = int(analysis.findings.iloc[0][OUTLIER_ROW_ID])
    filtered, review = finalize_outlier_review(analysis, profile, [excluded_id])
    result = attach_outlier_audit(correlate_frame(filtered, profile), profile, review)

    workbook = tmp_path / "reviewed.xlsx"
    write_excel_report(result, profile, workbook)
    audit = pd.read_excel(workbook, sheet_name="Outlier_Review")
    summary = pd.read_excel(workbook, sheet_name="Correlation_Summary")

    assert len(audit) == 3
    assert audit["ReviewStatus"].eq("Excluded").sum() == 1
    assert {"OriginalCount", "OutlierFlaggedCount", "OutlierExcludedCount", "FinalCorrelationCount"}.issubset(
        summary.columns
    )
    excluded_group = summary.loc[summary["Test Number"].eq(101)].iloc[0]
    assert excluded_group["OriginalCount"] == 7
    assert excluded_group["FinalCorrelationCount"] == 6
    assert excluded_group["OutlierExcludedCount"] == 1

    html = tmp_path / "reviewed.html"
    assert write_html_report(result, profile, html, image_dpi=30, image_quality=30) == 6
    report = html.read_text(encoding="utf-8")
    assert "Pre-correlation outlier review" in report
    assert "scaled MAD (1.4826 × MAD), n=6" in report
    assert "Flagged-sample audit" in report
    assert "Explicit review only; no automatic exclusions" in report


def test_no_findings_still_produces_detector_settings_audit() -> None:
    frame = pd.DataFrame({
        "DUT Nr": range(1, 7),
        "Test Number": [101] * 6,
        "Test Name": ["No anomaly"] * 6,
        "Corner": ["VNOM"] * 6,
        "Lab": [1, 2, 3, 4, 5, 6],
        "ATE": [1, 2, 3, 4, 5, 6],
    })
    profile = _review_profile()

    analysis = analyze_outliers(frame, profile)
    filtered, review = finalize_outlier_review(analysis, profile)
    audit = review.audit_frame()

    assert analysis.flagged_count == 0
    assert len(filtered) == len(frame)
    assert audit.loc[0, "ReviewStatus"] == "No outliers detected"
    assert audit.loc[0, "MADThreshold"] == pytest.approx(6.0)
