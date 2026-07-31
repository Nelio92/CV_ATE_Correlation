"""Self-contained, offline HTML sign-off reporting for CorreLaTE."""

from __future__ import annotations

import base64
import io
import math
import re
from collections import OrderedDict
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from . import __author__, __version__
from .correlation import CorrelationResult
from .models import CorrelationProfile
from .reporting import (
    _canonical_test_name,
    _insertion_bucket,
    _render_plot_figures,
    _test_name_after_test_number,
)


_INSERTION_COLUMNS = ("Insertion Type", "Insertion", "Temperature")
_TEST_IDENTITY_COLUMNS = {"Test Number", "Test Name", "TestName"}


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _display_value(value: Any) -> str:
    if _is_missing(value) or str(value).strip() == "":
        return "—"
    if isinstance(value, bool) or type(value).__name__ == "bool_":
        return "Yes" if value else "No"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        if math.isinf(value):
            return "∞"
        if not math.isfinite(value):
            return "—"
        if value.is_integer() and abs(value) < 1e12:
            return f"{int(value):,}"
        return f"{value:.6g}"
    return str(value).strip()


def _natural_key(value: Any) -> tuple[Any, ...]:
    text = _display_value(value).casefold()
    return tuple(
        (0, float(part)) if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", part) else (1, part)
        for part in re.split(r"([-+]?\d+(?:\.\d+)?)", text)
        if part
    )


def _unique_values(frame: pd.DataFrame, column: str) -> list[Any]:
    if column not in frame.columns:
        return []
    values: list[Any] = []
    seen: set[str] = set()
    for value in frame[column].tolist():
        if _is_missing(value) or str(value).strip() == "":
            continue
        identity = _display_value(value).casefold()
        if identity in seen:
            continue
        seen.add(identity)
        values.append(value)
    return sorted(values, key=_natural_key)


def _representative_value(summary: pd.Series, details: pd.DataFrame, column: str) -> Any:
    if column in summary.index and not _is_missing(summary[column]) and str(summary[column]).strip() != "":
        return summary[column]
    values = _unique_values(details, column)
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return "; ".join(_display_value(value) for value in values)


def _normalized_test_number(value: Any) -> str:
    if _is_missing(value) or str(value).strip() == "":
        return ""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value).strip()
    return str(int(numeric)) if numeric.is_integer() else f"{numeric:g}"


def _test_identities(details: pd.DataFrame) -> tuple[tuple[str, tuple[str, ...]], ...]:
    names_by_number: dict[str, set[str]] = {}
    number_column = details["Test Number"] if "Test Number" in details.columns else pd.Series("", index=details.index)
    name_column = details["Test Name"] if "Test Name" in details.columns else pd.Series("", index=details.index)
    for raw_number, raw_name in zip(number_column, name_column):
        number = _normalized_test_number(raw_number)
        name = "" if _is_missing(raw_name) else str(raw_name).strip()
        key = number or "—"
        names_by_number.setdefault(key, set())
        if name:
            names_by_number[key].add(name)
    return tuple(
        (number, tuple(sorted(names, key=_natural_key)))
        for number, names in sorted(names_by_number.items(), key=lambda item: _natural_key(item[0]))
    )


def _family_key(summary: pd.Series, identities: tuple[tuple[str, tuple[str, ...]], ...]) -> tuple[Any, ...]:
    return str(summary.get("TestSet", "") or "").strip(), identities


def _group_details(result: CorrelationResult, profile: CorrelationProfile) -> dict[int, pd.DataFrame]:
    details = _test_name_after_test_number(_canonical_test_name(result.details, profile))
    if "GroupIndex" not in details.columns:
        raise ValueError("Correlation details must contain GroupIndex for HTML reporting")
    return {
        int(group_index): group.reset_index(drop=True)
        for group_index, group in details.groupby("GroupIndex", sort=True)
    }


def _insertion_information(summary: pd.Series, details: pd.DataFrame) -> tuple[str, str, Any, str]:
    insertion_type = _insertion_bucket(summary, details)
    insertion = _representative_value(summary, details, "Insertion")
    temperature = _representative_value(summary, details, "Temperature")
    insertion_text = _display_value(insertion) if insertion is not None else insertion_type
    temperature_text = _display_value(temperature)
    label = f"{insertion_text} · {insertion_type}"
    if temperature_text != "—":
        label += f" · {temperature_text} °C"
    return insertion_type, insertion_text, temperature, label


