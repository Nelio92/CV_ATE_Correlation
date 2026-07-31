from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from cv_ate_correlation.cli import _build_parser
from cv_ate_correlation.extraction import LegacyWideTeCsvAdapter
from cv_ate_correlation.models import (
    CorrelationProfile,
    ExtractionProfile,
    GuardBandProfile,
    InsertionProfile,
    TestPolicy as CorrelationTestPolicy,
    TestSelector as ProfileTestSelector,
)
from cv_ate_correlation.yield_forecast import (
    ProductiveInsertionInput,
    correlate_productive_value,
    forecast_yield,
    load_productive_csv_inputs,
    validate_productive_insertion_inputs,
)
from cv_ate_correlation.yield_forecast_report import write_yield_forecast_html


def _profile() -> CorrelationProfile:
    return CorrelationProfile(
        name="Productive yield profile",
        strategy="Linear",
        reference_column="Lab",
        candidate_column="ATE",
        group_by=("Test Number", "Corner", "Insertion", "Temperature"),
        lower_limit_column="Low",
        upper_limit_column="High",
        unit_column="Unit",
        test_policies=(CorrelationTestPolicy(
            "Forecast tests",
            ProfileTestSelector(exact=(101,)),
            "Linear",
            GuardBandProfile(kind="max_residuals", requirement_min=0, requirement_max=10),
        ),),
    )


def _factors() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "Test Number": 101,
            "Corner": "VNOM",
            "Insertion": insertion,
            "Temperature": temperature,
            "TestSet": "Forecast tests",
            "CorrelationStrategy": "Linear",
            "CorrelationFactorA": factor_a,
            "CorrelationFactorB": factor_b,
            "AdjustedLowerLimit": 1.0,
            "AdjustedUpperLimit": 9.0,
            "Unit": "V",
        }
        for insertion, temperature, factor_a, factor_b in (
            ("S1", 135, 2.0, 1.0),
            ("B1", 25, 1.0, 0.0),
        )
    ])


def _production() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "DUT Nr": dut,
            "Test Number": 101,
            "Test Name": "Forecast voltage",
            "Corner": "VNOM",
            "Insertion": insertion,
            "Insertion Type": insertion_type,
            "Temperature": temperature,
            "ATE": value,
            "Unit": "V",
        }
        for insertion, insertion_type, temperature, values in (
            ("S1", "FE", 135, (0.0, 1.0, 2.0, 5.0)),
            ("B1", "BE", 25, (1.0, 3.0, 9.0, 10.0)),
        )
        for dut, value in enumerate(values, start=1)
    ])


def test_forecast_applies_factors_and_counts_inclusive_limit_failures() -> None:
    result = forecast_yield(_production(), _factors(), _profile())

    s1 = result.summary.loc[result.summary["Insertion"].eq("S1")].iloc[0]
    b1 = result.summary.loc[result.summary["Insertion"].eq("B1")].iloc[0]
    assert s1["SampleCount"] == 4
    assert s1["PassCount"] == 3
    assert s1["FailCount"] == 1
    assert s1["UpperFailCount"] == 1
    assert s1["LowerFailCount"] == 0
    assert s1["YieldPercent"] == pytest.approx(75.0)
    assert b1["PassCount"] == 3
    assert b1["FailCount"] == 1
    assert b1["YieldPercent"] == pytest.approx(75.0)

    s1_values = result.details.loc[
        result.details["Insertion"].eq("S1"), "ForecastCorrelatedValue"
    ].tolist()
    assert s1_values == [1.0, 3.0, 5.0, 11.0]
    boundary_rows = result.details.loc[
        result.details["ForecastCorrelatedValue"].isin([1.0, 9.0])
    ]
    assert boundary_rows["ForecastPass"].all()


@pytest.mark.parametrize(
    ("strategy", "factor_a", "factor_b", "covariate", "expected"),
    [
        ("Linear", 2.0, 1.0, None, 9.0),
        ("Mean_Deltas", 1.0, 0.5, None, 4.5),
        ("Median_Deltas", 1.0, -0.5, None, 3.5),
        ("Physics-based", 0.25, 2.0, 4.0, 1.0),
    ],
)
def test_productive_value_uses_the_selected_correlation_equation(
    strategy: str,
    factor_a: float,
    factor_b: float,
    covariate: float | None,
    expected: float,
) -> None:
    assert correlate_productive_value(
        4.0,
        strategy,
        factor_a,
        factor_b,
        covariate=covariate,
    ) == pytest.approx(expected)


