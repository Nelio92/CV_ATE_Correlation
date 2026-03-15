"""\
CV ↔ ATE correlation (flat script, no classes).

Reads a single raw data Excel file that contains CV and ATE data (typically in
separate sheets), merges rows by device/test keys, then groups by:
    Test Number, supply corner, frequency, and temperature
and performs per-group correlation using a robust offset-only model:
    - correlation factor = median(CV - ATE)

For each group (test case), the script:
    - Computes delta = (CV - ATE)
    - Uses median(delta) as the correction factor
    - Produces corrected ATE: ATE_correlated = ATE + median(delta)
    - Computes residuals as: residual = delta - median(delta)
    - Computes σ(residual) and a guard-band (X * σ)
    - Computes an R² value for the offset-only model (ATE_pred = CV - median(delta))
    - Outputs correlation factors + derived limits to an Excel file
    - Outputs row-level corrected values + residuals to a second Excel sheet

This script is designed to be configured in-code (no CLI required).
"""

from __future__ import annotations

import math
import re
import textwrap
from pathlib import Path

import pandas as pd


# =========================
# USER CONFIG (in-code)
# =========================

INPUT_XLSX = r"C:/UserData/Infineon/TE_CTRX/CTRX8188/Data_Reviews/CV_TE_Correlation/ATE_Extracted_PA_Power_Data.xlsx"  # e.g. r"C:\path\to\raw_cv_ate.xlsx"
OUTPUT_XLSX = r"C:/UserData/Infineon/TE_CTRX/CTRX8188/Data_Reviews/CV_TE_Correlation/Correlation/Correlation_PA_Power.xlsx"  # e.g. r"C:\path\to\correlation_results.xlsx" OR a folder path
OUTPUT_PLOTS_DIR = r"C:/UserData/Infineon/TE_CTRX/CTRX8188/Data_Reviews/CV_TE_Correlation/Correlation/plots_PA_Power"  # optional; if empty uses OUTPUT_XLSX folder + "plots"

# If you want to process multiple same-layout sheets (CV+ATE columns in each sheet), list them here.
# When non-empty, this overrides CV_SHEET/ATE_SHEET and runs each sheet independently.
SHEETS_TO_RUN = ["FE_Filtered"]
CV_SHEET = r""  # used when SHEETS_TO_RUN is empty
ATE_SHEET = r""  # used when SHEETS_TO_RUN is empty

# Columns that identify the same device/test row in both sheets.
# These should exist in both CV and ATE sheets.
MERGE_KEYS = [
    "DUT Nr",
    "Wafer",
    "X",
    "Y",
    "Temperature",
    "Voltage corner",
    "Frequency_GHz",
    "Test Number",
]

# Required columns containing numeric results
# For the provided PN correlation workbook these are in the same sheet.
CV_VALUE_COL = "CV_PA_Power"    # in-sheet CV values
ATE_VALUE_COL = "ATE_PA_Power"  # in-sheet ATE values

# TXLO-specific identifier (optional but recommended to keep test-cases separated)
LO_IDAC_COL = "LO IDAC"

# Optional columns (if present) for limits (ATE limits)
ATE_LOW_COL = "Low"
ATE_HIGH_COL = "High"
ATE_UNIT_COL = "Unit"

# Test-case definition
# - TXLO power: grouped by Test Number → Voltage corner → Frequency → Temperature
# - TXPA power: grouped by LUT value → Voltage corner → Frequency → Temperature
# Set to "auto" to pick TXPA grouping when a usable LUT value is present.
TEST_CASE_TYPE = "auto"  # "TXLO" | "TXPA" | "auto"

TXLO_GROUP_COLS = ["Test Number", "Voltage corner", "Frequency_GHz", "Temperature"]
TXPA_GROUP_COLS = ["LUT value", "Voltage corner", "Frequency_GHz", "Temperature"]

# Correlation / derived-limit settings
MIN_POINTS_PER_GROUP = 5

# New limits default behavior:
#   correlated limits = mean(ATE_correlated) ± 6σ(ATE_correlated)
CORRELATED_SIGMA_MULT = 6.0

# Plot settings
PLOT_DPI = 160

# LO and PA power REQUIREMENTS
LO_POWER_IDAC_112_REQ_MIN = 9  # dBm
LO_POWER_IDAC_112_REQ_MAX = 16   # dBm
PA_POWER_LUT_255_REQ_MIN = 9  # dBm
PA_POWER_LUT_255_REQ_MAX = 16   # dBm

# =========================
# Helpers
# =========================

def _as_path_maybe_folder(path_str: str, default_filename: str) -> Path:
    p = Path(path_str)
    if p.suffix.lower() != ".xlsx":
        return p / default_filename
    return p


def _to_float_series(s: pd.Series) -> pd.Series:
    # Handles European decimals and stray whitespace.
    # Keeps NaNs as NaN.
    s2 = s.astype(str).str.strip()
    s2 = s2.replace({"": pd.NA, "nan": pd.NA, "NaN": pd.NA, "None": pd.NA})
    s2 = s2.str.replace(" ", "", regex=False)
    s2 = s2.str.replace(",", ".", regex=False)
    return pd.to_numeric(s2, errors="coerce")


def _safe_slug(text: str) -> str:
    t = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(text))
    return t.strip("_")[:180] or "plot"


def _find_test_name_column(columns: list[str]) -> str | None:
    # Best-effort detection of the human-readable test name column.
    if not columns:
        return None

    def _norm(s: str) -> str:
        return re.sub(r"[\s_]+", "", str(s)).lower().strip()

    norm_map = {_norm(c): c for c in columns}
    for key in ("testname", "testcasename"):
        if key in norm_map:
            return norm_map[key]
    for c in columns:
        if "testname" in _norm(c):
            return c
    return None


