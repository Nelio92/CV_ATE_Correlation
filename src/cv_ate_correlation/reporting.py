"""Generic Excel and plot reporting."""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from .models import CorrelationProfile
from .correlation import CorrelationResult
from .excel import format_workbook


def write_excel_report(result: CorrelationResult, profile: CorrelationProfile, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    policy_columns = [
        column
        for column in ("TestSet", "CorrelationStrategy", "GuardBandPolicy")
        if column in result.summary.columns
    ]
    factor_columns = [*profile.group_by, *policy_columns, "TestName", "Count", "CorrelationFactor"]
    guard_columns = [
        *profile.group_by, *policy_columns, "TestName", "Count", "GuardBandMethod", "OriginalLowerLimit",
        "OriginalUpperLimit", "AdjustedLowerLimit", "AdjustedUpperLimit", "WorstCaseUpperLimit", "Unit",
    ]
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        result.summary[factor_columns].to_excel(writer, index=False, sheet_name="Correlation_Factors")
        result.summary[[column for column in guard_columns if column in result.summary]].to_excel(
            writer, index=False, sheet_name="Guard_Bands"
        )
        result.summary.to_excel(writer, index=False, sheet_name="Correlation_Summary")
        result.details.to_excel(writer, index=False, sheet_name="Correlated_Data")
    format_workbook(output)


def write_plots(result: CorrelationResult, profile: CorrelationProfile, output_folder: Path, dpi: int = 160) -> int:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output_folder.mkdir(parents=True, exist_ok=True)
    count = 0
    for group_index, details in result.details.groupby("GroupIndex", sort=True):
        summary = result.summary.iloc[int(group_index)]
        fig, (raw_axis, corrected_axis) = plt.subplots(2, 1, figsize=(11, 8), sharex=True)
        sample = range(len(details))
        raw_axis.plot(sample, details["ReferenceValue"], "o-", label="Reference")
        raw_axis.plot(sample, details["CandidateValue"], "s--", label="ATE")
        raw_axis.set_ylabel(summary.get("Unit", "") or "Value")
        raw_axis.grid(alpha=0.25)
        raw_axis.legend()
        corrected_axis.plot(sample, details["ReferenceValue"], "o-", label="Reference")
        corrected_axis.plot(sample, details["CorrectedCandidate"], "^--", label="ATE correlated")
        for column, label in (("AdjustedLowerLimit", "Low"), ("AdjustedUpperLimit", "High")):
            value = summary.get(column)
            if pd.notna(value):
                corrected_axis.axhline(float(value), linestyle=":", label=f"{label}={float(value):.4g}")
        corrected_axis.set_xlabel("Samples")
        corrected_axis.set_ylabel(summary.get("Unit", "") or "Value")
        corrected_axis.grid(alpha=0.25)
        corrected_axis.legend()
        title_fields = [*profile.group_by]
        if summary.get("TestSet"):
            title_fields.append("TestSet")
        title = " | ".join(f"{key}={summary[key]}" for key in title_fields)
        fig.suptitle(title)
        fig.tight_layout()
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", title).strip("_")[:180]
        fig.savefig(output_folder / f"{slug or group_index}.png", dpi=dpi, bbox_inches="tight")
        plt.close(fig)
        count += 1
    return count