def test_empty_reported_pooling_matches_an_unpooled_profile() -> None:
    factors = _factors()
    factors["PooledParameters"] = ""

    result = forecast_yield(_production(), factors, _profile())

    assert len(result.summary) == 2


def test_invalid_correlated_window_fails_all_samples_and_is_audited() -> None:
    factors = _factors()
    factors.loc[factors["Insertion"].eq("S1"), "AdjustedLowerLimit"] = 9.0
    factors.loc[factors["Insertion"].eq("S1"), "AdjustedUpperLimit"] = 1.0

    result = forecast_yield(_production(), factors, _profile())

    s1 = result.summary.loc[result.summary["Insertion"].eq("S1")].iloc[0]
    assert bool(s1["ForecastLimitWindowInvalid"])
    assert s1["FailCount"] == s1["SampleCount"]
    assert result.details.loc[
        result.details["Insertion"].eq("S1"), "ForecastFailureReason"
    ].eq("Invalid correlated limit window").all()


def test_forecast_rejects_productive_group_without_approved_factor() -> None:
    production = _production()
    production.loc[0, "Corner"] = "VMAX"

    with pytest.raises(ValueError, match="No approved correlation factor matches"):
        forecast_yield(production, _factors(), _profile())


def test_productive_insertion_validation_requires_explicit_csv_per_selection(
    tmp_path: Path,
) -> None:
    s1 = InsertionProfile("S1", "FE", 135, ("characterization.csv",))
    csv = tmp_path / "production.csv"
    csv.write_text("header", encoding="utf-8")

    assignments = validate_productive_insertion_inputs(
        [{"name": "S1", "selected": True, "files": [str(csv)]}],
        (s1,),
    )
    assert assignments[0].insertion == s1
    assert assignments[0].files == (csv.resolve(),)

    with pytest.raises(ValueError, match="at least one productive CSV"):
        validate_productive_insertion_inputs(
            [{"name": "S1", "selected": True, "files": []}],
            (s1,),
        )


def _write_productive_csv(path: Path) -> None:
    rows = [
        "WAFER;X;Y;101",
        ";;;Forecast voltage",
        ";;;0",
        ";;;10",
        ";;;V",
        *[";;;" for _ in range(8)],
        "1;2;3;4.5",
        "1;4;5;6.5",
    ]
    path.write_text("\n".join(rows), encoding="latin1")


def test_productive_adapter_extracts_all_csv_rows_without_chip_manifest(
    tmp_path: Path,
) -> None:
    csv = tmp_path / "productive.csv"
    _write_productive_csv(csv)
    insertion = InsertionProfile("S1", "FE", 135, ())
    extraction = ExtractionProfile(
        name="Productive extraction",
        selector=ProfileTestSelector(exact=(101,)),
        output_columns=(
            "Wafer", "X", "Y", "Test Number", "Test Name", "Test Value",
            "Insertion", "Insertion Type", "Temperature",
        ),
    )

    result = LegacyWideTeCsvAdapter().extract_productive_files(
        (csv,), extraction, insertion
    )

    assert len(result) == 2
    assert result["Test Value"].tolist() == [4.5, 6.5]
    assert result["Insertion"].tolist() == ["S1", "S1"]
    assert result["Productive Source File"].tolist() == [str(csv.resolve())] * 2


def test_productive_loading_populates_plural_candidate_from_canonical_raw_value(
    tmp_path: Path,
) -> None:
    csv = tmp_path / "productive.csv"
    _write_productive_csv(csv)
    insertion = InsertionProfile("S1", "FE", 135, ())
    extraction = ExtractionProfile(
        name="Plural candidate extraction",
        selector=ProfileTestSelector(exact=(101,)),
        output_columns=(
            "Wafer", "X", "Y", "Test Number", "Test Name", "Test Value",
            "Test Values", "Corner", "Insertion", "Insertion Type", "Temperature",
        ),
    )
    profile = replace(_profile(), candidate_column="Test Values")

    loaded = load_productive_csv_inputs(
        (ProductiveInsertionInput(insertion, (csv,)),),
        extraction,
        profile,
    )

    assert loaded["Test Values"].tolist() == [4.5, 6.5]
    assert loaded["Productive Value Source Column"].tolist() == ["Test Value"] * 2


