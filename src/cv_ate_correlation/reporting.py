"""Generic Excel and plot reporting."""

from __future__ import annotations

import hashlib
import math
import re
import textwrap
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .models import CorrelationProfile
from .correlation import CorrelationResult
from .excel import format_workbook


def _canonical_test_name(frame: pd.DataFrame, profile: CorrelationProfile) -> pd.DataFrame:
    output = frame.copy()
    if "Test Number" not in output.columns:
        output.insert(0, "Test Number", "")
    if "Test Name" not in output.columns:
        for candidate in ("TestName", profile.test_name_column):
            if candidate and candidate in output.columns:
                output.insert(
                    output.columns.get_loc(candidate),
                    "Test Name",
                    output[candidate],
                )
                if candidate == "TestName":
                    output = output.drop(columns=[candidate])
                break
        else:
            output.insert(output.columns.get_loc("Test Number") + 1, "Test Name", "")
    return output


def _test_name_after_test_number(frame: pd.DataFrame) -> pd.DataFrame:
    columns = list(frame.columns)
    if "Test Number" not in columns or "Test Name" not in columns:
        return frame
    columns.remove("Test Name")
    columns.insert(columns.index("Test Number") + 1, "Test Name")
    return frame[columns]


def _report_identity_columns(summary: pd.DataFrame, profile: CorrelationProfile) -> list[str]:
    policy_columns = [
        column
        for column in ("TestSet", "CorrelationStrategy", "GuardBandPolicy")
        if column in summary.columns
    ]
    pooled_columns = [
        column
        for column in summary.columns
        if column == "PooledParameters" or column.startswith("Merged ")
    ]
    requested = [
        "Test Number", *profile.group_by, *policy_columns, *pooled_columns, "Test Name", "Count",
    ]
    return [column for column in dict.fromkeys(requested) if column in summary.columns]


def _correlation_factors_report(
    summary: pd.DataFrame,
    profile: CorrelationProfile,
) -> tuple[pd.DataFrame, list[str]]:
    report = summary.copy()
    strategy = report["CorrelationStrategy"].astype(str)
    one_factor = strategy.isin(("Mean_Deltas", "Median_Deltas"))
    two_factor = strategy.isin(("Linear", "Physics-based"))
    report.loc[one_factor, ["CorrelationFactorA", "CorrelationFactorB"]] = math.nan
    report.loc[two_factor, "CorrelationFactor"] = math.nan
    factor_columns = [
        column
        for column in ("CorrelationFactor", "CorrelationFactorA", "CorrelationFactorB")
        if column in report.columns and report[column].notna().any()
    ]
    columns = [*_report_identity_columns(report, profile), *factor_columns]
    return _test_name_after_test_number(report[columns]), factor_columns


def _guard_bands_report(
    summary: pd.DataFrame,
    profile: CorrelationProfile,
) -> tuple[pd.DataFrame, list[str]]:
    report = summary.copy()
    shifted_upper = report["GuardBandPolicy"].astype(str).eq("shifted_upper_limit")
    report.loc[shifted_upper, "AdjustedLowerLimit"] = math.nan
    report.loc[~shifted_upper, "WorstCaseUpperLimit"] = math.nan
    new_limit_columns = [
        column
        for column in ("AdjustedLowerLimit", "AdjustedUpperLimit", "WorstCaseUpperLimit")
        if column in report.columns and report[column].notna().any()
    ]
    interest_columns = ["GuardBandMethod", *new_limit_columns]
    columns = [
        *_report_identity_columns(report, profile),
        *interest_columns,
        *(column for column in ("Unit",) if column in report.columns),
    ]
    return _test_name_after_test_number(report[columns]), interest_columns


