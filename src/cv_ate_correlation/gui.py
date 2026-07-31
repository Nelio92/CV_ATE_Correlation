"""Lightweight desktop interface for all shared CV/ATE workflows."""

from __future__ import annotations

import queue
import math
import re
import threading
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Mapping

import pandas as pd

# VS Code's "Run Python File" executes this file by path, outside its package.
# Add the src directory in that mode so absolute package imports still resolve.
if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cv_ate_correlation import __author__, __version__
from cv_ate_correlation.correlation import attach_covariate_from_test_rows, correlate_frame
from cv_ate_correlation.excel import write_dataframe_workbook
from cv_ate_correlation.extraction import LegacyWideTeCsvAdapter
from cv_ate_correlation.handoff import (
    MANIFEST_SHEET,
    REQUEST_SHEET,
    create_measurement_request,
    import_measurement_results,
)
from cv_ate_correlation.html_report import write_html_report
from cv_ate_correlation.models import (
    DEFAULT_COORDINATE_FALLBACK,
    normalize_correlation_strategy,
    normalize_guard_band_kind,
)
from cv_ate_correlation.outliers import (
    DEFAULT_MAD_THRESHOLD,
    OUTLIER_FLAGGED_SERIES,
    OUTLIER_INPUT_ROW,
    OUTLIER_MAX_SCORE,
    OUTLIER_REASON,
    OUTLIER_ROW_ID,
    OutlierAnalysis,
    OutlierReview,
    analyze_outliers,
    attach_outlier_audit,
    finalize_outlier_review,
)
from cv_ate_correlation.profile_store import (
    delete_custom_profile,
    load_custom_profile_specs,
    parse_test_selector,
    profile_store_path,
    save_custom_profile_spec,
)
from cv_ate_correlation.profiles_8188 import (
    CORRELATION_PROFILES,
    EXTRACTION_PROFILES,
    builtin_profile_ids,
    get_correlation_profile,
    get_extraction_profile,
    refresh_profiles,
)
from cv_ate_correlation.reporting import write_excel_report
from cv_ate_correlation.yield_forecast import (
    forecast_yield,
    load_productive_csv_inputs,
    validate_productive_insertion_inputs,
)
from cv_ate_correlation.yield_forecast_report import write_yield_forecast_html


Action = Callable[[], Any]
SuccessHandler = Callable[[Any], None]
APPLICATION_TITLE = "CorreLaTE: ATE-to-Lab Correlation"
APPLICATION_AUTHOR = __author__
APPLICATION_VERSION = __version__
LOGO_ASSET_SIZES = (64, 256)


def logo_asset_path(size: int = 64) -> Path:
    """Return the packaged Signal Bloom logo at a supported pixel size."""
    if size not in LOGO_ASSET_SIZES:
        raise ValueError(f"Unsupported logo size {size}; choose one of {LOGO_ASSET_SIZES}")
    return Path(__file__).resolve().with_name("assets") / f"correlate-signal-bloom-{size}.png"


def about_information() -> tuple[tuple[str, str], ...]:
    """Return the facts displayed in the About section."""
    return (
        ("Version", APPLICATION_VERSION),
        ("Author", APPLICATION_AUTHOR),
        ("Interface", "Six-step desktop workflow and command-line interface using one shared engine"),
        ("Correlation models", "Linear (OLS), Mean_Deltas, Median_Deltas, and Physics-based with automatic Kf"),
        ("Guard-band policies", "distribution_sigma, max_residuals, and mean_deltas"),
        (
            "Outlier handling",
            "Pre-fit scaled-MAD review (default n=12); findings are retained unless the user explicitly enables and selects exclusions",
        ),
        (
            "Reports",
            "Focused Excel factors and guard bands, a self-contained HTML sign-off report, and a correlated productive-yield forecast HTML",
        ),
        (
            "Visual identity",
            "Signal Bloom — blue ATE and green Lab petals converge around a golden fitted path. "
            "The bloom represents scattered measurements becoming one coherent correlated result; "
            "the white points emphasize transparent, traceable data.",
        ),
    )


@dataclass(frozen=True)
class GroupingConditionOption:
    key: str
    label: str
    default_column: str
    aliases: tuple[str, ...]
    hint: str


GROUPING_CONDITION_OPTIONS = (
    GroupingConditionOption(
        "dut_nr", "DUT Nr", "DUT Nr", (),
        "One group per device; column example: DUT Nr, values: 1, 2, 17.",
    ),
    GroupingConditionOption(
        "test_number", "Test Number", "Test Number", (),
        "One group per test; column example: Test Number, value: 53171.",
    ),
    GroupingConditionOption(
        "frequency", "Frequency", "Frequency_GHz", ("Frequency",),
        "Separate frequencies; column example: Frequency_GHz, values: 76.5, 77, 81.",
    ),
    GroupingConditionOption(
        "supply_corner", "Supply Corner", "Voltage corner", ("Supply Corner",),
        "Separate supply corners; column example: Voltage corner, values: VMIN, VNOM, VMAX.",
    ),
    GroupingConditionOption(
        "channel", "Channel", "Channel", ("PA Channel",),
        "Separate physical channels; column example: Channel, values: TX1, TX2, CH3.",
    ),
    GroupingConditionOption(
        "digital_control", "Digital Control", "Digital Control", ("LUT value", "LO IDAC"),
        "Separate digital settings; column examples: LUT value or LO IDAC, values: 0, 112, 255.",
    ),
)

GROUPING_SOURCES = {
    "Existing input column": "existing",
    "File name": "filename",
    "Test name": "test_name",
}
GROUPING_METHODS = {
    "Use existing column": "existing",
    "Text mappings (no regex)": "mapping",
    "Number after prefix (no regex)": "number_after",
    "Advanced regex": "regex",
}
GROUPING_VALUE_TYPES = {"Text": "str", "Integer": "int", "Decimal": "float"}
CORRELATION_STRATEGY_EXPLANATIONS = {
    "Linear": "OLS fit: CV_pred = a × ATE + b; correlation factors are a and b",
    "Mean_Deltas": "CV_pred = ATE + mean(CV − ATE)",
    "Median_Deltas": "CV_pred = ATE + median(CV − ATE)",
    "Physics-based": "CV_pred = ATE − (alpha × Kf + beta)",
}
GUARD_BAND_EXPLANATIONS = {
    "distribution_sigma": "limits = mean(corrected ATE) ± k × σ(corrected ATE)",
    "max_residuals": "new LTL = REQ_MIN + |max residual|; new UTL = REQ_MAX − |max residual|",
    "mean_deltas": "new LTL = REQ_MIN + |mean(CV − ATE)|; new UTL = REQ_MAX − |mean(CV − ATE)|",
}


