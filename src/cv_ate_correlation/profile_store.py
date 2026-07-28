"""Persistent custom profile definitions for CorreLaTE."""

from __future__ import annotations

import json
import math
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

from .models import (
    CorrelationProfile,
    CovariateProfile,
    DEFAULT_COORDINATE_FALLBACK,
    DerivedField,
    ExtractionProfile,
    GuardBandProfile,
    InsertionProfile,
    MatchCase,
    RegexField,
    TestSelector,
)

SCHEMA_VERSION = 1
BASE_OUTPUT_COLUMNS = (
    "DUT Nr",
    "Wafer",
    "X",
    "Y",
    "DoE split",
    "Test Number",
    "Test Name",
    "Test Value",
    "Low",
    "High",
    "Unit",
    "Insertion",
    "Insertion Type",
    "Temperature",
)
_PROFILE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")


def profile_store_path() -> Path:
    """Return the per-user custom-profile store, overridable for automation."""
    override = os.environ.get("CORRELATE_PROFILE_STORE")
    if override:
        return Path(override).expanduser()
    base = Path(os.environ.get("APPDATA", Path.home()))
    return base / "CorreLaTE" / "profiles.json"


def _split_csv(text: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in re.split(r"[,\n]", text) if value.strip())


def _parse_scalar(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        return ""
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return stripped


def parse_test_selector(expression: str) -> TestSelector:
    """Parse numbers, inclusive ranges, and name fragments from a compact expression."""
    exact: list[int] = []
    ranges: list[tuple[int, int]] = []
    names: list[str] = []
    for token in re.split(r"[,;\n]", expression):
        token = token.strip()
        if not token:
            continue
        if token.isdigit():
            exact.append(int(token))
            continue
        range_match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
        if range_match:
            start, end = (int(value) for value in range_match.groups())
            if start > end:
                raise ValueError(f"Invalid descending test range: {token}")
            ranges.append((start, end))
            continue
        names.append(token)
    if not exact and not ranges and not names:
        raise ValueError("At least one test number, range, or name fragment is required")
    return TestSelector(tuple(dict.fromkeys(exact)), tuple(dict.fromkeys(ranges)), tuple(dict.fromkeys(names)))


def parse_condition_rules(text: str) -> tuple[DerivedField, ...]:
    """Parse `target ; source ; mode ; pattern ; value ; default` lines."""
    grouped: dict[tuple[str, str, Any], list[MatchCase]] = defaultdict(list)
    target_defaults: dict[tuple[str, str], Any] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split(";")]
        if len(parts) != 6:
            raise ValueError(f"Condition rule line {line_number} must contain 6 semicolon-separated fields")
        target, source, mode, pattern, value_text, default_text = parts
        if not target or source not in {"filename", "test_name"} or mode not in {"contains", "regex"} or not pattern:
            raise ValueError(f"Condition rule line {line_number} has an invalid target, source, mode, or pattern")
        default = _parse_scalar(default_text)
        target_key = (target, source)
        if target_key in target_defaults and target_defaults[target_key] != default:
            raise ValueError(f"Condition rules for '{target}' must use the same default value")
        target_defaults[target_key] = default
        grouped[(target, source, default)].append(MatchCase(pattern, _parse_scalar(value_text), mode))
    return tuple(
        DerivedField(target, source, tuple(cases), default)
        for (target, source, default), cases in grouped.items()
    )


def parse_regex_rules(text: str) -> tuple[RegexField, ...]:
    """Parse `target ; source ; pattern ; group ; cast ; default` lines."""
    rules: list[RegexField] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split(";")]
        if len(parts) != 6:
            raise ValueError(f"Regex rule line {line_number} must contain 6 semicolon-separated fields")
        target, source, pattern, group_text, cast, default_text = parts
        if not target or source not in {"filename", "test_name"} or not pattern or cast not in {"str", "int", "float"}:
            raise ValueError(f"Regex rule line {line_number} has an invalid target, source, pattern, or cast")
        group: str | int = int(group_text) if group_text.isdigit() else group_text
        if group == "":
            group = 1
        rules.append(RegexField(target, source, pattern, group, cast, _parse_scalar(default_text)))
    return tuple(rules)