def _find_doe_split_column(columns: list[str]) -> str | None:
    if not columns:
        return None

    def _norm(s: str) -> str:
        return re.sub(r"[\s_\-]+", "", str(s)).lower().strip()

    norm_map = {_norm(c): c for c in columns}
    for key in ("doesplit", "doe", "doe_split"):
        if key in norm_map:
            return norm_map[key]
    for c in columns:
        if "doe" in _norm(c) and "split" in _norm(c):
            return c
    return None


_FWLU_RE = re.compile(r"FwLu(?P<lut>\d{2,3})(?!\d)", flags=re.IGNORECASE)


def _extract_lut_value(test_name: str):
    m = _FWLU_RE.search(str(test_name) if test_name is not None else "")
    if not m:
        return pd.NA
    try:
        return int(m.group("lut"))
    except Exception:
        return pd.NA


def _ensure_lut_value_column(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    if "LUT value" in df.columns:
        # Normalize type (keeps missing values)
        df["LUT value"] = pd.to_numeric(df["LUT value"], errors="coerce").astype("Int64")
        return df
    if "Test Name" in df.columns:
        df["LUT value"] = df["Test Name"].astype(str).map(_extract_lut_value).astype("Int64")
        return df
    df["LUT value"] = pd.Series([pd.NA] * len(df), dtype="Int64")
    return df


def _resolve_group_cols(df: pd.DataFrame) -> list[str]:
    mode = str(TEST_CASE_TYPE).strip().upper()
    if mode == "TXLO":
        return TXLO_GROUP_COLS
    if mode == "TXPA":
        return TXPA_GROUP_COLS

    # auto
    if df is not None and not df.empty:
        if "LUT value" in df.columns:
            cand = pd.to_numeric(df["LUT value"], errors="coerce")
            if cand.notna().any():
                return TXPA_GROUP_COLS
        if "Test Name" in df.columns:
            cand = df["Test Name"].astype(str).map(_extract_lut_value)
            if pd.Series(cand).notna().any():
                return TXPA_GROUP_COLS
    return TXLO_GROUP_COLS


def _insert_column_after(df: pd.DataFrame, after_col: str, col: str) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    if col not in df.columns:
        return df
    cols = list(df.columns)
    cols.remove(col)
    if after_col in cols:
        idx = cols.index(after_col) + 1
        cols.insert(idx, col)
        return df[cols]
    # If the target column isn't present, keep as-is.
    return df


def _r2_score(y_true: pd.Series, y_pred: pd.Series) -> float:
    yt = y_true.to_numpy(dtype=float)
    yp = y_pred.to_numpy(dtype=float)
    if len(yt) < 2:
        return math.nan
    y_mean = float(yt.mean())
    ss_tot = float(((yt - y_mean) ** 2).sum())
    if ss_tot == 0.0:
        return math.nan
    ss_res = float(((yt - yp) ** 2).sum())
    return 1.0 - (ss_res / ss_tot)


def _maybe_extend_txlo_group_cols_with_idac(df: pd.DataFrame, group_cols: list[str]) -> list[str]:
    if df is None or df.empty:
        return group_cols
    if list(group_cols) != list(TXLO_GROUP_COLS):
        return group_cols
    if LO_IDAC_COL not in df.columns:
        return group_cols
    return [*TXLO_GROUP_COLS, LO_IDAC_COL]


def _compute_new_limits(
    *,
    test_case_mode: str,
    group_cols: list[str],
    group_dict: dict,
    g: pd.DataFrame,
    median_delta: float,
) -> dict:
    """Compute new limits based on correlated distribution.

        Returns a dict containing:
            - Corr_Low, Corr_High (limits in correlated/CV domain)
            - Limit_Method (string)
            - CorrMean, CorrStd, MaxAbsResidual
    """
    corr = pd.to_numeric(g["ATE_correlated"], errors="coerce")
    corr_mean = float(corr.mean())
    corr_std = float(corr.std(ddof=1)) if len(corr) > 1 else math.nan

    residual = pd.to_numeric(g["Residual"], errors="coerce")
    max_abs_residual = float(residual.abs().max()) if len(residual) else math.nan

    corr_low = math.nan
    corr_high = math.nan
    method = "mean±6σ(correlated)"

    mode = str(test_case_mode).strip().upper()

    # Special case 1: MAX LO power (IDAC 112): requirements ± max|residual|
    if mode == "TXLO":
        idac_val = None
        if LO_IDAC_COL in group_cols and LO_IDAC_COL in group_dict:
            idac_val = group_dict.get(LO_IDAC_COL)
        elif LO_IDAC_COL in g.columns:
            idac_series = pd.to_numeric(g[LO_IDAC_COL], errors="coerce").dropna()
            if len(idac_series.unique()) == 1:
                idac_val = int(idac_series.iloc[0])

        if idac_val == 112 and not math.isnan(max_abs_residual):
            corr_low = float(LO_POWER_IDAC_112_REQ_MIN) + max_abs_residual
            corr_high = float(LO_POWER_IDAC_112_REQ_MAX) - max_abs_residual
            method = "requirements±max|residual| (LO IDAC 112)"

    # Special case 2: MAX PA power (LUT 255): requirements ± max|residual|
    if mode == "TXPA":
        lut_val = None
        if "LUT value" in group_cols and "LUT value" in group_dict:
            lut_val = group_dict.get("LUT value")
        elif "LUT value" in g.columns:
            lut_series = pd.to_numeric(g["LUT value"], errors="coerce").dropna()
            if len(lut_series.unique()) == 1:
                lut_val = int(lut_series.iloc[0])

        if lut_val == 255 and not math.isnan(max_abs_residual):
            corr_low = float(PA_POWER_LUT_255_REQ_MIN) + max_abs_residual
            corr_high = float(PA_POWER_LUT_255_REQ_MAX) - max_abs_residual
            method = "requirements±max|residual| (PA LUT 255)"

    # Default: mean ± 6σ on correlated distribution
    if math.isnan(corr_low) or math.isnan(corr_high):
        if not math.isnan(corr_mean) and not math.isnan(corr_std):
            corr_low = corr_mean - (CORRELATED_SIGMA_MULT * corr_std)
            corr_high = corr_mean + (CORRELATED_SIGMA_MULT * corr_std)

    return {
        "CorrMean": corr_mean,
        "CorrStd": corr_std,
        "MaxAbsResidual": max_abs_residual,
        "Corr_Low": corr_low,
        "Corr_High": corr_high,
        "Limit_Method": method,
    }


# =========================
# Main
# =========================

if __name__ == "__main__":
    if not str(INPUT_XLSX).strip():
        raise SystemExit("INPUT_XLSX is empty. Set it in the USER CONFIG block.")
    if not str(OUTPUT_XLSX).strip():
        raise SystemExit("OUTPUT_XLSX is empty. Set it in the USER CONFIG block.")
    if SHEETS_TO_RUN:
        sheets_to_run = [str(s).strip() for s in SHEETS_TO_RUN if str(s).strip()]
        if not sheets_to_run:
            raise SystemExit("SHEETS_TO_RUN is set but empty after stripping. Fix USER CONFIG.")
    else:
        if not str(CV_SHEET).strip() or not str(ATE_SHEET).strip():
            raise SystemExit("CV_SHEET / ATE_SHEET are empty. Set them in the USER CONFIG block.")
        sheets_to_run = [str(CV_SHEET).strip()]
        if str(CV_SHEET).strip() != str(ATE_SHEET).strip():
            sheets_to_run = []

    input_xlsx = Path(INPUT_XLSX)
    if not input_xlsx.is_file():
        raise SystemExit(f"Input file not found: {input_xlsx}")

    output_xlsx = _as_path_maybe_folder(OUTPUT_XLSX, "CV_ATE_Correlation.xlsx")
    plots_dir = Path(OUTPUT_PLOTS_DIR) if str(OUTPUT_PLOTS_DIR).strip() else output_xlsx.parent / "plots"
    plots_dir.mkdir(parents=True, exist_ok=True)

    # Prepare combined outputs
    all_factors_rows = []
    all_data_rows = []

    # Plot dependency
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import transforms as mtransforms
    import matplotlib.patheffects as patheffects

    def run_one_same_sheet(sheet_name: str):
        df = pd.read_excel(input_xlsx, sheet_name=sheet_name)
        test_name_col = _find_test_name_column(list(df.columns))
        doe_split_col = _find_doe_split_column(list(df.columns))
        missing = [c for c in (MERGE_KEYS + [CV_VALUE_COL, ATE_VALUE_COL]) if c not in df.columns]
        if missing:
            raise SystemExit(f"Sheet '{sheet_name}' missing columns: {missing}")

        keep = list(dict.fromkeys(MERGE_KEYS + [CV_VALUE_COL, ATE_VALUE_COL, ATE_LOW_COL, ATE_HIGH_COL, ATE_UNIT_COL]))
        if "LUT value" in df.columns:
            keep.append("LUT value")
        if test_name_col:
            keep.append(test_name_col)
        if doe_split_col:
            keep.append(doe_split_col)
        if LO_IDAC_COL in df.columns:
            keep.append(LO_IDAC_COL)
        keep = [c for c in keep if c in df.columns]
        merged_local = df[keep].copy()

        merged_local[CV_VALUE_COL] = _to_float_series(merged_local[CV_VALUE_COL])
        merged_local[ATE_VALUE_COL] = _to_float_series(merged_local[ATE_VALUE_COL])

        for k in MERGE_KEYS:
            if k in ("X", "Y", "Test Number"):
                merged_local[k] = pd.to_numeric(merged_local[k], errors="coerce")
            else:
                merged_local[k] = merged_local[k].astype(str).str.strip()

        merged_local = merged_local.dropna(subset=[CV_VALUE_COL, ATE_VALUE_COL])
        rename_map = {CV_VALUE_COL: "CV", ATE_VALUE_COL: "ATE"}
        if test_name_col and test_name_col in merged_local.columns:
            rename_map[test_name_col] = "Test Name"
        if doe_split_col and doe_split_col in merged_local.columns and doe_split_col != "DoE split":
            rename_map[doe_split_col] = "DoE split"
        if LO_IDAC_COL in merged_local.columns:
            rename_map[LO_IDAC_COL] = LO_IDAC_COL
        merged_local = merged_local.rename(columns=rename_map)
        merged_local = _ensure_lut_value_column(merged_local)

        if LO_IDAC_COL in merged_local.columns:
            merged_local[LO_IDAC_COL] = pd.to_numeric(merged_local[LO_IDAC_COL], errors="coerce").astype("Int64")
        return merged_local

    def run_one_two_sheets():
        cv_df = pd.read_excel(input_xlsx, sheet_name=CV_SHEET)
        ate_df = pd.read_excel(input_xlsx, sheet_name=ATE_SHEET)

        test_name_cv = _find_test_name_column(list(cv_df.columns))
        test_name_ate = _find_test_name_column(list(ate_df.columns))
        doe_split_cv = _find_doe_split_column(list(cv_df.columns))
        doe_split_ate = _find_doe_split_column(list(ate_df.columns))

        missing_cv = [c for c in (MERGE_KEYS + [CV_VALUE_COL]) if c not in cv_df.columns]
        missing_ate = [c for c in (MERGE_KEYS + [ATE_VALUE_COL]) if c not in ate_df.columns]
        if missing_cv:
            raise SystemExit(f"CV sheet missing columns: {missing_cv}")
        if missing_ate:
            raise SystemExit(f"ATE sheet missing columns: {missing_ate}")

        cv_keep = list(dict.fromkeys(MERGE_KEYS + [CV_VALUE_COL, ATE_LOW_COL, ATE_HIGH_COL, ATE_UNIT_COL]))
        if "LUT value" in cv_df.columns:
            cv_keep.append("LUT value")
        if test_name_cv:
            cv_keep.append(test_name_cv)
        if doe_split_cv:
            cv_keep.append(doe_split_cv)
        cv_keep = [c for c in cv_keep if c in cv_df.columns]
        ate_keep = list(dict.fromkeys(MERGE_KEYS + [ATE_VALUE_COL]))
        if "LUT value" in ate_df.columns:
            ate_keep.append("LUT value")
        if test_name_ate:
            ate_keep.append(test_name_ate)
        if doe_split_ate:
            ate_keep.append(doe_split_ate)

        # TXLO IDAC column (keep if present)
        if LO_IDAC_COL in cv_df.columns:
            cv_keep.append(LO_IDAC_COL)
        if LO_IDAC_COL in ate_df.columns:
            ate_keep.append(LO_IDAC_COL)

        cv_df = cv_df[cv_keep].copy()
        ate_df = ate_df[ate_keep].copy()

        cv_df[CV_VALUE_COL] = _to_float_series(cv_df[CV_VALUE_COL])
        ate_df[ATE_VALUE_COL] = _to_float_series(ate_df[ATE_VALUE_COL])

        for k in MERGE_KEYS:
            if k in ("X", "Y", "Test Number"):
                cv_df[k] = pd.to_numeric(cv_df[k], errors="coerce")
                ate_df[k] = pd.to_numeric(ate_df[k], errors="coerce")
            else:
                cv_df[k] = cv_df[k].astype(str).str.strip()
                ate_df[k] = ate_df[k].astype(str).str.strip()

        cv_df = cv_df.dropna(subset=[CV_VALUE_COL])
        ate_df = ate_df.dropna(subset=[ATE_VALUE_COL])

        merged_local = pd.merge(
            cv_df,
            ate_df,
            how="inner",
            on=MERGE_KEYS,
            suffixes=("_CV", "_ATE"),
            validate="many_to_many",
        )

        merged_local = merged_local.rename(
            columns={
                f"{CV_VALUE_COL}_CV": "CV",
                f"{ATE_VALUE_COL}_ATE": "ATE",
            }
        )

        # Normalize/choose a single Test Name column if present
        if test_name_cv and f"{test_name_cv}_CV" in merged_local.columns:
            merged_local = merged_local.rename(columns={f"{test_name_cv}_CV": "Test Name"})
        elif test_name_cv and test_name_cv in merged_local.columns:
            merged_local = merged_local.rename(columns={test_name_cv: "Test Name"})
        elif test_name_ate and f"{test_name_ate}_ATE" in merged_local.columns:
            merged_local = merged_local.rename(columns={f"{test_name_ate}_ATE": "Test Name"})
        elif test_name_ate and test_name_ate in merged_local.columns:
            merged_local = merged_local.rename(columns={test_name_ate: "Test Name"})

        # Normalize/choose a single DoE split column if present
        if "DoE split" not in merged_local.columns:
            if doe_split_cv and f"{doe_split_cv}_CV" in merged_local.columns:
                merged_local = merged_local.rename(columns={f"{doe_split_cv}_CV": "DoE split"})
            elif doe_split_cv and doe_split_cv in merged_local.columns:
                merged_local = merged_local.rename(columns={doe_split_cv: "DoE split"})
            elif doe_split_ate and f"{doe_split_ate}_ATE" in merged_local.columns:
                merged_local = merged_local.rename(columns={f"{doe_split_ate}_ATE": "DoE split"})
            elif doe_split_ate and doe_split_ate in merged_local.columns:
                merged_local = merged_local.rename(columns={doe_split_ate: "DoE split"})

        # Normalize/choose a single LUT value column if present
        if "LUT value" not in merged_local.columns:
            if "LUT value_CV" in merged_local.columns:
                merged_local = merged_local.rename(columns={"LUT value_CV": "LUT value"})
            elif "LUT value_ATE" in merged_local.columns:
                merged_local = merged_local.rename(columns={"LUT value_ATE": "LUT value"})

        merged_local = _ensure_lut_value_column(merged_local)

        # Normalize/choose a single LO IDAC column if present
        if LO_IDAC_COL not in merged_local.columns:
            if f"{LO_IDAC_COL}_CV" in merged_local.columns:
                merged_local = merged_local.rename(columns={f"{LO_IDAC_COL}_CV": LO_IDAC_COL})
            elif f"{LO_IDAC_COL}_ATE" in merged_local.columns:
                merged_local = merged_local.rename(columns={f"{LO_IDAC_COL}_ATE": LO_IDAC_COL})

        if LO_IDAC_COL in merged_local.columns:
            merged_local[LO_IDAC_COL] = pd.to_numeric(merged_local[LO_IDAC_COL], errors="coerce").astype("Int64")

        if "CV" not in merged_local.columns or "ATE" not in merged_local.columns:
            raise SystemExit(
                "After merge, CV/ATE columns not found. "
                "If CV_VALUE_COL and ATE_VALUE_COL are the same name, ensure suffix mapping is correct."
            )
        return merged_local

    def correlate_merged(merged_local: pd.DataFrame, sheet_label: str):
        if merged_local.empty:
            return

        group_cols = _resolve_group_cols(merged_local)
        if "LUT value" in group_cols:
            merged_local = _ensure_lut_value_column(merged_local)

        group_cols = _maybe_extend_txlo_group_cols_with_idac(merged_local, group_cols)

        missing_group_cols = [c for c in group_cols if c not in merged_local.columns]
        if missing_group_cols:
            raise SystemExit(f"Missing GROUP_COLS in merged data: {missing_group_cols}")

        local_plots_dir = plots_dir / _safe_slug(sheet_label)
        local_plots_dir.mkdir(parents=True, exist_ok=True)

        grouped = merged_local.groupby(group_cols, dropna=False)

        for group_key, g in grouped:
            g = g.dropna(subset=["ATE", "CV"]).copy()
            n_points = len(g)
            if n_points < MIN_POINTS_PER_GROUP:
                continue

            test_name = ""
            if "Test Name" in g.columns:
                vals = g["Test Name"].astype(str).replace({"nan": ""}).str.strip()
                vals = [v for v in vals.tolist() if v]
                uniq = list(dict.fromkeys(vals))
                if len(uniq) == 1:
                    test_name = uniq[0]
                elif len(uniq) > 1:
                    shown = uniq[:2]
                    test_name = "; ".join(shown) + (f" (+{len(uniq)-2} more)" if len(uniq) > 2 else "")

            # Offset-only correlation factors
            # delta = CV - ATE
            g["Delta(CV-ATE)"] = g["CV"] - g["ATE"]
            median_delta = float(g["Delta(CV-ATE)"].median())
            max_delta = float(g["Delta(CV-ATE)"].max())

            # Apply correction to ATE so it aligns to CV
            g["ATE_correlated"] = g["ATE"] + median_delta

            # Residuals in delta-domain
            g["Residual"] = g["Delta(CV-ATE)"] - median_delta
            residual_std = float(g["Residual"].std(ddof=1)) if len(g) > 1 else math.nan

            # R² for the offset-only model in ATE-domain: ATE_pred = CV - median_delta
            r2 = _r2_score(g["ATE"], g["CV"] - median_delta)

            # Limits: only ATE limits exist
            ate_low = None
            ate_high = None
            unit = ""
            if ATE_LOW_COL in g.columns:
                low_series = _to_float_series(g[ATE_LOW_COL])
                low_vals = low_series.dropna().tolist()
                ate_low = float(low_vals[0]) if low_vals else None
            if ATE_HIGH_COL in g.columns:
                high_series = _to_float_series(g[ATE_HIGH_COL])
                high_vals = high_series.dropna().tolist()
                ate_high = float(high_vals[0]) if high_vals else None
            if ATE_UNIT_COL in g.columns:
                unit_vals = g[ATE_UNIT_COL].astype(str).replace({"nan": ""}).str.strip()
                unit_vals = [v for v in unit_vals.tolist() if v]
                unit = unit_vals[0] if unit_vals else ""

            # New limits (requested):
            # - default: mean(ATE_correlated) ± 6σ(ATE_correlated)
            # - special cases:
            #     TXLO max LO power (IDAC 112): requirements ± max|residual|
            #     TXPA max PA power (LUT 255): requirements ± max|residual|
            inferred_mode = "TXPA" if "LUT value" in group_cols else "TXLO"
            group_dict = dict(zip(group_cols, group_key if isinstance(group_key, tuple) else (group_key,)))
            limits = _compute_new_limits(
                test_case_mode=inferred_mode,
                group_cols=group_cols,
                group_dict=group_dict,
                g=g,
                median_delta=median_delta,
            )
            corr_low = limits["Corr_Low"]
            corr_high = limits["Corr_High"]

            # Special-cases: additional requirement-based limits using 3*sigma(residuals)
            ltl_new_3s = math.nan
            utl_new_3s = math.nan
            if not math.isnan(residual_std):
                if inferred_mode == "TXLO":
                    idac_val = group_dict.get(LO_IDAC_COL) if LO_IDAC_COL in group_dict else None
                    if idac_val == 112:
                        ltl_new_3s = float(LO_POWER_IDAC_112_REQ_MIN) + (3.0 * residual_std)
                        utl_new_3s = float(LO_POWER_IDAC_112_REQ_MAX) - (3.0 * residual_std)
                else:
                    lut_val = group_dict.get("LUT value") if "LUT value" in group_dict else None
                    if lut_val == 255:
                        ltl_new_3s = float(PA_POWER_LUT_255_REQ_MIN) + (3.0 * residual_std)
                        utl_new_3s = float(PA_POWER_LUT_255_REQ_MAX) - (3.0 * residual_std)

            corr_window_invalid = (
                (not math.isnan(corr_low))
                and (not math.isnan(corr_high))
                and (corr_low > corr_high)
            )
            corr_window_width = (corr_high - corr_low) if (not math.isnan(corr_low) and not math.isnan(corr_high)) else math.nan
            if corr_window_invalid:
                print(
                    "WARNING: Correlated limit window is inverted "
                    f"(Corr_Low={corr_low:.6g} > Corr_High={corr_high:.6g}) "
                    f"for {sheet_label} | {title if 'title' in locals() else group_dict}"
                )

            # Save factors row
            all_factors_rows.append(
                {
                    "DataSheet": sheet_label,
                    **group_dict,
                    "Test Name": test_name,
                    "N": n_points,
                    "MedianDelta(CV-ATE)": median_delta,
                    "MaxDelta(CV-ATE)": max_delta,
                    "R2_OffsetModel": r2,
                    "ResidualStd(Delta)": residual_std,
                    "MaxAbsResidual(Delta)": limits["MaxAbsResidual"],
                    "CorrMean": limits["CorrMean"],
                    "CorrStd": limits["CorrStd"],
                    "Corr_Low": corr_low,
                    "Corr_High": corr_high,
                    "LTL_New_3s": ltl_new_3s,
                    "UTL_New_3s": utl_new_3s,
                    "Corr_Window_Width": corr_window_width,
                    "Corr_Window_Invalid": corr_window_invalid,
                    "Limit_Method": limits["Limit_Method"],
                    "ATE_Low": ate_low,
                    "ATE_High": ate_high,
                    "Unit": unit,
                }
            )

            # Save row-level correlated data
            for _, r in g.iterrows():
                all_data_rows.append(
                    {
                        "DataSheet": sheet_label,
                        **{k: r[k] for k in MERGE_KEYS},
                        # Extra context (helps sign-off and debugging). Present for TXPA.
                        "LUT value": (int(r["LUT value"]) if "LUT value" in g.columns and pd.notna(r.get("LUT value")) else pd.NA),
                        "DoE split": (str(r.get("DoE split", "")).strip() if "DoE split" in g.columns else ""),
                        LO_IDAC_COL: (int(pd.to_numeric(r.get(LO_IDAC_COL), errors="coerce")) if LO_IDAC_COL in g.columns and pd.notna(r.get(LO_IDAC_COL)) else pd.NA),
                        "Test Name": (str(r.get("Test Name", "")).strip() if "Test Name" in g.columns else ""),
                        "CV": float(r["CV"]),
                        "ATE": float(r["ATE"]),
                        "ATE_correlated": float(r["ATE_correlated"]),
                        "Delta(CV-ATE)": float(r["Delta(CV-ATE)"]),
                        "Residual": float(r["Residual"]),
                    }
                )

            # Plot (3 subplots):
            # 1) per-sample series (CV vs index, ATE vs index) with DoE split structure
            # 2) regression view (ATE vs CV) with offset-only line
            # 3) correlated-domain series (CV vs index, ATE_correlated vs index) + correlated limits
            if "DoE split" in g.columns:
                g_sort = g.copy()
                g_sort["__doe_sort"] = (
                    g_sort["DoE split"]
                    .astype(str)
                    .replace({"nan": "", "None": ""})
                    .str.strip()
                    .where(lambda s: s != "", other="(blank)")
                    .str.upper()
                )
                sort_cols = ["__doe_sort"]
                if "DUT Nr" in g_sort.columns:
                    sort_cols.append("DUT Nr")
                g_plot = g_sort.sort_values(by=sort_cols).drop(columns=["__doe_sort"]).reset_index(drop=True)
                x_label = "Samples (sorted by DoE split)"
            elif "DUT Nr" in g.columns:
                g_plot = g.sort_values(by=["DUT Nr"]).reset_index(drop=True)
                x_label = "Samples (sorted by DUT Nr)"
            else:
                g_plot = g.reset_index(drop=True)
                x_label = "Samples"

            x_idx = pd.Series(range(len(g_plot)))

            doe = None
            boundaries: list[int] = []
            starts: list[int] = []
            ends: list[int] = []
            if "DoE split" in g_plot.columns:
                doe = (
                    g_plot["DoE split"]
                    .astype(str)
                    .replace({"nan": "", "None": ""})
                    .str.strip()
                )
                doe = doe.where(doe != "", other="(blank)").str.upper()
                boundaries = [i for i in range(1, len(doe)) if doe.iloc[i] != doe.iloc[i - 1]]
                starts = [0] + boundaries
                ends = boundaries + [len(doe)]

            fig, (ax1, ax2, ax3) = plt.subplots(
                nrows=3,
                ncols=1,
                figsize=(12.0, 12.0),
                gridspec_kw={"height_ratios": [2.0, 2.0, 2.0]},
            )

            ax1.plot(
                x_idx,
                g_plot["CV"],
                marker="o",
                linestyle="-",
                linewidth=2.2,
                markersize=6,
                label="CV (individual)",
            )
            ax1.plot(
                x_idx,
                g_plot["ATE"],
                marker="s",
                linestyle="--",
                linewidth=2.2,
                markersize=6,
                label="ATE (individual)",
            )

            # Visual guide: mark DoE split boundaries + label each DoE segment
            if doe is not None:
                for cut in boundaries:
                    ax1.axvline(cut - 0.5, color="gray", linestyle="--", linewidth=1.2, alpha=0.45, zorder=0)

                # Centered labels (x in data coords, y in axes coords)
                blend1 = mtransforms.blended_transform_factory(ax1.transData, ax1.transAxes)
                for s, e in zip(starts, ends):
                    if e <= s:
                        continue
                    label = str(doe.iloc[s])
                    x_center = (s + (e - 1)) / 2.0
                    ax1.text(
                        x_center,
                        0.96,
                        label,
                        transform=blend1,
                        ha="center",
                        va="top",
                        fontsize=11,
                        color="black",
                        zorder=5,
                        path_effects=[patheffects.withStroke(linewidth=3.0, foreground="white", alpha=0.9)],
                    )

            # ATE limits removed (only correlated limits are relevant)

            title_parts = [f"{k}={v}" for k, v in group_dict.items()]
            if test_name:
                title_parts.insert(1 if title_parts else 0, f"Test Name={test_name}")
            title = " | ".join(title_parts)
            title_wrapped = "\n".join(textwrap.wrap(f"{sheet_label} | {title}", width=110))
            fig.suptitle(title_wrapped, fontsize=13, y=0.98)

            ax1.set_xlabel(x_label, fontsize=12)
            ax1.set_ylabel(f"Value{(' ['+unit+']') if unit else ''}", fontsize=12)
            ax1.tick_params(axis="both", labelsize=11)
            ax1.grid(True, alpha=0.25)

            # Tight x/y scaling to data only
            ax1.set_xlim(-0.5, len(g_plot) - 0.5)
            y_candidates = [
                float(g_plot["CV"].min()),
                float(g_plot["CV"].max()),
                float(g_plot["ATE"].min()),
                float(g_plot["ATE"].max()),
            ]

            y_min = min(y_candidates)
            y_max = max(y_candidates)
            y_span = y_max - y_min
            pad = (0.06 * y_span) if y_span > 0 else (abs(y_min) * 0.06 + 1.0)
            ax1.set_ylim(y_min - pad, y_max + pad)

            # Keep the top subplot free of regression text (as requested).
            note_top = f"N={n_points}  median(CV-ATE)={median_delta:.4g}  max(CV-ATE)={max_delta:.4g}"
            if not math.isnan(residual_std):
                note_top += f"  σ(residual)={residual_std:.4g}"
            ax1.text(
                0.015,
                0.02,
                note_top,
                transform=ax1.transAxes,
                fontsize=11,
                va="bottom",
                ha="left",
                bbox={"facecolor": "white", "alpha": 0.85, "edgecolor": "none", "pad": 3.0},
            )

            ax1.legend(fontsize=11, framealpha=0.92)

            # Regression subplot: CV vs ATE scattering + offset-only line (slope=1)
            ax2.scatter(
                g_plot["CV"],
                g_plot["ATE"],
                s=40,
                alpha=0.95,
                marker="o",
                linewidths=1.1,
                facecolors="none",
                edgecolors="tab:blue",
                zorder=3,
                label="ATE raw (scatter)",
            )
            x_min = float(g_plot["CV"].min())
            x_max = float(g_plot["CV"].max())
            x_span = x_max - x_min
            x_pad = (0.06 * x_span) if x_span > 0 else (abs(x_min) * 0.06 + 1.0)
            x_line = pd.Series([x_min, x_max])
            y_line = x_line - median_delta
            ax2.plot(
                x_line,
                y_line,
                linewidth=2.6,
                linestyle="--",
                color="tab:red",
                zorder=1,
                label=f"Offset model: ATE = CV - medianΔ",
            )

            ax2.set_xlabel(f"CV{(' ['+unit+']') if unit else ''}", fontsize=12)
            ax2.set_ylabel(f"ATE{(' ['+unit+']') if unit else ''}", fontsize=12)
            ax2.tick_params(axis="both", labelsize=11)
            ax2.grid(True, alpha=0.25)
            ax2.set_xlim(x_min - x_pad, x_max + x_pad)

            y2_candidates = [
                float(g_plot["ATE"].min()),
                float(g_plot["ATE"].max()),
                float(y_line.min()),
                float(y_line.max()),
            ]
            y2_min = min(y2_candidates)
            y2_max = max(y2_candidates)
            y2_span = y2_max - y2_min
            y2_pad = (0.06 * y2_span) if y2_span > 0 else (abs(y2_min) * 0.06 + 1.0)
            ax2.set_ylim(y2_min - y2_pad, y2_max + y2_pad)

            note_reg = f"N={n_points}  R²={r2:.3f}  medianΔ={median_delta:.4g}  maxΔ={max_delta:.4g}"
            ax2.text(
                0.015,
                0.02,
                note_reg,
                transform=ax2.transAxes,
                fontsize=11,
                va="bottom",
                ha="left",
                bbox={"facecolor": "white", "alpha": 0.85, "edgecolor": "none", "pad": 3.0},
            )
            ax2.legend(fontsize=10, framealpha=0.92)

            # Correlated-domain subplot (standard series like subplot 1)
            ax3.plot(
                x_idx,
                g_plot["CV"],
                marker="o",
                linestyle="-",
                linewidth=2.2,
                markersize=6,
                label="CV (individual)",
            )
            ax3.plot(
                x_idx,
                g_plot["ATE_correlated"],
                marker="^",
                linestyle="--",
                linewidth=2.2,
                markersize=6,
                label="ATE_correlated (individual)",
            )

            # DoE split boundaries + labels (same as subplot 1)
            if doe is not None:
                for cut in boundaries:
                    ax3.axvline(cut - 0.5, color="gray", linestyle="--", linewidth=1.2, alpha=0.45, zorder=0)

                blend3 = mtransforms.blended_transform_factory(ax3.transData, ax3.transAxes)
                for s, e in zip(starts, ends):
                    if e <= s:
                        continue
                    label = str(doe.iloc[s])
                    x_center = (s + (e - 1)) / 2.0
                    ax3.text(
                        x_center,
                        0.96,
                        label,
                        transform=blend3,
                        ha="center",
                        va="top",
                        fontsize=11,
                        color="black",
                        zorder=5,
                        path_effects=[patheffects.withStroke(linewidth=3.0, foreground="white", alpha=0.9)],
                    )

            if not math.isnan(corr_low):
                ax3.axhline(
                    corr_low,
                    color="cyan",
                    linestyle="-.",
                    linewidth=2.2,
                    label=f"Corr Low = {corr_low:.4g} dBm",
                )
            if not math.isnan(corr_high):
                ax3.axhline(
                    corr_high,
                    color="cyan",
                    linestyle="-.",
                    linewidth=2.2,
                    label=f"Corr High = {corr_high:.4g} dBm",
                )

            # Requirements lines (only for the relevant MAX power special-cases)
            req_min = math.nan
            req_max = math.nan
            if inferred_mode == "TXLO":
                idac_val = group_dict.get(LO_IDAC_COL) if LO_IDAC_COL in group_dict else None
                if idac_val == 112:
                    req_min = float(LO_POWER_IDAC_112_REQ_MIN)
                    req_max = float(LO_POWER_IDAC_112_REQ_MAX)
            else:
                lut_val = group_dict.get("LUT value") if "LUT value" in group_dict else None
                if lut_val == 255:
                    req_min = float(PA_POWER_LUT_255_REQ_MIN)
                    req_max = float(PA_POWER_LUT_255_REQ_MAX)

            if not math.isnan(req_min):
                ax3.axhline(req_min, color="red", linestyle="-", linewidth=2.0, label=f"REQ Min = {req_min:.4g} dBm")
            if not math.isnan(req_max):
                ax3.axhline(req_max, color="red", linestyle="-", linewidth=2.0, label=f"REQ Max = {req_max:.4g} dBm")

            # Additional special-case limits: REQ ± 3*sigma(residuals)
            ltl_new_3s_plot = math.nan
            utl_new_3s_plot = math.nan
            if (not math.isnan(req_min)) and (not math.isnan(req_max)) and (not math.isnan(residual_std)):
                ltl_new_3s_plot = float(req_min) + (3.0 * residual_std)
                utl_new_3s_plot = float(req_max) - (3.0 * residual_std)
                ax3.axhline(
                    ltl_new_3s_plot,
                    color="orange",
                    linestyle="--",
                    linewidth=2.0,
                    label=f"LTL_New_3s = {ltl_new_3s_plot:.4g} dBm",
                )
                ax3.axhline(
                    utl_new_3s_plot,
                    color="orange",
                    linestyle="--",
                    linewidth=2.0,
                    label=f"UTL_New_3s = {utl_new_3s_plot:.4g} dBm",
                )

            # Tight y scaling for correlated-domain subplot (data + limits + req)
            y3_candidates = [
                float(pd.to_numeric(g_plot["CV"], errors="coerce").min()),
                float(pd.to_numeric(g_plot["CV"], errors="coerce").max()),
                float(pd.to_numeric(g_plot["ATE_correlated"], errors="coerce").min()),
                float(pd.to_numeric(g_plot["ATE_correlated"], errors="coerce").max()),
            ]
            if not math.isnan(corr_low):
                y3_candidates.append(float(corr_low))
            if not math.isnan(corr_high):
                y3_candidates.append(float(corr_high))
            if not math.isnan(req_min):
                y3_candidates.append(float(req_min))
            if not math.isnan(req_max):
                y3_candidates.append(float(req_max))
            if not math.isnan(ltl_new_3s_plot):
                y3_candidates.append(float(ltl_new_3s_plot))
            if not math.isnan(utl_new_3s_plot):
                y3_candidates.append(float(utl_new_3s_plot))

            y3_min = min(y3_candidates)
            y3_max = max(y3_candidates)
            y3_span = y3_max - y3_min
            y3_pad = (0.06 * y3_span) if y3_span > 0 else (abs(y3_min) * 0.06 + 1.0)
            ax3.set_ylim(y3_min - y3_pad, y3_max + y3_pad)

            ax3.set_xlabel(x_label, fontsize=12)
            ax3.set_ylabel(f"Value{(' ['+unit+']') if unit else ''}", fontsize=12)
            ax3.tick_params(axis="both", labelsize=11)
            ax3.grid(True, alpha=0.25)
            ax3.legend(fontsize=10, framealpha=0.92)

            note_dist = f"Method={limits['Limit_Method']}  μ_corr={limits['CorrMean']:.4g}  σ_corr={limits['CorrStd']:.4g}"
            if not math.isnan(limits["MaxAbsResidual"]):
                note_dist += f"  max|res|={limits['MaxAbsResidual']:.4g}"
            if corr_window_invalid:
                note_dist += "  [INVALID LIMIT WINDOW]"
            ax3.text(
                0.015,
                0.02,
                note_dist,
                transform=ax3.transAxes,
                fontsize=11,
                va="bottom",
                ha="left",
                bbox={"facecolor": "white", "alpha": 0.85, "edgecolor": "none", "pad": 3.0},
            )

            fig.tight_layout(rect=[0, 0, 1, 0.96])

            fname = _safe_slug(title) + ".png"
            fig.savefig(local_plots_dir / fname, dpi=PLOT_DPI, bbox_inches="tight")
            plt.close(fig)

    # Run
    if sheets_to_run:
        for sh in sheets_to_run:
            merged_sheet = run_one_same_sheet(sh)
            correlate_merged(merged_sheet, sh)
    else:
        merged_2 = run_one_two_sheets()
        correlate_merged(merged_2, f"{CV_SHEET}__{ATE_SHEET}")

    if not all_factors_rows:
        raise SystemExit(
            "No groups produced results. Check MIN_POINTS_PER_GROUP, grouping columns, and whether CV/ATE rows merge correctly."
        )

    factors_df = pd.DataFrame(all_factors_rows)
    correlated_df = pd.DataFrame(all_data_rows)

    # POR baseline correction/signoff summary removed (not needed).

    # Put Test Name directly after the primary identifier (Test Number for TXLO, LUT value for TXPA)
    if "Test Number" in factors_df.columns:
        factors_df = _insert_column_after(factors_df, "Test Number", "Test Name")
    elif "LUT value" in factors_df.columns:
        factors_df = _insert_column_after(factors_df, "LUT value", "Test Name")

    if "Test Number" in correlated_df.columns:
        correlated_df = _insert_column_after(correlated_df, "Test Number", "Test Name")
    elif "LUT value" in correlated_df.columns:
        correlated_df = _insert_column_after(correlated_df, "LUT value", "Test Name")

    output_xlsx.parent.mkdir(parents=True, exist_ok=True)
    try:
        with pd.ExcelWriter(output_xlsx, engine="openpyxl") as writer:
            factors_df.to_excel(writer, index=False, sheet_name="Correlation_Factors")
            correlated_df.to_excel(writer, index=False, sheet_name="Correlated_Data")
    except PermissionError:
        # Most common cause: the workbook is open in Excel.
        alt_path = output_xlsx.with_name(output_xlsx.stem + "_new.xlsx")
        with pd.ExcelWriter(alt_path, engine="openpyxl") as writer:
            factors_df.to_excel(writer, index=False, sheet_name="Correlation_Factors")
            correlated_df.to_excel(writer, index=False, sheet_name="Correlated_Data")
        output_xlsx = alt_path

    print(f"Wrote factors: {len(factors_df)} groups")
    print(f"Wrote data: {len(correlated_df)} rows")
    print(f"Output Excel: {output_xlsx}")
    print(f"Plots folder: {plots_dir}")
