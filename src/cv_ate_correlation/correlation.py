"""Pure DataFrame correlation engine shared by the CLI and GUI."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

import pandas as pd

from .models import ConditionalDimension, CorrelationProfile, TestPolicy
from .guardbands import compute_guard_band


@dataclass(frozen=True)
class CorrelationResult:
    summary: pd.DataFrame
    details: pd.DataFrame


def _to_numeric(series: pd.Series) -> pd.Series:
    text = series.astype(str).str.strip().replace({"": pd.NA, "nan": pd.NA, "None": pd.NA})
    return pd.to_numeric(text.str.replace(" ", "", regex=False).str.replace(",", ".", regex=False), errors="coerce")


def _r2_score(actual: pd.Series, predicted: pd.Series) -> float:
    actual_values = actual.to_numpy(dtype=float)
    predicted_values = predicted.to_numpy(dtype=float)
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
    missing = [column for column in (*config.merge_keys, config.value_column) if column not in lookup.columns]
    if missing:
        raise ValueError(f"Covariate lookup is missing columns: {missing}")
    left, right = frame.copy(), lookup[list(config.merge_keys) + [config.value_column]].copy()
    normalized_keys: list[str] = []
    for index, key in enumerate(config.merge_keys):
        normalized_key = f"__covariate_key_{index}"
        normalized_keys.append(normalized_key)
        left[normalized_key] = left[key].astype(str).str.strip()
        right[normalized_key] = right[key].astype(str).str.strip()
    right[config.value_column] = _to_numeric(right[config.value_column])
    right = right.dropna(subset=[config.value_column]).drop_duplicates(normalized_keys, keep="first")
    right = right[normalized_keys + [config.value_column]].rename(columns={config.value_column: config.output_name})
    merged = left.merge(right, how="left", on=normalized_keys, validate="many_to_one")
    return merged.drop(columns=normalized_keys)


def _group_sizes(frame: pd.DataFrame, columns: list[str]) -> pd.Series:
    if not columns:
        return pd.Series([len(frame)], dtype="int64")
    return frame.groupby(columns, dropna=False, sort=True).size()


def _minimum_point_error(
    working: pd.DataFrame,
    profile: CorrelationProfile,
    group_columns: list[str],
    sizes: pd.Series,
) -> ValueError:
    distribution = sizes.value_counts().sort_index()
    distribution_text = ", ".join(
        f"{int(group_size)} point{'s' if int(group_size) != 1 else ''}: {int(group_count):,} groups"
        for group_size, group_count in distribution.items()
    )
    recommendations: list[tuple[int, int, str]] = []
    for dimension in profile.group_by:
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
        f"{', '.join(profile.group_by)}; the largest group contains {int(sizes.max()) if len(sizes) else 0} points. "
        f"Group-size distribution: {distribution_text or 'no groups'}."
    )
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

    summary_rows: list[dict[str, Any]] = []
    detail_frames: list[pd.DataFrame] = []
    group_columns = list(profile.group_by)
    if profile.test_policies:
        group_columns.append("__TestPolicyIndex")
    grouped = working.groupby(group_columns, dropna=False, sort=True)
    group_sizes = grouped.size()
    if not (group_sizes >= profile.minimum_points).any():
        raise _minimum_point_error(working, profile, group_columns, group_sizes)
    for raw_key, raw_group in grouped:
        group = raw_group.copy()
        if len(group) < profile.minimum_points:
            continue
        key = raw_key if isinstance(raw_key, tuple) else (raw_key,)
        policy: TestPolicy | None = None
        if profile.test_policies:
            policy = profile.test_policies[int(key[-1])]
            key = key[:-1]
        group_values = dict(zip(profile.group_by, key))
        strategy = policy.strategy if policy else profile.strategy
        guard_band = policy.guard_band if policy else profile.guard_band
        reference = group[profile.reference_column].astype(float)
        candidate = group[profile.candidate_column].astype(float)
        delta = reference - candidate
        factor = float(delta.mean()) if strategy == "mean_delta" else float(delta.median())
        corrected = candidate + factor
        residual = delta - factor

        lower_limit = _first_numeric(group, profile.lower_limit_column)
        upper_limit = _first_numeric(group, profile.upper_limit_column)
        guard = compute_guard_band(
            guard_band, group, group_values, corrected, residual, factor, lower_limit, upper_limit
        )
        unit = ""
        if profile.unit_column and profile.unit_column in group.columns:
            units = [value for value in group[profile.unit_column].astype(str).replace({"nan": ""}).str.strip() if value]
            unit = units[0] if units else ""

        row: dict[str, Any] = {
            **group_values,
            "TestSet": policy.name if policy else "",
            "CorrelationStrategy": strategy,
            "GuardBandPolicy": guard_band.kind,
            "TestName": _representative_text(group, profile.test_name_column),
            "Count": len(group),
            "CorrelationFactor": factor,
            "MaximumDelta": float(delta.max()),
            "R2": _r2_score(reference, corrected),
            "ResidualStd": float(residual.std(ddof=1)) if len(residual) > 1 else math.nan,
            **guard,
            "OriginalLowerLimit": lower_limit,
            "OriginalUpperLimit": upper_limit,
            "Unit": unit,
        }

        covariate_name = profile.covariate.output_name if profile.covariate else None
        physics_corrected = pd.Series(math.nan, index=group.index, dtype=float)
        physics_residual = pd.Series(math.nan, index=group.index, dtype=float)
        if covariate_name and covariate_name in group.columns:
            covariate = _to_numeric(group[covariate_name])
            slope, intercept = _ols(covariate, candidate - reference)
            if not math.isnan(slope) and not math.isnan(intercept):
                physics_corrected = candidate - (slope * covariate + intercept)
                physics_residual = reference - physics_corrected
            physics_guard = compute_guard_band(
                profile.covariate_guard_band or guard_band,
                group, group_values, physics_corrected, physics_residual,
                factor, lower_limit, upper_limit,
            )
            present = int(covariate.notna().sum())
            row.update({
                "CovariateSlope": slope,
                "CovariateIntercept": intercept,
                "CovariateR2": _r2_score(reference, physics_corrected) if physics_corrected.notna().all() else math.nan,
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
        details["CorrectedCandidate"] = corrected
        details["Residual"] = residual
        details["TestSet"] = policy.name if policy else ""
        details["CorrelationStrategy"] = strategy
        details["GuardBandPolicy"] = guard_band.kind
        details["CovariateCorrectedCandidate"] = physics_corrected
        details["CovariateResidual"] = physics_residual
        details["GroupIndex"] = len(summary_rows) - 1
        detail_frames.append(details)

    return CorrelationResult(pd.DataFrame(summary_rows), pd.concat(detail_frames, ignore_index=True))
