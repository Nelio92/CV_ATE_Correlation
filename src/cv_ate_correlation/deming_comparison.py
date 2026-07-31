"""Optional OLS/Deming/Median comparison without changing production strategies."""

from __future__ import annotations

import hashlib
import math
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .correlation import CorrelationResult
from .excel import format_workbook


@dataclass(frozen=True)
class DemingFit:
    """Deming line and diagnostics for ``CV/Lab = slope * ATE + intercept``."""

    slope: float
    intercept: float
    pearson_r: float
    status: str


def deming_fit(
    ate: pd.Series,
    lab: pd.Series,
    *,
    error_variance_ratio: float = 1.0,
) -> DemingFit:
    """Fit Deming regression using ``Var(Lab error) / Var(ATE error)`` as the ratio.

    A ratio of 1.0 is an equal-error-variance sensitivity assumption, not an
    estimate. Replicate or metrology uncertainty data is required to estimate it.
    """
    if not math.isfinite(error_variance_ratio) or error_variance_ratio <= 0:
        raise ValueError("Deming error_variance_ratio must be a finite value greater than zero")
    x = pd.to_numeric(ate, errors="coerce")
    y = pd.to_numeric(lab, errors="coerce")
    valid = x.notna() & y.notna()
    x = x[valid].astype(float)
    y = y[valid].astype(float)
    if len(x) < 2:
        return DemingFit(math.nan, math.nan, math.nan, "Fewer than two numeric pairs")
    centered_x = x - float(x.mean())
    centered_y = y - float(y.mean())
    sxx = float((centered_x * centered_x).sum())
    syy = float((centered_y * centered_y).sum())
    sxy = float((centered_x * centered_y).sum())
    if sxx == 0.0:
        return DemingFit(math.nan, math.nan, math.nan, "ATE values have no variation")
    if syy == 0.0:
        return DemingFit(math.nan, math.nan, math.nan, "Lab values have no variation")
    pearson_r = sxy / math.sqrt(sxx * syy)
    if sxy == 0.0:
        return DemingFit(math.nan, math.nan, pearson_r, "ATE and Lab covariance is zero")
    ratio = error_variance_ratio
    difference = syy - ratio * sxx
    discriminant = difference * difference + 4.0 * ratio * sxy * sxy
    slope = (difference + math.sqrt(discriminant)) / (2.0 * sxy)
    intercept = float(y.mean()) - slope * float(x.mean())
    status = "Available"
    if abs(pearson_r) < 0.7:
        status = "Available; weak linear association makes the Deming slope unstable"
    return DemingFit(float(slope), float(intercept), float(pearson_r), status)


def _r2(actual: pd.Series, predicted: pd.Series) -> float:
    valid = actual.notna() & predicted.notna()
    actual = actual[valid].astype(float)
    predicted = predicted[valid].astype(float)
    if len(actual) < 2:
        return math.nan
    total = float(((actual - actual.mean()) ** 2).sum())
    return math.nan if total == 0.0 else 1.0 - float(((actual - predicted) ** 2).sum()) / total