def write_excel_report(result: CorrelationResult, profile: CorrelationProfile, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    summary = _test_name_after_test_number(_canonical_test_name(result.summary, profile))
    details = _test_name_after_test_number(_canonical_test_name(result.details, profile))
    factors, factor_columns = _correlation_factors_report(summary, profile)
    guards, guard_interest_columns = _guard_bands_report(summary, profile)
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        factors.to_excel(writer, index=False, sheet_name="Correlation_Factors")
        guards.to_excel(writer, index=False, sheet_name="Guard_Bands")
        _test_name_after_test_number(summary).to_excel(writer, index=False, sheet_name="Correlation_Summary")
        _test_name_after_test_number(details).to_excel(writer, index=False, sheet_name="Correlated_Data")
    format_workbook(output, highlighted_columns={
        "Correlation_Factors": factor_columns,
        "Guard_Bands": guard_interest_columns,
    })


def _finite_values(values: Iterable[Any]) -> list[float]:
    numeric = pd.to_numeric(pd.Series(list(values), dtype="object"), errors="coerce")
    return [float(value) for value in numeric.dropna() if math.isfinite(float(value))]


def _set_padded_ylim(axis: Any, values: Iterable[Any]) -> None:
    finite = _finite_values(values)
    if not finite:
        return
    low, high = min(finite), max(finite)
    span = high - low
    padding = 0.06 * span if span > 0 else abs(low) * 0.06 + 1.0
    axis.set_ylim(low - padding, high + padding)


def _insertion_bucket(summary: pd.Series, details: pd.DataFrame) -> str:
    groups: set[str] = set()
    for column in ("Insertion Type", "Insertion"):
        values: list[Any] = []
        if column in details.columns:
            values.extend(details[column].dropna().tolist())
        if column in summary.index and pd.notna(summary[column]):
            values.append(summary[column])
        for value in values:
            text = str(value).strip().upper()
            if not text or text == "MERGED":
                continue
            if text == "BE" or text.startswith("B"):
                groups.add("BE")
            elif text == "FE" or text.startswith(("S", "F")):
                groups.add("FE")
    if len(groups) > 1:
        raise ValueError("A plot group contains both FE and BE insertion rows; include insertion in the grouping")
    return next(iter(groups), "FE")


def _title_for_group(summary: pd.Series, profile: CorrelationProfile, sample_count: int) -> tuple[str, str]:
    parts = [profile.name]
    for column in profile.group_by:
        if column in summary.index and pd.notna(summary[column]):
            parts.append(f"{column}={summary[column]}")
    test_name = str(summary.get("TestName", "") or "").strip()
    if test_name:
        parts.append(f"Test Name={test_name}")
    if summary.get("TestSet"):
        parts.append(f"TestSet={summary['TestSet']}")
    for column in summary.index:
        if column.startswith("Merged ") and not column.endswith(" Count") and pd.notna(summary[column]):
            parts.append(f"{column}={summary[column]}")
    parts.append(f"N={sample_count}")
    unwrapped = " | ".join(parts)
    return unwrapped, "\n".join(textwrap.wrap(unwrapped, width=110, break_long_words=False))


def write_plots(result: CorrelationResult, profile: CorrelationProfile, output_folder: Path, dpi: int = 160) -> int:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patheffects as path_effects
    import matplotlib.transforms as transforms

    output_folder.mkdir(parents=True, exist_ok=True)
    folder_name = output_folder.resolve().name or "plots"
    insertion_folders = {
        "FE": output_folder / f"{folder_name}_FE",
        "BE": output_folder / f"{folder_name}_BE",
    }
    for folder in insertion_folders.values():
        folder.mkdir(parents=True, exist_ok=True)

    count = 0
    for group_index, details in result.details.groupby("GroupIndex", sort=True):
        summary = result.summary.iloc[int(group_index)]
        pooled_dimensions = tuple(
            value.strip() for value in str(summary.get("PooledParameters", "")).split(",") if value.strip()
        )
        sort_columns = list(dict.fromkeys(
            column
            for column in ("DoE split", *profile.detail_key_columns, *pooled_dimensions)
            if column in details.columns
        ))
        if sort_columns:
            details = details.sort_values(sort_columns, kind="stable")
        details = details.reset_index(drop=True)
        sample = pd.Series(range(len(details)), dtype="int64")
        unwrapped_title, title = _title_for_group(summary, profile, len(details))
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", unwrapped_title).strip("_")
        digest = hashlib.sha1(unwrapped_title.encode("utf-8")).hexdigest()[:10]
        destination = insertion_folders[_insertion_bucket(summary, details)]
        fixed_base = f"G{int(group_index):04d}_Samples_{len(details)}"
        longest_suffix = "__models.png"
        available_slug = max(
            0,
            240
            - len(str(destination.resolve()))
            - 1
            - len(fixed_base)
            - len(digest)
            - len(longest_suffix)
            - 2,
        )
        readable_slug = slug[:min(48, available_slug)]
        base = f"{fixed_base}_{readable_slug + '_' if readable_slug else ''}{digest}"
        unit = str(summary.get("Unit", "") or "").strip()
        value_label = f"Value [{unit}]" if unit else "Value"
        residual_label = f"Residual [{unit}]" if unit else "Residual"

        doe: pd.Series | None = None
        boundaries: list[int] = []
        starts: list[int] = []
        ends: list[int] = []
        if "DoE split" in details.columns:
            doe = details["DoE split"].astype(str).replace({"nan": "", "None": ""}).str.strip()
            doe = doe.where(doe != "", other="(blank)").str.upper()
            boundaries = [index for index in range(1, len(doe)) if doe.iloc[index] != doe.iloc[index - 1]]
            starts = [0, *boundaries]
            ends = [*boundaries, len(doe)]

        def add_doe_guides(axis: Any) -> None:
            if doe is None:
                return
            for boundary in boundaries:
                axis.axvline(boundary - 0.5, color="gray", linestyle="--", linewidth=1.2, alpha=0.45, zorder=0)
            blend = transforms.blended_transform_factory(axis.transData, axis.transAxes)
            for start, end in zip(starts, ends):
                if end <= start:
                    continue
                axis.text(
                    (start + end - 1) / 2.0,
                    0.96,
                    str(doe.iloc[start]),
                    transform=blend,
                    ha="center",
                    va="top",
                    fontsize=11,
                    path_effects=[path_effects.withStroke(linewidth=3.0, foreground="white", alpha=0.9)],
                )

        x_label = "Samples (sorted by DoE split)" if doe is not None else "Samples"
        reference = pd.to_numeric(details["ReferenceValue"], errors="coerce")
        candidate = pd.to_numeric(details["CandidateValue"], errors="coerce")
        linear_prediction = pd.to_numeric(details["LinearCorrectedCandidate"], errors="coerce")
        mean_prediction = pd.to_numeric(details["MeanDeltasCorrectedCandidate"], errors="coerce")
        median_prediction = pd.to_numeric(details["MedianDeltasCorrectedCandidate"], errors="coerce")
        physics_prediction = pd.to_numeric(details["PhysicsCorrectedCandidate"], errors="coerce")

        # Figure A: raw and correlated series.
        fig_series, (raw_axis, corrected_axis) = plt.subplots(2, 1, figsize=(12.0, 9.0))
        fig_series.suptitle(title, fontsize=13, y=0.98)
        raw_axis.set_title("Raw CV/Lab and ATE series", fontsize=12)
        raw_axis.plot(sample, reference, marker="o", linestyle="-", linewidth=2.2, markersize=6, label="CV/Lab")
        raw_axis.plot(sample, candidate, marker="s", linestyle="--", linewidth=2.2, markersize=6, label="ATE raw")
        add_doe_guides(raw_axis)
        raw_axis.set_xlim(-0.5, len(details) - 0.5)
        raw_axis.set_xlabel(x_label, fontsize=12)
        raw_axis.set_ylabel(value_label, fontsize=12)
        raw_axis.tick_params(axis="both", labelsize=11)
        raw_axis.grid(True, alpha=0.25)
        _set_padded_ylim(raw_axis, [*reference, *candidate])
        raw_axis.text(
            0.015,
            0.02,
            (
                f"N={len(details)}  OLS a={float(summary['LinearSlope']):.4g}  "
                f"b={float(summary['LinearIntercept']):.4g}  mean(CV−ATE)={float(summary['MeanDelta']):.4g}  "
                f"median(CV−ATE)={float(summary['MedianDelta']):.4g}  "
                f"max|CV−ATE|={float(summary['MaximumAbsoluteDelta']):.4g}"
            ),
            transform=raw_axis.transAxes,
            fontsize=11,
            va="bottom",
            bbox={"facecolor": "white", "alpha": 0.85, "edgecolor": "none", "pad": 3.0},
        )
        raw_axis.legend(fontsize=10, framealpha=0.92)

        corrected_axis.set_title("Correlated series and limits", fontsize=12)
        corrected_axis.plot(sample, reference, marker="o", linestyle="-", linewidth=2.2, markersize=6, label="CV/Lab")
        corrected_axis.plot(
            sample, linear_prediction, marker="x", linestyle="--", color="tab:green",
            linewidth=2.0, markersize=6, label="Linear CV_pred",
        )
        corrected_axis.plot(
            sample, mean_prediction, marker="v", linestyle=(0, (5, 2)), color="tab:orange",
            linewidth=2.0, markersize=5, label="Mean_Deltas CV_pred",
        )
        corrected_axis.plot(
            sample, median_prediction, marker="^", linestyle="-.", color="tab:red",
            linewidth=2.0, markersize=5, label="Median_Deltas CV_pred",
        )
        if physics_prediction.notna().any():
            corrected_axis.plot(
                sample, physics_prediction, marker="D", linestyle=":", color="tab:purple",
                linewidth=2.0, markersize=4, label="Physics-based CV_pred",
            )
        add_doe_guides(corrected_axis)
        limit_styles: list[tuple[str, str, str, str]] = []
        if str(summary.get("GuardBandPolicy", "")) != "shifted_upper_limit":
            limit_styles.append(("AdjustedLowerLimit", "New LTL", "cyan", "-."))
        limit_styles.extend((
            ("AdjustedUpperLimit", "New UTL", "cyan", "-."),
            ("WorstCaseUpperLimit", "Worst-case UTL", "tab:red", "--"),
        ))
        correlated_y: list[Any] = [
            *reference, *linear_prediction, *mean_prediction, *median_prediction, *physics_prediction,
        ]
        for column, label, color, line_style in limit_styles:
            value = summary.get(column)
            if pd.notna(value):
                numeric_value = float(value)
                corrected_axis.axhline(
                    numeric_value, color=color, linestyle=line_style, linewidth=2.0,
                    label=f"{label} = {numeric_value:.4g}",
                )
                correlated_y.append(numeric_value)
        corrected_axis.set_xlim(-0.5, len(details) - 0.5)
        corrected_axis.set_xlabel(x_label, fontsize=12)
        corrected_axis.set_ylabel(value_label, fontsize=12)
        corrected_axis.tick_params(axis="both", labelsize=11)
        corrected_axis.grid(True, alpha=0.25)
        _set_padded_ylim(corrected_axis, correlated_y)
        invalid_window = bool(summary.get("LimitWindowInvalid", False))
        note = (
            f"Primary={summary.get('CorrelationStrategy', '')}  GB={summary.get('GuardBandPolicy', '')}  "
            f"max|residual|={float(summary['MaxAbsResidual']):.4g}"
        )
        if invalid_window:
            note += "  [INVALID LIMIT WINDOW]"
        corrected_axis.text(
            0.015,
            0.02,
            note,
            transform=corrected_axis.transAxes,
            fontsize=11,
            color="darkred" if invalid_window else "black",
            va="bottom",
            bbox={"facecolor": "white", "alpha": 0.85, "edgecolor": "none", "pad": 3.0},
        )
        corrected_axis.legend(fontsize=9, framealpha=0.92)
        fig_series.tight_layout(rect=[0, 0, 1, 0.96])
        fig_series.savefig(destination / f"{base}__series.png", dpi=dpi, bbox_inches="tight")
        plt.close(fig_series)
        count += 1

        # Figure B: CV-vs-ATE model view and residuals.
        fig_models, (model_axis, residual_axis) = plt.subplots(2, 1, figsize=(12.0, 9.0))
        fig_models.suptitle(title, fontsize=13, y=0.98)
        model_axis.set_title("CV/Lab prediction from ATE", fontsize=12)
        model_axis.scatter(
            candidate, reference, s=40, alpha=0.95, marker="o", linewidths=1.1,
            facecolors="none", edgecolors="tab:blue", zorder=3, label="Measured CV/Lab",
        )
        x_values = _finite_values(candidate)
        x_min, x_max = min(x_values), max(x_values)
        if x_min == x_max:
            x_min -= abs(x_min) * 0.06 + 1.0
            x_max += abs(x_max) * 0.06 + 1.0
        x_line = pd.Series([x_min, x_max], dtype=float)
        linear_cv = float(summary["LinearSlope"]) * x_line + float(summary["LinearIntercept"])
        mean_cv = x_line + float(summary["MeanDelta"])
        median_cv = x_line + float(summary["MedianDelta"])
        model_axis.plot(
            x_line, linear_cv, linewidth=2.6, linestyle="--", color="tab:green",
            label="Linear OLS: CV = a·ATE + b",
        )
        model_axis.plot(
            x_line, mean_cv, linewidth=2.4, linestyle=(0, (5, 2)), color="tab:orange",
            label="Mean_Deltas (fixed slope 1): CV = ATE + meanΔ",
        )
        model_axis.plot(
            x_line, median_cv, linewidth=2.4, linestyle="-.", color="tab:red",
            label="Median_Deltas (fixed slope 1): CV = ATE + medianΔ",
        )
        physics_mask = physics_prediction.notna() & candidate.notna()
        if physics_mask.any():
            model_axis.scatter(
                candidate[physics_mask], physics_prediction[physics_mask], s=28, alpha=0.9,
                marker="D", linewidths=0.9, facecolors="none", edgecolors="tab:purple",
                label="Physics: CV = ATE − (αKf + β)",
            )
        x_padding = 0.06 * (x_max - x_min)
        model_axis.set_xlim(x_min - x_padding, x_max + x_padding)
        model_axis.set_xlabel(f"ATE [{unit}]" if unit else "ATE", fontsize=12)
        model_axis.set_ylabel(f"CV/Lab [{unit}]" if unit else "CV/Lab", fontsize=12)
        model_axis.tick_params(axis="both", labelsize=11)
        model_axis.grid(True, alpha=0.25)
        _set_padded_ylim(model_axis, [*reference, *linear_cv, *mean_cv, *median_cv, *physics_prediction])
        physics_metrics = (
            f"R²_Physics={float(summary['PhysicsR2']):.3f}  α={float(summary['PhysicsAlpha']):.4g}  "
            f"β={float(summary['PhysicsBeta']):.4g}"
            if physics_prediction.notna().any()
            else f"Physics unavailable: {summary.get('PhysicsStatus', 'unknown reason')}"
        )
        model_axis.text(
            0.015,
            0.02,
            (
                f"N={len(details)}  OLS a={float(summary['LinearSlope']):.4g}  "
                f"b={float(summary['LinearIntercept']):.4g}  R²_Linear={float(summary['LinearR2']):.3f}  "
                f"R²_Mean={float(summary['MeanDeltasR2']):.3f}  "
                f"R²_Median={float(summary['MedianDeltasR2']):.3f}  {physics_metrics}"
            ),
            transform=model_axis.transAxes,
            fontsize=10,
            va="bottom",
            bbox={"facecolor": "white", "alpha": 0.85, "edgecolor": "none", "pad": 3.0},
        )
        model_axis.legend(fontsize=9, framealpha=0.92)

        residual_axis.set_title("Model residuals: CV/Lab − CV_pred", fontsize=12)
        residual_axis.axhline(0.0, color="black", linewidth=1.2, alpha=0.6)
        residual_series = (
            ("LinearResidual", "Linear residual", "x", "--", "tab:green"),
            ("MeanDeltasResidual", "Mean_Deltas residual", "v", (0, (5, 2)), "tab:orange"),
            ("MedianDeltasResidual", "Median_Deltas residual", "^", "-.", "tab:red"),
            ("PhysicsResidual", "Physics-based residual", "D", ":", "tab:purple"),
        )
        residual_y: list[Any] = [0.0]
        for column, label, marker, line_style, color in residual_series:
            values = pd.to_numeric(details[column], errors="coerce")
            if not values.notna().any():
                continue
            residual_axis.plot(
                sample, values, marker=marker, linestyle=line_style, color=color,
                linewidth=1.8, markersize=5 if marker != "D" else 4, label=label,
            )
            residual_y.extend(values.tolist())
        add_doe_guides(residual_axis)
        residual_axis.set_xlim(-0.5, len(details) - 0.5)
        residual_axis.set_xlabel(x_label, fontsize=12)
        residual_axis.set_ylabel(residual_label, fontsize=12)
        residual_axis.tick_params(axis="both", labelsize=11)
        residual_axis.grid(True, alpha=0.25)
        _set_padded_ylim(residual_axis, residual_y)
        residual_note = (
            f"σ_Linear={float(summary['LinearResidualStd']):.4g}  "
            f"σ_Mean={float(summary['MeanDeltasResidualStd']):.4g}  "
            f"σ_Median={float(summary['MedianDeltasResidualStd']):.4g}"
        )
        if physics_prediction.notna().any():
            residual_note += f"  σ_Physics={float(summary['PhysicsResidualStd']):.4g}"
        residual_axis.text(
            0.015,
            0.02,
            residual_note,
            transform=residual_axis.transAxes,
            fontsize=10,
            va="bottom",
            bbox={"facecolor": "white", "alpha": 0.85, "edgecolor": "none", "pad": 3.0},
        )
        residual_axis.legend(fontsize=9, framealpha=0.92)
        fig_models.tight_layout(rect=[0, 0, 1, 0.96])
        fig_models.savefig(destination / f"{base}__models.png", dpi=dpi, bbox_inches="tight")
        plt.close(fig_models)
        count += 1
    return count