def _parse_mapping(text: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for token in re.split(r"[,\n]", text):
        token = token.strip()
        if not token:
            continue
        if "=" not in token:
            raise ValueError(f"Expected key=value mapping, got '{token}'")
        key, value = (part.strip() for part in token.split("=", 1))
        if not key or not value:
            raise ValueError(f"Invalid key=value mapping: '{token}'")
        mapping[key] = value
    return mapping


def _require_string(spec: Mapping[str, Any], key: str) -> str:
    value = str(spec.get(key, "")).strip()
    if not value:
        raise ValueError(f"Profile field '{key}' is required")
    return value


def profile_spec_to_models(profile_id: str, spec: Mapping[str, Any]) -> tuple[ExtractionProfile, CorrelationProfile]:
    """Validate one JSON-compatible custom definition and build runtime profiles."""
    if not _PROFILE_ID_PATTERN.fullmatch(profile_id):
        raise ValueError("Profile ID must use 3-64 lowercase letters, numbers, underscores, or hyphens")
    display_name = _require_string(spec, "display_name")
    selector_expression = _require_string(spec, "tests")
    group_by = _split_csv(_require_string(spec, "group_by"))
    if not group_by:
        raise ValueError("At least one grouping condition is required")

    strategy = str(spec.get("strategy", "median_offset"))
    if strategy not in {"mean_delta", "median_offset"}:
        raise ValueError("Strategy must be mean_delta or median_offset")
    guard_kind = str(spec.get("guard_band_kind", "distribution_sigma"))
    if guard_kind not in {"distribution_sigma", "shifted_upper_limit"}:
        raise ValueError("Guard-band kind must be distribution_sigma or shifted_upper_limit")
    minimum_points = int(spec.get("minimum_points", 5))
    if minimum_points < 1:
        raise ValueError("Minimum points must be at least 1")
    sigma_multiplier = float(spec.get("sigma_multiplier", 6.0))
    if sigma_multiplier <= 0:
        raise ValueError("Sigma multiplier must be positive")

    condition_rules = parse_condition_rules(str(spec.get("condition_rules", "")))
    regex_rules = parse_regex_rules(str(spec.get("regex_rules", "")))
    coordinate_columns = _split_csv(str(spec.get("coordinate_columns", "WAFER,X,Y")))
    if len(coordinate_columns) != 3:
        raise ValueError("Coordinate headers must contain exactly three names: wafer, X, and Y")
    coordinate_fallback_text = str(spec.get("coordinate_fallback", "")).strip()
    coordinate_fallback = (
        _parse_mapping(coordinate_fallback_text)
        if coordinate_fallback_text
        else dict(DEFAULT_COORDINATE_FALLBACK)
    )
    additional_columns = _split_csv(str(spec.get("additional_output_columns", "")))
    rule_columns = tuple(rule.target for rule in (*condition_rules, *regex_rules))
    output_columns = tuple(dict.fromkeys((*BASE_OUTPUT_COLUMNS, *additional_columns, *rule_columns, *group_by)))

    insertion_profiles: list[InsertionProfile] = []
    assigned_insertion_files: dict[str, str] = {}
    insertion_names: set[str] = set()
    raw_insertions = spec.get("insertions", [])
    if not isinstance(raw_insertions, list):
        raise ValueError("Profile field 'insertions' must be a list")
    for index, raw_insertion in enumerate(raw_insertions, start=1):
        if not isinstance(raw_insertion, dict):
            raise ValueError(f"Insertion {index} has an invalid definition")
        insertion_name = str(raw_insertion.get("name", "")).strip()
        insertion_group = str(raw_insertion.get("group", "")).strip().upper()
        raw_file_values = raw_insertion.get("raw_files", [])
        if not isinstance(raw_file_values, list):
            raise ValueError(f"Insertion '{insertion_name or index}' raw files must be a list")
        raw_files = tuple(str(value).strip() for value in raw_file_values if str(value).strip())
        if not insertion_name:
            raise ValueError(f"Insertion {index} needs a name")
        if insertion_name.casefold() in insertion_names:
            raise ValueError(f"Insertion name '{insertion_name}' is duplicated")
        insertion_names.add(insertion_name.casefold())
        if insertion_group not in {"FE", "BE"}:
            raise ValueError(f"Insertion '{insertion_name}' must use insertion group FE or BE")
        try:
            insertion_temperature = float(raw_insertion.get("temperature"))
        except (TypeError, ValueError) as error:
            raise ValueError(f"Insertion '{insertion_name}' needs a numeric temperature") from error
        if not math.isfinite(insertion_temperature):
            raise ValueError(f"Insertion '{insertion_name}' needs a finite numeric temperature")
        if not raw_files:
            raise ValueError(f"Insertion '{insertion_name}' needs at least one raw test-data file")
        for raw_file in raw_files:
            normalized_file = str(Path(raw_file).expanduser().resolve()).casefold()
            previous_insertion = assigned_insertion_files.get(normalized_file)
            if previous_insertion is not None:
                raise ValueError(
                    f"Raw file '{raw_file}' is assigned to both '{previous_insertion}' and '{insertion_name}'"
                )
            assigned_insertion_files[normalized_file] = insertion_name
        insertion_profiles.append(InsertionProfile(
            insertion_name,
            insertion_group,  # type: ignore[arg-type]
            insertion_temperature,
            raw_files,
        ))

    extraction = ExtractionProfile(
        name=display_name,
        selector=parse_test_selector(selector_expression),
        output_columns=output_columns,
        derived_fields=condition_rules,
        regex_fields=regex_rules,
        coordinate_columns=(coordinate_columns[0], coordinate_columns[1], coordinate_columns[2]),
        coordinate_fallback=coordinate_fallback,
        insertion_field=str(spec.get("insertion_field", "Insertion Type")).strip() or "Insertion Type",
        fallback_insertion_values=(
            _split_csv(str(spec.get("fallback_insertion_values", ""))) or ("BE",)
        ),
        insertions=tuple(insertion_profiles),
    )

    covariate: CovariateProfile | None = None
    covariate_value = str(spec.get("covariate_value_column", "")).strip()
    covariate_keys = _split_csv(str(spec.get("covariate_merge_keys", "")))
    if covariate_value or covariate_keys:
        if not covariate_value or not covariate_keys:
            raise ValueError("Covariate value column and merge keys must either both be set or both be empty")
        covariate = CovariateProfile(
            covariate_value,
            covariate_keys,
            str(spec.get("covariate_output_name", "Covariate")).strip() or "Covariate",
        )

    def optional_column(key: str) -> str | None:
        value = str(spec.get(key, "")).strip()
        return value or None

    correlation = CorrelationProfile(
        name=display_name,
        strategy=strategy,  # type: ignore[arg-type]
        reference_column=_require_string(spec, "reference_column"),
        candidate_column=_require_string(spec, "candidate_column"),
        group_by=group_by,
        minimum_points=minimum_points,
        lower_limit_column=optional_column("lower_limit_column"),
        upper_limit_column=optional_column("upper_limit_column"),
        unit_column=optional_column("unit_column"),
        test_name_column=optional_column("test_name_column"),
        detail_key_columns=_split_csv(str(spec.get("detail_key_columns", ""))),
        guard_band=GuardBandProfile(kind=guard_kind, sigma_multiplier=sigma_multiplier),  # type: ignore[arg-type]
        covariate=covariate,
    )
    return extraction, correlation


def load_custom_profile_specs(path: Path | None = None) -> dict[str, dict[str, Any]]:
    destination = path or profile_store_path()
    if not destination.exists():
        return {}
    try:
        payload = json.loads(destination.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot read custom profile store '{destination}': {error}") from error
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != SCHEMA_VERSION
        or not isinstance(payload.get("profiles"), dict)
    ):
        raise ValueError(f"Unsupported custom profile store schema in '{destination}'")
    specs: dict[str, dict[str, Any]] = {}
    for profile_id, spec in payload["profiles"].items():
        if not isinstance(profile_id, str) or not isinstance(spec, dict):
            raise ValueError(f"Invalid custom profile entry in '{destination}'")
        profile_spec_to_models(profile_id, spec)
        specs[profile_id] = dict(spec)
    return specs


def save_custom_profile_spec(profile_id: str, spec: Mapping[str, Any], path: Path | None = None) -> None:
    destination = path or profile_store_path()
    from .profiles_8188 import builtin_profile_ids

    if profile_id in builtin_profile_ids():
        raise ValueError(f"Built-in profile '{profile_id}' is read-only; choose a different profile ID")
    profile_spec_to_models(profile_id, spec)
    specs = load_custom_profile_specs(destination)
    specs[profile_id] = dict(spec)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "profiles": specs}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(destination)


def delete_custom_profile(profile_id: str, path: Path | None = None) -> bool:
    destination = path or profile_store_path()
    specs = load_custom_profile_specs(destination)
    if profile_id not in specs:
        return False
    del specs[profile_id]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "profiles": specs}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return True


def load_custom_profiles(
    path: Path | None = None,
) -> tuple[dict[str, ExtractionProfile], dict[str, CorrelationProfile]]:
    extraction: dict[str, ExtractionProfile] = {}
    correlation: dict[str, CorrelationProfile] = {}
    for profile_id, spec in load_custom_profile_specs(path).items():
        extraction[profile_id], correlation[profile_id] = profile_spec_to_models(profile_id, spec)
    return extraction, correlation
