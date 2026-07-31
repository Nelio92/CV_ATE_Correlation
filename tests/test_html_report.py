from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from cv_ate_correlation.cli import _build_parser
from cv_ate_correlation.correlation import CorrelationResult, correlate_frame
from cv_ate_correlation.html_report import write_html_report
from cv_ate_correlation.models import (
    CorrelationProfile,
    GuardBandProfile,
    TestPolicy as CorrelationTestPolicy,
    TestSelector as ProfileTestSelector,
)


def _pooled_insertion_result() -> tuple[CorrelationResult, CorrelationProfile]:
    frame = pd.DataFrame([
        {
            "DUT Nr": dut,
            "Test Number": test_number,
            "Test Name": test_name,
            "Frequency": 81,
            "Supply Corner": "VMIN",
            "Channel": channel,
            "Insertion": insertion,
            "Insertion Type": insertion_type,
            "Temperature": temperature,
            "reference": 10.0 + channel + dut / 100,
            "ate": 9.5 + channel + dut / 100,
            "low": 8.0,
            "high": 15.0,
            "Unit": "dBm",
        }
        for insertion, insertion_type, temperature in (("S1", "FE", 135), ("B1", "BE", 25))
        for test_number, test_name, channel in (
            (1001, "TXPA <unsafe & channel 1>", 1),
            (1002, "TXPA channel 2", 2),
        )
        for dut in range(1, 6)
    ])
    profile = CorrelationProfile(
        name="HTML sign-off profile",
        strategy="Median_Deltas",
        reference_column="reference",
        candidate_column="ate",
        group_by=(
            "Test Number", "Frequency", "Supply Corner", "Channel", "Insertion", "Temperature",
        ),
        minimum_points=5,
        lower_limit_column="low",
        upper_limit_column="high",
        unit_column="Unit",
        detail_key_columns=("DUT Nr",),
        test_policies=(CorrelationTestPolicy(
            "Two-channel sign-off",
            ProfileTestSelector(ranges=((1001, 1002),)),
            "Median_Deltas",
            GuardBandProfile(kind="distribution_sigma", sigma_multiplier=3.0),
            pooled_columns=("Test Number", "Channel"),
        ),),
    )
    return correlate_frame(frame, profile), profile


def test_html_report_is_self_contained_and_groups_pooled_tests_across_insertions(
    tmp_path: Path,
) -> None:
    result, profile = _pooled_insertion_result()
    output = tmp_path / "correlation-signoff.html"

    plot_count = write_html_report(
        result, profile, output, image_dpi=50, image_quality=45
    )

    report = output.read_text(encoding="utf-8")
    assert plot_count == 4
    assert list(tmp_path.iterdir()) == [output]
    assert report.startswith("<!doctype html>")
    assert "HTML sign-off profile" in report
    assert "Two-channel sign-off" in report
    assert "MERGED / POOLED" in report
    assert "Test Number, Channel" in report
    assert "1001" in report and "1002" in report
    assert "TXPA &lt;unsafe &amp; channel 1&gt;" in report
    assert "TXPA channel 2" in report
    assert "Correlation factors by insertion" in report
    assert "Correlated limits by insertion" in report
    assert "S1 · FE · 135 °C" in report
    assert "B1 · BE · 25 °C" in report
    assert report.index("<td>FE</td><td>S1</td>") < report.index("<td>BE</td><td>B1</td>")
    factor_table = report.split('class="factor-table"', 1)[1].split("</table>", 1)[0]
    assert "Factor / offset" in factor_table
    assert "Factor A (slope / α)" not in factor_table
    assert "Factor B (intercept / β)" not in factor_table
    assert report.count('class="test-family"') == 1
    assert report.count('class="plot-card"') == 4
    assert report.count('class="plot-row"') == 2
    assert report.index("Model plots by insertion") < report.index("Series plots by insertion")
    assert report.count("data:image/") == 5  # Four figures plus the embedded logo.
    assert 'src="http' not in report
    assert "<link" not in report
    assert "<script src=" not in report


def test_html_report_overview_documents_profile_models_policies_and_sample_scope(
    tmp_path: Path,
) -> None:
    result, profile = _pooled_insertion_result()
    output = tmp_path / "overview.html"

    write_html_report(result, profile, output, image_dpi=40, image_quality=35)

    report = output.read_text(encoding="utf-8")
    assert "Correlation strategies applied" in report
    assert "Median_Deltas" in report
    assert "Guard-band policies applied" in report
    assert "distribution_sigma" in report
    assert "Models rendered" in report
    assert "Linear OLS, Mean_Deltas, Median_Deltas" in report
    assert "Affected individual tests" in report
    assert "Test families / sign-off sections" in report
    assert "Correlation populations" in report
    assert "Correlation rows" in report
    assert "Campaign conditions and corners" in report
    assert "Frequency" in report
    assert "Supply Corner" in report
    assert "Insertion Type" in report
    assert "offline static sign-off report" in report


def test_correlate_cli_exposes_html_report_and_hides_legacy_plot_folder() -> None:
    parser = _build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    correlate_help = subparsers.choices["correlate"].format_help()
    args = parser.parse_args([
        "correlate",
        "--profile", "ctrx8144-txpa",
        "--input", "input.xlsx",
        "--sheet", "Correlation_Input",
        "--output", "report.xlsx",
        "--html-report", "signoff.html",
    ])

    assert args.html_report == Path("signoff.html")
    assert args.mad_threshold == 12.0
    assert args.exclude_outlier_row == []
    assert "--html-report" in correlate_help
    assert "--mad-threshold" in correlate_help
    assert "--exclude-outlier-row" in correlate_help
    assert "--plots" not in correlate_help