def test_forecast_skips_only_unusable_candidate_rows_and_audits_them() -> None:
    production = _production()
    production["ATE"] = production["ATE"].astype(object)
    production.loc[0, "ATE"] = ""
    production.loc[1, "ATE"] = "not measured"

    result = forecast_yield(production, _factors(), _profile())

    assert len(result.details) == 6
    assert int(result.summary["SampleCount"].sum()) == 6
    assert len(result.rejected) == 2
    assert result.rejected["ForecastRejectionReason"].tolist() == [
        "Blank productive ATE value",
        "Non-numeric productive ATE value",
    ]
    assert result.rejected["ForecastRejectedValue"].tolist() == ["", "not measured"]


def test_forecast_all_invalid_candidates_has_actionable_file_diagnostics() -> None:
    production = _production()
    production["ATE"] = ""
    production["Productive Source File"] = "lot-a.csv"

    with pytest.raises(
        ValueError,
        match=r"all 8 extracted productive row\(s\).*lot-a\.csv: 8 rows",
    ):
        forecast_yield(production, _factors(), _profile())


def test_productive_loading_rejects_conflicting_numeric_candidate_columns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csv = tmp_path / "productive.csv"
    csv.write_text("placeholder", encoding="utf-8")
    insertion = InsertionProfile("S1", "FE", 135, ())
    frame = pd.DataFrame({
        "Test Number": [101, 101],
        "Test Value": [4.5, 6.5],
        "Test Values": [4.5, 99.0],
        "Insertion": ["S1", "S1"],
        "Temperature": [135, 135],
        "Productive Source File": [str(csv), str(csv)],
    })
    monkeypatch.setattr(
        LegacyWideTeCsvAdapter,
        "extract_productive_files",
        lambda self, files, profile, selected_insertion: frame.copy(),
    )

    with pytest.raises(ValueError, match="1 conflicting numeric value"):
        load_productive_csv_inputs(
            (ProductiveInsertionInput(insertion, (csv,)),),
            ExtractionProfile(
                name="Conflicting candidate extraction",
                selector=ProfileTestSelector(exact=(101,)),
                output_columns=tuple(frame.columns),
            ),
            replace(_profile(), candidate_column="Test Values"),
        )


def test_yield_html_embeds_cdf_plots_aligns_insertions_and_highlights_failures(
    tmp_path: Path,
) -> None:
    result = forecast_yield(_production(), _factors(), _profile())
    output = tmp_path / "yield.html"

    count = write_yield_forecast_html(
        result, _profile(), output, image_dpi=35, image_quality=30
    )

    report = output.read_text(encoding="utf-8")
    assert count == 2
    assert list(tmp_path.iterdir()) == [output]
    assert "CorreLaTE correlated yield forecast" in report
    assert "Empirical CDF" not in report  # Rendered inside the embedded images.
    assert "Forecast statistics and correlated limits" in report
    assert "75%" in report
    assert "fail-family" in report
    assert "fail-card" in report
    assert "FAIL markers are highlighted in red" in report
    assert report.count('class="plot-row"') == 1
    assert report.count('class="plot-card fail-card"') == 2
    assert report.index("S1 · FE · 135 °C") < report.index("B1 · BE · 25 °C")
    assert report.count("data:image/") == 3  # Two CDF figures and the logo.
    assert 'src="http' not in report


def test_yield_html_reports_rows_skipped_for_invalid_measurements(tmp_path: Path) -> None:
    production = _production()
    production["ATE"] = production["ATE"].astype(object)
    production.loc[0, "ATE"] = ""
    result = forecast_yield(production, _factors(), _profile())
    output = tmp_path / "yield-with-skips.html"

    write_yield_forecast_html(result, _profile(), output, image_dpi=35, image_quality=30)

    report = output.read_text(encoding="utf-8")
    assert "Input quality warning" in report
    assert "1 extracted row(s) with blank or non-numeric" in report
    assert "excluded—not converted to zero" in report
    assert "Skipped blank/non-numeric rows" in report


def test_forecast_cli_accepts_repeated_insertion_csv_assignments() -> None:
    parser = _build_parser()
    args = parser.parse_args([
        "forecast-yield",
        "--profile", "ctrx8144-txpa",
        "--correlation-report", "correlation.xlsx",
        "--productive-input", "S1=lot-a.csv",
        "--productive-input", "S1=lot-b.csv",
        "--productive-input", "B1=lot-c.csv",
        "--html-report", "yield.html",
    ])

    assert args.correlation_sheet == "Correlation_Summary"
    assert args.productive_input == [
        ("S1", Path("lot-a.csv")),
        ("S1", Path("lot-b.csv")),
        ("B1", Path("lot-c.csv")),
    ]
    assert args.html_report == Path("yield.html")