def _pooled_parameters(summary_rows: pd.DataFrame) -> tuple[str, ...]:
    values: list[str] = []
    if "PooledParameters" not in summary_rows.columns:
        return ()
    for raw in summary_rows["PooledParameters"].tolist():
        if _is_missing(raw):
            continue
        for value in str(raw).split(","):
            value = value.strip()
            if value and value not in values:
                values.append(value)
    return tuple(values)


def _context_dimensions(profile: CorrelationProfile, pooled: Sequence[str]) -> tuple[str, ...]:
    excluded = {*_TEST_IDENTITY_COLUMNS, *_INSERTION_COLUMNS, *pooled}
    return tuple(column for column in profile.group_by if column not in excluded)


def _context_row(
    summary: pd.Series,
    details: pd.DataFrame,
    dimensions: Sequence[str],
) -> dict[str, Any]:
    insertion_type, insertion, temperature, _label = _insertion_information(summary, details)
    row: dict[str, Any] = {
        "Insertion Type": insertion_type,
        "Insertion": insertion,
        "Temperature": temperature,
    }
    for dimension in dimensions:
        row[dimension] = _representative_value(summary, details, dimension)
    return row


def _table_html(
    heading: str,
    rows: Sequence[dict[str, Any]],
    columns: Sequence[tuple[str, str]],
    *,
    table_class: str,
) -> str:
    header = "".join(f"<th scope=\"col\">{escape(label)}</th>" for _key, label in columns)
    body_rows: list[str] = []
    for row in rows:
        invalid = bool(row.get("LimitWindowInvalid", False))
        cells = []
        for key, _label in columns:
            value = _display_value(row.get(key))
            classes = []
            if key in {"CorrelationFactor", "CorrelationFactorA", "CorrelationFactorB"} and value != "—":
                classes.append("factor-value")
            if key in {"AdjustedLowerLimit", "AdjustedUpperLimit", "WorstCaseUpperLimit"} and value != "—":
                classes.append("limit-value")
            if key == "LimitWindowInvalid" and invalid:
                classes.append("invalid-value")
            class_attribute = f' class="{" ".join(classes)}"' if classes else ""
            cells.append(f"<td{class_attribute}>{escape(value)}</td>")
        row_class = ' class="invalid-row"' if invalid else ""
        body_rows.append(f"<tr{row_class}>{''.join(cells)}</tr>")
    if not body_rows:
        body_rows.append(f'<tr><td colspan="{len(columns)}">No applicable rows.</td></tr>')
    return (
        f'<div class="table-card"><h3>{escape(heading)}</h3><div class="table-scroll">'
        f'<table class="{escape(table_class)}"><thead><tr>{header}</tr></thead>'
        f'<tbody>{"".join(body_rows)}</tbody></table></div></div>'
    )


