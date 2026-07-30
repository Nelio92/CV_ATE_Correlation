"""Pure DataFrame correlation engine shared by the CLI and GUI."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

import pandas as pd

from .models import (
    ConditionalDimension,
    CorrelationProfile,
    TestPolicy,
    normalize_correlation_strategy,
    normalize_guard_band_kind,
)
from .guardbands import compute_guard_band


@dataclass(frozen=True)
class CorrelationResult:
    summary: pd.DataFrame
    details: pd.DataFrame


def _to_numeric(series: pd.Series) -> pd.Series:
    text = series.astype(str).str.strip().replace({"": pd.NA, "nan": pd.NA, "None": pd.NA})
    return pd.to_numeric(text.str.replace(" ", "", regex=False).str.replace(",", ".", regex=False), errors="coerce")


def _r2_score(actual: pd.Series, predicted: pd.Series) -> float:
    valid = actual.notna() & predicted.notna()
    actual_values = actual[valid].to_numpy(dtype=float)
    predicted_values = predicted[valid].to_numpy(dtype=float)
    if len(actual_values) < 2:
        return math.nan
    total = float(((actual_values - actual_values.mean()) ** 2).sum())
    return math.nan if total == 0.0 else 1.0 - float(((actual_values - predicted_values) ** 2).sum()) / total


def _ols(x: pd.Series, y: pd.Series) -> tuple[float, float]:
    valid = x.notna() & y.notna()
    x_values, y_values = x[valid].astype(float), y[valid].astype(float)
    if len(x_values) < 2:
        return math.nan, math.nan
    variance = float(((x_values - x_values.mean()) ** 2).sum())
    if variance == 0.0:
        return math.nan, math.nan
    slope = float(((x_values - x_values.mean()) * (y_values - y_values.mean())).sum()) / variance
    return slope, float(y_values.mean()) - slope * float(x_values.mean())


def _derive_dimension(frame: pd.DataFrame, dimension: ConditionalDimension) -> None:
    def derive(row: pd.Series) -> Any:
        if dimension.when and any(row.get(field) not in accepted for field, accepted in dimension.when.items()):
            return dimension.default
        match = re.search(dimension.pattern, str(row.get(dimension.source, "")), flags=re.IGNORECASE)
        return dimension.default if not match else dimension.value_format.format(match.group(dimension.group))

    frame[dimension.target] = frame.apply(derive, axis=1)


def _representative_text(frame: pd.DataFrame, column: str | None) -> str:
    if not column or column not in frame.columns:
        return ""
    values = [value for value in frame[column].astype(str).replace({"nan": ""}).str.strip().tolist() if value]
    unique = list(dict.fromkeys(values))
    if len(unique) <= 1:
        return unique[0] if unique else ""
    return "; ".join(unique[:2]) + (f" (+{len(unique) - 2} more)" if len(unique) > 2 else "")


def _first_numeric(frame: pd.DataFrame, column: str | None) -> float | None:
    if not column or column not in frame.columns:
        return None
    values = _to_numeric(frame[column]).dropna()
    return float(values.iloc[0]) if len(values) else None


def _test_policy_index(row: pd.Series, profile: CorrelationProfile) -> int:
    raw_number = row.get("Test Number", "")
    try:
        test_number = int(float(raw_number))
    except (TypeError, ValueError):
        test_number = -1
    test_name = str(row.get(profile.test_name_column or "Test Name", ""))
    matches = [
        index
        for index, policy in enumerate(profile.test_policies)
        if policy.selector.matches(test_number, test_name)
    ]
    identity = f"Test Number={raw_number!r}, Test Name={test_name!r}"
    if not matches:
        raise ValueError(f"No test-set policy matches {identity}")
    if len(matches) > 1:
        names = ", ".join(profile.test_policies[index].name for index in matches)
        raise ValueError(f"Multiple test-set policies ({names}) match {identity}")
    return matches[0]


def attach_covariate(frame: pd.DataFrame, lookup: pd.DataFrame, profile: CorrelationProfile) -> pd.DataFrame:
    if profile.covariate is None:
        return frame
    config = profile.covariate
    missing_input = [column for column in config.merge_keys if column not in frame.columns]
    if missing_input:
        raise ValueError(f"Aligned correlation input is missing covariate merge-key columns: {missing_input}")
    missing_lookup = [column for column in (*config.merge_keys, config.value_column) if column not in lookup.columns]
    if missing_lookup:
        raise ValueError(f"Covariate lookup is missing columns: {missing_lookup}")
    if config.output_name in frame.columns:
        raise ValueError(
            f"Aligned correlation input already contains covariate output column '{config.output_name}'. "
            "Remove it or choose a different covariate output name."
        )
    left, right = frame.copy(), lookup[list(config.merge_keys) + [config.value_column]].copy()
    normalized_keys: list[str] = []
    for index, key in enumerate(config.merge_keys):
        normalized_key = f"__covariate_key_{index}"
        normalized_keys.append(normalized_key)
        left_text = left[key].astype(str).str.strip()
        right_text = right[key].astype(str).str.strip()
        left_numeric = pd.to_numeric(left_text, errors="coerce")
        right_numeric = pd.to_numeric(right_text, errors="coerce")
        left_present = ~left[key].isna() & left_text.ne("")
        right_present = ~right[key].isna() & right_text.ne("")
        numeric_key = (
            bool(left_present.any())
            and bool(right_present.any())
            and bool(left_numeric[left_present].notna().all())
            and bool(right_numeric[right_present].notna().all())
        )
        if numeric_key:
            left[normalized_key] = left_numeric.map(lambda value: "" if pd.isna(value) else f"{float(value):.15g}")
            right[normalized_key] = right_numeric.map(lambda value: "" if pd.isna(value) else f"{float(value):.15g}")
        else:
            left[normalized_key] = left_text
            right[normalized_key] = right_text
    right[config.value_column] = _to_numeric(right[config.value_column])
    right = right.dropna(subset=[config.value_column])
    if right.empty:
        raise ValueError(f"Covariate lookup column '{config.value_column}' contains no numeric values")
    value_counts = right.groupby(normalized_keys, dropna=False)[config.value_column].nunique(dropna=True)
    conflicts = value_counts[value_counts > 1]
    if not conflicts.empty:
        examples = ", ".join(str(key) for key in conflicts.index[:3])
        raise ValueError(
            f"Covariate lookup has conflicting numeric values for {len(conflicts)} merge-key combination(s); "
            f"examples: {examples}. Provide exactly one Kf value per {', '.join(config.merge_keys)} combination."
        )
    right = right.drop_duplicates(normalized_keys, keep="first")
    right = right[normalized_keys + [config.value_column]].rename(columns={config.value_column: config.output_name})
    merged = left.merge(right, how="left", on=normalized_keys, validate="many_to_one")
    return merged.drop(columns=normalized_keys)


def attach_covariate_from_test_rows(frame: pd.DataFrame, profile: CorrelationProfile) -> pd.DataFrame:
    """Extract configured Kf rows, attach their values, and remove them from correlation targets."""
    if profile.covariate is None:
        return frame
    config = profile.covariate
    if "Test Number" not in frame.columns:
        raise ValueError("Raw extraction is missing the 'Test Number' column required to identify Kf rows")
    test_numbers = pd.to_numeric(frame["Test Number"], errors="coerce")
    covariate_mask = test_numbers.eq(config.test_number)
    if not bool(covariate_mask.any()):
        raise ValueError(
            f"Raw extraction contains no Kf rows for configured test number {config.test_number}. "
            "Verify the Kf test number and that it exists in every required insertion."
        )
    lookup = frame.loc[covariate_mask].copy()
    targets = frame.loc[~covariate_mask].copy()
    if targets.empty:
        raise ValueError(
            f"Raw extraction contains only Kf test number {config.test_number} and no correlation target rows"
        )
    prepared = attach_covariate(targets, lookup, profile)
    missing = int(pd.to_numeric(prepared[config.output_name], errors="coerce").isna().sum())
    if missing:
        raise ValueError(
            f"Kf test {config.test_number} did not match {missing} correlation row(s) by "
            f"{', '.join(config.merge_keys)}. Include every condition that changes Kf in the merge keys."
        )
    return prepared


def _group_sizes(frame: pd.DataFrame, columns: list[str]) -> pd.Series:
    if not columns:
        return pd.Series([len(frame)], dtype="int64")
    return frame.groupby(columns, dropna=False, sort=True).size()


def _minimum_point_error(
    working: pd.DataFrame,
    profile: CorrelationProfile,
    group_columns: list[str],
    sizes: pd.Series,
    policy: TestPolicy | None = None,
) -> ValueError:
    distribution = sizes.value_counts().sort_index()
    distribution_text = ", ".join(
        f"{int(group_size)} point{'s' if int(group_size) != 1 else ''}: {int(group_count):,} groups"
        for group_size, group_count in distribution.items()
    )
    recommendations: list[tuple[int, int, str]] = []
    for dimension in group_columns:
        reduced_columns = list(group_columns)
        reduced_columns.remove(dimension)
        reduced_sizes = _group_sizes(working, reduced_columns)
        passing = int((reduced_sizes >= profile.minimum_points).sum())
        if passing:
            recommendations.append((int(reduced_sizes.max()), passing, dimension))
    recommendations.sort(key=lambda item: (-item[0], -item[1], item[2]))

    message = (
        f"No correlation groups met the minimum point count of {profile.minimum_points}. "
        f"{len(working):,} valid Lab/CV-to-ATE value pairs formed {len(sizes):,} groups using "
        f"{', '.join(group_columns) or 'one fully pooled group'}; the largest group contains "
        f"{int(sizes.max()) if len(sizes) else 0} points. "
        f"Group-size distribution: {distribution_text or 'no groups'}."
    )
    if policy is not None:
        message = f"Test set '{policy.name}': {message}"
    if recommendations:
        suggestions = "; ".join(
            f"remove '{dimension}' → {passing:,} groups pass (largest group: {largest})"
            for largest, passing, dimension in recommendations[:3]
        )
        message += (
            f" Likely over-grouping detected: {suggestions}. Update the profile's Grouping conditions; keep device "
            "identifiers such as DUT Nr in Detail key columns unless a separate factor per device is intentional."
        )
    else:
        message += (
            " No single grouping dimension resolves the shortage. Check missing repetitions, selected grouping conditions, "
            "and Minimum points/group. Lower the minimum only when scientifically justified."
        )
    return ValueError(message)


def _pooled_columns(profile: CorrelationProfile, policy: TestPolicy | None) -> tuple[str, ...]:
    requested = (*profile.pooled_columns, *((policy.pooled_columns if policy else ())))
    return tuple(column for column in dict.fromkeys(requested) if column in profile.group_by)


def _merged_value_text(series: pd.Series) -> tuple[str, int]:
    values: list[str] = []
    seen: set[str] = set()
    for value in series.tolist():
        if pd.isna(value):
            text = "<blank>"
        elif isinstance(value, float) and value.is_integer():
            text = str(int(value))
        else:
            text = str(value).strip()
        identity = text.casefold()
        if identity not in seen:
            seen.add(identity)
            values.append(text)

    def sort_key(text: str) -> tuple[int, float | str]:
        try:
            return 0, float(text)
        except ValueError:
            return 1, text.casefold()

    values.sort(key=sort_key)
    return "; ".join(values), len(values)


def _pooled_summary(group: pd.DataFrame, pooled_columns: tuple[str, ...]) -> dict[str, Any]:
    if not pooled_columns:
        return {}
    summary: dict[str, Any] = {"PooledParameters": ", ".join(pooled_columns)}
    for column in pooled_columns:
        values, count = _merged_value_text(group[column])
        summary[f"Merged {column}"] = values
        summary[f"Merged {column} Count"] = count
    return summary


def _consistent_numeric(frame: pd.DataFrame, column: str | None, group_label: str) -> float | None:
    if not column or column not in frame.columns:
        return None
    values = _to_numeric(frame[column]).dropna().unique()
    if len(values) > 1:
        preview = ", ".join(f"{float(value):g}" for value in values[:5])
        raise ValueError(
            f"Pooled correlation group {group_label} contains different values in limit column '{column}': "
            f"{preview}. Tests sharing one factor/guard band must use compatible limits."
        )
    return float(values[0]) if len(values) else None


def correlate_frame(frame: pd.DataFrame, profile: CorrelationProfile) -> CorrelationResult:
    required = set(profile.group_by) | {profile.reference_column, profile.candidate_column}
    if profile.test_policies:
        if any(policy.selector.exact or policy.selector.ranges for policy in profile.test_policies):
            required.add("Test Number")
        if any(policy.selector.name_contains for policy in profile.test_policies):
            if not profile.test_name_column:
                raise ValueError("A test-name column is required by a name-based test set")
            required.add(profile.test_name_column)
    required -= {dimension.target for dimension in profile.derived_dimensions}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"Input is missing required columns: {missing}")

    working = frame.copy()
    for dimension in profile.derived_dimensions:
        _derive_dimension(working, dimension)
    if profile.test_policies:
        working["__TestPolicyIndex"] = working.apply(_test_policy_index, axis=1, profile=profile)
    working[profile.reference_column] = _to_numeric(working[profile.reference_column])
    working[profile.candidate_column] = _to_numeric(working[profile.candidate_column])
    valid_pairs = working[profile.reference_column].notna() & working[profile.candidate_column].notna()
    if not valid_pairs.any():
        reference_count = int(working[profile.reference_column].notna().sum())
        candidate_count = int(working[profile.candidate_column].notna().sum())
        raise ValueError(
            f"No rows contain numeric values in both '{profile.reference_column}' and "
            f"'{profile.candidate_column}'. Numeric rows: {reference_count:,} reference, "
            f"{candidate_count:,} ATE, from {len(working):,} input rows."
        )
    working = working.loc[valid_pairs].copy()

    plans: list[
        tuple[TestPolicy | None, pd.DataFrame, tuple[str, ...], list[str], Any, pd.Series]
    ] = []
    policies: tuple[TestPolicy | None, ...] = profile.test_policies or (None,)
    for policy_index, policy in enumerate(policies):
        policy_frame = (
            working.loc[working["__TestPolicyIndex"] == policy_index].copy()
            if policy is not None
            else working
        )
        pooled_columns = _pooled_columns(profile, policy)
        group_columns = [column for column in profile.group_by if column not in pooled_columns]
        grouped = policy_frame.groupby(group_columns, dropna=False, sort=True) if group_columns else None
        group_sizes = grouped.size() if grouped is not None else pd.Series([len(policy_frame)], dtype="int64")
        plans.append((policy, policy_frame, pooled_columns, group_columns, grouped, group_sizes))

    if not any((sizes >= profile.minimum_points).any() for *_prefix, sizes in plans):
        best = max(plans, key=lambda plan: int(plan[-1].max()) if len(plan[-1]) else 0)
        raise _minimum_point_error(best[1], profile, best[3], best[5], best[0])

    summary_rows: list[dict[str, Any]] = []
    detail_frames: list[pd.DataFrame] = []
    for policy, policy_frame, pooled_columns, group_columns, grouped, _group_sizes_for_policy in plans:
        groups = grouped if grouped is not None else [((), policy_frame)]
        for raw_key, raw_group in groups:
            group = raw_group.copy()
            if len(group) < profile.minimum_points:
                continue
            key = raw_key if isinstance(raw_key, tuple) else (raw_key,)
            group_values = dict(zip(group_columns, key))
            display_group_values = {
                column: group_values[column] if column in group_values else "MERGED"
                for column in profile.group_by
            }
            pooled_summary = _pooled_summary(group, pooled_columns)
            group_label = ", ".join(f"{column}={value}" for column, value in group_values.items()) or "<all rows>"
            configured_strategy = policy.strategy if policy else profile.strategy
            strategy = normalize_correlation_strategy(configured_strategy)
            guard_band = policy.guard_band if policy else profile.guard_band
            reference = group[profile.reference_column].astype(float)
            candidate = group[profile.candidate_column].astype(float)
            delta = reference - candidate
            mean_delta = float(delta.mean())
            mean_corrected = candidate + mean_delta
            mean_residual = reference - mean_corrected
            linear_slope, linear_intercept = _ols(candidate, reference)
            linear_corrected = pd.Series(math.nan, index=group.index, dtype=float)
            linear_residual = pd.Series(math.nan, index=group.index, dtype=float)
            linear_status = "ATE/candidate values have insufficient variation"
            if not math.isnan(linear_slope) and not math.isnan(linear_intercept):
                linear_corrected = linear_slope * candidate + linear_intercept
                linear_residual = reference - linear_corrected
                linear_status = "Available"
            median_factor = float(delta.median())
            median_corrected = candidate + median_factor
            median_residual = reference - median_corrected

            covariate_name = profile.covariate.output_name if profile.covariate else None
            covariate = pd.Series(math.nan, index=group.index, dtype=float)
            physics_corrected = pd.Series(math.nan, index=group.index, dtype=float)
            physics_residual = pd.Series(math.nan, index=group.index, dtype=float)
            slope = math.nan
            intercept = math.nan
            physics_status = "Kf test data is not configured"
            if covariate_name and covariate_name in group.columns:
                covariate = _to_numeric(group[covariate_name])
                slope, intercept = _ols(covariate, candidate - reference)
                if not math.isnan(slope) and not math.isnan(intercept):
                    physics_corrected = candidate - (slope * covariate + intercept)
                    physics_residual = reference - physics_corrected
                    physics_status = "Available"
                elif int(covariate.notna().sum()) < 2:
                    physics_status = "Fewer than two Kf values are available"
                else:
                    physics_status = "Kf has insufficient variation"
            elif covariate_name:
                physics_status = f"Kf column '{covariate_name}' is missing"

            if strategy == "Mean_Deltas":
                factor = mean_delta
                factor_a = 1.0
                factor_b = mean_delta
                corrected = mean_corrected
                residual = mean_residual
            elif strategy == "Linear":
                if linear_status != "Available":
                    raise ValueError(
                        f"Linear OLS correlation is unavailable for group {group_label}: {linear_status}."
                    )
                factor = math.nan
                factor_a = linear_slope
                factor_b = linear_intercept
                corrected = linear_corrected
                residual = linear_residual
            elif strategy == "Median_Deltas":
                factor = median_factor
                factor_a = 1.0
                factor_b = median_factor
                corrected = median_corrected
                residual = median_residual
            else:
                if physics_status != "Available":
                    raise ValueError(
                        f"Physics-based correlation is unavailable for group {group_label}: {physics_status}."
                    )
                missing_covariates = int(physics_corrected.isna().sum())
                if missing_covariates:
                    raise ValueError(
                        f"Physics-based correlation group {group_label} has {missing_covariates} rows without a "
                        "numeric Kf value."
                    )
                factor = math.nan
                factor_a = slope
                factor_b = intercept
                corrected = physics_corrected
                residual = physics_residual

            lower_limit = (
                _consistent_numeric(group, profile.lower_limit_column, group_label)
                if pooled_columns else _first_numeric(group, profile.lower_limit_column)
            )
            upper_limit = (
                _consistent_numeric(group, profile.upper_limit_column, group_label)
                if pooled_columns else _first_numeric(group, profile.upper_limit_column)
            )
            guard = compute_guard_band(
                guard_band, group, group_values, corrected, residual, factor, lower_limit, upper_limit
            )
            unit = ""
            if profile.unit_column and profile.unit_column in group.columns:
                units = [value for value in group[profile.unit_column].astype(str).replace({"nan": ""}).str.strip() if value]
                unique_units = list(dict.fromkeys(units))
                if pooled_columns and len(unique_units) > 1:
                    raise ValueError(
                        f"Pooled correlation group {group_label} contains different units: {unique_units}. "
                        "Tests sharing one factor/guard band must use the same unit."
                    )
                unit = unique_units[0] if unique_units else ""

            row: dict[str, Any] = {
                **display_group_values,
                "TestSet": policy.name if policy else "",
                "CorrelationStrategy": strategy,
                "GuardBandPolicy": normalize_guard_band_kind(guard_band.kind),
                **pooled_summary,
                "TestName": _representative_text(group, profile.test_name_column),
                "Count": len(group),
                "CorrelationFactor": factor,
                "CorrelationFactorA": factor_a,
                "CorrelationFactorB": factor_b,
                "MeanDelta": mean_delta,
                "LinearSlope": linear_slope,
                "LinearIntercept": linear_intercept,
                "LinearStatus": linear_status,
                "MedianDelta": median_factor,
                "MaximumDelta": float(delta.max()),
                "MaximumAbsoluteDelta": float(delta.abs().max()),
                "R2": _r2_score(reference, corrected),
                "ResidualStd": float(residual.std(ddof=1)) if len(residual) > 1 else math.nan,
                "LinearR2": _r2_score(reference, linear_corrected),
                "LinearResidualStd": float(linear_residual.std(ddof=1)) if len(linear_residual) > 1 else math.nan,
                "MeanDeltasR2": _r2_score(reference, mean_corrected),
                "MeanDeltasResidualStd": (
                    float(mean_residual.std(ddof=1)) if len(mean_residual) > 1 else math.nan
                ),
                "MedianDeltasR2": _r2_score(reference, median_corrected),
                "MedianDeltasResidualStd": (
                    float(median_residual.std(ddof=1)) if len(median_residual) > 1 else math.nan
                ),
                "PhysicsAlpha": slope,
                "PhysicsBeta": intercept,
                "PhysicsR2": _r2_score(reference, physics_corrected),
                "PhysicsResidualStd": float(physics_residual.std(ddof=1)),
                "PhysicsStatus": physics_status,
                **guard,
                "OriginalLowerLimit": lower_limit,
                "OriginalUpperLimit": upper_limit,
                "Unit": unit,
            }
            if "Test Number" not in row:
                row["Test Number"] = _representative_text(group, "Test Number")

            if covariate_name and covariate_name in group.columns:
                physics_guard = compute_guard_band(
                    profile.covariate_guard_band or guard_band,
                    group, group_values, physics_corrected, physics_residual,
                    factor, lower_limit, upper_limit,
                )
                present = int(covariate.notna().sum())
                row.update({
                    "CovariateSlope": slope,
                    "CovariateIntercept": intercept,
                    "CovariateR2": _r2_score(reference, physics_corrected),
                    "CovariateResidualStd": float(physics_residual.std(ddof=1)),
                    "CovariateCountPresent": present,
                    "CovariateCountMissing": int(covariate.isna().sum()),
                    "CovariateUnique": int(covariate.dropna().nunique()),
                    "CovariateMin": float(covariate.min()) if present else math.nan,
                    "CovariateMax": float(covariate.max()) if present else math.nan,
                    "CovariateMean": float(covariate.mean()) if present else math.nan,
                    **{f"Covariate{name}": value for name, value in physics_guard.items()},
                })

            summary_rows.append(row)
            details = group.copy()
            details = details.drop(columns=["__TestPolicyIndex"], errors="ignore")
            details["ReferenceValue"] = reference
            details["CandidateValue"] = candidate
            details["Delta"] = delta
            details["CorrelationFactor"] = factor
            details["CorrelationFactorA"] = factor_a
            details["CorrelationFactorB"] = factor_b
            details["CorrectedCandidate"] = corrected
            details["Residual"] = residual
            details["MeanDelta"] = mean_delta
            details["LinearSlope"] = linear_slope
            details["LinearIntercept"] = linear_intercept
            details["LinearStatus"] = linear_status
            details["LinearCorrectedCandidate"] = linear_corrected
            details["LinearResidual"] = linear_residual
            details["MedianDelta"] = median_factor
            details["MeanDeltasCorrectedCandidate"] = mean_corrected
            details["MeanDeltasResidual"] = mean_residual
            details["MedianDeltasCorrectedCandidate"] = median_corrected
            details["MedianDeltasResidual"] = median_residual
            details["PhysicsAlpha"] = slope
            details["PhysicsBeta"] = intercept
            details["PhysicsCorrectedCandidate"] = physics_corrected
            details["PhysicsResidual"] = physics_residual
            details["PhysicsStatus"] = physics_status
            details["TestSet"] = policy.name if policy else ""
            details["CorrelationStrategy"] = strategy
            details["GuardBandPolicy"] = normalize_guard_band_kind(guard_band.kind)
            if pooled_columns:
                details["PooledParameters"] = pooled_summary["PooledParameters"]
            for name, value in guard.items():
                details[name] = value
            details["CovariateCorrectedCandidate"] = physics_corrected
            details["CovariateResidual"] = physics_residual
            details["GroupIndex"] = len(summary_rows) - 1
            detail_frames.append(details)

    return CorrelationResult(pd.DataFrame(summary_rows), pd.concat(detail_frames, ignore_index=True))
