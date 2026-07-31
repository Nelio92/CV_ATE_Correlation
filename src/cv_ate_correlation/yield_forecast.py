"""Deterministic yield forecasting for productive ATE data using approved correlations."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from typing import Any

import pandas as pd

from .extraction import LegacyWideTeCsvAdapter
from .models import (
    CorrelationProfile,
    ExtractionProfile,
    InsertionProfile,
    TestPolicy,
    normalize_correlation_strategy,
)


@dataclass(frozen=True)
class YieldForecastResult:
    """Forecast summary and fully traceable productive sample details."""

    summary: pd.DataFrame
    details: pd.DataFrame


@dataclass(frozen=True)
class ProductiveInsertionInput:
    """Productive CSV files assigned to one Section 1 insertion."""

    insertion: InsertionProfile
    files: tuple[Path, ...]


def validate_productive_insertion_inputs(
    definitions: list[Mapping[str, Any]],
    available_insertions: tuple[InsertionProfile, ...],
) -> tuple[ProductiveInsertionInput, ...]:
    """Validate selected insertions and their productive CSV assignments."""
    available = {insertion.name: insertion for insertion in available_insertions}
    if not available:
        raise ValueError(
            "The selected profile has no insertions defined in Section 1. Add and save insertions first."
        )
    validated: list[ProductiveInsertionInput] = []
    selected_names: set[str] = set()
    assigned_files: dict[Path, str] = {}
    for definition in definitions:
        if not bool(definition.get("selected", False)):
            continue
        name = str(definition.get("name", "")).strip()
        if name not in available:
            raise ValueError(f"Unknown Section 1 insertion selected for yield forecast: '{name}'")
        if name in selected_names:
            raise ValueError(f"Productive insertion '{name}' is duplicated")
        selected_names.add(name)
        raw_files = definition.get("files", [])
        if not isinstance(raw_files, (list, tuple)):
            raise ValueError(f"Productive files for insertion '{name}' must be a list")
        files = tuple(Path(str(value)).expanduser().resolve() for value in raw_files)
        if not files:
            raise ValueError(f"Selected insertion '{name}' needs at least one productive CSV file")
        for path in files:
            if path.suffix.casefold() != ".csv":
                raise ValueError(f"Productive input must be CSV: {path}")
            if not path.is_file():
                raise FileNotFoundError(f"Productive CSV does not exist: {path}")
            previous = assigned_files.get(path)
            if previous is not None:
                raise ValueError(
                    f"Productive CSV '{path.name}' is assigned to both '{previous}' and '{name}'"
                )
            assigned_files[path] = name
        validated.append(ProductiveInsertionInput(available[name], files))
    if not validated:
        raise ValueError("Select at least one Section 1 insertion for the yield forecast")
    return tuple(validated)


def _attach_productive_covariate(
    frame: pd.DataFrame,
    profile: CorrelationProfile,
) -> pd.DataFrame:
    if profile.covariate is None:
        return frame
    test_numbers = pd.to_numeric(frame["Test Number"], errors="coerce")
    covariate_mask = test_numbers.eq(profile.covariate.test_number)
    requires_physics = any(
        normalize_correlation_strategy(policy.strategy) == "Physics-based"
        for policy in profile.test_policies
    )
    if not bool(covariate_mask.any()):
        if requires_physics:
            raise ValueError(
                f"Productive CSVs contain no Kf rows for configured test "
                f"{profile.covariate.test_number} required by Physics-based forecasting"
            )
        return frame

    lookup = frame.loc[covariate_mask].copy()
    targets = frame.loc[~covariate_mask].copy()
    coordinate_keys = [
        column for column in (
            "Productive Source File", "Wafer", "WAFER", "X", "Y", "Temperature", "Insertion",
        )
        if column in frame.columns and not frame[column].isna().all()
    ]
    if not {"X", "Y"}.issubset(coordinate_keys):
        raise ValueError(
            "Productive Kf attachment requires X and Y coordinates in the raw CSV export"
        )
    value_column = profile.covariate.value_column
    if value_column not in lookup.columns:
        value_column = "Test Value"
    lookup[value_column] = _to_numeric(lookup[value_column])
    lookup = lookup.dropna(subset=[value_column])
    if lookup.empty:
        raise ValueError(
            f"Productive Kf test {profile.covariate.test_number} contains no numeric values"
        )
    conflicts = lookup.groupby(coordinate_keys, dropna=False)[value_column].nunique(dropna=True)
    conflicts = conflicts[conflicts > 1]
    if not conflicts.empty:
        raise ValueError(
            f"Productive Kf data has conflicting values for {len(conflicts):,} coordinate(s)"
        )
    lookup = lookup.drop_duplicates(coordinate_keys)[coordinate_keys + [value_column]].rename(
        columns={value_column: profile.covariate.output_name}
    )
    prepared = targets.merge(lookup, how="left", on=coordinate_keys, validate="many_to_one")
    if requires_physics:
        missing = int(pd.to_numeric(
            prepared[profile.covariate.output_name], errors="coerce"
        ).isna().sum())
        if missing:
            raise ValueError(
                f"Productive Kf test {profile.covariate.test_number} did not match "
                f"{missing:,} target row(s) by {', '.join(coordinate_keys)}"
            )
    return prepared


def load_productive_csv_inputs(
    assignments: tuple[ProductiveInsertionInput, ...],
    extraction_profile: ExtractionProfile,
    correlation_profile: CorrelationProfile,
) -> pd.DataFrame:
    """Stream selected productive CSVs and prepare uncorrelated ATE values for forecasting."""
    adapter = LegacyWideTeCsvAdapter()
    frames = [
        adapter.extract_productive_files(
            assignment.files,
            extraction_profile,
            assignment.insertion,
        )
        for assignment in assignments
    ]
    frame = pd.concat(frames, ignore_index=True)
    frame = _attach_productive_covariate(frame, correlation_profile)
    if correlation_profile.candidate_column not in frame.columns:
        if "Test Value" not in frame.columns:
            raise ValueError(
                f"Productive extraction has neither '{correlation_profile.candidate_column}' "
                "nor 'Test Value'"
            )
        frame[correlation_profile.candidate_column] = frame["Test Value"]
    return frame


def _to_numeric(series: pd.Series) -> pd.Series:
    text = series.astype(str).str.strip().replace({"": pd.NA, "nan": pd.NA, "None": pd.NA})
    return pd.to_numeric(
        text.str.replace(" ", "", regex=False).str.replace(",", ".", regex=False),
        errors="coerce",
    )


def _identity(value: Any) -> tuple[str, str]:
    if value is None or pd.isna(value):
        return "missing", ""
    text = str(value).strip()
    try:
        number = float(text.replace(",", "."))
    except ValueError:
        return "text", text.casefold()
    if math.isfinite(number):
        return "number", f"{number:.15g}"
    return "text", text.casefold()


def _split_columns(value: Any) -> tuple[str, ...]:
    if value is None or pd.isna(value):
        return ()
    return tuple(item.strip() for item in str(value).split(",") if item.strip())


def _derive_dimensions(frame: pd.DataFrame, profile: CorrelationProfile) -> pd.DataFrame:
    output = frame.copy()
    for dimension in profile.derived_dimensions:
        def derive(row: pd.Series) -> Any:
            if dimension.when and any(
                row.get(field) not in accepted for field, accepted in dimension.when.items()
            ):
                return dimension.default
            match = re.search(
                dimension.pattern,
                str(row.get(dimension.source, "")),
                flags=re.IGNORECASE,
            )
            return (
                dimension.default
                if match is None
                else dimension.value_format.format(match.group(dimension.group))
            )

        output[dimension.target] = output.apply(derive, axis=1)
    return output


def _matching_policy(row: pd.Series, profile: CorrelationProfile) -> TestPolicy:
    raw_number = row.get("Test Number", "")
    try:
        number = int(float(raw_number))
    except (TypeError, ValueError):
        number = -1
    name = str(row.get(profile.test_name_column or "Test Name", ""))
    matches = [
        policy for policy in profile.test_policies
        if policy.selector.matches(number, name)
    ]
    identity = f"Test Number={raw_number!r}, Test Name={name!r}"
    if not matches:
        raise ValueError(f"Productive row does not match any configured test set: {identity}")
    if len(matches) > 1:
        raise ValueError(f"Productive row matches multiple configured test sets: {identity}")
    return matches[0]


def _factor_key(row: pd.Series, columns: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    return tuple(_identity(row.get(column)) for column in columns)


def _finite_factor(row: pd.Series, column: str, context: str) -> float:
    try:
        value = float(row.get(column))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{context} has no numeric {column}") from error
    if not math.isfinite(value):
        raise ValueError(f"{context} has no finite {column}")
    return value


def correlate_productive_value(
    candidate: float,
    strategy: str,
    factor_a: float,
    factor_b: float,
    *,
    covariate: float | None = None,
) -> float:
    """Apply the selected production correlation equation to one ATE value."""
    normalized = normalize_correlation_strategy(strategy)
    if normalized == "Linear":
        return factor_a * candidate + factor_b
    if normalized in {"Mean_Deltas", "Median_Deltas"}:
        return candidate + factor_b
    if covariate is None or not math.isfinite(covariate):
        raise ValueError("Physics-based yield forecasting requires a finite productive Kf value")
    return candidate - (factor_a * covariate + factor_b)


def _factor_lookup(
    factors: pd.DataFrame,
    profile: CorrelationProfile,
) -> dict[str, tuple[tuple[str, ...], dict[tuple[tuple[str, str], ...], tuple[int, pd.Series]]]]:
    if not profile.test_policies:
        raise ValueError(
            "Yield forecasting requires test sets defined in Section 1; this profile has none"
        )
    required = {
        "CorrelationStrategy",
        "AdjustedLowerLimit",
        "AdjustedUpperLimit",
        *profile.group_by,
    }
    missing = sorted(required - set(factors.columns))
    if missing:
        raise ValueError(f"Correlation summary is missing required columns: {missing}")

    result: dict[
        str,
        tuple[tuple[str, ...], dict[tuple[tuple[str, str], ...], tuple[int, pd.Series]]],
    ] = {}
    test_set_values = (
        factors["TestSet"].fillna("").astype(str).str.strip()
        if "TestSet" in factors.columns
        else pd.Series("", index=factors.index)
    )
    for policy in profile.test_policies:
        selected = factors.loc[test_set_values.eq(policy.name)]
        if selected.empty and len(profile.test_policies) == 1 and not bool(test_set_values.ne("").any()):
            selected = factors
        if selected.empty:
            raise ValueError(
                f"Correlation summary has no factors for configured test set '{policy.name}'"
            )
        configured_pooled = tuple(dict.fromkeys((*profile.pooled_columns, *policy.pooled_columns)))
        group_columns = tuple(column for column in profile.group_by if column not in configured_pooled)
        lookup: dict[tuple[tuple[str, str], ...], tuple[int, pd.Series]] = {}
        for index, row in selected.iterrows():
            reported_pooled = _split_columns(row.get("PooledParameters"))
            if set(reported_pooled) != set(configured_pooled):
                raise ValueError(
                    f"Correlation summary pooling for test set '{policy.name}' does not match the active profile: "
                    f"reported {reported_pooled or 'none'}, configured {configured_pooled or 'none'}"
                )
            strategy = normalize_correlation_strategy(str(row.get("CorrelationStrategy", "")))
            expected = normalize_correlation_strategy(policy.strategy)
            if strategy != expected:
                raise ValueError(
                    f"Correlation summary strategy '{strategy}' does not match active test set "
                    f"'{policy.name}' strategy '{expected}'"
                )
            key = _factor_key(row, group_columns)
            if key in lookup:
                raise ValueError(
                    f"Correlation summary contains duplicate factors for test set '{policy.name}' "
                    f"and grouping values {key}"
                )
            lookup[key] = (int(index), row)
        result[policy.name] = group_columns, lookup
    return result


def _quantile(values: pd.Series, percentile: float) -> float:
    return float(values.quantile(percentile))


def forecast_yield(
    production: pd.DataFrame,
    correlation_summary: pd.DataFrame,
    profile: CorrelationProfile,
) -> YieldForecastResult:
    """Correlate real productive samples and evaluate them against correlated limits."""
    prepared = _derive_dimensions(production, profile)
    required = {"Test Number", profile.candidate_column, *profile.group_by}
    if profile.test_name_column:
        required.add(profile.test_name_column)
    missing = sorted(required - set(prepared.columns))
    if missing:
        raise ValueError(f"Productive test data is missing required columns: {missing}")
    if prepared.empty:
        raise ValueError("Productive test data is empty")

    prepared = prepared.copy()
    prepared[profile.candidate_column] = _to_numeric(prepared[profile.candidate_column])
    invalid_candidates = int(prepared[profile.candidate_column].isna().sum())
    if invalid_candidates:
        raise ValueError(
            f"Productive test data contains {invalid_candidates:,} blank or non-numeric "
            f"'{profile.candidate_column}' value(s)"
        )

    lookups = _factor_lookup(correlation_summary.reset_index(drop=True), profile)
    output_rows: list[dict[str, Any]] = []
    for source_index, row in prepared.iterrows():
        policy = _matching_policy(row, profile)
        group_columns, lookup = lookups[policy.name]
        factor_match = lookup.get(_factor_key(row, group_columns))
        test_identity = f"test {row.get('Test Number')} ({row.get(profile.test_name_column or 'Test Name', '')})"
        if factor_match is None:
            values = ", ".join(f"{column}={row.get(column)!r}" for column in group_columns)
            raise ValueError(
                f"No approved correlation factor matches productive {test_identity}, "
                f"test set '{policy.name}', {values}"
            )
        correlation_group_index, factor = factor_match
        strategy = normalize_correlation_strategy(str(factor["CorrelationStrategy"]))
        context = f"Correlation factor for {test_identity}, test set '{policy.name}'"
        factor_a = _finite_factor(factor, "CorrelationFactorA", context)
        factor_b = _finite_factor(factor, "CorrelationFactorB", context)
        lower = _finite_factor(factor, "AdjustedLowerLimit", context)
        upper = _finite_factor(factor, "AdjustedUpperLimit", context)
        candidate = float(row[profile.candidate_column])
        covariate: float | None = None
        if strategy == "Physics-based":
            if profile.covariate is None or profile.covariate.output_name not in prepared.columns:
                raise ValueError(
                    f"Physics-based yield forecasting requires productive Kf column "
                    f"'{profile.covariate.output_name if profile.covariate else 'Kf'}'"
                )
            raw_covariate = pd.to_numeric(
                pd.Series([row.get(profile.covariate.output_name)]), errors="coerce"
            ).iloc[0]
            covariate = None if pd.isna(raw_covariate) else float(raw_covariate)
        corrected = correlate_productive_value(
            candidate,
            strategy,
            factor_a,
            factor_b,
            covariate=covariate,
        )
        invalid_window = lower > upper
        below = corrected < lower
        above = corrected > upper
        passed = not invalid_window and not below and not above
        if invalid_window:
            failure = "Invalid correlated limit window"
        elif below:
            failure = "Below correlated LTL"
        elif above:
            failure = "Above correlated UTL"
        else:
            failure = ""
        output_rows.append({
            **row.to_dict(),
            "ProductionSourceIndex": source_index,
            "CorrelationGroupIndex": correlation_group_index,
            "TestSet": policy.name,
            "CorrelationStrategy": strategy,
            "CorrelationFactorA": factor_a,
            "CorrelationFactorB": factor_b,
            "ProductiveATEValue": candidate,
            "ProductiveKfValue": covariate,
            "ForecastCorrelatedValue": corrected,
            "ForecastLowerLimit": lower,
            "ForecastUpperLimit": upper,
            "ForecastLimitWindowInvalid": invalid_window,
            "ForecastPass": passed,
            "ForecastFail": not passed,
            "ForecastFailureReason": failure,
            "Unit": factor.get("Unit", row.get(profile.unit_column or "Unit", "")),
        })

    details = pd.DataFrame(output_rows).reset_index(drop=True)
    name_column = profile.test_name_column or "Test Name"
    identity_columns = ["CorrelationGroupIndex", "Test Number"]
    if name_column in details.columns:
        identity_columns.append(name_column)
    grouped = details.groupby(identity_columns, dropna=False, sort=False)
    summary_rows: list[dict[str, Any]] = []
    detail_group_indices = pd.Series(index=details.index, dtype="int64")
    for forecast_group_index, (_key, group) in enumerate(grouped):
        first = group.iloc[0]
        values = pd.to_numeric(group["ForecastCorrelatedValue"], errors="coerce")
        raw_values = pd.to_numeric(group["ProductiveATEValue"], errors="coerce")
        pass_count = int(group["ForecastPass"].astype(bool).sum())
        fail_count = len(group) - pass_count
        lower_fail_count = int(group["ForecastFailureReason"].eq("Below correlated LTL").sum())
        upper_fail_count = int(group["ForecastFailureReason"].eq("Above correlated UTL").sum())
        standard_deviation = float(values.std(ddof=1)) if len(values) > 1 else math.nan
        lower = float(first["ForecastLowerLimit"])
        upper = float(first["ForecastUpperLimit"])
        cpk = math.nan
        if math.isfinite(standard_deviation) and standard_deviation > 0 and lower <= upper:
            cpk = min(
                (upper - float(values.mean())) / (3.0 * standard_deviation),
                (float(values.mean()) - lower) / (3.0 * standard_deviation),
            )
        row: dict[str, Any] = {
            "ForecastGroupIndex": forecast_group_index,
            "CorrelationGroupIndex": int(first["CorrelationGroupIndex"]),
            "Test Number": first["Test Number"],
            "Test Name": first.get(name_column, ""),
            "TestSet": first["TestSet"],
        }
        for column in profile.group_by:
            unique = group[column].drop_duplicates().tolist() if column in group.columns else []
            row[column] = unique[0] if len(unique) == 1 else "; ".join(str(value) for value in unique)
        row.update({
            "Insertion Type": first.get("Insertion Type", ""),
            "Insertion": first.get("Insertion", ""),
            "Temperature": first.get("Temperature", ""),
            "CorrelationStrategy": first["CorrelationStrategy"],
            "CorrelationFactorA": first["CorrelationFactorA"],
            "CorrelationFactorB": first["CorrelationFactorB"],
            "ForecastLowerLimit": lower,
            "ForecastUpperLimit": upper,
            "ForecastLimitWindowInvalid": bool(first["ForecastLimitWindowInvalid"]),
            "SampleCount": len(group),
            "PassCount": pass_count,
            "FailCount": fail_count,
            "LowerFailCount": lower_fail_count,
            "UpperFailCount": upper_fail_count,
            "YieldPercent": 100.0 * pass_count / len(group),
            "RawMinimum": float(raw_values.min()),
            "RawMaximum": float(raw_values.max()),
            "RawMean": float(raw_values.mean()),
            "ForecastMinimum": float(values.min()),
            "ForecastP01": _quantile(values, 0.01),
            "ForecastP05": _quantile(values, 0.05),
            "ForecastMedian": _quantile(values, 0.50),
            "ForecastMean": float(values.mean()),
            "ForecastStd": standard_deviation,
            "ForecastP95": _quantile(values, 0.95),
            "ForecastP99": _quantile(values, 0.99),
            "ForecastMaximum": float(values.max()),
            "ForecastCpk": cpk,
            "Unit": first.get("Unit", ""),
        })
        summary_rows.append(row)
        detail_group_indices.loc[group.index] = forecast_group_index

    details["ForecastGroupIndex"] = detail_group_indices.astype("int64")
    summary = pd.DataFrame(summary_rows)
    return YieldForecastResult(summary=summary, details=details)