def _factor_rows(
    group_indices: Sequence[int],
    summary: pd.DataFrame,
    detail_groups: dict[int, pd.DataFrame],
    dimensions: Sequence[str],
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    rows: list[dict[str, Any]] = []
    for group_index in group_indices:
        source = summary.iloc[group_index]
        strategy = str(source.get("CorrelationStrategy", "") or "")
        row = _context_row(source, detail_groups[group_index], dimensions)
        row.update({
            "CorrelationStrategy": strategy,
            "Count": source.get("Count"),
            "OriginalCount": source.get("OriginalCount"),
            "OutlierFlaggedCount": source.get("OutlierFlaggedCount"),
            "OutlierExcludedCount": source.get("OutlierExcludedCount"),
            "CorrelationFactor": (
                source.get("CorrelationFactor")
                if strategy in {"Mean_Deltas", "Median_Deltas"} else None
            ),
            "CorrelationFactorA": (
                source.get("CorrelationFactorA")
                if strategy in {"Linear", "Physics-based"} else None
            ),
            "CorrelationFactorB": (
                source.get("CorrelationFactorB")
                if strategy in {"Linear", "Physics-based"} else None
            ),
        })
        rows.append(row)
    rows.sort(key=_insertion_row_key)
    columns: list[tuple[str, str]] = [
        ("Insertion Type", "Type"),
        ("Insertion", "Insertion"),
        ("Temperature", "Temperature [°C]"),
        *((dimension, dimension) for dimension in dimensions),
        ("CorrelationStrategy", "Strategy"),
        ("Count", "Samples"),
    ]
    for key, label in (
        ("OriginalCount", "Original samples"),
        ("OutlierFlaggedCount", "Flagged"),
        ("OutlierExcludedCount", "Excluded"),
    ):
        if any(not _is_missing(row.get(key)) for row in rows):
            columns.append((key, label))
    for key, label in (
        ("CorrelationFactor", "Factor / offset"),
        ("CorrelationFactorA", "Factor A (slope / α)"),
        ("CorrelationFactorB", "Factor B (intercept / β)"),
    ):
        if any(not _is_missing(row.get(key)) for row in rows):
            columns.append((key, label))
    return rows, columns


def _limit_rows(
    group_indices: Sequence[int],
    summary: pd.DataFrame,
    detail_groups: dict[int, pd.DataFrame],
    dimensions: Sequence[str],
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    rows: list[dict[str, Any]] = []
    for group_index in group_indices:
        source = summary.iloc[group_index]
        row = _context_row(source, detail_groups[group_index], dimensions)
        row.update({
            "GuardBandPolicy": source.get("GuardBandPolicy"),
            "GuardBandMethod": source.get("GuardBandMethod"),
            "AdjustedLowerLimit": source.get("AdjustedLowerLimit"),
            "AdjustedUpperLimit": source.get("AdjustedUpperLimit"),
            "WorstCaseUpperLimit": source.get("WorstCaseUpperLimit"),
            "Unit": source.get("Unit"),
            "LimitWindowInvalid": source.get("LimitWindowInvalid", False),
        })
        rows.append(row)
    rows.sort(key=_insertion_row_key)
    columns: list[tuple[str, str]] = [
        ("Insertion Type", "Type"),
        ("Insertion", "Insertion"),
        ("Temperature", "Temperature [°C]"),
        *((dimension, dimension) for dimension in dimensions),
        ("GuardBandPolicy", "Guard-band policy"),
        ("GuardBandMethod", "Method"),
    ]
    for key, label in (
        ("AdjustedLowerLimit", "New LTL"),
        ("AdjustedUpperLimit", "New UTL"),
        ("WorstCaseUpperLimit", "Worst-case UTL"),
    ):
        if any(not _is_missing(row.get(key)) for row in rows):
            columns.append((key, label))
    columns.extend((("Unit", "Unit"), ("LimitWindowInvalid", "Invalid window")))
    return rows, columns


def _insertion_row_key(row: dict[str, Any]) -> tuple[Any, ...]:
    insertion_type = str(row.get("Insertion Type", "") or "").upper()
    return (
        0 if insertion_type == "FE" else 1 if insertion_type == "BE" else 2,
        _natural_key(row.get("Insertion")),
        _natural_key(row.get("Temperature")),
    )


def _identity_table(identities: Sequence[tuple[str, Sequence[str]]]) -> str:
    rows = []
    for number, names in identities:
        names_html = "<br>".join(escape(name) for name in names) if names else "—"
        rows.append(
            f"<tr><td class=\"test-number\">{escape(number)}</td><td>{names_html}</td></tr>"
        )
    return (
        '<div class="identity-scroll"><table class="identity-table">'
        '<thead><tr><th scope="col">Test number</th><th scope="col">Test name(s)</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
    )


def _figure_data_uri(figure: Any, *, dpi: int, quality: int) -> tuple[str, str]:
    buffer = io.BytesIO()
    try:
        figure.savefig(
            buffer,
            format="webp",
            dpi=dpi,
            bbox_inches="tight",
            pil_kwargs={"quality": quality, "method": 4},
        )
        mime = "image/webp"
    except (KeyError, OSError, ValueError):
        buffer = io.BytesIO()
        figure.savefig(buffer, format="png", dpi=dpi, bbox_inches="tight")
        mime = "image/png"
    payload = base64.b64encode(buffer.getvalue()).decode("ascii")
    return mime, f"data:{mime};base64,{payload}"


def _logo_data_uri() -> str:
    logo = Path(__file__).resolve().with_name("assets") / "correlate-signal-bloom-64.png"
    payload = base64.b64encode(logo.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{payload}"


def _model_names(details: pd.DataFrame) -> list[str]:
    models: list[str] = []
    candidates = (
        ("LinearCorrectedCandidate", "Linear OLS"),
        ("MeanDeltasCorrectedCandidate", "Mean_Deltas"),
        ("MedianDeltasCorrectedCandidate", "Median_Deltas"),
        ("PhysicsCorrectedCandidate", "Physics-based"),
        ("DemingCorrectedCandidate", "Linear Deming (sensitivity only)"),
    )
    for column, label in candidates:
        if column in details.columns and pd.to_numeric(details[column], errors="coerce").notna().any():
            models.append(label)
    return models


def _overview_dimensions(details: pd.DataFrame, profile: CorrelationProfile) -> str:
    dimensions = list(dict.fromkeys((*profile.group_by, "Insertion Type")))
    rows = []
    for dimension in dimensions:
        if dimension in _TEST_IDENTITY_COLUMNS or dimension not in details.columns:
            continue
        values = _unique_values(details, dimension)
        if not values:
            continue
        displayed = [_display_value(value) for value in values[:24]]
        if len(values) > 24:
            displayed.append(f"+{len(values) - 24} more")
        rows.append(
            f"<tr><th scope=\"row\">{escape(dimension)}</th>"
            f"<td>{escape('; '.join(displayed))}</td><td>{len(values):,}</td></tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="3">No grouping dimensions were reported.</td></tr>')
    return (
        '<div class="table-card"><h3>Campaign conditions and corners</h3><div class="table-scroll">'
        '<table><thead><tr><th scope="col">Dimension</th><th scope="col">Values</th>'
        f'<th scope="col">Unique</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div></div>'
    )


def _metadata_table(rows: Sequence[tuple[str, str]]) -> str:
    body = "".join(
        f"<tr><th scope=\"row\">{escape(label)}</th><td>{escape(value)}</td></tr>"
        for label, value in rows
    )
    return f'<div class="table-card"><h3>Profile and analysis</h3><table class="metadata"><tbody>{body}</tbody></table></div>'


def _outlier_review_html(review: Any) -> str:
    audit = review.audit_frame()
    preferred = (
        "ReviewStatus",
        "OutlierInputRow",
        "TestSet",
        "Test Number",
        "Test Name",
        "DUT Nr",
        "Wafer",
        "WAFER",
        "X",
        "Y",
        "DoE split",
        "Insertion Type",
        "Insertion",
        "Temperature",
        "OutlierFlaggedSeries",
        "OutlierMaxRobustScore",
        "LabValue",
        "LabRobustScore",
        "ATEValue",
        "ATERobustScore",
        "PairedMetric",
        "PairedValue",
        "PairedRobustScore",
        "OutlierReviewGuidance",
        "OutlierReason",
        "Excluded",
    )
    columns = [column for column in preferred if column in audit.columns]
    rows = [{column: value for column, value in row.items()} for row in audit.to_dict("records")]
    labels = [(column, column.replace("Outlier", "Outlier ")) for column in columns]
    return _table_html(
        "Flagged-sample audit",
        rows,
        labels,
        table_class="outlier-table",
    )


def _condition_groups(
    group_indices: Sequence[int],
    summary: pd.DataFrame,
    dimensions: Sequence[str],
) -> list[tuple[tuple[tuple[str, str], ...], list[int]]]:
    grouped: OrderedDict[tuple[tuple[str, str], ...], list[int]] = OrderedDict()
    for group_index in group_indices:
        row = summary.iloc[group_index]
        key = tuple((dimension, _display_value(row.get(dimension))) for dimension in dimensions)
        grouped.setdefault(key, []).append(group_index)
    return list(grouped.items())


def _plot_rows_html(
    plot_kind: str,
    condition_groups: Sequence[tuple[tuple[tuple[str, str], ...], list[int]]],
    summary: pd.DataFrame,
    detail_groups: dict[int, pd.DataFrame],
    images: dict[int, dict[str, tuple[str, str]]],
    family_label: str,
) -> str:
    rows: list[str] = []
    for condition, group_indices in condition_groups:
        condition_html = "".join(
            f'<span class="condition-chip"><b>{escape(name)}</b>: {escape(value)}</span>'
            for name, value in condition
            if value != "—"
        )
        if not condition_html:
            condition_html = '<span class="condition-chip">Shared conditions</span>'
        cards: list[str] = []
        ordered_indices = sorted(
            group_indices,
            key=lambda index: (
                0 if _insertion_information(summary.iloc[index], detail_groups[index])[0] == "FE" else 1,
                _natural_key(_insertion_information(summary.iloc[index], detail_groups[index])[1]),
                _natural_key(_insertion_information(summary.iloc[index], detail_groups[index])[2]),
            ),
        )
        for group_index in ordered_indices:
            _type, _insertion, _temperature, insertion_label = _insertion_information(
                summary.iloc[group_index], detail_groups[group_index]
            )
            image = images.get(group_index, {}).get(plot_kind)
            if image is None:
                image_html = '<div class="missing-plot">Plot unavailable</div>'
            else:
                _mime, data_uri = image
                alt = f"{plot_kind.title()} plot for {family_label}, {insertion_label}"
                image_html = (
                    f'<img src="{data_uri}" alt="{escape(alt)}" loading="lazy" decoding="async" '
                    'title="Click to enlarge">'
                )
            cards.append(
                f'<figure class="plot-card"><figcaption>{escape(insertion_label)}</figcaption>{image_html}</figure>'
            )
        rows.append(
            f'<div class="condition-row"><div class="condition-label">{condition_html}</div>'
            f'<div class="plot-row">{"".join(cards)}</div></div>'
        )
    return "".join(rows)


def _page_css() -> str:
    return """
:root { --blue:#174a7e; --blue2:#2f75b5; --green:#3f8f68; --gold:#d6a63a; --ink:#182433;
  --muted:#627184; --line:#d8e1ea; --paper:#fff; --wash:#f3f6f9; --danger:#a6212b; }
* { box-sizing:border-box; } html { scroll-behavior:smooth; }
body { margin:0; color:var(--ink); background:var(--wash); font:14px/1.45 "Segoe UI",Arial,sans-serif; }
a { color:var(--blue2); } header { color:#fff; background:linear-gradient(130deg,#10395f,#1c5f91 62%,#267556);
  padding:28px max(24px,calc((100vw - 1500px)/2)); box-shadow:0 3px 16px #102a3d44; }
.brand { display:flex; align-items:center; gap:16px; } .brand img { width:64px; height:64px; }
h1 { margin:0; font-size:30px; letter-spacing:.2px; } header p { margin:5px 0 0; color:#e9f4fa; }
.report-shell { max-width:1500px; margin:0 auto; padding:22px; }
.notice { margin:0 0 18px; padding:13px 16px; border-left:5px solid var(--gold); background:#fff8df; }
.overview-grid,.table-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; align-items:start; }
.table-card { background:var(--paper); border:1px solid var(--line); border-radius:10px; box-shadow:0 2px 9px #24384d12; overflow:hidden; }
.table-card h3 { margin:0; padding:12px 15px; color:#fff; background:var(--blue); font-size:15px; }
.table-scroll,.identity-scroll { overflow:auto; } table { width:100%; border-collapse:collapse; font-size:12.5px; }
th,td { padding:8px 10px; border:1px solid var(--line); text-align:left; vertical-align:top; white-space:nowrap; }
thead th { color:#fff; background:var(--blue2); position:sticky; top:0; z-index:1; } tbody th { color:#274b68; background:#eef4f8; }
.metadata th { width:38%; white-space:normal; } .metadata td { white-space:normal; }
.factor-value,.limit-value { color:#255d3f; background:#e2f0d9; font-weight:700; }
.invalid-row { background:#fff0f1; } .invalid-value { color:var(--danger); font-weight:800; }
.controls { position:sticky; top:0; z-index:20; display:flex; flex-wrap:wrap; gap:9px; align-items:center; margin:20px 0;
  padding:12px; background:#ffffffed; border:1px solid var(--line); border-radius:10px; backdrop-filter:blur(6px); }
.controls input { flex:1 1 340px; min-width:220px; padding:9px 11px; border:1px solid #aab8c7; border-radius:6px; }
button { padding:8px 12px; color:#fff; background:var(--blue); border:0; border-radius:6px; cursor:pointer; }
button:hover { background:var(--blue2); } .family-index { max-height:230px; overflow:auto; columns:3; padding:12px 24px; background:#fff;
  border:1px solid var(--line); border-radius:10px; } .family-index li { break-inside:avoid; margin:4px 0; }
.test-family { margin:18px 0; border:1px solid #b9c8d6; border-radius:12px; background:#fff; box-shadow:0 3px 12px #1a314617; overflow:hidden; }
.test-family>summary { cursor:pointer; padding:15px 18px; color:#fff; background:linear-gradient(90deg,var(--blue),#286e8f); font-size:16px; font-weight:700; }
.test-family[open]>summary { border-bottom:4px solid var(--gold); } .family-content { padding:18px; }
.family-heading { display:flex; flex-wrap:wrap; gap:8px; align-items:center; } .badge { display:inline-block; padding:4px 8px; border-radius:999px;
  color:#fff; background:var(--green); font-size:11px; font-weight:800; letter-spacing:.4px; }
.identity-table { margin:10px 0 16px; } .identity-table .test-number { font-weight:800; color:var(--blue); }
.section-note { color:var(--muted); margin:5px 0 13px; } h2 { margin-top:28px; color:#163f63; } h3.plot-heading { margin:24px 0 8px; color:#174a7e; }
.condition-row { margin:0 0 18px; border:1px solid var(--line); border-radius:9px; overflow:hidden; }
.condition-label { display:flex; flex-wrap:wrap; gap:6px; padding:8px 10px; background:#edf3f7; }
.condition-chip { padding:3px 7px; border-radius:5px; background:#fff; border:1px solid #ccd9e3; font-size:12px; }
.plot-row { display:grid; grid-auto-flow:column; grid-auto-columns:minmax(430px,1fr); gap:12px; overflow-x:auto; padding:12px; background:#fafcfd; }
.plot-card { margin:0; border:1px solid #ccd8e2; border-radius:8px; background:#fff; overflow:hidden; }
.plot-card figcaption { padding:8px 10px; color:#fff; background:#3f607a; font-weight:700; }
.plot-card img { display:block; width:100%; height:auto; cursor:zoom-in; } .missing-plot { min-height:240px; display:grid; place-items:center; color:var(--muted); }
footer { margin-top:32px; padding:20px; color:#536477; text-align:center; border-top:1px solid var(--line); }
#image-modal { display:none; position:fixed; inset:0; z-index:100; padding:28px; background:#07131de8; align-items:center; justify-content:center; }
#image-modal.open { display:flex; } #image-modal img { max-width:96vw; max-height:92vh; background:#fff; box-shadow:0 8px 32px #000; }
#image-modal button { position:fixed; top:16px; right:20px; font-size:18px; } .hidden-family { display:none; }
@media (max-width:850px) { .overview-grid,.table-grid { grid-template-columns:1fr; } .family-index { columns:1; }
  .plot-row { grid-auto-columns:minmax(340px,88vw); } }
@media print { body { background:#fff; font-size:10px; } header { print-color-adjust:exact; -webkit-print-color-adjust:exact; }
  .controls,.family-index,#image-modal { display:none!important; } .report-shell { max-width:none; padding:4px; }
  .test-family { break-before:page; box-shadow:none; } .test-family>summary { list-style:none; print-color-adjust:exact; -webkit-print-color-adjust:exact; }
  .test-family:not([open])>.family-content { display:block!important; } .plot-row { grid-auto-flow:row; grid-template-columns:repeat(2,1fr); overflow:visible; }
  .plot-card { break-inside:avoid; } .table-scroll,.identity-scroll { overflow:visible; } th,td { white-space:normal; } }
"""


def _page_javascript() -> str:
    return """
const families=[...document.querySelectorAll('.test-family')];
const search=document.getElementById('family-search');
function filterFamilies(){const q=search.value.trim().toLowerCase();let shown=0;families.forEach(f=>{const hit=!q||f.dataset.search.includes(q);f.classList.toggle('hidden-family',!hit);if(hit)shown++;});document.getElementById('visible-count').textContent=shown;}
search.addEventListener('input',filterFamilies);
document.getElementById('expand-all').addEventListener('click',()=>families.filter(f=>!f.classList.contains('hidden-family')).forEach(f=>f.open=true));
document.getElementById('collapse-all').addEventListener('click',()=>families.forEach(f=>f.open=false));
const modal=document.getElementById('image-modal'),modalImage=modal.querySelector('img');
document.addEventListener('click',event=>{const image=event.target.closest('.plot-card img');if(!image)return;modalImage.src=image.src;modalImage.alt=image.alt;modal.classList.add('open');});
function closeModal(){modal.classList.remove('open');modalImage.removeAttribute('src');}
modal.addEventListener('click',closeModal);modal.querySelector('button').addEventListener('click',closeModal);
document.addEventListener('keydown',event=>{if(event.key==='Escape')closeModal();});
"""


def write_html_report(
    result: CorrelationResult,
    profile: CorrelationProfile,
    output: Path,
    *,
    image_dpi: int = 90,
    image_quality: int = 72,
) -> int:
    """Write one self-contained offline HTML sign-off report and return its embedded plot count."""
    if image_dpi <= 0:
        raise ValueError("HTML image_dpi must be greater than zero")
    if not 1 <= image_quality <= 100:
        raise ValueError("HTML image_quality must be between 1 and 100")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    summary = _test_name_after_test_number(_canonical_test_name(result.summary, profile)).reset_index(drop=True)
    detail_groups = _group_details(result, profile)
    details = _test_name_after_test_number(_canonical_test_name(result.details, profile))
    if len(summary) != len(detail_groups):
        raise ValueError(
            f"HTML reporting found {len(summary)} summary groups but {len(detail_groups)} detail groups"
        )

    families: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()
    for group_index in sorted(detail_groups):
        if group_index < 0 or group_index >= len(summary):
            raise ValueError(f"GroupIndex {group_index} has no matching summary row")
        identities = _test_identities(detail_groups[group_index])
        key = _family_key(summary.iloc[group_index], identities)
        family = families.setdefault(key, {"identities": identities, "group_indices": []})
        family["group_indices"].append(group_index)

    images: dict[int, dict[str, tuple[str, str]]] = {}

    def capture_figure(
        group_index: int,
        _insertion_bucket_name: str,
        plot_kind: str,
        figure: Any,
        _summary: pd.Series,
        _details: pd.DataFrame,
        _title: str,
    ) -> None:
        images.setdefault(group_index, {})[plot_kind] = _figure_data_uri(
            figure, dpi=image_dpi, quality=image_quality
        )

    plot_count = _render_plot_figures(result, profile, capture_figure)

    strategies = sorted(
        {_display_value(value) for value in summary.get("CorrelationStrategy", pd.Series(dtype=object)) if not _is_missing(value)},
        key=_natural_key,
    )
    guard_policies = sorted(
        {_display_value(value) for value in summary.get("GuardBandPolicy", pd.Series(dtype=object)) if not _is_missing(value)},
        key=_natural_key,
    )
    all_identities = _test_identities(details)
    sample_counts = pd.to_numeric(summary.get("Count", pd.Series(dtype=float)), errors="coerce").dropna()
    pooled = sorted(
        {
            str(value).strip()
            for value in summary.get("PooledParameters", pd.Series(dtype=object))
            if not _is_missing(value) and str(value).strip()
        },
        key=_natural_key,
    )
    invalid_windows = int(summary.get("LimitWindowInvalid", pd.Series(False, index=summary.index)).fillna(False).astype(bool).sum())
    models = _model_names(details)
    covariate = (
        f"{profile.covariate.output_name} from test {profile.covariate.test_number}; merge keys: "
        f"{', '.join(profile.covariate.merge_keys)}"
        if profile.covariate is not None
        else "Not configured"
    )
    generated = datetime.now().astimezone().isoformat(timespec="seconds")
    overview_rows = [
        ("Correlation profile", profile.name),
        ("Generated", generated),
        ("Reference / Lab column", profile.reference_column),
        ("Candidate / ATE column", profile.candidate_column),
        ("Correlation strategies applied", ", ".join(strategies) or "—"),
        ("Guard-band policies applied", ", ".join(guard_policies) or "—"),
        ("Models rendered", ", ".join(models) or "—"),
        ("Affected individual tests", f"{len(all_identities):,}"),
        ("Test families / sign-off sections", f"{len(families):,}"),
        ("Correlation populations", f"{len(summary):,}"),
        ("Correlation rows", f"{len(details):,}"),
        (
            "Samples per population",
            (
                f"{int(sample_counts.min()):,}–{int(sample_counts.max()):,} "
                f"(median {float(sample_counts.median()):g})"
                if not sample_counts.empty else "—"
            ),
        ),
        ("Minimum points required", f"{profile.minimum_points:,}"),
        ("Grouping dimensions", ", ".join(profile.group_by) or "Fully pooled"),
        ("Detail keys", ", ".join(profile.detail_key_columns) or "None configured"),
        ("Merged / pooled parameters", "; ".join(pooled) or "None"),
        ("Physics/Kf source", covariate),
        ("Invalid correlated limit windows", f"{invalid_windows:,}"),
    ]
    if result.outlier_review is not None:
        review = result.outlier_review
        overview_rows.extend((
            ("Outlier detector", f"scaled MAD (1.4826 × MAD), n={review.threshold:g}"),
            ("Outlier filtering mode", "Explicit review only; no automatic exclusions"),
            ("Outlier samples flagged", f"{review.flagged_count:,}"),
            ("Flagged samples retained", f"{review.retained_flagged_count:,}"),
            ("Flagged samples excluded", f"{review.excluded_count:,}"),
            ("Rows before / after review", f"{review.original_row_count:,} / {review.final_row_count:,}"),
        ))
    else:
        overview_rows.append(("Outlier review", "Not attached to this result"))

    family_sections: list[str] = []
    family_index_items: list[str] = []
    for family_number, ((_test_set, identities), family) in enumerate(families.items(), start=1):
        group_indices: list[int] = family["group_indices"]
        family_summary = summary.iloc[group_indices]
        pooled_parameters = _pooled_parameters(family_summary)
        dimensions = _context_dimensions(profile, pooled_parameters)
        factor_rows, factor_columns = _factor_rows(group_indices, summary, detail_groups, dimensions)
        limit_rows, limit_columns = _limit_rows(group_indices, summary, detail_groups, dimensions)
        conditions = _condition_groups(group_indices, summary, dimensions)
        merged = bool(pooled_parameters)
        test_set = str(family_summary.iloc[0].get("TestSet", "") or "").strip()
        numbers = [number for number, _names in identities]
        all_names = [name for _number, names in identities for name in names]
        if merged:
            title = f"Merged test family · {len(identities)} tests"
        elif all_names:
            title = all_names[0]
        else:
            title = f"Test {numbers[0] if numbers else family_number}"
        family_label = f"{', '.join(numbers)} · {title}"
        anchor = f"test-family-{family_number:03d}"
        family_index_items.append(
            f'<li><a href="#{anchor}">{escape(", ".join(numbers))} · {escape(title)}</a></li>'
        )
        pooled_note = ""
        if merged:
            pooled_note = (
                '<span class="badge">MERGED / POOLED</span>'
                f'<p class="section-note">One shared factor and correlated limit population is used across '
                f'<b>{escape(", ".join(pooled_parameters))}</b>. Every contributing test remains listed below.</p>'
            )
        elif len(identities) > 1:
            pooled_note = '<span class="badge">MULTI-TEST FAMILY</span>'
        test_set_html = f'<span class="badge">{escape(test_set)}</span>' if test_set else ""
        family_strategies = [
            _display_value(value)
            for value in _unique_values(family_summary, "CorrelationStrategy")
        ]
        family_guard_policies = [
            _display_value(value)
            for value in _unique_values(family_summary, "GuardBandPolicy")
        ]
        search_text = " ".join(
            [test_set, *numbers, *all_names, *family_strategies, *family_guard_policies]
        ).casefold()
        model_plots = _plot_rows_html(
            "models", conditions, summary, detail_groups, images, family_label
        )
        series_plots = _plot_rows_html(
            "series", conditions, summary, detail_groups, images, family_label
        )
        family_sections.append(
            f'<details class="test-family" id="{anchor}" data-search="{escape(search_text, quote=True)}">'
            f'<summary>{escape(", ".join(numbers))} · {escape(title)} · {len(group_indices)} population(s)</summary>'
            '<div class="family-content">'
            f'<div class="family-heading">{pooled_note}{test_set_html}</div>'
            f'{_identity_table(identities)}'
            '<div class="table-grid">'
            f'{_table_html("Correlation factors by insertion", factor_rows, factor_columns, table_class="factor-table")}'
            f'{_table_html("Correlated limits by insertion", limit_rows, limit_columns, table_class="limit-table")}'
            '</div>'
            '<h3 class="plot-heading">Model plots by insertion</h3>'
            '<p class="section-note">All insertions for the same correlation conditions are kept on one horizontal review row. Click a plot to enlarge it.</p>'
            f'{model_plots}'
            '<h3 class="plot-heading">Series plots by insertion</h3>'
            f'{series_plots}'
            '</div></details>'
        )

    logo_uri = _logo_data_uri()
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="generator" content="CorreLaTE {escape(__version__)}">
<title>CorreLaTE sign-off · {escape(profile.name)}</title>
<style>{_page_css()}</style>
</head>
<body>
<header><div class="brand"><img src="{logo_uri}" alt="CorreLaTE Signal Bloom logo"><div>
<h1>CorreLaTE correlation sign-off</h1><p>{escape(profile.name)} · generated {escape(generated)}</p>
</div></div></header>
<main class="report-shell">
<div class="notice"><b>Sign-off scope.</b> This self-contained report is the human-review artifact. The companion Excel report remains the numerical authority for complete summary and row-level data. Images and styling are embedded; no network connection or external plot folder is required.</div>
<h2>High-level correlation information</h2>
<div class="overview-grid">{_metadata_table(overview_rows)}{_overview_dimensions(details, profile)}</div>
{('<h2>Pre-correlation outlier review</h2>' + _outlier_review_html(result.outlier_review)) if result.outlier_review is not None else ''}
<h2>Test-family review</h2>
<div class="controls"><input id="family-search" type="search" placeholder="Filter by test number, test name, test set, strategy, or policy…" aria-label="Filter test families"><button id="expand-all" type="button">Expand visible</button><button id="collapse-all" type="button">Collapse all</button><span><b id="visible-count">{len(families)}</b> / {len(families)} families</span></div>
<ol class="family-index">{''.join(family_index_items)}</ol>
{''.join(family_sections)}
</main>
<footer>CorreLaTE {escape(__version__)} · {escape(__author__)} · offline static sign-off report</footer>
<div id="image-modal" role="dialog" aria-modal="true" aria-label="Enlarged correlation plot"><button type="button" aria-label="Close image">Close ×</button><img alt=""></div>
<script>{_page_javascript()}</script>
</body>
</html>
"""
    output.write_text(html, encoding="utf-8", newline="\n")
    return plot_count