def add_deming_comparison(
    result: CorrelationResult,
    *,
    error_variance_ratio: float = 1.0,
) -> CorrelationResult:
    """Add equal-structure Deming predictions and diagnostics to a correlation result."""
    summary = result.summary.copy()
    details = result.details.copy()
    if "GroupIndex" not in details.columns:
        raise ValueError("Correlation details must contain GroupIndex")

    summary_columns = (
        "DemingErrorVarianceRatio", "DemingSlope", "DemingIntercept", "DemingPearsonR",
        "DemingR2", "DemingResidualStd", "DemingRMSE", "DemingMAE",
        "DemingMaxAbsResidual", "DemingOrthogonalRMSE", "DemingVsOLSMaxPredictionDifference",
        "DemingStatus",
    )
    for column in summary_columns:
        summary[column] = "" if column == "DemingStatus" else math.nan
    details["DemingCorrectedCandidate"] = math.nan
    details["DemingResidual"] = math.nan

    for raw_index, group in details.groupby("GroupIndex", sort=True):
        group_index = int(raw_index)
        if group_index < 0 or group_index >= len(summary):
            raise ValueError(f"GroupIndex {group_index} has no matching summary row")
        reference = pd.to_numeric(group["ReferenceValue"], errors="coerce")
        candidate = pd.to_numeric(group["CandidateValue"], errors="coerce")
        fit = deming_fit(candidate, reference, error_variance_ratio=error_variance_ratio)
        summary.loc[group_index, "DemingErrorVarianceRatio"] = error_variance_ratio
        summary.loc[group_index, "DemingPearsonR"] = fit.pearson_r
        summary.loc[group_index, "DemingStatus"] = fit.status
        if not math.isfinite(fit.slope) or not math.isfinite(fit.intercept):
            continue
        prediction = fit.slope * candidate + fit.intercept
        residual = reference - prediction
        valid = residual.notna()
        residual_values = residual[valid].astype(float)
        summary.loc[group_index, "DemingSlope"] = fit.slope
        summary.loc[group_index, "DemingIntercept"] = fit.intercept
        summary.loc[group_index, "DemingR2"] = _r2(reference, prediction)
        summary.loc[group_index, "DemingResidualStd"] = float(residual_values.std(ddof=1))
        summary.loc[group_index, "DemingRMSE"] = float((residual_values.pow(2).mean()) ** 0.5)
        summary.loc[group_index, "DemingMAE"] = float(residual_values.abs().mean())
        summary.loc[group_index, "DemingMaxAbsResidual"] = float(residual_values.abs().max())
        summary.loc[group_index, "DemingOrthogonalRMSE"] = float(
            (residual_values.pow(2).mean() / (1.0 + fit.slope * fit.slope)) ** 0.5
        )
        if "LinearCorrectedCandidate" in group.columns:
            ols = pd.to_numeric(group["LinearCorrectedCandidate"], errors="coerce")
            summary.loc[group_index, "DemingVsOLSMaxPredictionDifference"] = float(
                (prediction - ols).abs().max()
            )
        details.loc[group.index, "DemingCorrectedCandidate"] = prediction
        details.loc[group.index, "DemingResidual"] = residual
    return CorrelationResult(summary, details)