class ScrollablePage(ttk.Frame):
    """Notebook page with a vertical scrollbar and mouse-wheel support."""

    def __init__(self, parent: tk.Misc, *, padding: int = 0) -> None:
        super().__init__(parent)
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        background = ttk.Style(self).lookup("TFrame", "background") or "#f0f0f0"
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, background=background)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.content = ttk.Frame(self.canvas, padding=padding)
        self._window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.content.bind("<Configure>", self._update_scroll_region)
        self.canvas.bind("<Configure>", self._resize_content)
        self.bind_all("<MouseWheel>", self._on_mousewheel, add="+")

    def _update_scroll_region(self, _event: object | None = None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _resize_content(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)

    def _on_mousewheel(self, event: tk.Event) -> None:
        widget = self.winfo_containing(event.x_root, event.y_root)
        while widget is not None and widget is not self:
            widget = widget.master
        if widget is self and event.delta:
            self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")


def _split_group_columns(value: Any) -> tuple[str, ...]:
    return tuple(
        item.strip()
        for item in str(value or "").replace("\n", ",").split(",")
        if item.strip()
    )


def insertion_definitions(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return editable insertion definitions from a persisted profile spec."""
    raw_insertions = spec.get("insertions", [])
    if not isinstance(raw_insertions, list):
        return []
    return [
        {
            "name": str(item.get("name", "")),
            "group": str(item.get("group", "FE")).upper(),
            "temperature": str(item.get("temperature", "")),
            "raw_files": [str(path) for path in item.get("raw_files", [])],
        }
        for item in raw_insertions
        if isinstance(item, dict)
    ]


def validate_insertion_definitions(definitions: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Validate insertion identity and guarantee at least one existing raw data file per insertion."""
    if not definitions:
        raise ValueError("Add at least one insertion")
    validated: list[dict[str, Any]] = []
    names: set[str] = set()
    assigned_files: dict[Path, str] = {}
    for index, definition in enumerate(definitions, start=1):
        name = str(definition.get("name", "")).strip()
        group = str(definition.get("group", "")).strip().upper()
        if not name:
            raise ValueError(f"Insertion {index} needs a name")
        if name.casefold() in names:
            raise ValueError(f"Insertion name '{name}' is duplicated")
        names.add(name.casefold())
        if group not in {"FE", "BE"}:
            raise ValueError(f"Insertion '{name}' must use insertion group FE or BE")
        try:
            temperature = float(str(definition.get("temperature", "")).strip())
        except ValueError as error:
            raise ValueError(f"Insertion '{name}' needs a numeric temperature in °C") from error
        if not math.isfinite(temperature):
            raise ValueError(f"Insertion '{name}' needs a finite numeric temperature in °C")
        raw_files = [Path(str(path)).expanduser().resolve() for path in definition.get("raw_files", [])]
        if not raw_files:
            raise ValueError(f"Insertion '{name}' needs at least one corresponding raw test-data file")
        missing = [str(path) for path in raw_files if not path.is_file()]
        if missing:
            raise ValueError(f"Insertion '{name}' has missing raw test-data files: {missing}")
        for path in raw_files:
            previous = assigned_files.get(path)
            if previous is not None:
                raise ValueError(f"Raw file '{path.name}' is assigned to both '{previous}' and '{name}'")
            assigned_files[path] = name
        validated.append({
            "name": name,
            "group": group,
            "temperature": temperature,
            "raw_files": [str(path) for path in raw_files],
        })
    return validated


def coordinate_fallback_values(spec: Mapping[str, Any]) -> dict[str, str]:
    """Load the three editable BE FUSE coordinate test numbers from a profile spec."""
    values = dict(DEFAULT_COORDINATE_FALLBACK)
    raw_mapping = spec.get("coordinate_fallback", "")
    if isinstance(raw_mapping, Mapping):
        items = raw_mapping.items()
    else:
        items = []
        for token in re.split(r"[,\n]", str(raw_mapping)):
            if "=" in token:
                key, value = token.split("=", 1)
                items.append((key, value))
    for key, value in items:
        coordinate = str(key).strip().upper()
        if coordinate in values and str(value).strip():
            values[coordinate] = str(value).strip()
    return values


def compile_coordinate_fallback(values: Mapping[str, Any]) -> str:
    """Validate GUI entries and serialize the BE FUSE coordinate fallback mapping."""
    normalized: dict[str, str] = {}
    for coordinate in DEFAULT_COORDINATE_FALLBACK:
        value = str(values.get(coordinate, "")).strip()
        match = re.fullmatch(r"(?:TN\s*)?(\d+)", value, flags=re.IGNORECASE)
        if match is None:
            raise ValueError(
                f"BE {coordinate} fallback must be a numeric test number; for example: "
                f"{DEFAULT_COORDINATE_FALLBACK[coordinate]}"
            )
        normalized[coordinate] = match.group(1)
    return ", ".join(f"{coordinate}={value}" for coordinate, value in normalized.items())


def test_set_definitions(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Load persisted test sets or migrate the former profile-wide policy fields."""
    saved = spec.get("test_sets")
    if isinstance(saved, list) and saved:
        definitions = [dict(item) for item in saved if isinstance(item, dict)]
    else:
        definitions = [{
        "name": "Tests 1",
        "tests": str(spec.get("tests", "")),
        "strategy": str(spec.get("strategy", "Median_Deltas")),
        "guard_band_kind": str(spec.get("guard_band_kind", "distribution_sigma")),
        "sigma_multiplier": str(spec.get("sigma_multiplier", "6.0")),
        "requirement_min": str(spec.get("requirement_min", "")),
        "requirement_max": str(spec.get("requirement_max", "")),
        "pooled_columns": str(spec.get("pooled_columns", "")),
        }]
    for definition in definitions:
        try:
            definition["strategy"] = normalize_correlation_strategy(
                str(definition.get("strategy", "Median_Deltas"))
            )
        except ValueError:
            pass
        try:
            definition["guard_band_kind"] = normalize_guard_band_kind(
                str(definition.get("guard_band_kind", "distribution_sigma")),
                migrate_legacy_shifted=True,
            )
        except ValueError:
            pass
    return definitions


def validate_test_set_definitions(definitions: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Validate test selectors and their independently selected calculation policies."""
    if not definitions:
        raise ValueError("Add at least one test set")
    validated: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, definition in enumerate(definitions, start=1):
        name = str(definition.get("name", "")).strip() or f"Tests {index}"
        if name.casefold() in names:
            raise ValueError(f"Test set name '{name}' is duplicated")
        names.add(name.casefold())
        tests = str(definition.get("tests", "")).strip()
        parse_test_selector(tests)
        try:
            strategy = normalize_correlation_strategy(str(definition.get("strategy", "")))
        except ValueError as error:
            raise ValueError(f"Test set '{name}' has an invalid correlation strategy") from error
        if strategy not in CORRELATION_STRATEGY_EXPLANATIONS:
            raise ValueError(f"Test set '{name}' has an invalid correlation strategy")
        try:
            guard_kind = normalize_guard_band_kind(
                str(definition.get("guard_band_kind", "")),
                migrate_legacy_shifted=True,
            )
        except ValueError as error:
            raise ValueError(f"Test set '{name}' has an invalid guard-band policy") from error
        if guard_kind not in GUARD_BAND_EXPLANATIONS:
            raise ValueError(f"Test set '{name}' has an invalid guard-band policy")
        try:
            sigma_multiplier = float(str(definition.get("sigma_multiplier", "")).strip())
        except ValueError as error:
            raise ValueError(f"Test set '{name}' needs a numeric sigma multiplier") from error
        if not math.isfinite(sigma_multiplier) or sigma_multiplier <= 0:
            raise ValueError(f"Test set '{name}' sigma multiplier must be positive")
        requirement_min: float | str = ""
        requirement_max: float | str = ""
        if guard_kind in {"max_residuals", "mean_deltas"}:
            try:
                requirement_min = float(str(definition.get("requirement_min", "")).strip())
                requirement_max = float(str(definition.get("requirement_max", "")).strip())
            except ValueError as error:
                raise ValueError(f"Test set '{name}' needs numeric REQ_MIN and REQ_MAX values") from error
            if not math.isfinite(requirement_min) or not math.isfinite(requirement_max):
                raise ValueError(f"Test set '{name}' REQ_MIN and REQ_MAX must be finite")
            if requirement_min >= requirement_max:
                raise ValueError(f"Test set '{name}' REQ_MIN must be smaller than REQ_MAX")
        validated.append({
            "name": name,
            "tests": tests,
            "strategy": strategy,
            "guard_band_kind": guard_kind,
            "sigma_multiplier": sigma_multiplier,
            "requirement_min": requirement_min,
            "requirement_max": requirement_max,
            "pooled_columns": ", ".join(_split_group_columns(definition.get("pooled_columns", ""))),
        })
    return validated


def grouping_condition_state(spec: Mapping[str, Any]) -> dict[str, tuple[bool, str]]:
    """Resolve saved and legacy grouping columns into the six GUI categories."""
    selected = _split_group_columns(spec.get("group_by", ""))
    state: dict[str, tuple[bool, str]] = {}
    for option in GROUPING_CONDITION_OPTIONS:
        saved_column = str(spec.get(f"grouping_{option.key}_column", "")).strip()
        candidates = (option.default_column, *option.aliases)
        matched_column = next((column for column in selected if column in candidates), "")
        column = saved_column or matched_column or option.default_column
        state[option.key] = (column in selected or bool(matched_column), column)
    return state


def grouping_condition_definitions(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the six standard conditions plus persisted user-defined conditions."""
    saved = spec.get("grouping_conditions")
    if isinstance(saved, list):
        definitions = [
            dict(item)
            for item in saved
            if isinstance(item, dict)
            and str(item.get("key", "")).casefold() not in {"insertion", "temperature"}
            and str(item.get("column", "")).casefold() not in {"insertion", "temperature"}
        ]
        by_key = {str(item.get("key", "")): item for item in definitions}
        for option in GROUPING_CONDITION_OPTIONS:
            if option.key not in by_key:
                definitions.append({
                    "key": option.key,
                    "label": option.label,
                    "column": option.default_column,
                    "enabled": False,
                    "source": "existing",
                    "method": "existing",
                    "expression": "",
                    "default": "",
                    "cast": "str",
                    "custom": False,
                })
        return definitions

    selected = _split_group_columns(spec.get("group_by", ""))
    state = grouping_condition_state(spec)
    definitions: list[dict[str, Any]] = []
    # Insertion and Temperature are supplied by Insertions and are not editable grouping conditions.
    recognized: set[str] = {"Insertion", "Temperature"}
    for option in GROUPING_CONDITION_OPTIONS:
        enabled, column = state[option.key]
        if enabled:
            recognized.add(column)
        definitions.append({
            "key": option.key,
            "label": option.label,
            "column": column,
            "enabled": enabled,
            "source": "existing",
            "method": "existing",
            "expression": "",
            "default": "",
            "cast": "str",
            "custom": False,
        })
    for index, column in enumerate((value for value in selected if value not in recognized), start=1):
        definitions.append({
            "key": f"custom_{index}",
            "label": column,
            "column": column,
            "enabled": True,
            "source": "existing",
            "method": "existing",
            "expression": "",
            "default": "",
            "cast": "str",
            "custom": True,
        })

    by_column = {str(item["column"]): item for item in definitions}
    mapping_lines: dict[str, list[str]] = {}
    for raw_line in str(spec.get("condition_rules", "")).splitlines():
        parts = [part.strip() for part in raw_line.split(";")]
        if len(parts) != 6 or parts[0] not in by_column:
            continue
        target, source, _mode, marker, value, default = parts
        definition = by_column[target]
        definition.update(source=source, method="mapping", default=default)
        mapping_lines.setdefault(target, []).append(f"{marker} => {value}")
    for target, lines in mapping_lines.items():
        by_column[target]["expression"] = "\n".join(lines)

    for raw_line in str(spec.get("regex_rules", "")).splitlines():
        parts = [part.strip() for part in raw_line.split(";")]
        if len(parts) != 6 or parts[0] not in by_column:
            continue
        target, source, pattern, _group, cast, default = parts
        by_column[target].update(
            source=source,
            method="regex",
            expression=pattern,
            default=default,
            cast=cast,
        )
    return definitions


def compile_grouping_conditions(
    definitions: list[Mapping[str, Any]],
) -> tuple[tuple[str, ...], str, str]:
    """Compile guided condition identification into the existing profile rule format."""
    group_by: list[str] = []
    condition_lines: list[str] = []
    regex_lines: list[str] = []
    for definition in definitions:
        if not bool(definition.get("enabled")):
            continue
        label = str(definition.get("label", "")).strip()
        column = str(definition.get("column", "")).strip()
        if not label or not column:
            raise ValueError("Every enabled grouping condition needs a name and column")
        if column not in group_by:
            group_by.append(column)
        method = str(definition.get("method", "existing"))
        if method == "existing":
            continue
        source = str(definition.get("source", ""))
        if source not in {"filename", "test_name"}:
            raise ValueError(f"Grouping condition '{label}' needs File name or Test name as its source")
        expression = str(definition.get("expression", "")).strip()
        default = str(definition.get("default", "")).strip()
        cast = str(definition.get("cast", "str"))
        if not expression:
            raise ValueError(f"Grouping condition '{label}' needs an identification rule")
        if ";" in column or ";" in default:
            raise ValueError("Grouping columns and defaults cannot contain semicolons")
        if method == "mapping":
            for line_number, raw_line in enumerate(expression.splitlines(), start=1):
                line = raw_line.strip()
                if not line:
                    continue
                separator = "=>" if "=>" in line else "="
                if separator not in line:
                    raise ValueError(
                        f"Mapping line {line_number} for '{label}' must look like HOT => 125"
                    )
                marker, value = (part.strip() for part in line.split(separator, 1))
                if not marker or not value or ";" in marker or ";" in value:
                    raise ValueError(f"Invalid mapping line {line_number} for '{label}'")
                condition_lines.append(f"{column} ; {source} ; contains ; {marker} ; {value} ; {default}")
        elif method == "number_after":
            escaped_prefix = re.escape(expression)
            pattern = rf"{escaped_prefix}\s*[_:=\-]?\s*(-?\d+(?:\.\d+)?)"
            regex_lines.append(f"{column} ; {source} ; {pattern} ; 1 ; {cast} ; {default}")
        elif method == "regex":
            try:
                compiled = re.compile(expression)
            except re.error as error:
                raise ValueError(f"Invalid advanced regex for '{label}': {error}") from error
            if compiled.groups < 1:
                raise ValueError(f"Advanced regex for '{label}' needs one capture group, for example: CH(\\d+)")
            regex_lines.append(f"{column} ; {source} ; {expression} ; 1 ; {cast} ; {default}")
        else:
            raise ValueError(f"Unknown identification method for '{label}': {method}")
    if not group_by:
        raise ValueError("Select at least one grouping condition")
    return tuple(group_by), "\n".join(condition_lines), "\n".join(regex_lines)


def correlation_group_columns(selected: tuple[str, ...]) -> tuple[str, ...]:
    """Separate insertion campaigns and temperatures without exposing them as selectable conditions."""
    manual = tuple(column for column in selected if column not in {"Insertion", "Temperature"})
    return (*manual, "Insertion", "Temperature")


def workbook_sheet_names(path: str | Path) -> tuple[str, ...]:
    """Return workbook sheets for GUI selectors without keeping the file open."""
    return tuple(pd.ExcelFile(Path(path)).sheet_names)


class CorrelationDesktopApp:
    """Six-step Tkinter shell with an About section around the shared engine."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APPLICATION_TITLE)
        self.root.geometry("1180x760")
        self.root.minsize(760, 500)

        style = ttk.Style(root)
        style.configure("BrandTitle.TLabel", foreground="#173F73", font=("Segoe UI", 22, "bold"))
        style.configure("BrandSubtitle.TLabel", foreground="#278F9E", font=("Segoe UI", 10))
        style.configure("About.TButton", font=("Segoe UI", 9, "bold"), padding=(12, 7))
        style.configure("Heading.TLabel", font=("Segoe UI", 12, "bold"))
        style.configure("Hint.TLabel", foreground="#555555")
        style.configure("Input.TLabel", foreground="#1F4E78", font=("Segoe UI", 9, "bold"))
        style.configure("Output.TLabel", foreground="#2E7D32", font=("Segoe UI", 9, "bold"))
        style.configure("Input.TEntry", fieldbackground="#EAF2F8")
        style.configure("Output.TEntry", fieldbackground="#EAF4EA")

        self._window_icon = self._load_logo(256)
        if self._window_icon is not None:
            try:
                self.root.iconphoto(True, self._window_icon)
            except tk.TclError:
                self._window_icon = None
        self._header_logo = self._load_logo(64)
        self._about_dialog: tk.Toplevel | None = None
        self._build_brand_header()

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=(8, 6))

        self.status = tk.StringVar(value="Ready")
        status_frame = ttk.Frame(root, padding=(12, 4, 12, 10))
        status_frame.pack(fill="x")
        status_frame.columnconfigure(0, weight=1)
        ttk.Label(status_frame, textvariable=self.status).grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(status_frame, mode="indeterminate", length=210)
        self.progress.grid(row=0, column=1, sticky="e")

        self._job_results: queue.Queue[tuple[bool, Any]] = queue.Queue()
        self._active_button: ttk.Button | None = None
        self._active_success_handler: SuccessHandler | None = None
        self._extraction_profile_combos: list[ttk.Combobox] = []
        self._correlation_profile_combos: list[ttk.Combobox] = []

        self._build_profile_tab()
        self._build_extraction_tab()
        self._build_request_tab()
        self._build_import_tab()
        self._build_correlation_tab()
        self._build_yield_forecast_tab()

    def _load_logo(self, size: int) -> tk.PhotoImage | None:
        path = logo_asset_path(size)
        if not path.is_file():
            return None
        try:
            return tk.PhotoImage(master=self.root, file=str(path))
        except tk.TclError:
            return None

    def _build_brand_header(self) -> None:
        header = ttk.Frame(self.root, padding=(16, 10, 16, 8))
        header.pack(fill="x")
        header.columnconfigure(2, weight=1)
        text_column = 0
        if self._header_logo is not None:
            ttk.Label(header, image=self._header_logo).grid(
                row=0, column=0, rowspan=2, padx=(0, 12), sticky="w"
            )
            text_column = 1
        ttk.Label(header, text="CorreLaTE", style="BrandTitle.TLabel").grid(
            row=0, column=text_column, sticky="sw"
        )
        ttk.Label(header, text="ATE-to-Lab Correlation", style="BrandSubtitle.TLabel").grid(
            row=1, column=text_column, pady=(0, 3), sticky="nw"
        )
        ttk.Button(
            header,
            text="ⓘ  About",
            style="About.TButton",
            command=self._show_about_dialog,
        ).grid(row=0, column=3, rowspan=2, padx=(24, 0), sticky="e")
        ttk.Separator(self.root, orient="horizontal").pack(fill="x")

    def _show_about_dialog(self) -> None:
        if self._about_dialog is not None and self._about_dialog.winfo_exists():
            self._about_dialog.lift()
            self._about_dialog.focus_force()
            return

        dialog = tk.Toplevel(self.root)
        self._about_dialog = dialog
        dialog.title(f"About CorreLaTE {APPLICATION_VERSION}")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        if self._window_icon is not None:
            try:
                dialog.iconphoto(True, self._window_icon)
            except tk.TclError:
                pass

        content = ttk.Frame(dialog, padding=24)
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=1)

        identity = ttk.Frame(content)
        identity.grid(row=0, column=0, sticky="ew")
        if self._header_logo is not None:
            ttk.Label(identity, image=self._header_logo).grid(
                row=0, column=0, rowspan=3, padx=(0, 14), sticky="nw"
            )
        ttk.Label(identity, text="CorreLaTE", style="BrandTitle.TLabel").grid(
            row=0, column=1, sticky="w"
        )
        ttk.Label(identity, text="ATE-to-Lab Correlation", style="BrandSubtitle.TLabel").grid(
            row=1, column=1, sticky="w"
        )
        ttk.Label(
            identity,
            text="Profile-driven extraction, measurement handoff, correlation, guard-banding, and reporting.",
            style="Hint.TLabel",
            wraplength=620,
        ).grid(row=2, column=1, pady=(5, 0), sticky="w")

        ttk.Separator(content, orient="horizontal").grid(row=1, column=0, pady=16, sticky="ew")
        information = ttk.Frame(content)
        information.grid(row=2, column=0, sticky="ew")
        information.columnconfigure(1, weight=1)
        for row, (label, value) in enumerate(about_information()):
            ttk.Label(information, text=label, font=("Segoe UI", 9, "bold")).grid(
                row=row, column=0, padx=(0, 18), pady=4, sticky="nw"
            )
            ttk.Label(information, text=value, wraplength=590, justify="left").grid(
                row=row, column=1, pady=4, sticky="nw"
            )

        guidance = ttk.LabelFrame(content, text="Workflow and data handling", padding=12)
        guidance.grid(row=3, column=0, pady=(16, 0), sticky="ew")
        ttk.Label(
            guidance,
            text=(
                "Profiles → Extract ATE and Kf → Create the Lab/CV request → Validate and align returned results → "
                "Generate factors and limits → Forecast productive yield from selected insertion CSVs. Measurement processing is local. "
                "The Lab/CV request omits ATE values, limits, and internal Kf; keep the separate ATE manifest on the TE side."
            ),
            style="Hint.TLabel",
            wraplength=720,
            justify="left",
        ).grid(row=0, column=0, sticky="w")

        def close_dialog() -> None:
            self._about_dialog = None
            dialog.destroy()

        ttk.Button(content, text="Close", command=close_dialog).grid(
            row=4, column=0, pady=(18, 0), sticky="e"
        )
        dialog.protocol("WM_DELETE_WINDOW", close_dialog)
        dialog.update_idletasks()
        x = self.root.winfo_rootx() + self.root.winfo_width() - dialog.winfo_reqwidth() - 24
        y = self.root.winfo_rooty() + 72
        dialog.geometry(f"+{max(0, x)}+{max(0, y)}")
        dialog.grab_set()

    def _make_tab(self, title: str, heading: str, hint: str) -> ttk.Frame:
        scrollable = ScrollablePage(self.notebook, padding=18)
        outer = scrollable.content
        outer.columnconfigure(0, weight=1)
        self.notebook.add(scrollable, text=title)
        ttk.Label(outer, text=heading, style="Heading.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(outer, text=hint, style="Hint.TLabel", wraplength=850).grid(
            row=1, column=0, pady=(4, 14), sticky="w"
        )
        roles = ttk.Frame(outer)
        roles.grid(row=2, column=0, pady=(0, 10), sticky="w")
        ttk.Label(roles, text="INPUT", style="Input.TLabel").pack(side="left")
        ttk.Label(roles, text=" = choose an existing file/folder", style="Hint.TLabel").pack(side="left")
        ttk.Label(roles, text="   OUTPUT", style="Output.TLabel").pack(side="left")
        ttk.Label(roles, text=" = choose a new destination", style="Hint.TLabel").pack(side="left")
        form = ttk.Frame(outer)
        form.grid(row=3, column=0, sticky="nsew")
        form.columnconfigure(1, weight=1)
        return form

    @staticmethod
    def _add_label(form: ttk.Frame, row: int, text: str) -> None:
        ttk.Label(form, text=text).grid(row=row, column=0, padx=(0, 10), pady=7, sticky="w")

    def _add_combo(
        self,
        form: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        values: tuple[str, ...] | list[str],
        *,
        readonly: bool = False,
    ) -> ttk.Combobox:
        self._add_label(form, row, label)
        combo = ttk.Combobox(
            form,
            textvariable=variable,
            values=values,
            state="readonly" if readonly else "normal",
        )
        combo.grid(row=row, column=1, padx=(0, 8), pady=7, sticky="ew")
        return combo

    def _add_registered_profile_combo(
        self,
        form: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        *,
        extraction: bool = False,
    ) -> ttk.Combobox:
        registry = EXTRACTION_PROFILES if extraction else CORRELATION_PROFILES
        combo = self._add_combo(form, row, label, variable, sorted(registry), readonly=True)
        target = self._extraction_profile_combos if extraction else self._correlation_profile_combos
        target.append(combo)
        return combo

    def _refresh_profile_choices(self) -> None:
        extraction_values = sorted(EXTRACTION_PROFILES)
        correlation_values = sorted(CORRELATION_PROFILES)
        for combo in self._extraction_profile_combos:
            combo.configure(values=extraction_values)
            if combo.get() not in extraction_values and extraction_values:
                combo.set(extraction_values[0])
        for combo in self._correlation_profile_combos:
            combo.configure(values=correlation_values)
            if combo.get() not in correlation_values and correlation_values:
                combo.set(correlation_values[0])

    def _add_path(
        self,
        form: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        command: Callable[[], None],
        *,
        direction: str,
        button_text: str | None = None,
    ) -> tuple[ttk.Entry, ttk.Button]:
        if direction not in {"input", "output"}:
            raise ValueError("Path direction must be 'input' or 'output'")
        role = direction.upper()
        ttk.Label(form, text=f"{role} · {label}", style=f"{role.title()}.TLabel").grid(
            row=row, column=0, padx=(0, 10), pady=7, sticky="w"
        )
        entry = ttk.Entry(form, textvariable=variable, style=f"{role.title()}.TEntry")
        entry.grid(row=row, column=1, padx=(0, 8), pady=7, sticky="ew")
        if button_text is None:
            button_text = "Open…" if direction == "input" else "Save as…"
        button = ttk.Button(form, text=button_text, command=command)
        button.grid(row=row, column=2, pady=7, sticky="ew")
        return entry, button

    @staticmethod
    def _require(**values: str) -> None:
        missing = [label for label, value in values.items() if not value.strip()]
        if missing:
            raise ValueError(f"Required: {', '.join(missing)}")

    def _validate_required(self, **values: str) -> bool:
        try:
            self._require(**values)
        except ValueError as error:
            messagebox.showerror("Missing information", str(error), parent=self.root)
            return False
        return True

    def _choose_open(
        self,
        path_variable: tk.StringVar,
        sheet_variable: tk.StringVar | None = None,
        sheet_combo: ttk.Combobox | None = None,
    ) -> None:
        value = filedialog.askopenfilename(
            parent=self.root,
            filetypes=[("Excel workbooks", "*.xlsx *.xlsm *.xls"), ("All files", "*.*")],
        )
        if not value:
            return
        path_variable.set(value)
        if sheet_variable is None or sheet_combo is None:
            return
        try:
            sheets = workbook_sheet_names(value)
        except Exception as error:
            sheet_combo.configure(values=())
            sheet_variable.set("")
            messagebox.showerror("Cannot read workbook", str(error), parent=self.root)
            return
        sheet_combo.configure(values=sheets)
        if sheets:
            sheet_variable.set(sheets[0])

    def _choose_save(self, variable: tk.StringVar, title: str) -> None:
        value = filedialog.asksaveasfilename(
            parent=self.root,
            title=title,
            defaultextension=".xlsx",
            filetypes=[("Excel workbooks", "*.xlsx")],
        )
        if value:
            variable.set(value)

    def _choose_save_html(self, variable: tk.StringVar, title: str) -> None:
        value = filedialog.asksaveasfilename(
            parent=self.root,
            title=title,
            defaultextension=".html",
            filetypes=[("HTML reports", "*.html")],
        )
        if value:
            variable.set(value)

    def _choose_folder(self, variable: tk.StringVar, title: str) -> None:
        value = filedialog.askdirectory(parent=self.root, title=title)
        if value:
            variable.set(value)

    def _start_job(
        self,
        button: ttk.Button,
        description: str,
        action: Action,
        *,
        on_success: SuccessHandler | None = None,
    ) -> None:
        if self._active_button is not None:
            messagebox.showwarning(
                "Operation in progress",
                "Wait for the current operation to finish.",
                parent=self.root,
            )
            return
        self._active_button = button
        self._active_success_handler = on_success
        button.state(["disabled"])
        self.status.set(description)
        self.progress.start(12)

        def worker() -> None:
            try:
                self._job_results.put((True, action()))
            except Exception as error:  # The main Tk thread displays workflow errors.
                self._job_results.put((False, f"{type(error).__name__}: {error}"))

        threading.Thread(target=worker, daemon=True).start()
        self.root.after(100, self._poll_job)

    def _poll_job(self) -> None:
        if self._active_button is None:
            return
        try:
            success, text = self._job_results.get_nowait()
        except queue.Empty:
            self.root.after(100, self._poll_job)
            return
        self.progress.stop()
        self._active_button.state(["!disabled"])
        self._active_button = None
        handler = self._active_success_handler
        self._active_success_handler = None
        self.status.set(str(text) if success and handler is None else "Ready" if success else "Operation failed")
        if success:
            if handler is not None:
                try:
                    handler(text)
                except Exception as error:
                    self.status.set("Operation failed")
                    messagebox.showerror(
                        "Operation failed",
                        f"{type(error).__name__}: {error}",
                        parent=self.root,
                    )
            else:
                messagebox.showinfo("Complete", str(text), parent=self.root)
        else:
            messagebox.showerror("Operation failed", str(text), parent=self.root)

    def _show_outlier_review_dialog(
        self,
        analysis: OutlierAnalysis,
        profile: Any,
        *,
        allow_filtering: bool,
    ) -> tuple[pd.DataFrame, OutlierReview] | None:
        """Show detector results and return only exclusions explicitly approved by the user."""
        dialog = tk.Toplevel(self.root)
        dialog.title("CorreLaTE · Pre-correlation outlier review")
        dialog.transient(self.root)
        dialog.geometry("1160x690")
        dialog.minsize(820, 500)
        if self._window_icon is not None:
            try:
                dialog.iconphoto(True, self._window_icon)
            except tk.TclError:
                pass

        result: tuple[pd.DataFrame, OutlierReview] | None = None
        selected_exclusions: set[int] = set()
        content = ttk.Frame(dialog, padding=18)
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=1)
        content.rowconfigure(3, weight=1)

        identity = ttk.Frame(content)
        identity.grid(row=0, column=0, sticky="ew")
        if self._header_logo is not None:
            ttk.Label(identity, image=self._header_logo).grid(
                row=0, column=0, rowspan=3, padx=(0, 14), sticky="nw"
            )
        ttk.Label(identity, text="Pre-correlation outlier review", style="Heading.TLabel").grid(
            row=0, column=1, sticky="w"
        )
        ttk.Label(
            identity,
            text=(
                f"Scaled MAD: |x − median(x)| / (1.4826 × MAD) > {analysis.threshold:g}. "
                "Detection is per unpooled test and corner; Lab/CV, ATE/TE, and paired-model signals are reviewed independently."
            ),
            style="Hint.TLabel",
            wraplength=920,
        ).grid(row=1, column=1, pady=(4, 0), sticky="w")
        ttk.Label(
            identity,
            text=(
                "A statistical flag is a review candidate, not proof of bad data. Original rows are never modified. "
                "Exclusion requires an explicit selection and is recorded in Excel and HTML outputs."
            ),
            style="Hint.TLabel",
            wraplength=920,
        ).grid(row=2, column=1, pady=(4, 0), sticky="w")

        findings = analysis.findings
        test_summary = "None"
        if not findings.empty:
            identities: list[str] = []
            number_column = findings.get("Test Number", pd.Series("", index=findings.index))
            name_column_name = profile.test_name_column or "Test Name"
            name_column = findings.get(name_column_name, pd.Series("", index=findings.index))
            for number, name in zip(number_column, name_column):
                text = f"{number:g}" if isinstance(number, float) and number.is_integer() else str(number)
                if str(name).strip() and str(name).strip().casefold() != "nan":
                    text += f" · {str(name).strip()}"
                if text not in identities:
                    identities.append(text)
            test_summary = "; ".join(identities[:8])
            if len(identities) > 8:
                test_summary += f"; +{len(identities) - 8} more"
        summary_frame = ttk.LabelFrame(content, text="Detection summary", padding=10)
        summary_frame.grid(row=1, column=0, pady=(14, 10), sticky="ew")
        summary_frame.columnconfigure(1, weight=1)
        summary_rows = (
            ("Flagged raw samples", f"{analysis.flagged_count:,}"),
            ("Affected tests", f"{analysis.affected_test_count:,}"),
            ("Affected unpooled populations", f"{analysis.affected_population_count:,}"),
            ("Valid Lab/CV–ATE pairs reviewed", f"{analysis.valid_pair_count:,}"),
            ("Tests", test_summary),
        )
        for row, (label, value) in enumerate(summary_rows):
            ttk.Label(summary_frame, text=label, font=("Segoe UI", 9, "bold")).grid(
                row=row, column=0, padx=(0, 14), pady=2, sticky="nw"
            )
            ttk.Label(summary_frame, text=value, wraplength=850, justify="left").grid(
                row=row, column=1, pady=2, sticky="nw"
            )

        filtering_state = ttk.Frame(content)
        filtering_state.grid(row=2, column=0, pady=(0, 8), sticky="ew")
        filtering_message = (
            "Manual filtering is enabled for this run. No rows are selected by default."
            if allow_filtering
            else "Filtering is disabled (default). Continue to correlate with every raw sample retained."
        )
        ttk.Label(
            filtering_state,
            text=filtering_message,
            foreground="#2E7D32" if allow_filtering else "#555555",
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left")

        tree: ttk.Treeview | None = None
        tree_row_ids: dict[str, int] = {}
        if findings.empty:
            empty = ttk.LabelFrame(content, text="Review result", padding=24)
            empty.grid(row=3, column=0, sticky="nsew")
            ttk.Label(
                empty,
                text="No outliers were detected in the Lab/CV, ATE/TE, or paired-model series.",
                font=("Segoe UI", 12, "bold"),
                foreground="#2E7D32",
                wraplength=800,
            ).pack(expand=True)
        else:
            table = ttk.Frame(content)
            table.grid(row=3, column=0, sticky="nsew")
            table.rowconfigure(0, weight=1)
            table.columnconfigure(0, weight=1)
            name_column = profile.test_name_column or "Test Name"
            preferred_columns = (
                OUTLIER_INPUT_ROW,
                "TestSet",
                "Test Number",
                name_column,
                "DUT Nr",
                "Wafer",
                "WAFER",
                "X",
                "Y",
                "DoE split",
                "Insertion Type",
                "Insertion",
                "Temperature",
                *profile.group_by,
                OUTLIER_FLAGGED_SERIES,
                OUTLIER_MAX_SCORE,
                "LabValue",
                "LabRobustScore",
                "ATEValue",
                "ATERobustScore",
                "PairedMetric",
                "PairedValue",
                "PairedRobustScore",
                "OutlierReviewGuidance",
                OUTLIER_REASON,
            )
            display_columns = tuple(
                column for column in dict.fromkeys(preferred_columns) if column in findings.columns
            )
            tree_columns = ("Exclude", *display_columns)
            tree = ttk.Treeview(
                table,
                columns=tree_columns,
                show="headings",
                selectmode="extended",
            )
            vertical = ttk.Scrollbar(table, orient="vertical", command=tree.yview)
            horizontal = ttk.Scrollbar(table, orient="horizontal", command=tree.xview)
            tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
            tree.grid(row=0, column=0, sticky="nsew")
            vertical.grid(row=0, column=1, sticky="ns")
            horizontal.grid(row=1, column=0, sticky="ew")
            for column in tree_columns:
                heading = {
                    "Exclude": "Exclude?",
                    OUTLIER_INPUT_ROW: "Input row",
                    OUTLIER_FLAGGED_SERIES: "Flagged signal(s)",
                    OUTLIER_MAX_SCORE: "Max robust score",
                }.get(column, column)
                tree.heading(column, text=heading)
                width = 85 if column in {"Exclude", OUTLIER_INPUT_ROW, "X", "Y"} else 150
                if column in {OUTLIER_REASON, "OutlierReviewGuidance", name_column, "OutlierPopulation"}:
                    width = 300
                tree.column(column, width=width, minwidth=65, stretch=False)

            def display_cell(value: Any) -> str:
                try:
                    if pd.isna(value):
                        return ""
                except (TypeError, ValueError):
                    pass
                if isinstance(value, float):
                    if math.isinf(value):
                        return "∞"
                    return f"{value:.6g}"
                return str(value)

            for _index, finding in findings.iterrows():
                row_id = int(finding[OUTLIER_ROW_ID])
                item = f"outlier-{row_id}"
                values = ["No", *(display_cell(finding[column]) for column in display_columns)]
                tree.insert("", "end", iid=item, values=values)
                tree_row_ids[item] = row_id

        actions = ttk.Frame(content)
        actions.grid(row=4, column=0, pady=(14, 0), sticky="ew")
        selection_text = tk.StringVar(value="0 rows selected for exclusion")
        ttk.Label(actions, textvariable=selection_text, style="Hint.TLabel").pack(side="left")

        def refresh_selection_display() -> None:
            selection_text.set(f"{len(selected_exclusions):,} row(s) selected for exclusion")

        def toggle_rows(_event: object | None = None) -> None:
            if not allow_filtering or tree is None:
                return
            for item in tree.selection():
                row_id = tree_row_ids[item]
                values = list(tree.item(item, "values"))
                if row_id in selected_exclusions:
                    selected_exclusions.remove(row_id)
                    values[0] = "No"
                else:
                    selected_exclusions.add(row_id)
                    values[0] = "Yes"
                tree.item(item, values=values)
            refresh_selection_display()

        toggle_button = ttk.Button(actions, text="Toggle selected row(s)", command=toggle_rows)
        toggle_button.pack(side="left", padx=(12, 0))
        if not allow_filtering or tree is None:
            toggle_button.state(["disabled"])
        if tree is not None:
            tree.bind("<Double-1>", toggle_rows)

        def finish(exclusions: set[int]) -> None:
            nonlocal result
            try:
                result = finalize_outlier_review(analysis, profile, exclusions)
            except ValueError as error:
                messagebox.showerror("Invalid outlier exclusions", str(error), parent=dialog)
                return
            dialog.destroy()

        def apply_selected() -> None:
            if not selected_exclusions:
                messagebox.showinfo(
                    "No exclusions selected",
                    "Select at least one flagged row, or use Continue with all data.",
                    parent=dialog,
                )
                return
            finish(set(selected_exclusions))

        ttk.Button(actions, text="Cancel", command=dialog.destroy).pack(side="right", padx=(8, 0))
        apply_button = ttk.Button(actions, text="Apply selected exclusions", command=apply_selected)
        apply_button.pack(side="right", padx=(8, 0))
        if not allow_filtering or findings.empty:
            apply_button.state(["disabled"])
        ttk.Button(actions, text="Continue with all data", command=lambda: finish(set())).pack(
            side="right", padx=(8, 0)
        )

        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.grab_set()
        dialog.wait_window()
        return result

    def _build_profile_tab(self) -> None:
        outer = ttk.Frame(self.notebook, padding=18)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)
        self.notebook.add(outer, text="1 · Profiles")
        ttk.Label(outer, text="Create a reusable ATE-to-Lab profile", style="Heading.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            outer,
            text=(
                "Define the tests, grouping identification, correlation strategy, guard-band policy, and automatic "
                "Kf extraction. Built-in CTRX8188 profiles remain read-only; custom profiles are available immediately "
                "in every workflow and in the CLI."
            ),
            style="Hint.TLabel",
            wraplength=900,
        ).grid(row=1, column=0, pady=(4, 10), sticky="w")

        controls = ttk.Frame(outer)
        controls.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        controls.columnconfigure(1, weight=1)
        ttk.Label(controls, text="Custom profile").grid(row=0, column=0, padx=(0, 10), sticky="w")
        selected = tk.StringVar()
        custom_combo = ttk.Combobox(controls, textvariable=selected, state="readonly")
        custom_combo.grid(row=0, column=1, padx=(0, 8), sticky="ew")
        ttk.Button(controls, text="Load", command=lambda: load_selected()).grid(row=0, column=2, padx=3)
        ttk.Button(controls, text="New", command=lambda: clear_editor()).grid(row=0, column=3, padx=3)
        ttk.Label(controls, text=f"Saved in {profile_store_path()}", style="Hint.TLabel").grid(
            row=1, column=1, columnspan=3, pady=(5, 0), sticky="w"
        )

        editor = ttk.Notebook(outer)
        editor.grid(row=3, column=0, sticky="nsew")
        insertion_scroll = ScrollablePage(editor, padding=12)
        basics_scroll = ScrollablePage(editor, padding=12)
        correlation_scroll = ScrollablePage(editor, padding=12)
        insertions_page = insertion_scroll.content
        basics = basics_scroll.content
        correlation = correlation_scroll.content
        editor.add(insertion_scroll, text="Insertions")
        editor.add(basics_scroll, text="Identity & Tests")
        editor.add(correlation_scroll, text="Correlation Inputs & Kf")
        for page in (insertions_page, basics, correlation):
            page.columnconfigure(1, weight=1)

        defaults = {
            "profile_id": "",
            "display_name": "",
            "tests": "",
            "group_by": "Test Number",
            "strategy": "Median_Deltas",
            "reference_column": "CV Value",
            "candidate_column": "Test Value",
            "minimum_points": "5",
            "detail_key_columns": "DUT Nr, Wafer, X, Y",
            "additional_output_columns": "",
            "coordinate_columns": "WAFER, X, Y",
            "coordinate_fallback": "WAFER=62007, X=62008, Y=62009",
            "insertion_field": "Insertion Type",
            "fallback_insertion_values": "BE",
            "lower_limit_column": "Low",
            "upper_limit_column": "High",
            "unit_column": "Unit",
            "test_name_column": "Test Name",
            "guard_band_kind": "distribution_sigma",
            "sigma_multiplier": "6.0",
            "physics_kf_enabled": "Enabled",
            "covariate_value_column": "Test Value",
            "covariate_merge_keys": "DUT Nr, Temperature, Insertion",
            "covariate_output_name": "Kf",
            "covariate_test_number": "52046",
        }
        variables = {key: tk.StringVar(value=value) for key, value in defaults.items()}
        self._profile_editor_vars = variables
        fallback_values = coordinate_fallback_values(defaults)
        coordinate_fallback_vars = {
            coordinate: tk.StringVar(value=value)
            for coordinate, value in fallback_values.items()
        }
        self._coordinate_fallback_vars = coordinate_fallback_vars

        configured_insertions: list[dict[str, Any]] = insertion_definitions(defaults)
        selected_insertion = tk.StringVar()
        insertion_name = tk.StringVar()
        insertion_group = tk.StringVar(value="FE")
        insertion_temperature = tk.StringVar()
        current_insertion = {"index": -1, "loading": False}

        ttk.Label(
            insertions_page,
            text=(
                "Define every test insertion before selecting tests. Each insertion belongs to the fixed FE or BE "
                "group, has one test temperature, and must have at least one assigned raw data file."
            ),
            style="Hint.TLabel",
            wraplength=900,
        ).grid(row=0, column=0, columnspan=3, pady=(0, 10), sticky="w")
        self._add_label(insertions_page, 1, "Insertion")
        insertion_combo = ttk.Combobox(insertions_page, textvariable=selected_insertion, state="readonly")
        insertion_combo.grid(row=1, column=1, padx=(0, 8), pady=6, sticky="ew")
        insertion_buttons = ttk.Frame(insertions_page)
        insertion_buttons.grid(row=1, column=2, sticky="w")

        self._add_label(insertions_page, 2, "Insertion name")
        ttk.Entry(insertions_page, textvariable=insertion_name).grid(
            row=2, column=1, padx=(0, 8), pady=6, sticky="ew"
        )
        ttk.Label(
            insertions_page,
            text="Name identifying this insertion; for example: S1, S2, RT, or HT.",
            style="Hint.TLabel",
        ).grid(row=2, column=2, sticky="w")

        self._add_combo(
            insertions_page, 3, "Insertion group", insertion_group, ("FE", "BE"), readonly=True,
        )
        self._add_label(insertions_page, 4, "Temperature (°C)")
        ttk.Entry(insertions_page, textvariable=insertion_temperature).grid(
            row=4, column=1, padx=(0, 8), pady=6, sticky="ew"
        )
        ttk.Label(
            insertions_page,
            text="Test temperature corresponding to this insertion; for example: -40, 25, or 135.",
            style="Hint.TLabel",
        ).grid(row=4, column=2, sticky="w")

        self._add_label(insertions_page, 5, "Raw test data")
        insertion_files = tk.Listbox(insertions_page, height=9, selectmode="extended")
        insertion_files.grid(row=5, column=1, padx=(0, 8), pady=6, sticky="nsew")
        insertions_page.rowconfigure(5, weight=1)
        file_buttons = ttk.Frame(insertions_page)
        file_buttons.grid(row=5, column=2, sticky="nw")
        ttk.Label(
            file_buttons,
            text="Assign one or more corresponding raw CSV files. Saving checks that every insertion has an existing file.",
            style="Hint.TLabel",
            wraplength=330,
        ).pack(anchor="w", pady=(8, 0))

        def store_current_insertion() -> None:
            if current_insertion["loading"] or current_insertion["index"] < 0:
                return
            configured_insertions[int(current_insertion["index"])].update({
                "name": insertion_name.get().strip(),
                "group": insertion_group.get(),
                "temperature": insertion_temperature.get().strip(),
                "raw_files": list(insertion_files.get(0, "end")),
            })

        def insertion_selector_values() -> tuple[str, ...]:
            return tuple(
                f"{item.get('group', 'FE')} · {item.get('name') or 'Unnamed'} · "
                f"{item.get('temperature') or '?'} °C · {len(item.get('raw_files', []))} file(s)"
                for item in configured_insertions
            )

        def load_insertion(index: int) -> None:
            definition = configured_insertions[index]
            current_insertion.update(index=index, loading=True)
            insertion_combo.configure(values=insertion_selector_values())
            insertion_combo.current(index)
            insertion_name.set(str(definition.get("name", "")))
            insertion_group.set(str(definition.get("group", "FE")))
            insertion_temperature.set(str(definition.get("temperature", "")))
            insertion_files.delete(0, "end")
            for path in definition.get("raw_files", []):
                insertion_files.insert("end", str(path))
            current_insertion["loading"] = False

        def choose_insertion(_event: object | None = None) -> None:
            selected_index = max(insertion_combo.current(), 0)
            store_current_insertion()
            load_insertion(selected_index)

        def add_insertion() -> None:
            store_current_insertion()
            configured_insertions.append({
                "name": f"Insertion {len(configured_insertions) + 1}",
                "group": "FE",
                "temperature": "25",
                "raw_files": [],
            })
            load_insertion(len(configured_insertions) - 1)

        def remove_insertion() -> None:
            if current_insertion["index"] < 0:
                return
            del configured_insertions[int(current_insertion["index"])]
            if configured_insertions:
                load_insertion(min(int(current_insertion["index"]), len(configured_insertions) - 1))
            else:
                current_insertion["index"] = -1
                insertion_combo.configure(values=())
                selected_insertion.set("")
                insertion_name.set("")
                insertion_temperature.set("")
                insertion_files.delete(0, "end")

        def browse_insertion_files() -> None:
            files = filedialog.askopenfilenames(
                parent=self.root,
                title=f"Select raw test data for {insertion_name.get() or 'insertion'}",
                filetypes=[("Raw TE CSV files", "*.csv"), ("All files", "*.*")],
            )
            existing = set(insertion_files.get(0, "end"))
            for path in files:
                if path not in existing:
                    insertion_files.insert("end", path)
                    existing.add(path)

        def remove_selected_insertion_files() -> None:
            for index in reversed(insertion_files.curselection()):
                insertion_files.delete(index)

        ttk.Button(insertion_buttons, text="Add…", command=add_insertion).pack(side="left", padx=3)
        ttk.Button(insertion_buttons, text="Remove", command=remove_insertion).pack(side="left", padx=3)
        ttk.Button(file_buttons, text="Browse…", command=browse_insertion_files).pack(anchor="w", pady=3)
        ttk.Button(file_buttons, text="Remove selected", command=remove_selected_insertion_files).pack(
            anchor="w", pady=3
        )
        insertion_combo.bind("<<ComboboxSelected>>", choose_insertion)
        self._profile_insertions = configured_insertions

        fallback_frame = ttk.LabelFrame(
            insertions_page,
            text="BE coordinate fallback (FUSE module)",
            padding=8,
        )
        fallback_frame.grid(row=6, column=0, columnspan=3, pady=(10, 0), sticky="ew")
        for column in (1, 3, 5):
            fallback_frame.columnconfigure(column, weight=1)
        ttk.Label(
            fallback_frame,
            text=(
                "Used only for BE insertions when normal WAFER/X/Y values or columns are missing. "
                "Enter the FUSE test numbers with or without the TN prefix."
            ),
            style="Hint.TLabel",
            wraplength=900,
        ).grid(row=0, column=0, columnspan=6, pady=(0, 7), sticky="w")
        fallback_labels = {
            "WAFER": "Wafer test number",
            "X": "X-coordinate test number",
            "Y": "Y-coordinate test number",
        }
        for index, coordinate in enumerate(("WAFER", "X", "Y")):
            label_column = index * 2
            ttk.Label(fallback_frame, text=fallback_labels[coordinate]).grid(
                row=1,
                column=label_column,
                padx=(0 if index == 0 else 14, 6),
                sticky="w",
            )
            ttk.Entry(
                fallback_frame,
                textvariable=coordinate_fallback_vars[coordinate],
                width=12,
            ).grid(row=1, column=label_column + 1, sticky="ew")

        def add_entry(page: ttk.Frame, row: int, label: str, key: str, hint: str) -> None:
            self._add_label(page, row, label)
            ttk.Entry(page, textvariable=variables[key]).grid(
                row=row, column=1, padx=(0, 8), pady=6, sticky="ew"
            )
            ttk.Label(page, text=hint, style="Hint.TLabel", wraplength=360).grid(
                row=row, column=2, pady=6, sticky="w"
            )

        add_entry(basics, 0, "Profile ID", "profile_id", "Lowercase ID; for example: my-dc-current.")
        add_entry(
            basics, 1, "Display name", "display_name",
            "Human-readable name shown in reports; for example: PMIC leakage at hot.",
        )

        configured_test_sets = test_set_definitions(defaults)
        selected_test_set = tk.StringVar()
        test_set_name = tk.StringVar()
        test_set_tests = tk.StringVar()
        test_set_strategy = tk.StringVar(value="Median_Deltas")
        test_set_guard_band = tk.StringVar(value="distribution_sigma")
        test_set_sigma = tk.StringVar(value="6.0")
        test_set_requirement_min = tk.StringVar()
        test_set_requirement_max = tk.StringVar()
        test_set_pooled_columns = tk.StringVar()
        strategy_equation = tk.StringVar()
        guard_band_equation = tk.StringVar()
        current_test_set = {"index": 0, "loading": False}

        self._add_label(basics, 2, "Tests")
        test_set_frame = ttk.LabelFrame(basics, text="Test-specific correlation and guard-band", padding=8)
        test_set_frame.grid(row=2, column=1, columnspan=2, padx=(0, 8), pady=6, sticky="ew")
        test_set_frame.columnconfigure(1, weight=1)
        self._add_label(test_set_frame, 0, "Test set")
        test_set_combo = ttk.Combobox(test_set_frame, textvariable=selected_test_set, state="readonly")
        test_set_combo.grid(row=0, column=1, padx=(0, 8), pady=4, sticky="ew")
        test_set_buttons = ttk.Frame(test_set_frame)
        test_set_buttons.grid(row=0, column=2, sticky="w")

        self._add_label(test_set_frame, 1, "Set name")
        ttk.Entry(test_set_frame, textvariable=test_set_name).grid(
            row=1, column=1, padx=(0, 8), pady=4, sticky="ew"
        )
        ttk.Label(
            test_set_frame,
            text="Descriptive label shown in reports; for example: DPLL phase noise or TXPA power.",
            style="Hint.TLabel",
            wraplength=390,
        ).grid(row=1, column=2, sticky="w")

        self._add_label(test_set_frame, 2, "Test selection")
        ttk.Entry(test_set_frame, textvariable=test_set_tests).grid(
            row=2, column=1, padx=(0, 8), pady=4, sticky="ew"
        )
        ttk.Label(
            test_set_frame,
            text="Exact numbers, inclusive ranges, or name fragments; for example: 101, 1200-1299, LeakageCurrent.",
            style="Hint.TLabel",
            wraplength=390,
        ).grid(row=2, column=2, sticky="w")

        strategy_combo = self._add_combo(
            test_set_frame,
            3,
            "Correlation strategy",
            test_set_strategy,
            tuple(CORRELATION_STRATEGY_EXPLANATIONS),
            readonly=True,
        )
        ttk.Label(
            test_set_frame,
            textvariable=strategy_equation,
            style="Hint.TLabel",
            wraplength=390,
        ).grid(row=3, column=2, sticky="w")

        guard_combo = self._add_combo(
            test_set_frame,
            4,
            "Guard-band policy",
            test_set_guard_band,
            tuple(GUARD_BAND_EXPLANATIONS),
            readonly=True,
        )
        ttk.Label(
            test_set_frame,
            textvariable=guard_band_equation,
            style="Hint.TLabel",
            wraplength=390,
        ).grid(row=4, column=2, sticky="w")

        self._add_label(test_set_frame, 5, "Sigma multiplier (k)")
        sigma_entry = ttk.Entry(test_set_frame, textvariable=test_set_sigma)
        sigma_entry.grid(row=5, column=1, padx=(0, 8), pady=4, sticky="ew")
        ttk.Label(
            test_set_frame,
            text="Used by distribution_sigma; for example: 6 gives mean ± 6σ.",
            style="Hint.TLabel",
            wraplength=390,
        ).grid(row=5, column=2, sticky="w")

        self._add_label(test_set_frame, 6, "REQ_MIN")
        requirement_min_entry = ttk.Entry(test_set_frame, textvariable=test_set_requirement_min)
        requirement_min_entry.grid(row=6, column=1, padx=(0, 8), pady=4, sticky="ew")
        ttk.Label(
            test_set_frame,
            text="Required by max_residuals and mean_deltas; base lower requirement before inward tightening.",
            style="Hint.TLabel",
            wraplength=390,
        ).grid(row=6, column=2, sticky="w")

        self._add_label(test_set_frame, 7, "REQ_MAX")
        requirement_max_entry = ttk.Entry(test_set_frame, textvariable=test_set_requirement_max)
        requirement_max_entry.grid(row=7, column=1, padx=(0, 8), pady=4, sticky="ew")
        ttk.Label(
            test_set_frame,
            text="Required by max_residuals and mean_deltas; base upper requirement before inward tightening.",
            style="Hint.TLabel",
            wraplength=390,
        ).grid(row=7, column=2, sticky="w")

        self._add_label(test_set_frame, 8, "Merge/pool parameters")
        ttk.Entry(test_set_frame, textvariable=test_set_pooled_columns).grid(
            row=8, column=1, padx=(0, 8), pady=4, sticky="ew"
        )
        ttk.Label(
            test_set_frame,
            text=(
                "Optional comma-separated enabled grouping columns whose values share one factor and guard band in this "
                "test set; for example: Test Number, Channel pools 8 channels into 88 samples for 11 DUTs."
            ),
            style="Hint.TLabel",
            wraplength=390,
        ).grid(row=8, column=2, sticky="w")

        def update_policy_explanations(*_args: object) -> None:
            strategy_equation.set(CORRELATION_STRATEGY_EXPLANATIONS.get(test_set_strategy.get(), ""))
            guard_band_equation.set(GUARD_BAND_EXPLANATIONS.get(test_set_guard_band.get(), ""))
            sigma_entry.configure(
                state="normal" if test_set_guard_band.get() == "distribution_sigma" else "disabled"
            )
            requirement_state = (
                "normal"
                if test_set_guard_band.get() in {"max_residuals", "mean_deltas"}
                else "disabled"
            )
            requirement_min_entry.configure(state=requirement_state)
            requirement_max_entry.configure(state=requirement_state)

        def store_current_test_set() -> None:
            if current_test_set["loading"] or not configured_test_sets:
                return
            configured_test_sets[int(current_test_set["index"])].update({
                "name": test_set_name.get().strip(),
                "tests": test_set_tests.get().strip(),
                "strategy": test_set_strategy.get(),
                "guard_band_kind": test_set_guard_band.get(),
                "sigma_multiplier": test_set_sigma.get().strip(),
                "requirement_min": test_set_requirement_min.get().strip(),
                "requirement_max": test_set_requirement_max.get().strip(),
                "pooled_columns": test_set_pooled_columns.get().strip(),
            })

        def test_set_selector_values() -> tuple[str, ...]:
            return tuple(
                f"{item.get('name') or 'Unnamed'} · {item.get('strategy')} · {item.get('guard_band_kind')}"
                + (f" · pool {item.get('pooled_columns')}" if str(item.get("pooled_columns", "")).strip() else "")
                for item in configured_test_sets
            )

        def load_test_set(index: int) -> None:
            definition = configured_test_sets[index]
            current_test_set.update(index=index, loading=True)
            test_set_combo.configure(values=test_set_selector_values())
            test_set_combo.current(index)
            test_set_name.set(str(definition.get("name", "")))
            test_set_tests.set(str(definition.get("tests", "")))
            test_set_strategy.set(str(definition.get("strategy", "Median_Deltas")))
            test_set_guard_band.set(str(definition.get("guard_band_kind", "distribution_sigma")))
            test_set_sigma.set(str(definition.get("sigma_multiplier", "6.0")))
            test_set_requirement_min.set(str(definition.get("requirement_min", "")))
            test_set_requirement_max.set(str(definition.get("requirement_max", "")))
            test_set_pooled_columns.set(str(definition.get("pooled_columns", "")))
            current_test_set["loading"] = False
            update_policy_explanations()

        def choose_test_set(_event: object | None = None) -> None:
            selected_index = max(test_set_combo.current(), 0)
            store_current_test_set()
            load_test_set(selected_index)

        def add_test_set() -> None:
            store_current_test_set()
            configured_test_sets.append({
                "name": f"Tests {len(configured_test_sets) + 1}",
                "tests": "",
                "strategy": "Median_Deltas",
                "guard_band_kind": "distribution_sigma",
                "sigma_multiplier": "6.0",
                "requirement_min": "",
                "requirement_max": "",
                "pooled_columns": "",
            })
            load_test_set(len(configured_test_sets) - 1)

        def remove_test_set() -> None:
            if len(configured_test_sets) == 1:
                messagebox.showinfo(
                    "Test sets",
                    "A profile needs at least one test set.",
                    parent=self.root,
                )
                return
            index = int(current_test_set["index"])
            del configured_test_sets[index]
            load_test_set(min(index, len(configured_test_sets) - 1))

        ttk.Button(test_set_buttons, text="Add…", command=add_test_set).pack(side="left", padx=3)
        ttk.Button(test_set_buttons, text="Remove", command=remove_test_set).pack(side="left", padx=3)
        test_set_combo.bind("<<ComboboxSelected>>", choose_test_set)
        test_set_strategy.trace_add("write", update_policy_explanations)
        test_set_guard_band.trace_add("write", update_policy_explanations)
        load_test_set(0)
        self._profile_test_sets = configured_test_sets

        self._add_label(basics, 3, "Grouping conditions")
        grouping_frame = ttk.LabelFrame(basics, text="Condition and identification", padding=8)
        grouping_frame.grid(row=3, column=1, columnspan=2, padx=(0, 8), pady=6, sticky="ew")
        grouping_frame.columnconfigure(1, weight=1)
        grouping_definitions = grouping_condition_definitions(defaults)
        condition_selector = tk.StringVar()
        condition_enabled = tk.BooleanVar()
        condition_label = tk.StringVar()
        condition_column = tk.StringVar()
        condition_source = tk.StringVar(value="Existing input column")
        condition_method = tk.StringVar(value="Use existing column")
        condition_default = tk.StringVar()
        condition_cast = tk.StringVar(value="Text")
        condition_rule_hint = tk.StringVar()
        current_condition = {"index": 0, "loading": False}

        self._add_label(grouping_frame, 0, "Condition")
        condition_combo = ttk.Combobox(grouping_frame, textvariable=condition_selector, state="readonly")
        condition_combo.grid(row=0, column=1, padx=(0, 8), pady=4, sticky="ew")
        condition_buttons = ttk.Frame(grouping_frame)
        condition_buttons.grid(row=0, column=2, sticky="e")

        ttk.Checkbutton(grouping_frame, text="Enabled for this profile", variable=condition_enabled).grid(
            row=1, column=1, pady=4, sticky="w"
        )
        ttk.Label(
            grouping_frame,
            text="Enable only the dimensions that define separate correlation groups.",
            style="Hint.TLabel",
        ).grid(row=1, column=2, sticky="w")

        self._add_label(grouping_frame, 2, "Condition name")
        condition_label_entry = ttk.Entry(grouping_frame, textvariable=condition_label)
        condition_label_entry.grid(row=2, column=1, padx=(0, 8), pady=4, sticky="ew")
        ttk.Label(
            grouping_frame,
            text="Displayed meaning; for example: Frequency, Channel, Bias Mode.",
            style="Hint.TLabel",
        ).grid(row=2, column=2, sticky="w")

        self._add_label(grouping_frame, 3, "Output/input column")
        ttk.Entry(grouping_frame, textvariable=condition_column).grid(
            row=3, column=1, padx=(0, 8), pady=4, sticky="ew"
        )
        ttk.Label(
            grouping_frame,
            text="Column used for grouping; for example: Frequency_GHz, PA Channel, LUT value.",
            style="Hint.TLabel",
        ).grid(row=3, column=2, sticky="w")

        source_combo = self._add_combo(
            grouping_frame, 4, "Identification source", condition_source,
            tuple(GROUPING_SOURCES), readonly=True,
        )
        self._add_combo(
            grouping_frame, 5, "Identification method", condition_method,
            tuple(GROUPING_METHODS), readonly=True,
        )

        self._add_label(grouping_frame, 6, "Identification rule")
        condition_expression = tk.Text(grouping_frame, height=4, wrap="none", font=("Consolas", 9))
        condition_expression.grid(row=6, column=1, padx=(0, 8), pady=4, sticky="ew")
        ttk.Label(
            grouping_frame,
            textvariable=condition_rule_hint,
            style="Hint.TLabel",
            wraplength=390,
        ).grid(row=6, column=2, sticky="nw")

        self._add_label(grouping_frame, 7, "Default value")
        condition_default_entry = ttk.Entry(grouping_frame, textvariable=condition_default)
        condition_default_entry.grid(row=7, column=1, padx=(0, 8), pady=4, sticky="ew")
        ttk.Label(
            grouping_frame,
            text="Used when no identification matches; for example: Unknown, ALL, or 25.",
            style="Hint.TLabel",
        ).grid(row=7, column=2, sticky="w")
        cast_combo = self._add_combo(
            grouping_frame, 8, "Identified value type", condition_cast,
            tuple(GROUPING_VALUE_TYPES), readonly=True,
        )
        ttk.Label(
            grouping_frame,
            text=(
                "Important: every enabled condition splits the data into smaller correlation groups. Device identifiers "
                "such as DUT Nr normally belong in Detail key columns, unless a separate factor per DUT is intentional "
                "and each DUT has at least Minimum points/group repetitions."
            ),
            style="Hint.TLabel",
            wraplength=760,
        ).grid(row=9, column=1, columnspan=2, pady=(8, 2), sticky="w")

        def method_hint(method: str) -> str:
            return {
                "Use existing column": "No rule needed: CorreLaTE reads the selected input column directly.",
                "Text mappings (no regex)": (
                    "One literal mapping per line; for example:\nHOT => 125\nRT => 25\n095 => VMIN"
                ),
                "Number after prefix (no regex)": (
                    "Enter only the text before the number; for example: FwLu extracts 255 from FwLu255."
                ),
                "Advanced regex": (
                    r"Optional expert mode. Use one capture group; for example: CH(\d+) extracts 3 from CH3."
                ),
            }[method]

        def update_identification_controls(*_args: object) -> None:
            existing = condition_method.get() == "Use existing column"
            if existing:
                condition_source.set("Existing input column")
            elif condition_source.get() == "Existing input column":
                condition_source.set("Test name")
            source_combo.configure(state="disabled" if existing else "readonly")
            condition_expression.configure(state="disabled" if existing else "normal")
            condition_default_entry.configure(state="disabled" if existing else "normal")
            cast_combo.configure(
                state="readonly" if condition_method.get() in {"Number after prefix (no regex)", "Advanced regex"}
                else "disabled"
            )
            condition_rule_hint.set(method_hint(condition_method.get()))

        def store_current_condition() -> None:
            if current_condition["loading"] or not grouping_definitions:
                return
            index = int(current_condition["index"])
            definition = grouping_definitions[index]
            definition.update({
                "enabled": condition_enabled.get(),
                "label": condition_label.get().strip(),
                "column": condition_column.get().strip(),
                "source": GROUPING_SOURCES[condition_source.get()],
                "method": GROUPING_METHODS[condition_method.get()],
                "expression": condition_expression.get("1.0", "end-1c").strip(),
                "default": condition_default.get().strip(),
                "cast": GROUPING_VALUE_TYPES[condition_cast.get()],
            })

        def condition_selector_values() -> tuple[str, ...]:
            return tuple(
                f"{'✓' if item.get('enabled') else '○'} {item.get('label') or 'Unnamed condition'}"
                for item in grouping_definitions
            )

        def load_condition(index: int) -> None:
            definition = grouping_definitions[index]
            current_condition.update(index=index, loading=True)
            condition_combo.configure(values=condition_selector_values())
            condition_combo.current(index)
            condition_enabled.set(bool(definition.get("enabled")))
            condition_label.set(str(definition.get("label", "")))
            condition_column.set(str(definition.get("column", "")))
            condition_source.set(next(
                label for label, value in GROUPING_SOURCES.items()
                if value == str(definition.get("source", "existing"))
            ))
            condition_method.set(next(
                label for label, value in GROUPING_METHODS.items()
                if value == str(definition.get("method", "existing"))
            ))
            condition_default.set(str(definition.get("default", "")))
            condition_cast.set(next(
                label for label, value in GROUPING_VALUE_TYPES.items()
                if value == str(definition.get("cast", "str"))
            ))
            condition_expression.configure(state="normal")
            condition_expression.delete("1.0", "end")
            condition_expression.insert("1.0", str(definition.get("expression", "")))
            current_condition["loading"] = False
            update_identification_controls()
            condition_label_entry.configure(state="normal" if definition.get("custom") else "disabled")

        def choose_condition(_event: object | None = None) -> None:
            store_current_condition()
            load_condition(max(condition_combo.current(), 0))

        def add_condition() -> None:
            store_current_condition()
            next_index = 1 + sum(bool(item.get("custom")) for item in grouping_definitions)
            grouping_definitions.append({
                "key": f"custom_{next_index}",
                "label": f"Custom Condition {next_index}",
                "column": f"Custom Condition {next_index}",
                "enabled": True,
                "source": "test_name",
                "method": "mapping",
                "expression": "",
                "default": "Unknown",
                "cast": "str",
                "custom": True,
            })
            load_condition(len(grouping_definitions) - 1)

        def remove_condition() -> None:
            index = int(current_condition["index"])
            if not bool(grouping_definitions[index].get("custom")):
                messagebox.showinfo(
                    "Standard grouping condition",
                    "Standard conditions can be disabled but not removed.",
                    parent=self.root,
                )
                return
            del grouping_definitions[index]
            load_condition(min(index, len(grouping_definitions) - 1))

        ttk.Button(condition_buttons, text="Add…", command=add_condition).pack(side="left", padx=3)
        ttk.Button(condition_buttons, text="Remove", command=remove_condition).pack(side="left", padx=3)
        condition_combo.bind("<<ComboboxSelected>>", choose_condition)
        condition_method.trace_add("write", update_identification_controls)
        load_condition(0)

        self._profile_grouping_definitions = grouping_definitions
        add_entry(
            basics, 5, "Lab/reference column", "reference_column",
            "Column containing the Lab/CV result; for example: Lab Current or CV_PA_Power.",
        )
        add_entry(
            basics, 6, "ATE/candidate column", "candidate_column",
            "Column containing the ATE result; for example: Test Value or ATE_PA_Power.",
        )
        add_entry(
            basics, 7, "Minimum points/group", "minimum_points",
            "Smallest sample count accepted for one correlation group; for example: 5.",
        )
        add_entry(
            basics, 8, "Detail key columns", "detail_key_columns",
            "Identifiers retained in row-level output; for example: DUT Nr, Wafer, X, Y.",
        )

        add_entry(
            correlation, 0, "Lower limit column", "lower_limit_column",
            "Input column holding original lower limits; for example: Low or LSL.",
        )
        add_entry(
            correlation, 1, "Upper limit column", "upper_limit_column",
            "Input column holding original upper limits; for example: High or USL.",
        )
        add_entry(
            correlation, 2, "Unit column", "unit_column",
            "Input column holding measurement units; for example: Unit with values dBm, V, or A.",
        )
        add_entry(
            correlation, 3, "Test-name column", "test_name_column",
            "Input column holding descriptive test names; for example: Test Name.",
        )
        ttk.Separator(correlation).grid(row=4, column=0, columnspan=3, pady=10, sticky="ew")
        self._add_combo(
            correlation, 5, "Automatic Physics/Kf model", variables["physics_kf_enabled"],
            ("Enabled", "Disabled"),
        )
        add_entry(
            correlation, 6, "Kf raw value column", "covariate_value_column",
            "Numeric result column on the raw Kf test row; normally Test Value.",
        )
        add_entry(
            correlation, 7, "Kf merge keys", "covariate_merge_keys",
            "Raw-data columns identifying one Kf; for example: DUT Nr, Temperature, Insertion.",
        )
        add_entry(
            correlation, 8, "Kf output name", "covariate_output_name",
            "Internal name assigned after the join; use Kf for the Physics-based model.",
        )
        add_entry(
            correlation, 9, "Kf test number", "covariate_test_number",
            "Raw-data test number containing Kf. The default is 52046.",
        )
        ttk.Label(
            correlation,
            text=(
                "Step 2 extracts this Kf test directly from every assigned raw file, joins it to the selected correlation "
                "tests by DUT, temperature, and insertion, stores it under the output name, and removes the Kf test rows "
                "before creating the CV request. Each key combination must map to one numeric Kf. Disable automatic "
                "Physics/Kf only for profiles where this model is not applicable."
            ),
            style="Hint.TLabel",
            wraplength=760,
        ).grid(row=10, column=1, columnspan=2, sticky="w")

        actions = ttk.Frame(outer)
        actions.grid(row=4, column=0, pady=(12, 0), sticky="e")

        def apply_grouping_definitions(spec: Mapping[str, Any]) -> None:
            grouping_definitions[:] = grouping_condition_definitions(spec)
            load_condition(0)

        def apply_test_sets(spec: Mapping[str, Any]) -> None:
            configured_test_sets[:] = test_set_definitions(spec)
            load_test_set(0)

        def apply_insertions(spec: Mapping[str, Any]) -> None:
            configured_insertions[:] = insertion_definitions(spec)
            if configured_insertions:
                load_insertion(0)
            else:
                current_insertion["index"] = -1
                insertion_combo.configure(values=())
                selected_insertion.set("")
                insertion_name.set("")
                insertion_temperature.set("")
                insertion_files.delete(0, "end")

        def apply_coordinate_fallback(spec: Mapping[str, Any]) -> None:
            values = coordinate_fallback_values(spec)
            for coordinate, variable in coordinate_fallback_vars.items():
                variable.set(values[coordinate])

        def refresh_custom_list(preferred: str = "") -> None:
            try:
                names = sorted(load_custom_profile_specs())
            except ValueError as error:
                custom_combo.configure(values=())
                selected.set("")
                self.status.set(f"Profile store error: {error}")
                return
            custom_combo.configure(values=names)
            if preferred in names:
                selected.set(preferred)
            elif names:
                selected.set(names[0])
            else:
                selected.set("")

        def clear_editor() -> None:
            selected.set("")
            for key, default in defaults.items():
                variables[key].set(default)
            apply_insertions(defaults)
            apply_coordinate_fallback(defaults)
            apply_test_sets(defaults)
            apply_grouping_definitions(defaults)
            variables["profile_id"].set("")
            variables["display_name"].set("")
            variables["tests"].set("")
            editor.select(insertion_scroll)

        def load_selected() -> None:
            profile_id = selected.get().strip()
            if not profile_id:
                messagebox.showinfo("Custom profiles", "No custom profile is selected.", parent=self.root)
                return
            try:
                spec = load_custom_profile_specs()[profile_id]
            except Exception as error:
                messagebox.showerror("Cannot load profile", str(error), parent=self.root)
                return
            for key, default in defaults.items():
                variables[key].set(str(spec.get(key, default)))
            if variables["physics_kf_enabled"].get().strip().casefold() not in {"disabled", "false", "no", "off", "0"}:
                if not variables["covariate_value_column"].get().strip():
                    variables["covariate_value_column"].set("Test Value")
                if not variables["covariate_merge_keys"].get().strip():
                    variables["covariate_merge_keys"].set("DUT Nr, Temperature, Insertion")
                if variables["covariate_output_name"].get().strip().casefold() in {"", "covariate"}:
                    variables["covariate_output_name"].set("Kf")
            apply_insertions(spec)
            apply_coordinate_fallback(spec)
            apply_test_sets(spec)
            apply_grouping_definitions(spec)
            variables["profile_id"].set(profile_id)
            editor.select(insertion_scroll)

        def save_profile() -> None:
            profile_id = variables["profile_id"].get().strip()
            spec = {key: variable.get().strip() for key, variable in variables.items() if key != "profile_id"}
            try:
                spec["coordinate_fallback"] = compile_coordinate_fallback({
                    coordinate: variable.get()
                    for coordinate, variable in coordinate_fallback_vars.items()
                })
                store_current_test_set()
                spec["test_sets"] = validate_test_set_definitions(configured_test_sets)
                primary_test_set = spec["test_sets"][0]
                spec["tests"] = ", ".join(
                    str(test_set["tests"]) for test_set in spec["test_sets"]
                )
                spec["strategy"] = primary_test_set["strategy"]
                spec["guard_band_kind"] = primary_test_set["guard_band_kind"]
                spec["sigma_multiplier"] = primary_test_set["sigma_multiplier"]
                spec["requirement_min"] = primary_test_set["requirement_min"]
                spec["requirement_max"] = primary_test_set["requirement_max"]
                store_current_insertion()
                spec["insertions"] = validate_insertion_definitions(configured_insertions)
                store_current_condition()
                group_by, condition_rules, regex_rules = compile_grouping_conditions(grouping_definitions)
                spec["group_by"] = ", ".join(correlation_group_columns(group_by))
                spec["condition_rules"] = condition_rules
                spec["regex_rules"] = regex_rules
                spec["grouping_conditions"] = [dict(definition) for definition in grouping_definitions]
                save_custom_profile_spec(profile_id, spec)
                refresh_profiles(strict=True)
                self._refresh_profile_choices()
                refresh_custom_list(profile_id)
            except Exception as error:
                messagebox.showerror("Cannot save profile", str(error), parent=self.root)
                return
            self.status.set(f"Saved custom profile '{profile_id}'")
            messagebox.showinfo(
                "Profile saved",
                f"'{profile_id}' is now available in all CorreLaTE workflows and CLI commands.",
                parent=self.root,
            )

        def remove_profile() -> None:
            profile_id = selected.get().strip() or variables["profile_id"].get().strip()
            if not profile_id:
                messagebox.showinfo("Custom profiles", "No custom profile is selected.", parent=self.root)
                return
            if profile_id in builtin_profile_ids():
                messagebox.showerror("Read-only profile", "Built-in profiles cannot be deleted.", parent=self.root)
                return
            if not messagebox.askyesno(
                "Delete custom profile",
                f"Delete '{profile_id}'?",
                parent=self.root,
            ):
                return
            try:
                if not delete_custom_profile(profile_id):
                    raise ValueError(f"Custom profile '{profile_id}' does not exist")
                refresh_profiles(strict=True)
                self._refresh_profile_choices()
                refresh_custom_list()
                clear_editor()
            except Exception as error:
                messagebox.showerror("Cannot delete profile", str(error), parent=self.root)
                return
            self.status.set(f"Deleted custom profile '{profile_id}'")

        ttk.Button(actions, text="Delete", command=remove_profile).pack(side="left", padx=4)
        ttk.Button(actions, text="Validate & Save", command=save_profile).pack(side="left", padx=4)
        custom_combo.bind("<<ComboboxSelected>>", lambda _event: load_selected())
        refresh_custom_list()

    def _build_extraction_tab(self) -> None:
        form = self._make_tab(
            "2 · Extract TE",
            "Extract normalized ATE measurements",
            "INPUT: raw TE data and chip manifest. OUTPUT: a new formatted workbook containing Extracted_Data.",
        )
        profile = tk.StringVar(value=next(iter(EXTRACTION_PROFILES)))
        raw_folder = tk.StringVar()
        chip_manifest = tk.StringVar()
        output = tk.StringVar()
        self._add_registered_profile_combo(form, 0, "Extraction profile", profile, extraction=True)
        raw_entry, raw_button = self._add_path(
            form,
            1,
            "Raw TE folder (built-ins)",
            raw_folder,
            lambda: self._choose_folder(raw_folder, "Select raw TE data folder"),
            direction="input",
            button_text="Select…",
        )
        self._add_path(
            form,
            2,
            "Chip manifest",
            chip_manifest,
            lambda: self._choose_open(chip_manifest),
            direction="input",
        )
        self._add_path(
            form,
            3,
            "Extracted workbook",
            output,
            lambda: self._choose_save(output, "Save extracted ATE workbook"),
            direction="output",
        )

        def run() -> None:
            values = {
                "profile": profile.get(),
                "raw TE folder": raw_folder.get(),
                "chip manifest": chip_manifest.get(),
                "output workbook": output.get(),
            }
            required = {key: value for key, value in values.items() if key != "raw TE folder"}
            selected_profile = get_extraction_profile(values["profile"])
            if not selected_profile.insertions:
                required["raw TE folder"] = values["raw TE folder"]
            if not self._validate_required(**required):
                return

            def action() -> str:
                frame = LegacyWideTeCsvAdapter().extract(
                    Path(values["raw TE folder"] or "."),
                    Path(values["chip manifest"]),
                    get_extraction_profile(values["profile"]),
                )
                correlation_profile = CORRELATION_PROFILES.get(values["profile"])
                if correlation_profile is not None and correlation_profile.covariate is not None:
                    frame = attach_covariate_from_test_rows(frame, correlation_profile)
                destination = Path(values["output workbook"])
                write_dataframe_workbook(destination, {"Extracted_Data": frame})
                return f"Extracted {len(frame):,} rows to {destination}."

            self._start_job(run_button, "Extracting raw TE data…", action)

        def update_raw_folder_state(*_args: object) -> None:
            uses_assigned_files = bool(get_extraction_profile(profile.get()).insertions)
            state = ["disabled"] if uses_assigned_files else ["!disabled"]
            raw_entry.state(state)
            raw_button.state(state)
            if uses_assigned_files:
                raw_folder.set("Files assigned in profile Insertions")
            elif raw_folder.get() == "Files assigned in profile Insertions":
                raw_folder.set("")

        run_button = ttk.Button(form, text="Run extraction", command=run)
        run_button.grid(row=4, column=1, pady=(18, 0), sticky="e")
        profile.trace_add("write", update_raw_folder_state)
        update_raw_folder_state()

    def _build_request_tab(self) -> None:
        form = self._make_tab(
            "3 · Create CV Request",
            "Create an editable CV measurement request",
            "INPUT: the extracted ATE workbook. OUTPUT: a CV request to send to Lab/CV and a separate internal ATE "
            "manifest to keep for Step 4; the manifest is generated here, not loaded.",
        )
        profile = tk.StringVar(value=next(iter(CORRELATION_PROFILES)))
        source = tk.StringVar()
        sheet = tk.StringVar()
        value_column = tk.StringVar(value="Test Value")
        request_output = tk.StringVar()
        manifest_output = tk.StringVar()
        self._add_registered_profile_combo(form, 0, "Correlation profile", profile)
        sheet_combo = self._add_combo(form, 2, "Input sheet", sheet, ())
        self._add_path(
            form,
            1,
            "Extracted workbook",
            source,
            lambda: self._choose_open(source, sheet, sheet_combo),
            direction="input",
        )
        self._add_label(form, 3, "ATE value column")
        ttk.Entry(form, textvariable=value_column).grid(row=3, column=1, padx=(0, 8), pady=7, sticky="ew")
        ttk.Label(
            form,
            text="Column containing the extracted ATE measurements; for example: Test Value.",
            style="Hint.TLabel",
            wraplength=300,
        ).grid(row=3, column=2, pady=7, sticky="w")
        self._add_path(
            form,
            4,
            "CV request workbook (send to Lab/CV)",
            request_output,
            lambda: self._choose_save(request_output, "Save CV measurement request"),
            direction="output",
        )
        self._add_path(
            form,
            5,
            "Internal ATE manifest (keep for Step 4)",
            manifest_output,
            lambda: self._choose_save(manifest_output, "Save internal ATE manifest"),
            direction="output",
        )

        def run() -> None:
            values = {
                "profile": profile.get(),
                "input workbook": source.get(),
                "input sheet": sheet.get(),
                "ATE value column": value_column.get(),
                "CV request workbook": request_output.get(),
                "ATE manifest workbook": manifest_output.get(),
            }
            if not self._validate_required(**values):
                return
            if Path(values["CV request workbook"]).resolve() == Path(values["ATE manifest workbook"]).resolve():
                messagebox.showerror(
                    "Invalid output paths",
                    "The CV request and internal ATE manifest must use different files.",
                    parent=self.root,
                )
                return

            def action() -> str:
                frame = pd.read_excel(Path(values["input workbook"]), sheet_name=values["input sheet"])
                request, _manifest = create_measurement_request(
                    frame,
                    get_correlation_profile(values["profile"]),
                    Path(values["CV request workbook"]),
                    Path(values["ATE manifest workbook"]),
                    candidate_value_column=values["ATE value column"],
                )
                return f"Created {len(request):,} CV requests and the separate internal ATE manifest."

            self._start_job(run_button, "Creating editable CV request…", action)

        run_button = ttk.Button(form, text="Create request", command=run)
        run_button.grid(row=6, column=1, pady=(18, 0), sticky="e")

    def _build_import_tab(self) -> None:
        form = self._make_tab(
            "4 · Import CV Results",
            "Validate and align returned CV measurements",
            "INPUT: the completed CV request and internal ATE manifest from Step 3. OUTPUT: a new aligned correlation workbook.",
        )
        profile = tk.StringVar(value=next(iter(CORRELATION_PROFILES)))
        returned = tk.StringVar()
        returned_sheet = tk.StringVar(value=REQUEST_SHEET)
        manifest = tk.StringVar()
        manifest_sheet = tk.StringVar(value=MANIFEST_SHEET)
        output = tk.StringVar()
        self._add_registered_profile_combo(form, 0, "Correlation profile", profile)
        returned_combo = self._add_combo(form, 2, "Returned sheet", returned_sheet, ())
        self._add_path(
            form,
            1,
            "Returned CV workbook (completed by Lab/CV)",
            returned,
            lambda: self._choose_open(returned, returned_sheet, returned_combo),
            direction="input",
        )
        manifest_combo = self._add_combo(form, 4, "Manifest sheet", manifest_sheet, ())
        self._add_path(
            form,
            3,
            "Internal ATE manifest (created in Step 3)",
            manifest,
            lambda: self._choose_open(manifest, manifest_sheet, manifest_combo),
            direction="input",
        )
        self._add_path(
            form,
            5,
            "Aligned correlation input",
            output,
            lambda: self._choose_save(output, "Save aligned correlation input"),
            direction="output",
        )

        def run() -> None:
            values = {
                "profile": profile.get(),
                "returned workbook": returned.get(),
                "returned sheet": returned_sheet.get(),
                "ATE manifest": manifest.get(),
                "manifest sheet": manifest_sheet.get(),
                "output workbook": output.get(),
            }
            if not self._validate_required(**values):
                return

            def action() -> str:
                frame = import_measurement_results(
                    Path(values["returned workbook"]),
                    Path(values["ATE manifest"]),
                    get_correlation_profile(values["profile"]),
                    returned_sheet=values["returned sheet"],
                    manifest_sheet=values["manifest sheet"],
                )
                destination = Path(values["output workbook"])
                write_dataframe_workbook(destination, {"Correlation_Input": frame})
                return f"Validated and aligned {len(frame):,} one-to-one rows to {destination}."

            self._start_job(run_button, "Validating returned CV measurements…", action)

        run_button = ttk.Button(form, text="Import results", command=run)
        run_button.grid(row=6, column=1, pady=(18, 0), sticky="e")

    def _build_correlation_tab(self) -> None:
        form = self._make_tab(
            "5 · Correlate",
            "Generate factors, guard-bands, Excel data, and an HTML sign-off report",
            "INPUT: the aligned correlation workbook. Kf is extracted from raw data in Step 2 and retained internally "
            "through Steps 3–4. OUTPUT: a formatted Excel report and optional self-contained HTML review artifact.",
        )
        profile = tk.StringVar(value=next(iter(CORRELATION_PROFILES)))
        source = tk.StringVar()
        sheet = tk.StringVar()
        report = tk.StringVar()
        html_report = tk.StringVar()
        covariate_hint = tk.StringVar()
        mad_threshold = tk.StringVar(value=f"{DEFAULT_MAD_THRESHOLD:g}")
        allow_outlier_filtering = tk.BooleanVar(value=False)
        self._add_registered_profile_combo(form, 0, "Correlation profile", profile)
        sheet_combo = self._add_combo(form, 2, "Input sheet", sheet, ())
        self._add_path(
            form,
            1,
            "Correlation input",
            source,
            lambda: self._choose_open(source, sheet, sheet_combo),
            direction="input",
        )
        self._add_label(form, 3, "Automatic Kf source")
        ttk.Label(form, textvariable=covariate_hint, style="Hint.TLabel", wraplength=720).grid(
            row=3, column=1, columnspan=2, pady=7, sticky="w"
        )
        self._add_label(form, 4, "Outlier threshold (n × scaled MAD)")
        ttk.Entry(form, textvariable=mad_threshold).grid(
            row=4, column=1, padx=(0, 8), pady=7, sticky="ew"
        )
        ttk.Label(
            form,
            text=(
                "Pre-fit review threshold using |x−median| / (1.4826×MAD) > n. Default: 12. "
                "Detection is applied separately to each unpooled test/corner population."
            ),
            style="Hint.TLabel",
            wraplength=420,
        ).grid(row=4, column=2, pady=7, sticky="w")
        ttk.Checkbutton(
            form,
            text="Allow manual exclusion of selected flagged rows (disabled by default)",
            variable=allow_outlier_filtering,
        ).grid(row=5, column=1, columnspan=2, pady=7, sticky="w")
        self._add_path(
            form,
            6,
            "Excel report",
            report,
            lambda: self._choose_save(report, "Save correlation report"),
            direction="output",
        )
        self._add_path(
            form,
            7,
            "HTML sign-off report (optional)",
            html_report,
            lambda: self._choose_save_html(html_report, "Save HTML sign-off report"),
            direction="output",
        )

        def update_covariate_state(*_args: object) -> None:
            config = get_correlation_profile(profile.get()).covariate
            covariate_hint.set(
                (
                    f"Embedded by Step 2 from raw test {config.test_number}: read '{config.value_column}', "
                    f"join by {', '.join(config.merge_keys)}, store as '{config.output_name}'."
                )
                if config is not None
                else "Automatic Physics/Kf is disabled for this profile"
            )

        def run() -> None:
            values = {
                "profile": profile.get(),
                "input workbook": source.get(),
                "input sheet": sheet.get(),
                "report workbook": report.get(),
                "HTML report": html_report.get(),
                "MAD threshold": mad_threshold.get(),
            }
            if not self._validate_required(**{
                key: values[key]
                for key in ("profile", "input workbook", "input sheet", "report workbook")
            }):
                return
            selected = get_correlation_profile(values["profile"])
            try:
                selected_threshold = float(values["MAD threshold"])
            except ValueError:
                messagebox.showerror(
                    "Invalid MAD threshold",
                    "Outlier threshold must be numeric.",
                    parent=self.root,
                )
                return
            if not math.isfinite(selected_threshold) or selected_threshold <= 0:
                messagebox.showerror(
                    "Invalid MAD threshold",
                    "Outlier threshold must be a finite number greater than zero.",
                    parent=self.root,
                )
                return

            def analyze_action() -> tuple[pd.DataFrame, OutlierAnalysis]:
                frame = pd.read_excel(Path(values["input workbook"]), sheet_name=values["input sheet"])
                if selected.covariate and selected.covariate.output_name not in frame.columns:
                    raise ValueError(
                        f"Aligned input is missing embedded Kf column '{selected.covariate.output_name}'. "
                        f"Rerun Step 2 with this profile so raw test {selected.covariate.test_number} is extracted, "
                        "then repeat Steps 3 and 4."
                    )
                return frame, analyze_outliers(frame, selected, selected_threshold)

            def review_ready(payload: Any) -> None:
                _frame, analysis = payload
                reviewed = self._show_outlier_review_dialog(
                    analysis,
                    selected,
                    allow_filtering=allow_outlier_filtering.get(),
                )
                if reviewed is None:
                    self.status.set("Correlation cancelled during outlier review")
                    return
                filtered_frame, review = reviewed

                def report_action() -> str:
                    result = correlate_frame(filtered_frame, selected)
                    result = attach_outlier_audit(result, selected, review)
                    destination = Path(values["report workbook"])
                    write_excel_report(result, selected, destination)
                    embedded_plot_count = 0
                    html_destination = values["HTML report"].strip()
                    if html_destination:
                        embedded_plot_count = write_html_report(
                            result, selected, Path(html_destination)
                        )
                    message = (
                        f"Generated {len(result.summary):,} correlation groups in {destination}; "
                        f"outlier review flagged {review.flagged_count:,} and excluded "
                        f"{review.excluded_count:,} raw sample(s)"
                    )
                    if html_destination:
                        message += (
                            f" and embedded {embedded_plot_count:,} plots in {html_destination}"
                        )
                    return message + "."

                self._start_job(
                    run_button,
                    "Calculating reviewed correlations and generating outputs…",
                    report_action,
                )

            self._start_job(
                run_button,
                "Checking Lab/CV, ATE/TE, and paired series for outliers…",
                analyze_action,
                on_success=review_ready,
            )

        run_button = ttk.Button(form, text="Generate report", command=run)
        run_button.grid(row=8, column=1, pady=(18, 0), sticky="e")
        profile.trace_add("write", update_covariate_state)
        update_covariate_state()

    def _build_yield_forecast_tab(self) -> None:
        form = self._make_tab(
            "6 · Forecast Yield",
            "Forecast productive yield using correlated tests and limits",
            "INPUT: the Section 5 correlation report and uncorrelated productive wide-CSV data assigned per selected "
            "Section 1 insertion. OUTPUT: one self-contained HTML report with sample-based CDF plots, correlated limits, "
            "yield statistics, and highlighted failures.",
        )
        profile = tk.StringVar(value=next(iter(CORRELATION_PROFILES)))
        correlation_report = tk.StringVar()
        correlation_sheet = tk.StringVar(value="Correlation_Summary")
        html_output = tk.StringVar()
        insertion_selector = tk.StringVar()
        insertion_selected = tk.BooleanVar(value=False)
        insertion_summary = tk.StringVar()
        assignments: list[dict[str, Any]] = []
        current = {"index": -1, "loading": False}

        self._add_registered_profile_combo(form, 0, "Correlation profile", profile)
        correlation_sheet_combo = self._add_combo(
            form, 2, "Correlation summary sheet", correlation_sheet, ()
        )
        self._add_path(
            form,
            1,
            "Section 5 correlation report",
            correlation_report,
            lambda: self._choose_open(
                correlation_report,
                correlation_sheet,
                correlation_sheet_combo,
            ),
            direction="input",
        )

        insertion_frame = ttk.LabelFrame(
            form,
            text="Productive CSV input by Section 1 insertion",
            padding=10,
        )
        insertion_frame.grid(row=3, column=0, columnspan=3, pady=10, sticky="ew")
        insertion_frame.columnconfigure(1, weight=1)
        self._add_label(insertion_frame, 0, "Insertion")
        insertion_combo = ttk.Combobox(
            insertion_frame,
            textvariable=insertion_selector,
            state="readonly",
        )
        insertion_combo.grid(row=0, column=1, padx=(0, 8), pady=5, sticky="ew")
        ttk.Label(
            insertion_frame,
            textvariable=insertion_summary,
            style="Hint.TLabel",
            wraplength=360,
        ).grid(row=0, column=2, sticky="w")
        ttk.Checkbutton(
            insertion_frame,
            text="Include this insertion in the yield forecast",
            variable=insertion_selected,
        ).grid(row=1, column=1, columnspan=2, pady=5, sticky="w")
        self._add_label(insertion_frame, 2, "Productive raw CSV files")
        file_area = ttk.Frame(insertion_frame)
        file_area.grid(row=2, column=1, columnspan=2, pady=5, sticky="ew")
        file_area.columnconfigure(0, weight=1)
        productive_files = tk.Listbox(file_area, height=6, selectmode="extended")
        productive_files.grid(row=0, column=0, rowspan=2, sticky="ew")
        file_scroll = ttk.Scrollbar(file_area, orient="vertical", command=productive_files.yview)
        file_scroll.grid(row=0, column=1, rowspan=2, sticky="ns")
        productive_files.configure(yscrollcommand=file_scroll.set)

        def selector_values() -> tuple[str, ...]:
            return tuple(
                f"{'✓' if item['selected'] else '○'} {item['name']} · {item['group']} · "
                f"{float(item['temperature']):g} °C · {len(item['files'])} file(s)"
                for item in assignments
            )

        def store_current() -> None:
            if current["loading"] or current["index"] < 0 or not assignments:
                return
            index = int(current["index"])
            assignments[index]["selected"] = insertion_selected.get()
            assignments[index]["files"] = list(productive_files.get(0, "end"))
            insertion_combo.configure(values=selector_values())
            insertion_combo.current(index)

        def load_insertion(index: int) -> None:
            if not assignments:
                current["index"] = -1
                insertion_combo.configure(values=())
                insertion_selector.set("")
                insertion_summary.set(
                    "No insertions are configured. Define and save them in Section 1 first."
                )
                insertion_selected.set(False)
                productive_files.delete(0, "end")
                return
            item = assignments[index]
            current.update(index=index, loading=True)
            insertion_combo.configure(values=selector_values())
            insertion_combo.current(index)
            insertion_selected.set(bool(item["selected"]))
            productive_files.delete(0, "end")
            for path in item["files"]:
                productive_files.insert("end", path)
            insertion_summary.set(
                f"{item['group']} insertion at {float(item['temperature']):g} °C. "
                "Only newly selected productive data is used; Section 1 characterization files are not reused."
            )
            current["loading"] = False

        def choose_insertion(_event: object | None = None) -> None:
            selected_index = max(insertion_combo.current(), 0)
            store_current()
            load_insertion(selected_index)

        def add_productive_files() -> None:
            if current["index"] < 0:
                messagebox.showinfo(
                    "No insertion",
                    "Define insertions in Section 1 before assigning productive data.",
                    parent=self.root,
                )
                return
            paths = filedialog.askopenfilenames(
                parent=self.root,
                title="Select productive raw CSV files for this insertion",
                filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            )
            existing = set(productive_files.get(0, "end"))
            for path in paths:
                if path not in existing:
                    productive_files.insert("end", path)
                    existing.add(path)
            if paths:
                insertion_selected.set(True)
                store_current()

        def remove_productive_files() -> None:
            for index in reversed(productive_files.curselection()):
                productive_files.delete(index)
            store_current()

        file_buttons = ttk.Frame(file_area)
        file_buttons.grid(row=0, column=2, padx=(8, 0), sticky="n")
        ttk.Button(file_buttons, text="Add CSVs…", command=add_productive_files).pack(
            fill="x", pady=(0, 5)
        )
        ttk.Button(file_buttons, text="Remove", command=remove_productive_files).pack(fill="x")
        insertion_combo.bind("<<ComboboxSelected>>", choose_insertion)
        insertion_selected.trace_add("write", lambda *_args: store_current())

        self._add_path(
            form,
            4,
            "Yield forecast HTML report",
            html_output,
            lambda: self._choose_save_html(html_output, "Save correlated yield forecast report"),
            direction="output",
        )
        ttk.Label(
            form,
            text=(
                "Productive values remain unmodified. CorreLaTE applies the approved factor for each matching test, "
                "insertion, and condition; checks inclusive correlated limits; and reports empirical yield from the "
                "provided samples. Physics-based test sets additionally require productive Kf rows in the CSVs."
            ),
            style="Hint.TLabel",
            wraplength=820,
        ).grid(row=5, column=0, columnspan=3, pady=(4, 8), sticky="w")

        def load_profile_insertions(*_args: object) -> None:
            assignments.clear()
            extraction_profile = get_extraction_profile(profile.get())
            assignments.extend({
                "name": insertion.name,
                "group": insertion.group,
                "temperature": insertion.temperature,
                "selected": False,
                "files": [],
            } for insertion in extraction_profile.insertions)
            load_insertion(0)

        def run() -> None:
            store_current()
            values = {
                "profile": profile.get(),
                "correlation report": correlation_report.get(),
                "correlation summary sheet": correlation_sheet.get(),
                "yield forecast HTML": html_output.get(),
            }
            if not self._validate_required(**values):
                return
            try:
                selected_assignments = validate_productive_insertion_inputs(
                    assignments,
                    get_extraction_profile(values["profile"]).insertions,
                )
            except Exception as error:
                messagebox.showerror(
                    "Invalid productive data assignment",
                    str(error),
                    parent=self.root,
                )
                return

            def action() -> str:
                selected_profile = get_correlation_profile(values["profile"])
                factors = pd.read_excel(
                    Path(values["correlation report"]),
                    sheet_name=values["correlation summary sheet"],
                )
                productive = load_productive_csv_inputs(
                    selected_assignments,
                    get_extraction_profile(values["profile"]),
                    selected_profile,
                )
                result = forecast_yield(productive, factors, selected_profile)
                destination = Path(values["yield forecast HTML"])
                plot_count = write_yield_forecast_html(result, selected_profile, destination)
                samples = int(result.summary["SampleCount"].sum())
                failures = int(result.summary["FailCount"].sum())
                skipped = len(result.rejected)
                yield_percent = 100.0 * (samples - failures) / samples
                affected = int(result.summary.loc[
                    result.summary["FailCount"].gt(0), "Test Number"
                ].nunique())
                return (
                    f"Forecasted {samples:,} productive samples at {yield_percent:.6g}% yield; "
                    f"{failures:,} failure(s) across {affected:,} affected test(s). "
                    f"Skipped {skipped:,} blank/non-numeric row(s). "
                    f"Embedded {plot_count:,} insertion CDF plots in {destination}."
                )

            self._start_job(
                run_button,
                "Extracting productive CSV data and forecasting correlated yield…",
                action,
            )

        run_button = ttk.Button(form, text="Generate yield forecast", command=run)
        run_button.grid(row=6, column=1, pady=(14, 0), sticky="e")
        ttk.Button(
            insertion_frame,
            text="Reload Section 1 insertions",
            command=load_profile_insertions,
        ).grid(row=3, column=1, pady=(6, 0), sticky="w")
        profile.trace_add("write", load_profile_insertions)
        load_profile_insertions()


def launch() -> None:
    root = tk.Tk()
    CorrelationDesktopApp(root)
    root.mainloop()


if __name__ == "__main__":
    launch()
