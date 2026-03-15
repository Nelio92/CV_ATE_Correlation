"""\
CV ↔ ATE correlation (flat script, no classes).

Reads a single raw data Excel file that contains CV and ATE data (typically in
separate sheets), merges rows by device/test keys, then groups by:
    Test Number, supply corner, frequency, and temperature

Per group (test case), correlation models are computed:
    1) Offset-only model (robust):      CV_pred = ATE + median(CV - ATE)
    2) Physics-based Kf model:          CV_pred = ATE - (alpha * Kf + beta)
       with alpha/beta fitted from:    (ATE - CV) = alpha * Kf + beta

The Kf values are read from a dedicated sheet (default: "KF_FE") and merged by
device/temperature keys.

Outputs:
    - Group-level coefficients + goodness-of-fit metrics (Excel)
    - Row-level predicted values + residuals per model (Excel)
    - Plots per group (two figures), each with 2 subplots:
        A) raw series + correlated series
        B) regression view + residuals per model

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

_REPO_ROOT = Path(__file__).resolve().parents[2]
INPUT_XLSX = str(_REPO_ROOT / "ATE_Extracted_PA_Power_Data_DoE.xlsx")
OUTPUT_XLSX = str(_REPO_ROOT / "CV_ATE_Correlation_TXPA_Power_FE.xlsx")  # can also be a folder path
OUTPUT_PLOTS_DIR = str(_REPO_ROOT / "plots_PA_Power_FE")  # optional; if empty uses OUTPUT_XLSX folder + "plots"
#INPUT_XLSX = str(_REPO_ROOT / "ATE_Extracted_LO_Power_Data.xlsx")
#OUTPUT_XLSX = str(_REPO_ROOT / "CV_ATE_Correlation_TXLO_Power_FE.xlsx")  # can also be a folder path
#OUTPUT_PLOTS_DIR = str(_REPO_ROOT / "plots_LO_Power_FE")  # optional; if empty uses OUTPUT_XLSX folder + "plots"

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
# Physics-based model input (Kf)
KF_SHEET = "KF_FE"  # new input parameter
KF_VALUE_COL = "Test Value"
# Use the individual Kf values as measured in the KF sheet.
# Even if Kf is expected to be supply/frequency independent, the source sheet
# provides Kf per DUT+temperature and is tagged by corner/frequency; we therefore
# merge only by DUT Nr + Temperature and broadcast to all corners/frequencies.
KF_MERGE_KEYS = ["DUT Nr", "Temperature"]
# TXLO-specific identifier (optional but recommended to keep test-cases separated)
LO_IDAC_COL = "LO IDAC"
# Optional process context (used to detect BE insertion)
INSERTION_TYPE_COL = "Insertion Type"
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
PA_POWER_LUT_255_REQ_MIN = 10  # dBm
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


def _norm_col_name(name: str) -> str:
    # Normalization used to match Excel headers that sometimes contain
    # whitespace/newlines or inconsistent underscores.
    return re.sub(r"[\s_\-]+", "", str(name)).lower().strip()


def _remap_columns_best_effort(df: pd.DataFrame, desired_cols: list[str]) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    norm_to_actual: dict[str, str] = {}
    collisions: set[str] = set()
    for c in list(df.columns):
        n = _norm_col_name(c)
        if n in norm_to_actual and norm_to_actual[n] != c:
            collisions.add(n)
            continue
        norm_to_actual[n] = c

    if collisions:
        # Don't rename ambiguous normalized columns.
        print(
            "WARNING: Column-name normalization collision(s) detected for: "
            + ", ".join(sorted(collisions))
            + ". Auto-remap will ignore ambiguous matches."
        )

    rename_map: dict[str, str] = {}
    for wanted in desired_cols:
        if wanted in df.columns:
            continue
        n = _norm_col_name(wanted)
        if n in collisions:
            continue
        actual = norm_to_actual.get(n)
        if actual and actual != wanted:
            rename_map[actual] = wanted

    if rename_map:
        df = df.rename(columns=rename_map)
    return df


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
_TX_CH_RE = re.compile(r"TX(?P<ch>[1-8])(?!\d)", flags=re.IGNORECASE)


def _extract_lut_value(test_name: str):
    m = _FWLU_RE.search(str(test_name) if test_name is not None else "")
    if not m:
        return pd.NA
    try:
        return int(m.group("lut"))
    except Exception:
        return pd.NA


def _extract_pa_channel(test_name: str) -> str | None:
    m = _TX_CH_RE.search(str(test_name) if test_name is not None else "")
    if not m:
        return None
    return f"TX{m.group('ch')}"


def _ensure_pa_channel_column(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    if "PA Channel" in df.columns:
        df["PA Channel"] = df["PA Channel"].astype(str).str.strip()
        return df
    if "Test Name" in df.columns:
        df["PA Channel"] = df["Test Name"].astype(str).map(_extract_pa_channel)
        return df
    df["PA Channel"] = ""
    return df


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


def _ols_fit_y_on_x(x: pd.Series, y: pd.Series) -> tuple[float, float]:
    """Fit y = a*x + b using ordinary least squares.

    Returns (a, b). If not enough variation, returns (nan, nan).
    """
    xx = pd.to_numeric(x, errors="coerce")
    yy = pd.to_numeric(y, errors="coerce")
    m = xx.notna() & yy.notna()
    xx = xx[m].astype(float)
    yy = yy[m].astype(float)
    if len(xx) < 2:
        return (math.nan, math.nan)
    x_mean = float(xx.mean())
    y_mean = float(yy.mean())
    var_x = float(((xx - x_mean) ** 2).sum())
    if var_x == 0.0:
        return (math.nan, math.nan)
    cov_xy = float(((xx - x_mean) * (yy - y_mean)).sum())
    a = cov_xy / var_x
    b = y_mean - (a * x_mean)
    return (a, b)


def _load_kf_values(*, input_xlsx: Path, sheet_name: str) -> pd.DataFrame:
    """Load Kf values as a lookup table keyed by KF_MERGE_KEYS."""
    if not str(sheet_name).strip():
        raise SystemExit("KF_SHEET is empty. Set it in the USER CONFIG block.")
    kf_df = pd.read_excel(input_xlsx, sheet_name=sheet_name)

    missing = [c for c in (KF_MERGE_KEYS + [KF_VALUE_COL]) if c not in kf_df.columns]
    if missing:
        raise SystemExit(f"Kf sheet '{sheet_name}' missing columns: {missing}")

    out = kf_df[KF_MERGE_KEYS + [KF_VALUE_COL]].copy()
    out[KF_VALUE_COL] = _to_float_series(out[KF_VALUE_COL])
    for k in KF_MERGE_KEYS:
        out[k] = out[k].astype(str).str.strip()

    out = out.dropna(subset=[KF_VALUE_COL])
    # Keep the individual Kf values; if multiple measurements exist for the same
    # DUT+Temperature, keep the first and warn (no aggregation).
    dup_mask = out.duplicated(subset=KF_MERGE_KEYS, keep=False)
    if bool(dup_mask.any()):
        dup_cnt = int(dup_mask.sum())
        print(
            f"WARNING: KF sheet '{sheet_name}' has {dup_cnt} duplicate rows for keys {KF_MERGE_KEYS}. "
            "Keeping the first occurrence per key (no aggregation)."
        )
        out = out.drop_duplicates(subset=KF_MERGE_KEYS, keep="first")

    out = out.rename(columns={KF_VALUE_COL: "Kf"})
    return out


def _maybe_extend_txlo_group_cols_with_idac(df: pd.DataFrame, group_cols: list[str]) -> list[str]:
    if df is None or df.empty:
        return group_cols
    if list(group_cols) != list(TXLO_GROUP_COLS):
        return group_cols
    if LO_IDAC_COL not in df.columns:
        return group_cols
    return [*TXLO_GROUP_COLS, LO_IDAC_COL]


def _is_be_insertion(sheet_label: str, g: pd.DataFrame) -> bool:
    """Detect BE insertion context.

    True if either:
      - the sheet label contains 'BE' (case-insensitive), e.g. 'BE_Filtered'
      - an 'Insertion Type' column contains 'BE' for any row in the group
    """
    if str(sheet_label).strip() and "BE" in str(sheet_label).upper():
        return True
    if g is not None and not g.empty and INSERTION_TYPE_COL in g.columns:
        s = (
            g[INSERTION_TYPE_COL]
            .astype(str)
            .replace({"nan": "", "None": ""})
            .str.strip()
            .str.upper()
        )
        return bool(s.str.contains("BE", na=False).any())
    return False


def _compute_new_limits(
    *,
    test_case_mode: str,
    group_cols: list[str],
    group_dict: dict,
    g: pd.DataFrame,
    sheet_label: str = "",
    corr_col: str = "ATE_correlated",
    residual_col: str = "Residual",
) -> dict:
    """Compute new limits based on correlated distribution.

        Returns a dict containing:
            - Corr_Low, Corr_High (limits in correlated/CV domain)
            - Limit_Method (string)
            - CorrMean, CorrStd, MaxAbsResidual
    """
    corr = pd.to_numeric(g[corr_col], errors="coerce")
    corr_mean = float(corr.mean())
    corr_std = float(corr.std(ddof=1)) if len(corr) > 1 else math.nan

    residual = pd.to_numeric(g[residual_col], errors="coerce")
    max_abs_residual = float(residual.abs().max()) if len(residual) else math.nan
    residual_max = float(residual.max()) if len(residual) else math.nan
    residual_min = float(residual.min()) if len(residual) else math.nan

    corr_low = math.nan
    corr_high = math.nan
    method = "mean±6σ(correlated)"

    mode = str(test_case_mode).strip().upper()

    is_be = _is_be_insertion(sheet_label, g)

    # Special case 1: MAX LO power (IDAC 112): requirements with signed-residual guardbands
    if mode == "TXLO":
        idac_val = None
        if LO_IDAC_COL in group_cols and LO_IDAC_COL in group_dict:
            idac_val = group_dict.get(LO_IDAC_COL)
        elif LO_IDAC_COL in g.columns:
            idac_series = pd.to_numeric(g[LO_IDAC_COL], errors="coerce").dropna()
            if len(idac_series.unique()) == 1:
                idac_val = int(idac_series.iloc[0])

        if idac_val == 112 and not math.isnan(residual_max) and not math.isnan(residual_min):
            # Guardbanding based on extrema of signed residuals, but with absolute
            # magnitudes to avoid wrong-direction shifts when all residuals have
            # the same sign.
            if is_be:
                corr_low = float(LO_POWER_IDAC_112_REQ_MIN)
                corr_high = float(LO_POWER_IDAC_112_REQ_MAX)
                method = "REQ_MIN/REQ_MAX (BE, LO IDAC 112)"
            else:
                #   LTL: REQ_MIN + abs(min(residual))
                #   UTL: REQ_MAX - abs(max(residual))
                corr_low = float(LO_POWER_IDAC_112_REQ_MIN) + abs(residual_min)
                corr_high = float(LO_POWER_IDAC_112_REQ_MAX) - abs(residual_max)
                method = "REQ_MIN + abs(residual_min); REQ_MAX - abs(residual_max) (LO IDAC 112)"

    # Special case 2: MAX PA power (LUT 255): requirements with signed-residual guardbands
    if mode == "TXPA":
        lut_val = None
        if "LUT value" in group_cols and "LUT value" in group_dict:
            lut_val = group_dict.get("LUT value")
        elif "LUT value" in g.columns:
            lut_series = pd.to_numeric(g["LUT value"], errors="coerce").dropna()
            if len(lut_series.unique()) == 1:
                lut_val = int(lut_series.iloc[0])

        if lut_val == 255 and not math.isnan(residual_max) and not math.isnan(residual_min):
            # Guardbanding based on extrema of signed residuals, but with absolute
            # magnitudes to avoid wrong-direction shifts when all residuals have
            # the same sign.
            if is_be:
                corr_low = float(PA_POWER_LUT_255_REQ_MIN)
                corr_high = float(PA_POWER_LUT_255_REQ_MAX)
                method = "REQ_MIN/REQ_MAX (BE, PA LUT 255)"
            else:
                #   LTL: REQ_MIN + abs(min(residual))
                #   UTL: REQ_MAX - abs(max(residual))
                corr_low = float(PA_POWER_LUT_255_REQ_MIN) + abs(residual_min)
                corr_high = float(PA_POWER_LUT_255_REQ_MAX) - abs(residual_max)
                method = "REQ_MIN + abs(residual_min); REQ_MAX - abs(residual_max) (PA LUT 255)"

    # Default: mean ± 6σ on correlated distribution
    if math.isnan(corr_low) or math.isnan(corr_high):
        if not math.isnan(corr_mean) and not math.isnan(corr_std):
            corr_low = corr_mean - (CORRELATED_SIGMA_MULT * corr_std)
            corr_high = corr_mean + (CORRELATED_SIGMA_MULT * corr_std)

    return {
        "CorrMean": corr_mean,
        "CorrStd": corr_std,
        "MaxAbsResidual": max_abs_residual,
        "ResidualMax": residual_max,
        "ResidualMin": residual_min,
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

        # Best-effort remap for headers containing newlines/whitespace, e.g. "CV_\nLO_Power".
        df = _remap_columns_best_effort(
            df,
            desired_cols=list(
                dict.fromkeys(
                    MERGE_KEYS
                    + [CV_VALUE_COL, ATE_VALUE_COL, ATE_LOW_COL, ATE_HIGH_COL, ATE_UNIT_COL, LO_IDAC_COL, "LUT value", INSERTION_TYPE_COL]
                )
            ),
        )

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
        if INSERTION_TYPE_COL in df.columns:
            keep.append(INSERTION_TYPE_COL)
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
        if INSERTION_TYPE_COL in merged_local.columns:
            rename_map[INSERTION_TYPE_COL] = INSERTION_TYPE_COL
        merged_local = merged_local.rename(columns=rename_map)
        merged_local = _ensure_lut_value_column(merged_local)

        if LO_IDAC_COL in merged_local.columns:
            merged_local[LO_IDAC_COL] = pd.to_numeric(merged_local[LO_IDAC_COL], errors="coerce").astype("Int64")
        return merged_local

    def run_one_two_sheets():
        cv_df = pd.read_excel(input_xlsx, sheet_name=CV_SHEET)
        ate_df = pd.read_excel(input_xlsx, sheet_name=ATE_SHEET)

        cv_df = _remap_columns_best_effort(
            cv_df,
            desired_cols=list(dict.fromkeys(MERGE_KEYS + [CV_VALUE_COL, ATE_LOW_COL, ATE_HIGH_COL, ATE_UNIT_COL, LO_IDAC_COL, "LUT value", INSERTION_TYPE_COL])),
        )
        ate_df = _remap_columns_best_effort(
            ate_df,
            desired_cols=list(dict.fromkeys(MERGE_KEYS + [ATE_VALUE_COL, LO_IDAC_COL, "LUT value", INSERTION_TYPE_COL])),
        )

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

        # Insertion Type column (keep if present)
        if INSERTION_TYPE_COL in cv_df.columns:
            cv_keep.append(INSERTION_TYPE_COL)
        if INSERTION_TYPE_COL in ate_df.columns:
            ate_keep.append(INSERTION_TYPE_COL)

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

        # Normalize/choose a single Insertion Type column if present
        if INSERTION_TYPE_COL not in merged_local.columns:
            if f"{INSERTION_TYPE_COL}_CV" in merged_local.columns:
                merged_local = merged_local.rename(columns={f"{INSERTION_TYPE_COL}_CV": INSERTION_TYPE_COL})
            elif f"{INSERTION_TYPE_COL}_ATE" in merged_local.columns:
                merged_local = merged_local.rename(columns={f"{INSERTION_TYPE_COL}_ATE": INSERTION_TYPE_COL})

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

        # Merge Kf (physics-based model)
        kf_lookup = _load_kf_values(input_xlsx=input_xlsx, sheet_name=KF_SHEET)
        merged_local = merged_local.merge(
            kf_lookup,
            how="left",
            on=KF_MERGE_KEYS,
            validate="many_to_one",
        )
        if "Kf" in merged_local.columns:
            missing_kf = int(pd.to_numeric(merged_local["Kf"], errors="coerce").isna().sum())
            total = int(len(merged_local))
            print(f"{sheet_label}: Kf merge coverage = {total - missing_kf}/{total} rows")

        group_cols = _resolve_group_cols(merged_local)
        if "LUT value" in group_cols:
            merged_local = _ensure_lut_value_column(merged_local)

            # LUT255: split correlation by PA channel (TX1..TX8) extracted from Test Name.
            merged_local = _ensure_pa_channel_column(merged_local)
            if "LUT value" in merged_local.columns and (merged_local["LUT value"] == 255).any():
                # Keep non-255 data grouped together under a constant label.
                merged_local["PA Channel"] = merged_local["PA Channel"].where(
                    merged_local["LUT value"] == 255,
                    other="ALL",
                )
                if "PA Channel" not in group_cols:
                    group_cols = [*group_cols, "PA Channel"]

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

            # Kf verification stats for output
            kf_series = pd.to_numeric(g.get("Kf"), errors="coerce") if "Kf" in g.columns else pd.Series([math.nan] * len(g))
            kf_missing = int(kf_series.isna().sum())
            kf_present = int(kf_series.notna().sum())
            kf_unique = int(pd.Series(kf_series.dropna().unique()).nunique()) if kf_present else 0
            kf_min = float(kf_series.min()) if kf_present else math.nan
            kf_max = float(kf_series.max()) if kf_present else math.nan
            kf_mean = float(kf_series.mean()) if kf_present else math.nan

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

            # R² for the offset-only model in CV-domain: CV_pred = ATE + median_delta
            r2_offset = _r2_score(g["CV"], g["ATE_correlated"])

            # Physics-based: CV_pred = ATE - (alpha*Kf + beta), fitted from (ATE - CV) = alpha*Kf + beta
            alpha = math.nan
            beta = math.nan
            r2_phys = math.nan
            residual_std_phys = math.nan
            if "Kf" in g.columns:
                kf = pd.to_numeric(g["Kf"], errors="coerce")
                d = pd.to_numeric(g["ATE"], errors="coerce") - pd.to_numeric(g["CV"], errors="coerce")
                m_kf = kf.notna() & d.notna()
                if int(m_kf.sum()) >= 2:
                    alpha, beta = _ols_fit_y_on_x(kf[m_kf], d[m_kf])
                    if not math.isnan(alpha) and not math.isnan(beta):
                        g["ATE_correlated_Physics"] = g["ATE"] - (alpha * g["Kf"] + beta)
                        g["Residual_Physics"] = g["CV"] - g["ATE_correlated_Physics"]
                        r2_phys = _r2_score(g["CV"], g["ATE_correlated_Physics"])
                        residual_std_phys = float(pd.to_numeric(g["Residual_Physics"], errors="coerce").std(ddof=1)) if len(g) > 1 else math.nan
                if "ATE_correlated_Physics" not in g.columns:
                    g["ATE_correlated_Physics"] = math.nan
                    g["Residual_Physics"] = math.nan
            else:
                g["ATE_correlated_Physics"] = math.nan
                g["Residual_Physics"] = math.nan

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
            #     TXLO max LO power (IDAC 112):
            #       - BE insertion: REQ_MIN, REQ_MAX
            #       - else: REQ_MIN + abs(residual_min), REQ_MAX - abs(residual_max)
            #     TXPA max PA power (LUT 255):
            #       - BE insertion: REQ_MIN, REQ_MAX
            #       - else: REQ_MIN + abs(residual_min), REQ_MAX - abs(residual_max)
            inferred_mode = "TXPA" if "LUT value" in group_cols else "TXLO"
            group_dict = dict(zip(group_cols, group_key if isinstance(group_key, tuple) else (group_key,)))
            limits = _compute_new_limits(
                test_case_mode=inferred_mode,
                group_cols=group_cols,
                group_dict=group_dict,
                g=g,
                sheet_label=sheet_label,
            )
            corr_low = limits["Corr_Low"]
            corr_high = limits["Corr_High"]

            # Physics-model limits (computed on physics-predicted CV distribution)
            limits_phys = _compute_new_limits(
                test_case_mode=inferred_mode,
                group_cols=group_cols,
                group_dict=group_dict,
                g=g,
                sheet_label=sheet_label,
                corr_col="ATE_correlated_Physics",
                residual_col="Residual_Physics",
            )

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
                    "R2_OffsetModel": r2_offset,
                    "ResidualStd(Delta)": residual_std,
                    "MaxAbsResidual(Delta)": limits["MaxAbsResidual"],
                    "ResidualMax(Delta)": limits["ResidualMax"],
                    "ResidualMin(Delta)": limits["ResidualMin"],
                    "CorrMean": limits["CorrMean"],
                    "CorrStd": limits["CorrStd"],
                    "Corr_Low": corr_low,
                    "Corr_High": corr_high,
                    "Corr_Window_Width": corr_window_width,
                    "Corr_Window_Invalid": corr_window_invalid,
                    "Limit_Method": limits["Limit_Method"],
                    "Phys_alpha": alpha,
                    "Phys_beta": beta,
                    "R2_Physics": r2_phys,
                    "ResidualStd_Physics": residual_std_phys,
                    "Kf_N_Present": kf_present,
                    "Kf_N_Missing": kf_missing,
                    "Kf_Unique": kf_unique,
                    "Kf_Min": kf_min,
                    "Kf_Max": kf_max,
                    "Kf_Mean": kf_mean,
                    "CorrMean_Physics": limits_phys["CorrMean"],
                    "CorrStd_Physics": limits_phys["CorrStd"],
                    "ResidualMax_Physics": limits_phys["ResidualMax"],
                    "ResidualMin_Physics": limits_phys["ResidualMin"],
                    "Corr_Low_Physics": limits_phys["Corr_Low"],
                    "Corr_High_Physics": limits_phys["Corr_High"],
                    "Limit_Method_Physics": limits_phys["Limit_Method"],
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
                        "PA Channel": (str(r.get("PA Channel", "")).strip() if "PA Channel" in g.columns else ""),
                        "DoE split": (str(r.get("DoE split", "")).strip() if "DoE split" in g.columns else ""),
                        LO_IDAC_COL: (int(pd.to_numeric(r.get(LO_IDAC_COL), errors="coerce")) if LO_IDAC_COL in g.columns and pd.notna(r.get(LO_IDAC_COL)) else pd.NA),
                        "Test Name": (str(r.get("Test Name", "")).strip() if "Test Name" in g.columns else ""),
                        "CV": float(r["CV"]),
                        "ATE": float(r["ATE"]),
                        "ATE_correlated": float(r["ATE_correlated"]),
                        "Kf": (float(r["Kf"]) if "Kf" in g.columns and pd.notna(r.get("Kf")) else math.nan),
                        "ATE_correlated_Physics": (float(r["ATE_correlated_Physics"]) if pd.notna(r.get("ATE_correlated_Physics")) else math.nan),
                        "Delta(CV-ATE)": float(r["Delta(CV-ATE)"]),
                        "Residual": float(r["Residual"]),
                        "Residual_Physics": (float(r["Residual_Physics"]) if pd.notna(r.get("Residual_Physics")) else math.nan),
                    }
                )

            # Plots (2 figures per test case):
            # A) series figure (2 subplots): raw series + correlated series
            # B) models figure (2 subplots): regression view + residuals per model
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

            title_parts = [f"{k}={v}" for k, v in group_dict.items()]
            if test_name:
                title_parts.insert(1 if title_parts else 0, f"Test Name={test_name}")
            title = " | ".join(title_parts)
            title_wrapped = "\n".join(textwrap.wrap(f"{sheet_label} | {title}", width=110))
            base = _safe_slug(title)

            # =========================
            # Figure A: Series (raw + correlated)
            # =========================
            fig_series, (ax_raw, ax_corr) = plt.subplots(
                nrows=2,
                ncols=1,
                figsize=(12.0, 9.0),
                gridspec_kw={"height_ratios": [2.0, 2.0]},
            )
            fig_series.suptitle(title_wrapped, fontsize=13, y=0.98)

            ax_raw.plot(
                x_idx,
                g_plot["CV"],
                marker="o",
                linestyle="-",
                linewidth=2.2,
                markersize=6,
                label="CV (individual)",
            )
            ax_raw.plot(
                x_idx,
                g_plot["ATE"],
                marker="s",
                linestyle="--",
                linewidth=2.2,
                markersize=6,
                label="ATE (individual)",
            )

            # DoE split boundaries + labels
            if doe is not None:
                for cut in boundaries:
                    ax_raw.axvline(cut - 0.5, color="gray", linestyle="--", linewidth=1.2, alpha=0.45, zorder=0)

                blend1 = mtransforms.blended_transform_factory(ax_raw.transData, ax_raw.transAxes)
                for s, e in zip(starts, ends):
                    if e <= s:
                        continue
                    label = str(doe.iloc[s])
                    x_center = (s + (e - 1)) / 2.0
                    ax_raw.text(
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

            ax_raw.set_xlabel(x_label, fontsize=12)
            ax_raw.set_ylabel(f"Value{(' ['+unit+']') if unit else ''}", fontsize=12)
            ax_raw.tick_params(axis="both", labelsize=11)
            ax_raw.grid(True, alpha=0.25)
            ax_raw.set_xlim(-0.5, len(g_plot) - 0.5)

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
            ax_raw.set_ylim(y_min - pad, y_max + pad)

            note_top = f"N={n_points}  median(CV-ATE)={median_delta:.4g}  max(CV-ATE)={max_delta:.4g}"
            if not math.isnan(residual_std):
                note_top += f"  σ(residual)={residual_std:.4g}"
            ax_raw.text(
                0.015,
                0.02,
                note_top,
                transform=ax_raw.transAxes,
                fontsize=11,
                va="bottom",
                ha="left",
                bbox={"facecolor": "white", "alpha": 0.85, "edgecolor": "none", "pad": 3.0},
            )
            ax_raw.legend(fontsize=11, framealpha=0.92)

            # Correlated series
            ax_corr.plot(
                x_idx,
                g_plot["CV"],
                marker="o",
                linestyle="-",
                linewidth=2.2,
                markersize=6,
                label="CV (individual)",
            )
            ax_corr.plot(
                x_idx,
                g_plot["ATE_correlated"],
                marker="^",
                linestyle="--",
                linewidth=2.2,
                markersize=6,
                label="Offset-correlated (CV_pred)",
            )

            if "ATE_correlated_Physics" in g_plot.columns and pd.to_numeric(g_plot["ATE_correlated_Physics"], errors="coerce").notna().any():
                ax_corr.plot(
                    x_idx,
                    g_plot["ATE_correlated_Physics"],
                    marker="D",
                    linestyle=":",
                    linewidth=2.2,
                    markersize=5,
                    label="Physics-correlated (CV_pred)",
                )

            if doe is not None:
                for cut in boundaries:
                    ax_corr.axvline(cut - 0.5, color="gray", linestyle="--", linewidth=1.2, alpha=0.45, zorder=0)

                blend3 = mtransforms.blended_transform_factory(ax_corr.transData, ax_corr.transAxes)
                for s, e in zip(starts, ends):
                    if e <= s:
                        continue
                    label = str(doe.iloc[s])
                    x_center = (s + (e - 1)) / 2.0
                    ax_corr.text(
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

            # Limits (offset model) - use Corr_Low/Corr_High from the offset-only model
            corr_low_plot = limits["Corr_Low"]
            corr_high_plot = limits["Corr_High"]
            if not math.isnan(corr_low_plot):
                ax_corr.axhline(
                    corr_low_plot,
                    color="cyan",
                    linestyle="-.",
                    linewidth=2.2,
                    label=f"Corr Low = {corr_low_plot:.4g} dBm",
                )
            if not math.isnan(corr_high_plot):
                ax_corr.axhline(
                    corr_high_plot,
                    color="cyan",
                    linestyle="-.",
                    linewidth=2.2,
                    label=f"Corr High = {corr_high_plot:.4g} dBm",
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
                ax_corr.axhline(req_min, color="red", linestyle="-", linewidth=2.0, label=f"REQ Min = {req_min:.4g} dBm")
            if not math.isnan(req_max):
                ax_corr.axhline(req_max, color="red", linestyle="-", linewidth=2.0, label=f"REQ Max = {req_max:.4g} dBm")

            y_corr_candidates = [
                float(pd.to_numeric(g_plot["CV"], errors="coerce").min()),
                float(pd.to_numeric(g_plot["CV"], errors="coerce").max()),
                float(pd.to_numeric(g_plot["ATE_correlated"], errors="coerce").min()),
                float(pd.to_numeric(g_plot["ATE_correlated"], errors="coerce").max()),
            ]
            if "ATE_correlated_Physics" in g_plot.columns:
                phys_vals = pd.to_numeric(g_plot["ATE_correlated_Physics"], errors="coerce")
                if phys_vals.notna().any():
                    y_corr_candidates.append(float(phys_vals.min()))
                    y_corr_candidates.append(float(phys_vals.max()))
            if not math.isnan(corr_low_plot):
                y_corr_candidates.append(float(corr_low_plot))
            if not math.isnan(corr_high_plot):
                y_corr_candidates.append(float(corr_high_plot))
            if not math.isnan(req_min):
                y_corr_candidates.append(float(req_min))
            if not math.isnan(req_max):
                y_corr_candidates.append(float(req_max))

            y_corr_min = min(y_corr_candidates)
            y_corr_max = max(y_corr_candidates)
            y_corr_span = y_corr_max - y_corr_min
            y_corr_pad = (0.06 * y_corr_span) if y_corr_span > 0 else (abs(y_corr_min) * 0.06 + 1.0)
            ax_corr.set_ylim(y_corr_min - y_corr_pad, y_corr_max + y_corr_pad)

            ax_corr.set_xlabel(x_label, fontsize=12)
            ax_corr.set_ylabel(f"Value{(' ['+unit+']') if unit else ''}", fontsize=12)
            ax_corr.tick_params(axis="both", labelsize=11)
            ax_corr.grid(True, alpha=0.25)
            ax_corr.legend(fontsize=10, framealpha=0.92)

            note_dist = (
                f"Offset limits: Method={limits['Limit_Method']}"
            )
            #if not math.isnan(limits["MaxAbsResidual"]):
                #note_dist += f"  max|res|={limits['MaxAbsResidual']:.4g}"
            #if not math.isnan(alpha) and not math.isnan(beta):
                #note_dist += f"  |  Physics: alpha={alpha:.4g} beta={beta:.4g}  R²={r2_phys:.3f}"
            if corr_window_invalid:
                note_dist += "  [INVALID LIMIT WINDOW]"
            ax_corr.text(
                0.015,
                0.02,
                note_dist,
                transform=ax_corr.transAxes,
                fontsize=11,
                va="bottom",
                ha="left",
                bbox={"facecolor": "white", "alpha": 0.85, "edgecolor": "none", "pad": 3.0},
            )

            fig_series.tight_layout(rect=[0, 0, 1, 0.96])
            fig_series.savefig(local_plots_dir / f"{base}__series.png", dpi=PLOT_DPI, bbox_inches="tight")
            plt.close(fig_series)

            # =========================
            # Figure B: Models (regression + residuals)
            # =========================
            fig_models, (ax_reg, ax_res) = plt.subplots(
                nrows=2,
                ncols=1,
                figsize=(12.0, 9.0),
                gridspec_kw={"height_ratios": [2.0, 2.0]},
            )
            fig_models.suptitle(title_wrapped, fontsize=13, y=0.98)

            ax_reg.scatter(
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
            ax_reg.plot(
                x_line,
                y_line,
                linewidth=2.6,
                linestyle="--",
                color="tab:red",
                zorder=1,
                label="Offset model: ATE = CV - medianΔ",
            )

            if (
                (not math.isnan(alpha))
                and (not math.isnan(beta))
                and ("Kf" in g_plot.columns)
                and pd.to_numeric(g_plot["Kf"], errors="coerce").notna().any()
            ):
                kf_plot = pd.to_numeric(g_plot["Kf"], errors="coerce")
                ate_pred_phys = pd.to_numeric(g_plot["CV"], errors="coerce") + (alpha * kf_plot + beta)
                m_phys = ate_pred_phys.notna() & pd.to_numeric(g_plot["CV"], errors="coerce").notna()
                if bool(m_phys.any()):
                    ax_reg.scatter(
                        pd.to_numeric(g_plot.loc[m_phys, "CV"], errors="coerce"),
                        ate_pred_phys.loc[m_phys],
                        s=26,
                        alpha=0.85,
                        marker="D",
                        linewidths=0.8,
                        facecolors="none",
                        edgecolors="tab:orange",
                        zorder=2,
                        label="Physics: ATE = CV + (αKf + β)",
                    )

            ax_reg.set_xlabel(f"CV{(' ['+unit+']') if unit else ''}", fontsize=12)
            ax_reg.set_ylabel(f"ATE{(' ['+unit+']') if unit else ''}", fontsize=12)
            ax_reg.tick_params(axis="both", labelsize=11)
            ax_reg.grid(True, alpha=0.25)
            ax_reg.set_xlim(x_min - x_pad, x_max + x_pad)

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
            ax_reg.set_ylim(y2_min - y2_pad, y2_max + y2_pad)

            note_reg = f"N={n_points}  R²_offset={r2_offset:.3f}  R²_phys={r2_phys:.3f}  medianΔ={median_delta:.4g}"
            ax_reg.text(
                0.015,
                0.02,
                note_reg,
                transform=ax_reg.transAxes,
                fontsize=11,
                va="bottom",
                ha="left",
                bbox={"facecolor": "white", "alpha": 0.85, "edgecolor": "none", "pad": 3.0},
            )
            ax_reg.legend(fontsize=10, framealpha=0.92)

            ax_res.axhline(0.0, color="black", linewidth=1.2, alpha=0.6)
            ax_res.plot(
                x_idx,
                pd.to_numeric(g_plot.get("Residual"), errors="coerce"),
                marker="o",
                linestyle="-",
                linewidth=1.8,
                markersize=5,
                label="Residual (offset)",
            )
            if "Residual_Physics" in g_plot.columns and pd.to_numeric(g_plot["Residual_Physics"], errors="coerce").notna().any():
                ax_res.plot(
                    x_idx,
                    pd.to_numeric(g_plot["Residual_Physics"], errors="coerce"),
                    marker="D",
                    linestyle=":",
                    linewidth=1.8,
                    markersize=4,
                    label="Residual (physics)",
                )

            if doe is not None:
                for cut in boundaries:
                    ax_res.axvline(cut - 0.5, color="gray", linestyle="--", linewidth=1.2, alpha=0.45, zorder=0)

                blendr = mtransforms.blended_transform_factory(ax_res.transData, ax_res.transAxes)
                for s, e in zip(starts, ends):
                    if e <= s:
                        continue
                    label = str(doe.iloc[s])
                    x_center = (s + (e - 1)) / 2.0
                    ax_res.text(
                        x_center,
                        0.96,
                        label,
                        transform=blendr,
                        ha="center",
                        va="top",
                        fontsize=11,
                        color="black",
                        zorder=5,
                        path_effects=[patheffects.withStroke(linewidth=3.0, foreground="white", alpha=0.9)],
                    )

            ax_res.set_xlabel(x_label, fontsize=12)
            ax_res.set_ylabel(f"Residual{(' ['+unit+']') if unit else ''}", fontsize=12)
            ax_res.tick_params(axis="both", labelsize=11)
            ax_res.grid(True, alpha=0.25)
            ax_res.legend(fontsize=10, framealpha=0.92)

            fig_models.tight_layout(rect=[0, 0, 1, 0.96])
            fig_models.savefig(local_plots_dir / f"{base}__models.png", dpi=PLOT_DPI, bbox_inches="tight")
            plt.close(fig_models)

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