def write_deming_comparison_report(result: CorrelationResult, output: Path) -> None:
    """Write the augmented comparison metrics and row-level predictions."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        result.summary.to_excel(writer, index=False, sheet_name="Model_Comparison")
        result.details.to_excel(writer, index=False, sheet_name="Comparison_Data")
    format_workbook(output)


def _finite(values: Iterable[Any]) -> list[float]:
    numeric = pd.to_numeric(pd.Series(list(values), dtype="object"), errors="coerce")
    return [float(value) for value in numeric.dropna() if math.isfinite(float(value))]


def _padded_limits(values: Iterable[Any]) -> tuple[float, float] | None:
    finite = _finite(values)
    if not finite:
        return None
    low, high = min(finite), max(finite)
    span = high - low
    padding = 0.06 * span if span else abs(low) * 0.06 + 1.0
    return low - padding, high + padding


def _set_ylim(axis: Any, values: Iterable[Any]) -> None:
    limits = _padded_limits(values)
    if limits is not None:
        axis.set_ylim(*limits)


def _insertion_bucket(summary: pd.Series, details: pd.DataFrame) -> str:
    for column in ("Insertion Type", "Insertion"):
        values = details[column].dropna().astype(str) if column in details.columns else pd.Series(dtype=str)
        if values.empty and column in summary.index and pd.notna(summary[column]):
            values = pd.Series([str(summary[column])])
        for value in values:
            text = str(value).strip().upper()
            if text == "BE" or text.startswith("B"):
                return "BE"
            if text == "FE" or text.startswith(("S", "F")):
                return "FE"
    return "FE"


def _group_title(summary: pd.Series, sample_count: int) -> tuple[str, str]:
    parts = ["OLS vs equal-variance Deming vs Median_Deltas"]
    for column in (
        "TestSet", "Frequency", "Supply Corner", "Digital Control", "Insertion", "Temperature",
        "Merged Test Number", "Merged Channel",
    ):
        if column in summary.index and pd.notna(summary[column]) and str(summary[column]).strip():
            parts.append(f"{column}={summary[column]}")
    parts.append(f"N={sample_count}")
    raw = " | ".join(parts)
    return raw, "\n".join(textwrap.wrap(raw, width=105, break_long_words=False))


def write_deming_comparison_plots(
    result: CorrelationResult,
    output_folder: Path,
    *,
    dpi: int = 160,
) -> int:
    """Write series and model plots containing only OLS, Deming, and Median_Deltas."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output_folder.mkdir(parents=True, exist_ok=True)
    folder_name = output_folder.resolve().name or "deming_comparison"
    destinations = {
        bucket: output_folder / f"{folder_name}_{bucket}" for bucket in ("FE", "BE")
    }
    for destination in destinations.values():
        destination.mkdir(parents=True, exist_ok=True)

    count = 0
    for raw_index, group in result.details.groupby("GroupIndex", sort=True):
        group_index = int(raw_index)
        summary = result.summary.iloc[group_index]
        sort_columns = [
            column for column in ("DoE split", "DUT Nr", "Test Number", "Channel")
            if column in group.columns
        ]
        if sort_columns:
            group = group.sort_values(sort_columns, kind="stable")
        group = group.reset_index(drop=True)
        sample = pd.Series(range(len(group)), dtype="int64")
        raw_title, title = _group_title(summary, len(group))
        digest = hashlib.sha1(raw_title.encode("utf-8")).hexdigest()[:10]
        destination = destinations[_insertion_bucket(summary, group)]
        base = f"G{group_index:04d}_Samples_{len(group)}_{digest}"
        unit = str(summary.get("Unit", "") or "").strip()
        reference = pd.to_numeric(group["ReferenceValue"], errors="coerce")
        candidate = pd.to_numeric(group["CandidateValue"], errors="coerce")
        ols = pd.to_numeric(group["LinearCorrectedCandidate"], errors="coerce")
        deming = pd.to_numeric(group["DemingCorrectedCandidate"], errors="coerce")
        median = pd.to_numeric(group["MedianDeltasCorrectedCandidate"], errors="coerce")
        value_label = f"Value [{unit}]" if unit else "Value"
        warning = str(summary.get("DemingStatus", ""))
        metrics = (
            f"r={float(summary['DemingPearsonR']):.3f}  "
            f"OLS a={float(summary['LinearSlope']):.4g}  "
            f"Deming a={float(summary['DemingSlope']):.4g}  "
            f"λ={float(summary['DemingErrorVarianceRatio']):g}  "
            f"medianΔ={float(summary['MedianDelta']):.4g}"
        )

        fig, (raw_axis, corrected_axis) = plt.subplots(2, 1, figsize=(12, 9))
        fig.suptitle(title, fontsize=13, y=0.98)
        raw_axis.set_title("Raw CV/Lab and ATE series")
        raw_axis.plot(sample, reference, "o-", linewidth=2, markersize=5, label="Measured CV/Lab")
        raw_axis.plot(sample, candidate, "s--", linewidth=2, markersize=5, label="ATE raw")
        raw_axis.set_ylabel(value_label)
        raw_axis.grid(True, alpha=0.25)
        _set_ylim(raw_axis, [*reference, *candidate])
        raw_axis.legend(fontsize=9)

        corrected_axis.set_title("Three-model corrected series")
        corrected_axis.plot(sample, reference, "o-", linewidth=2, markersize=5, label="Measured CV/Lab")
        corrected_axis.plot(sample, ols, "x--", color="tab:green", linewidth=2, label="Linear OLS")
        corrected_axis.plot(sample, deming, "+:", color="tab:blue", linewidth=2, label="Linear Deming (λ=1)")
        corrected_axis.plot(sample, median, "^-.", color="tab:red", linewidth=2, markersize=4, label="Median_Deltas")
        corrected_axis.set_xlabel("Samples")
        corrected_axis.set_ylabel(value_label)
        corrected_axis.grid(True, alpha=0.25)
        _set_ylim(corrected_axis, [*reference, *ols, *deming, *median])
        corrected_axis.text(
            0.01, 0.02, f"{metrics}\n{warning}", transform=corrected_axis.transAxes, fontsize=9,
            bbox={"facecolor": "white", "alpha": 0.88, "edgecolor": "none"},
        )
        corrected_axis.legend(fontsize=9)
        fig.tight_layout(rect=[0, 0, 1, 0.96])
        fig.savefig(destination / f"{base}__series.png", dpi=dpi, bbox_inches="tight")
        plt.close(fig)
        count += 1

        fig, (model_axis, residual_axis) = plt.subplots(2, 1, figsize=(12, 9))
        fig.suptitle(title, fontsize=13, y=0.98)
        model_axis.set_title("CV/Lab prediction from ATE")
        model_axis.scatter(
            candidate, reference, s=38, marker="o", facecolors="none", edgecolors="tab:gray",
            label="Measured CV/Lab",
        )
        x_values = _finite(candidate)
        x_min, x_max = min(x_values), max(x_values)
        if x_min == x_max:
            x_min -= abs(x_min) * 0.06 + 1.0
            x_max += abs(x_max) * 0.06 + 1.0
        x_line = pd.Series([x_min, x_max], dtype=float)
        ols_line = float(summary["LinearSlope"]) * x_line + float(summary["LinearIntercept"])
        deming_line = float(summary["DemingSlope"]) * x_line + float(summary["DemingIntercept"])
        median_line = x_line + float(summary["MedianDelta"])
        model_axis.plot(x_line, ols_line, "--", color="tab:green", linewidth=2.5, label="Linear OLS")
        model_axis.plot(x_line, deming_line, ":", color="tab:blue", linewidth=2.5, label="Linear Deming (λ=1)")
        model_axis.plot(x_line, median_line, "-.", color="tab:red", linewidth=2.3, label="Median_Deltas")
        model_axis.set_xlabel(f"ATE [{unit}]" if unit else "ATE")
        model_axis.set_ylabel(f"CV/Lab [{unit}]" if unit else "CV/Lab")
        model_axis.grid(True, alpha=0.25)
        _set_ylim(model_axis, [*reference, *ols_line, *deming_line, *median_line])
        model_axis.text(
            0.01, 0.02, f"{metrics}\n{warning}", transform=model_axis.transAxes, fontsize=9,
            bbox={"facecolor": "white", "alpha": 0.88, "edgecolor": "none"},
        )
        model_axis.legend(fontsize=9)

        residual_axis.set_title("Vertical residuals: CV/Lab − CV_pred")
        residual_axis.axhline(0, color="black", linewidth=1.1, alpha=0.6)
        residual_sets = (
            ("LinearResidual", "OLS residual", "x--", "tab:green"),
            ("DemingResidual", "Deming residual", "+:", "tab:blue"),
            ("MedianDeltasResidual", "Median_Deltas residual", "^-.", "tab:red"),
        )
        residual_values: list[Any] = [0.0]
        for column, label, style, color in residual_sets:
            values = pd.to_numeric(group[column], errors="coerce")
            residual_axis.plot(sample, values, style, color=color, linewidth=1.8, markersize=4, label=label)
            residual_values.extend(values.tolist())
        residual_axis.set_xlabel("Samples")
        residual_axis.set_ylabel(f"Residual [{unit}]" if unit else "Residual")
        residual_axis.grid(True, alpha=0.25)
        _set_ylim(residual_axis, residual_values)
        residual_axis.text(
            0.01, 0.02,
            f"RMSE OLS={float(pd.to_numeric(group['LinearResidual']).pow(2).mean() ** 0.5):.4g}  "
            f"Deming={float(summary['DemingRMSE']):.4g}  "
            f"Median={float(pd.to_numeric(group['MedianDeltasResidual']).pow(2).mean() ** 0.5):.4g}",
            transform=residual_axis.transAxes, fontsize=9,
            bbox={"facecolor": "white", "alpha": 0.88, "edgecolor": "none"},
        )
        residual_axis.legend(fontsize=9)
        fig.tight_layout(rect=[0, 0, 1, 0.96])
        fig.savefig(destination / f"{base}__models.png", dpi=dpi, bbox_inches="tight")
        plt.close(fig)
        count += 1
    return count
