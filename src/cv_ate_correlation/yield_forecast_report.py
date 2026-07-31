"""Self-contained HTML report for correlated productive-yield forecasts."""

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
from .models import CorrelationProfile
from .yield_forecast import YieldForecastResult


def _missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _display(value: Any) -> str:
    if _missing(value) or str(value).strip() == "":
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
    return tuple(
        (0, float(part)) if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", part) else (1, part.casefold())
        for part in re.split(r"([-+]?\d+(?:\.\d+)?)", _display(value))
        if part
    )


def _logo_data_uri() -> str:
    path = Path(__file__).resolve().with_name("assets") / "correlate-signal-bloom-64.png"
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _figure_data_uri(figure: Any, *, dpi: int, quality: int) -> str:
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
    return f"data:{mime};base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _render_cdf_images(
    result: YieldForecastResult,
    *,
    image_dpi: int,
    image_quality: int,
) -> dict[int, str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    images: dict[int, str] = {}
    for raw_index, raw_group in result.details.groupby("ForecastGroupIndex", sort=True):
        index = int(raw_index)
        summary = result.summary.loc[result.summary["ForecastGroupIndex"].eq(index)].iloc[0]
        group = raw_group.sort_values("ForecastCorrelatedValue", kind="stable")
        values = pd.to_numeric(group["ForecastCorrelatedValue"], errors="coerce").to_numpy(dtype=float)
        ranks = (pd.Series(range(1, len(group) + 1), dtype=float) - 0.5) / len(group) * 100.0
        fails = group["ForecastFail"].astype(bool).to_numpy()
        fig, axis = plt.subplots(figsize=(6.2, 4.5))
        if (~fails).any():
            axis.scatter(
                values[~fails], ranks[~fails], s=22, color="#2f75b5", alpha=0.86,
                edgecolors="white", linewidths=0.35, label="PASS samples", zorder=3,
            )
        if fails.any():
            axis.scatter(
                values[fails], ranks[fails], s=38, color="#c62828", marker="X",
                edgecolors="#7f0000", linewidths=0.6, label="FAIL samples", zorder=5,
            )
        lower = float(summary["ForecastLowerLimit"])
        upper = float(summary["ForecastUpperLimit"])
        axis.axvline(lower, color="#d68b00", linestyle="--", linewidth=2.0, label=f"Correlated LTL = {lower:.5g}")
        axis.axvline(upper, color="#2e7d32", linestyle="--", linewidth=2.0, label=f"Correlated UTL = {upper:.5g}")
        unit = str(summary.get("Unit", "") or "").strip()
        axis.set_xlabel(f"Forecast correlated value [{unit}]" if unit else "Forecast correlated value")
        axis.set_ylabel("Empirical CDF [%]")
        axis.set_ylim(-2.0, 102.0)
        plotted_x = [*values.tolist(), lower, upper]
        finite_x = [value for value in plotted_x if math.isfinite(value)]
        if finite_x:
            low, high = min(finite_x), max(finite_x)
            padding = 0.06 * (high - low) if high > low else abs(low) * 0.06 + 1.0
            axis.set_xlim(low - padding, high + padding)
        axis.grid(True, alpha=0.25)
        axis.set_title(
            f"{_display(summary.get('Insertion'))} · {_display(summary.get('Insertion Type'))} · "
            f"{_display(summary.get('Temperature'))} °C\n"
            f"Yield {float(summary['YieldPercent']):.4g}% · {int(summary['FailCount'])} FAIL / {int(summary['SampleCount'])}"
        )
        axis.legend(fontsize=8, framealpha=0.94, loc="best")
        fig.tight_layout()
        try:
            images[index] = _figure_data_uri(fig, dpi=image_dpi, quality=image_quality)
        finally:
            plt.close(fig)
    return images


def _condition_dimensions(profile: CorrelationProfile) -> tuple[str, ...]:
    excluded = {"Test Number", "Insertion", "Insertion Type", "Temperature"}
    return tuple(column for column in profile.group_by if column not in excluded)


def _condition_key(row: pd.Series, dimensions: Sequence[str]) -> tuple[tuple[str, str], ...]:
    return tuple((column, _display(row.get(column))) for column in dimensions)


def _insertion_key(row: pd.Series, insertion_order: dict[str, int]) -> tuple[Any, ...]:
    insertion = str(row.get("Insertion", "") or "")
    insertion_type = str(row.get("Insertion Type", "") or "").upper()
    return (
        insertion_order.get(insertion, 10_000),
        0 if insertion_type == "FE" else 1 if insertion_type == "BE" else 2,
        _natural_key(insertion),
        _natural_key(row.get("Temperature")),
    )


def _statistics_table(rows: pd.DataFrame, dimensions: Sequence[str]) -> str:
    columns = [
        ("Insertion", "Insertion"),
        ("Insertion Type", "Type"),
        ("Temperature", "Temperature [°C]"),
        *((dimension, dimension) for dimension in dimensions),
        ("SampleCount", "Samples"),
        ("YieldPercent", "Yield [%]"),
        ("PassCount", "PASS"),
        ("FailCount", "FAIL"),
        ("LowerFailCount", "Below LTL"),
        ("UpperFailCount", "Above UTL"),
        ("ForecastMean", "Mean"),
        ("ForecastStd", "Std"),
        ("ForecastMinimum", "Min"),
        ("ForecastP01", "P01"),
        ("ForecastMedian", "Median"),
        ("ForecastP99", "P99"),
        ("ForecastMaximum", "Max"),
        ("ForecastCpk", "Cpk"),
        ("ForecastLowerLimit", "Correlated LTL"),
        ("ForecastUpperLimit", "Correlated UTL"),
        ("ForecastLimitWindowInvalid", "Invalid limit window"),
        ("Unit", "Unit"),
    ]
    header = "".join(f"<th>{escape(label)}</th>" for _key, label in columns)
    body: list[str] = []
    for _index, row in rows.iterrows():
        failed = int(row["FailCount"]) > 0
        cells = []
        for key, _label in columns:
            css = ""
            if key == "YieldPercent":
                css = ' class="yield-fail"' if failed else ' class="yield-pass"'
            elif key == "FailCount" and failed:
                css = ' class="yield-fail"'
            elif key == "ForecastLimitWindowInvalid" and bool(row.get(key)):
                css = ' class="yield-fail"'
            cells.append(f"<td{css}>{escape(_display(row.get(key)))}</td>")
        body.append(f'<tr class="{"failed-row" if failed else ""}">{"".join(cells)}</tr>')
    return (
        '<div class="table-card"><h3>Forecast statistics and correlated limits</h3>'
        '<div class="table-scroll"><table><thead><tr>' + header + "</tr></thead><tbody>" +
        "".join(body) + "</tbody></table></div></div>"
    )


def _metadata_table(rows: Sequence[tuple[str, str]]) -> str:
    return (
        '<div class="table-card"><h3>Forecast scope</h3><table class="metadata"><tbody>' +
        "".join(
            f'<tr><th scope="row">{escape(label)}</th><td>{escape(value)}</td></tr>'
            for label, value in rows
        ) + "</tbody></table></div>"
    )


def _css() -> str:
    return """
:root { --blue:#174a7e; --blue2:#2f75b5; --green:#2e7d32; --gold:#d68b00; --red:#a6212b;
  --ink:#182433; --muted:#627184; --line:#d8e1ea; --paper:#fff; --wash:#f3f6f9; }
* { box-sizing:border-box; } body { margin:0; color:var(--ink); background:var(--wash);
  font:14px/1.45 "Segoe UI",Arial,sans-serif; }
a { color:var(--blue2); } header { color:#fff; background:linear-gradient(130deg,#10395f,#1c5f91 62%,#267556);
  padding:28px max(24px,calc((100vw - 1550px)/2)); box-shadow:0 3px 16px #102a3d44; }
.brand { display:flex; align-items:center; gap:16px; } .brand img { width:64px; height:64px; }
h1 { margin:0; font-size:30px; } header p { margin:5px 0 0; color:#e9f4fa; }
.report-shell { max-width:1550px; margin:0 auto; padding:22px; }
.notice { margin:0 0 18px; padding:13px 16px; border-left:5px solid var(--gold); background:#fff8df; }
.overview-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; }
.table-card { background:#fff; border:1px solid var(--line); border-radius:10px; overflow:hidden; box-shadow:0 2px 9px #24384d12; }
.table-card h3 { margin:0; padding:12px 15px; color:#fff; background:var(--blue); font-size:15px; }
.table-scroll { overflow:auto; } table { width:100%; border-collapse:collapse; font-size:12.5px; }
th,td { padding:8px 10px; border:1px solid var(--line); text-align:left; white-space:nowrap; }
thead th { color:#fff; background:var(--blue2); position:sticky; top:0; } tbody th { color:#274b68; background:#eef4f8; }
.metadata th { width:40%; white-space:normal; } .metadata td { white-space:normal; }
.controls { position:sticky; top:0; z-index:20; display:flex; gap:9px; align-items:center; margin:20px 0; padding:12px;
  background:#ffffffed; border:1px solid var(--line); border-radius:10px; }
.controls input { flex:1; min-width:220px; padding:9px 11px; border:1px solid #aab8c7; border-radius:6px; }
button { padding:8px 12px; color:#fff; background:var(--blue); border:0; border-radius:6px; cursor:pointer; }
.test-index { columns:3; padding:12px 28px; background:#fff; border:1px solid var(--line); border-radius:10px; }
.test-index li { margin:5px 0; break-inside:avoid; } .fail-link { color:var(--red); font-weight:800; }
.test-family { margin:18px 0; border:1px solid #b9c8d6; border-radius:12px; background:#fff; overflow:hidden; box-shadow:0 3px 12px #1a314617; }
.test-family>summary { cursor:pointer; padding:15px 18px; color:#fff; background:linear-gradient(90deg,var(--blue),#286e8f); font-size:16px; font-weight:700; }
.test-family.fail-family { border:3px solid var(--red); } .test-family.fail-family>summary { background:linear-gradient(90deg,#851b24,#c43c35); }
.family-content { padding:18px; } .badge { display:inline-block; margin:0 7px 10px 0; padding:4px 9px; border-radius:999px; color:#fff; font-weight:800; }
.fail-badge { background:var(--red); } .pass-badge { background:var(--green); } .strategy-badge { background:var(--blue2); }
.failed-row { background:#fff0f1; } .yield-fail { color:#8a1019; background:#ffd9dc; font-weight:800; }
.yield-pass { color:#215d35; background:#e2f0d9; font-weight:800; }
.condition-row { margin:18px 0; border:1px solid var(--line); border-radius:9px; overflow:hidden; }
.condition-label { display:flex; gap:7px; padding:8px 10px; background:#edf3f7; }
.condition-chip { padding:3px 7px; background:#fff; border:1px solid #ccd9e3; border-radius:5px; }
.plot-row { display:grid; grid-auto-flow:column; grid-auto-columns:minmax(470px,1fr); gap:12px; overflow-x:auto; padding:12px; }
.plot-card { margin:0; border:1px solid #ccd8e2; border-radius:8px; overflow:hidden; background:#fff; }
.plot-card.fail-card { border:3px solid var(--red); } .plot-card figcaption { padding:8px 10px; color:#fff; background:#3f607a; font-weight:700; }
.plot-card.fail-card figcaption { background:#a6212b; } .plot-card img { width:100%; display:block; cursor:zoom-in; }
.hidden-family { display:none; } footer { padding:22px; color:var(--muted); text-align:center; border-top:1px solid var(--line); }
#image-modal { display:none; position:fixed; inset:0; z-index:100; padding:28px; background:#07131de8; align-items:center; justify-content:center; }
#image-modal.open { display:flex; } #image-modal img { max-width:96vw; max-height:92vh; background:#fff; } #image-modal button { position:fixed; top:16px; right:20px; }
@media(max-width:850px){.overview-grid{grid-template-columns:1fr}.test-index{columns:1}.plot-row{grid-auto-columns:minmax(350px,88vw)}}
@media print {.controls,.test-index,#image-modal{display:none!important}.test-family{break-before:page}.plot-row{grid-auto-flow:row;grid-template-columns:repeat(2,1fr);overflow:visible}}
"""


def _javascript() -> str:
    return """
const families=[...document.querySelectorAll('.test-family')],search=document.getElementById('test-search');
function filter(){const q=search.value.trim().toLowerCase();let n=0;families.forEach(f=>{const hit=!q||f.dataset.search.includes(q);f.classList.toggle('hidden-family',!hit);if(hit)n++;});document.getElementById('visible-count').textContent=n;}
search.addEventListener('input',filter);document.getElementById('expand-all').onclick=()=>families.filter(f=>!f.classList.contains('hidden-family')).forEach(f=>f.open=true);document.getElementById('collapse-all').onclick=()=>families.forEach(f=>f.open=false);
const modal=document.getElementById('image-modal'),image=modal.querySelector('img');document.addEventListener('click',e=>{const target=e.target.closest('.plot-card img');if(!target)return;image.src=target.src;image.alt=target.alt;modal.classList.add('open');});function close(){modal.classList.remove('open');image.removeAttribute('src');}modal.onclick=close;modal.querySelector('button').onclick=close;document.addEventListener('keydown',e=>{if(e.key==='Escape')close();});
"""


def write_yield_forecast_html(
    result: YieldForecastResult,
    profile: CorrelationProfile,
    output: Path,
    *,
    image_dpi: int = 90,
    image_quality: int = 72,
) -> int:
    """Write a static yield-forecast report and return the embedded CDF plot count."""
    if result.summary.empty or result.details.empty:
        raise ValueError("Yield forecast contains no tests to report")
    if image_dpi <= 0:
        raise ValueError("HTML image_dpi must be greater than zero")
    if not 1 <= image_quality <= 100:
        raise ValueError("HTML image_quality must be between 1 and 100")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    images = _render_cdf_images(result, image_dpi=image_dpi, image_quality=image_quality)
    summary = result.summary.copy()
    dimensions = _condition_dimensions(profile)
    insertion_names = list(dict.fromkeys(str(value) for value in summary.get("Insertion", [])))
    insertion_order = {name: index for index, name in enumerate(insertion_names)}

    families: OrderedDict[tuple[str, str], list[int]] = OrderedDict()
    for index, row in summary.iterrows():
        key = (_display(row.get("Test Number")), _display(row.get("Test Name")))
        families.setdefault(key, []).append(int(index))

    total_samples = int(summary["SampleCount"].sum())
    rejected_samples = len(result.rejected)
    supplied_samples = total_samples + rejected_samples
    total_fails = int(summary["FailCount"].sum())
    total_passes = total_samples - total_fails
    affected_tests = int(summary.loc[summary["FailCount"].gt(0), "Test Number"].nunique())
    overall_yield = 100.0 * total_passes / total_samples
    generated = datetime.now().astimezone().isoformat(timespec="seconds")
    metadata = [
        ("Correlation profile", profile.name),
        ("Generated", generated),
        ("Extracted productive rows", f"{supplied_samples:,}"),
        ("Productive samples", f"{total_samples:,}"),
        ("Skipped blank/non-numeric rows", f"{rejected_samples:,}"),
        ("Overall forecast yield", f"{overall_yield:.6g}%"),
        ("Forecast PASS / FAIL", f"{total_passes:,} / {total_fails:,}"),
        ("Tests evaluated", f"{len(families):,}"),
        ("Tests with at least one FAIL", f"{affected_tests:,}"),
        ("Selected insertions", ", ".join(insertion_names)),
        ("Factor source", "Approved Correlation_Summary from Section 5"),
        ("Yield rule", "Inclusive correlated limits: LTL ≤ correlated productive value ≤ UTL"),
    ]
    rejected_notice = ""
    if rejected_samples:
        reason_counts = result.rejected.get(
            "ForecastRejectionReason", pd.Series(dtype="object")
        ).value_counts()
        reasons = ", ".join(
            f"{reason}: {int(count):,}" for reason, count in reason_counts.items()
        )
        rejected_notice = (
            '<div class="notice"><b>Input quality warning.</b> '
            f'{rejected_samples:,} extracted row(s) with blank or non-numeric productive ATE values '
            'were excluded—not converted to zero—and are not included in forecast yield. '
            f'{escape(reasons)}.</div>'
        )

    sections: list[str] = []
    index_items: list[str] = []
    for family_number, ((number, name), row_indices) in enumerate(families.items(), start=1):
        rows = summary.loc[row_indices].copy()
        family_fails = int(rows["FailCount"].sum())
        family_samples = int(rows["SampleCount"].sum())
        family_yield = 100.0 * (family_samples - family_fails) / family_samples
        failed = family_fails > 0
        anchor = f"forecast-test-{family_number:04d}"
        label = f"{number} · {name}" if name != "—" else f"Test {number}"
        index_items.append(
            f'<li><a class="{"fail-link" if failed else ""}" href="#{anchor}">{escape(label)}'
            f' · {family_fails} FAIL</a></li>'
        )
        condition_groups: OrderedDict[tuple[tuple[str, str], ...], list[int]] = OrderedDict()
        for row_index in row_indices:
            condition_groups.setdefault(_condition_key(summary.loc[row_index], dimensions), []).append(row_index)
        plot_rows: list[str] = []
        for condition, condition_indices in condition_groups.items():
            chips = "".join(
                f'<span class="condition-chip"><b>{escape(column)}</b>: {escape(value)}</span>'
                for column, value in condition
            ) or '<span class="condition-chip">Shared test conditions</span>'
            cards: list[str] = []
            ordered = sorted(
                condition_indices,
                key=lambda index: _insertion_key(summary.loc[index], insertion_order),
            )
            for row_index in ordered:
                row = summary.loc[row_index]
                group_index = int(row["ForecastGroupIndex"])
                row_failed = int(row["FailCount"]) > 0
                insertion_label = (
                    f"{_display(row.get('Insertion'))} · {_display(row.get('Insertion Type'))} · "
                    f"{_display(row.get('Temperature'))} °C · {_display(row.get('YieldPercent'))}% yield · "
                    f"{int(row['FailCount'])} FAIL"
                )
                cards.append(
                    f'<figure class="plot-card {"fail-card" if row_failed else ""}">'
                    f'<figcaption>{escape(insertion_label)}</figcaption>'
                    f'<img src="{images[group_index]}" alt="CDF plot for {escape(label)}, {escape(insertion_label)}" '
                    'loading="lazy" decoding="async" title="Click to enlarge"></figure>'
                )
            plot_rows.append(
                f'<div class="condition-row"><div class="condition-label">{chips}</div>'
                f'<div class="plot-row">{"".join(cards)}</div></div>'
            )
        rows = rows.sort_values(
            by=[column for column in ("Insertion", "Temperature") if column in rows.columns],
            kind="stable",
        )
        badges = (
            f'<span class="badge {"fail-badge" if failed else "pass-badge"}">'
            f'{"FAIL" if failed else "PASS"} · yield {family_yield:.6g}% · {family_fails} fail(s)</span>'
            f'<span class="badge strategy-badge">'
            f'{escape(", ".join(sorted(set(rows["CorrelationStrategy"].astype(str)))))}'
            f'</span>'
        )
        sections.append(
            f'<details class="test-family {"fail-family" if failed else ""}" id="{anchor}" '
            f'data-search="{escape((label + " " + str(family_fails)).casefold(), quote=True)}" '
            f'{"open" if failed else ""}><summary>{escape(label)} · yield {family_yield:.6g}% · '
            f'{family_fails} FAIL / {family_samples}</summary><div class="family-content">{badges}'
            f'{_statistics_table(rows, dimensions)}<h3>CDF distributions by insertion</h3>'
            '<p>Every marker represents one productive sample after applying the approved correlation factor. '
            'FAIL markers are highlighted in red; insertions sharing test conditions are aligned horizontally.</p>'
            f'{"".join(plot_rows)}</div></details>'
        )

    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="generator" content="CorreLaTE {escape(__version__)}"><title>CorreLaTE yield forecast · {escape(profile.name)}</title>
<style>{_css()}</style></head><body>
<header><div class="brand"><img src="{_logo_data_uri()}" alt="CorreLaTE Signal Bloom logo"><div>
<h1>CorreLaTE correlated yield forecast</h1><p>{escape(profile.name)} · generated {escape(generated)}</p></div></div></header>
<main class="report-shell"><div class="notice"><b>Forecast scope.</b> Productive ATE samples were transformed with the approved Section 5 correlation factors and checked against the corresponding correlated limits. This is a deterministic forecast from the supplied samples—not a guarantee of future manufacturing yield.</div>
{rejected_notice}
<div class="overview-grid">{_metadata_table(metadata)}<div class="table-card"><h3>Review priority</h3><table class="metadata"><tbody>
<tr><th>Tests requiring attention</th><td class="{'yield-fail' if affected_tests else 'yield-pass'}">{affected_tests:,}</td></tr>
<tr><th>Failing samples</th><td class="{'yield-fail' if total_fails else 'yield-pass'}">{total_fails:,}</td></tr>
<tr><th>Interpretation</th><td>Red test sections and red CDF markers identify at least one forecast FAIL.</td></tr>
</tbody></table></div></div>
<div class="controls"><input id="test-search" type="search" placeholder="Filter by test number or name…"><button id="expand-all" type="button">Expand visible</button><button id="collapse-all" type="button">Collapse all</button><span><b id="visible-count">{len(families)}</b> / {len(families)} tests</span></div>
<ol class="test-index">{''.join(index_items)}</ol>{''.join(sections)}</main>
<footer>CorreLaTE {escape(__version__)} · {escape(__author__)} · offline static correlated-yield forecast</footer>
<div id="image-modal" role="dialog" aria-modal="true"><button type="button">Close ×</button><img alt=""></div>
<script>{_javascript()}</script></body></html>
"""
    output.write_text(html, encoding="utf-8", newline="\n")
    return len(images)
