"""\
CV ↔ ATE delta-based correlation (flat script, no classes).

Input workbook contains both CV and ATE data in the SAME sheet but different columns.

For each data group (each test number = test case):
	group by Voltage corner → Frequency → Temperature
and within each group:
	- compute per-row delta = CV - ATE
	- compute avg_delta = mean(delta)
	- compute new ATE high limit:
		ATE_High_New = ATE_High_Old - avg_delta
	- compute worst-case guard-band per test case:
		WC_GuardBand = max(|delta_i - avg_delta|)
	- compute worst-case ATE high limit:
		ATE_High_WC = ATE_High_New - WC_GuardBand

Outputs:
	- Excel summary (one row per group)
	- Plots per group showing:
	  CV raw data, ATE raw data (different style), ATE old/new/WC limit lines

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

INPUT_XLSX = r"C:/UserData/Infineon/TE_CTRX/CTRX8188/Data_Reviews/CV_TE_Correlation/ATE_Extracted_DPLL_PN_Data.xlsx"

# Output file (or folder) for the generated summary
OUTPUT_XLSX = r"C:/UserData/Infineon/TE_CTRX/CTRX8188/Data_Reviews/CV_TE_Correlation/Correlation/CV_ATE_Delta_Summary_DPLL_PN.xlsx"

# Optional; if empty uses OUTPUT_XLSX folder + "plots_delta"
OUTPUT_PLOTS_DIR = r"C:/UserData/Infineon/TE_CTRX/CTRX8188/Data_Reviews/CV_TE_Correlation/Correlation/plots_delta"

# Sheets to process (same-layout sheets containing both CV and ATE columns)
SHEETS_TO_RUN = ["FE_PN", "BE_PN"]

# Value columns
CV_VALUE_COL = "CV_PN_DIV8"
ATE_VALUE_COL = "ATE_PN_DIV8"

# Limits columns (ATE limits)
ATE_LOW_COL = "Low"
ATE_HIGH_COL = "High"
UNIT_COL = "Unit"

# Grouping order: Test Number (test case) → Voltage corner → Frequency → Temperature
GROUP_COLS = ["Test Number", "Voltage corner", "Frequency_GHz", "Temperature"]

# Row identity keys (kept in the detailed output)
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

MIN_POINTS_PER_GROUP = 5
PLOT_DPI = 160


# =========================
# Helpers
# =========================


def _as_path_maybe_folder(path_str: str, default_filename: str) -> Path:
	p = Path(path_str)
	if p.suffix.lower() != ".xlsx":
		return p / default_filename
	return p


def _to_float_series(s: pd.Series) -> pd.Series:
	s2 = s.astype(str).str.strip()
	s2 = s2.replace({"": pd.NA, "nan": pd.NA, "NaN": pd.NA, "None": pd.NA})
	s2 = s2.str.replace(" ", "", regex=False)
	s2 = s2.str.replace(",", ".", regex=False)
	return pd.to_numeric(s2, errors="coerce")


def _safe_slug(text: str) -> str:
	t = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(text))
	return t.strip("_")[:180] or "plot"


def _find_test_name_column(columns: list[str]) -> str | None:
	# Try to find a column that represents the human-readable test name.
	# We match case-insensitively and ignore spaces/underscores.
	if not columns:
		return None

	def _norm(s: str) -> str:
		return re.sub(r"[\s_]+", "", str(s)).lower().strip()

	norm_map = {_norm(c): c for c in columns}
	for key in ("testname", "testcasename"):
		if key in norm_map:
			return norm_map[key]

	# Fallback: any column containing "testname" in its normalized form
	for c in columns:
		if "testname" in _norm(c):
			return c
	return None


# =========================
# Main
# =========================


if __name__ == "__main__":
	input_xlsx = Path(INPUT_XLSX)
	if not input_xlsx.is_file():
		raise SystemExit(f"Input file not found: {input_xlsx}")

	output_xlsx = _as_path_maybe_folder(OUTPUT_XLSX, "CV_ATE_Delta_Summary.xlsx")
	plots_dir = Path(OUTPUT_PLOTS_DIR) if str(OUTPUT_PLOTS_DIR).strip() else output_xlsx.parent / "plots_delta"
	plots_dir.mkdir(parents=True, exist_ok=True)

	sheets_to_run = [str(s).strip() for s in SHEETS_TO_RUN if str(s).strip()]
	if not sheets_to_run:
		raise SystemExit("SHEETS_TO_RUN is empty. Set it in USER CONFIG.")

	# Plot dependency
	import matplotlib

	matplotlib.use("Agg")
	import matplotlib.pyplot as plt

	summary_rows = []
	detail_rows = []

	for sheet_name in sheets_to_run:
		df = pd.read_excel(input_xlsx, sheet_name=sheet_name)
		test_name_col = _find_test_name_column(list(df.columns))

		required = set(MERGE_KEYS + GROUP_COLS + [CV_VALUE_COL, ATE_VALUE_COL, ATE_HIGH_COL])
		missing = [c for c in required if c not in df.columns]
		if missing:
			raise SystemExit(f"Sheet '{sheet_name}' missing columns: {missing}")

		keep = list(dict.fromkeys(MERGE_KEYS + [CV_VALUE_COL, ATE_VALUE_COL, ATE_LOW_COL, ATE_HIGH_COL, UNIT_COL]))
		if test_name_col:
			keep.append(test_name_col)
		keep = [c for c in keep if c in df.columns]
		df = df[keep].copy()

		# Normalize numbers
		df[CV_VALUE_COL] = _to_float_series(df[CV_VALUE_COL])
		df[ATE_VALUE_COL] = _to_float_series(df[ATE_VALUE_COL])
		if ATE_LOW_COL in df.columns:
			df[ATE_LOW_COL] = _to_float_series(df[ATE_LOW_COL])
		if ATE_HIGH_COL in df.columns:
			df[ATE_HIGH_COL] = _to_float_series(df[ATE_HIGH_COL])

		# Normalize key types a bit
		for k in MERGE_KEYS:
			if k in ("X", "Y", "Test Number"):
				df[k] = pd.to_numeric(df[k], errors="coerce")
			else:
				df[k] = df[k].astype(str).str.strip()

		if test_name_col and test_name_col in df.columns:
			df[test_name_col] = df[test_name_col].astype(str).replace({"nan": ""}).str.strip()

		df = df.dropna(subset=[CV_VALUE_COL, ATE_VALUE_COL])

		grouped = df.groupby(GROUP_COLS, dropna=False)
		sheet_plots_dir = plots_dir / _safe_slug(sheet_name)
		sheet_plots_dir.mkdir(parents=True, exist_ok=True)

		for group_key, g in grouped:
			g = g.dropna(subset=[CV_VALUE_COL, ATE_VALUE_COL]).copy()
			n = len(g)
			if n < MIN_POINTS_PER_GROUP:
				continue

			test_name = ""
			if test_name_col and test_name_col in g.columns:
				vals = (
					g[test_name_col]
					.astype(str)
					.replace({"nan": ""})
					.str.strip()
				)
				vals = [v for v in vals.tolist() if v]
				uniq = list(dict.fromkeys(vals))
				if len(uniq) == 1:
					test_name = uniq[0]
				elif len(uniq) > 1:
					shown = uniq[:3]
					test_name = "; ".join(shown) + (f" (+{len(uniq)-3} more)" if len(uniq) > 3 else "")

			g["CV"] = g[CV_VALUE_COL]
			g["ATE"] = g[ATE_VALUE_COL]
			g["Delta(CV-ATE)"] = g["CV"] - g["ATE"]

			avg_delta = float(g["Delta(CV-ATE)"].mean())
			std_delta = float(g["Delta(CV-ATE)"].std(ddof=1)) if n > 1 else math.nan
			wc_guardband = float((g["Delta(CV-ATE)"] - avg_delta).abs().max()) if n > 0 else math.nan

			# Limits
			ate_low = float(g[ATE_LOW_COL].dropna().iloc[0]) if (ATE_LOW_COL in g.columns and g[ATE_LOW_COL].notna().any()) else math.nan
			ate_high = float(g[ATE_HIGH_COL].dropna().iloc[0]) if (ATE_HIGH_COL in g.columns and g[ATE_HIGH_COL].notna().any()) else math.nan
			unit = ""
			if UNIT_COL in g.columns:
				u = g[UNIT_COL].astype(str).replace({"nan": ""}).str.strip()
				u = [v for v in u.tolist() if v]
				unit = u[0] if u else ""

			ate_high_new = math.nan
			if not math.isnan(ate_high):
				ate_high_new = ate_high - avg_delta

			ate_high_wc = math.nan
			if not math.isnan(ate_high_new) and not math.isnan(wc_guardband):
				ate_high_wc = ate_high_new - wc_guardband

			group_dict = dict(zip(GROUP_COLS, group_key if isinstance(group_key, tuple) else (group_key,)))

			summary_rows.append(
				{
					"DataSheet": sheet_name,
					**group_dict,
					"Test Name": test_name,
					"N": n,
					"AvgDelta(CV-ATE)": avg_delta,
					"StdDelta(CV-ATE)": std_delta,
					"WC_GuardBand": wc_guardband,
					"ATE_Low": ate_low,
					"ATE_High": ate_high,
					"ATE_High_New": ate_high_new,
					"ATE_High_WC": ate_high_wc,
					"Unit": unit,
				}
			)

			for _, r in g.iterrows():
				detail_rows.append(
					{
						"DataSheet": sheet_name,
						**{k: r[k] for k in MERGE_KEYS},
						"Test Name": (str(r.get(test_name_col, "")).strip() if test_name_col else ""),
						"CV": float(r["CV"]),
						"ATE": float(r["ATE"]),
						"Delta(CV-ATE)": float(r["Delta(CV-ATE)"]),
					}
				)

			# Plot: index vs values
			if "DUT Nr" in g.columns:
				g_plot = g.sort_values(by=["DUT Nr"]).reset_index(drop=True)
			else:
				g_plot = g.reset_index(drop=True)
			x_idx = pd.Series(range(len(g_plot)))

			fig, ax = plt.subplots(figsize=(11.0, 6.5))
			ax.plot(x_idx, g_plot["CV"], marker="o", linestyle="-", linewidth=2.2, markersize=6, label="CV raw")
			ax.plot(
				x_idx,
				g_plot["ATE"],
				marker="s",
				linestyle="--",
				linewidth=2.2,
				markersize=6,
				label="ATE raw",
			)

			# Old/new ATE limits
			if not math.isnan(ate_low):
				ax.axhline(ate_low, color="black", linestyle=":", linewidth=2.0, label="ATE Low")
			if not math.isnan(ate_high):
				ax.axhline(ate_high, color="black", linestyle=":", linewidth=2.0, label="ATE High")
			if not math.isnan(ate_high_new):
				ax.axhline(ate_high_new, color="cyan", linestyle="-.", linewidth=2.4, label="ATE High New (AVG)")
			if not math.isnan(ate_high_wc):
				ax.axhline(ate_high_wc, color="red", linestyle="--", linewidth=2.4, label="ATE High WC")

			title_parts = [f"{k}={v}" for k, v in group_dict.items()]
			if test_name:
				title_parts.insert(1 if title_parts else 0, f"Test Name={test_name}")
			title = " | ".join(title_parts)
			title_wrapped = "\n".join(textwrap.wrap(f"{sheet_name} | {title}", width=90))
			ax.set_title(title_wrapped, fontsize=13, pad=10)
			ax.set_xlabel("Samples (grouped DUTs)", fontsize=12)
			ax.set_ylabel(f"Value{(' ['+unit+']') if unit else ''}", fontsize=12)
			ax.tick_params(axis="both", labelsize=11)
			ax.grid(True, alpha=0.25)
			ax.legend(fontsize=11, framealpha=0.92)

			note = f"N={n}  avg(CV-ATE)={avg_delta:.4g}" + (f"  std={std_delta:.4g}" if not math.isnan(std_delta) else "")
			if not math.isnan(wc_guardband):
				note += f"  WC_GB={wc_guardband:.4g}"
			ax.text(
				0.015,
				0.02,
				note,
				transform=ax.transAxes,
				fontsize=11,
				va="bottom",
				ha="left",
				bbox={"facecolor": "white", "alpha": 0.85, "edgecolor": "none", "pad": 3.0},
			)

			fig.tight_layout()
			fig.savefig(sheet_plots_dir / (_safe_slug(title) + ".png"), dpi=PLOT_DPI, bbox_inches="tight")
			plt.close(fig)

	if not summary_rows:
		raise SystemExit("No groups produced results. Check MIN_POINTS_PER_GROUP and sheet/column configuration.")

	summary_df = pd.DataFrame(summary_rows)
	detail_df = pd.DataFrame(detail_rows)

	output_xlsx.parent.mkdir(parents=True, exist_ok=True)
	try:
		with pd.ExcelWriter(output_xlsx, engine="openpyxl") as writer:
			summary_df.to_excel(writer, index=False, sheet_name="Delta_Summary")
			detail_df.to_excel(writer, index=False, sheet_name="Delta_Details")
	except PermissionError:
		alt_xlsx = output_xlsx.with_name(output_xlsx.stem + "_new" + output_xlsx.suffix)
		with pd.ExcelWriter(alt_xlsx, engine="openpyxl") as writer:
			summary_df.to_excel(writer, index=False, sheet_name="Delta_Summary")
			detail_df.to_excel(writer, index=False, sheet_name="Delta_Details")
		output_xlsx = alt_xlsx

	print(f"Wrote summary groups: {len(summary_df)}")
	print(f"Wrote detail rows: {len(detail_df)}")
	print(f"Output Excel: {output_xlsx}")
	print(f"Plots folder: {plots_dir}")
