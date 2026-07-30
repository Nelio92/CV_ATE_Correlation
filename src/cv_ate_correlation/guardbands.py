"""Pluggable guard-band policies."""

from __future__ import annotations

import math
from typing import Any, Mapping

import pandas as pd

from .models import GuardBandProfile, RequirementRule, normalize_guard_band_kind


def _matches(frame: pd.DataFrame, values: Mapping[str, Any], conditions: Mapping[str, tuple[Any, ...]]) -> bool:
    for field, accepted in conditions.items():
        if field in values:
            candidates = [values[field]]
        elif field in frame.columns:
            candidates = frame[field].dropna().unique().tolist()
        else:
            return False
        if not candidates or not all(candidate in accepted for candidate in candidates):
            return False
    return True


def _rule_limits(
    rule: RequirementRule,
    frame: pd.DataFrame,
    group_values: Mapping[str, Any],
    residual: pd.Series,
) -> tuple[float, float, str] | None:
    if not _matches(frame, group_values, rule.when):
        return None
    if rule.bypass_when and _matches(frame, group_values, rule.bypass_when):
        return rule.lower, rule.upper, rule.bypass_method
    residual_by_name = {
        "minimum": float(residual.min()),
        "maximum": float(residual.max()),
    }
    lower = rule.lower + abs(residual_by_name[rule.lower_residual])
    upper = rule.upper - abs(residual_by_name[rule.upper_residual])
    return lower, upper, rule.method


def compute_guard_band(
    profile: GuardBandProfile,
    frame: pd.DataFrame,
    group_values: Mapping[str, Any],
    corrected: pd.Series,
    residual: pd.Series,
    correlation_factor: float,
    lower_limit: float | None,
    upper_limit: float | None,
) -> dict[str, Any]:
    corrected = pd.to_numeric(corrected, errors="coerce").dropna()
    residual = pd.to_numeric(residual, errors="coerce").dropna()
    corrected_mean = float(corrected.mean()) if len(corrected) else math.nan
    corrected_std = float(corrected.std(ddof=1)) if len(corrected) > 1 else math.nan
    residual_min = float(residual.min()) if len(residual) else math.nan
    residual_max = float(residual.max()) if len(residual) else math.nan
    max_abs_residual = float(residual.abs().max()) if len(residual) else math.nan

    result: dict[str, Any] = {
        "CorrectedMean": corrected_mean,
        "CorrectedStd": corrected_std,
        "ResidualMin": residual_min,
        "ResidualMax": residual_max,
        "MaxAbsResidual": max_abs_residual,
        "RequirementMin": profile.requirement_min if profile.requirement_min is not None else math.nan,
        "RequirementMax": profile.requirement_max if profile.requirement_max is not None else math.nan,
        "AdjustedLowerLimit": math.nan,
        "AdjustedUpperLimit": math.nan,
        "WorstCaseUpperLimit": math.nan,
        "GuardBandMethod": "",
        "LimitWindowInvalid": False,
    }

    kind = normalize_guard_band_kind(profile.kind)
    if kind == "shifted_upper_limit":
        result["AdjustedLowerLimit"] = lower_limit if lower_limit is not None else math.nan
        if upper_limit is not None:
            adjusted = upper_limit + profile.factor_multiplier * correlation_factor
            result["AdjustedUpperLimit"] = adjusted
            result["WorstCaseUpperLimit"] = adjusted + profile.residual_multiplier * max_abs_residual
        result["GuardBandMethod"] = "shifted upper limit with worst-case residual"
        return result

    if kind == "Max_residuals":
        if profile.requirement_min is None or profile.requirement_max is None:
            raise ValueError("Max_residuals requires numeric REQ_MIN and REQ_MAX profile inputs")
        if not math.isfinite(profile.requirement_min) or not math.isfinite(profile.requirement_max):
            raise ValueError("Max_residuals REQ_MIN and REQ_MAX must be finite numbers")
        if profile.requirement_min >= profile.requirement_max:
            raise ValueError("Max_residuals requires REQ_MIN to be smaller than REQ_MAX")
        if not math.isnan(max_abs_residual):
            result["AdjustedLowerLimit"] = profile.requirement_min + abs(max_abs_residual)
            result["AdjustedUpperLimit"] = profile.requirement_max - abs(max_abs_residual)
        adjusted_lower = result["AdjustedLowerLimit"]
        adjusted_upper = result["AdjustedUpperLimit"]
        result["LimitWindowInvalid"] = (
            not math.isnan(adjusted_lower)
            and not math.isnan(adjusted_upper)
            and adjusted_lower > adjusted_upper
        )
        result["GuardBandMethod"] = "REQ_MIN/REQ_MAX tightened by max absolute residual"
        return result

    for rule in profile.rules:
        limits = _rule_limits(rule, frame, group_values, residual)
        if limits is not None:
            result["AdjustedLowerLimit"], result["AdjustedUpperLimit"], result["GuardBandMethod"] = limits
            result["LimitWindowInvalid"] = result["AdjustedLowerLimit"] > result["AdjustedUpperLimit"]
            return result

    if not math.isnan(corrected_mean) and not math.isnan(corrected_std):
        width = profile.sigma_multiplier * corrected_std
        result["AdjustedLowerLimit"] = corrected_mean - width
        result["AdjustedUpperLimit"] = corrected_mean + width
        result["LimitWindowInvalid"] = result["AdjustedLowerLimit"] > result["AdjustedUpperLimit"]
    result["GuardBandMethod"] = f"mean±{profile.sigma_multiplier:g}σ(correlated)"
    return result
