"""Robust, auditable pre-correlation outlier review and optional filtering."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import pandas as pd

from .models import CorrelationProfile, TestPolicy, normalize_correlation_strategy


MAD_SCALE_FACTOR = 1.4826
DEFAULT_MAD_THRESHOLD = 6.0
OUTLIER_ROW_ID = "OutlierRowId"
OUTLIER_INPUT_ROW = "OutlierInputRow"
OUTLIER_FLAGGED = "OutlierFlagged"
OUTLIER_FLAGGED_SERIES = "OutlierFlaggedSeries"
OUTLIER_REASON = "OutlierReason"
OUTLIER_MAX_SCORE = "OutlierMaxRobustScore"
OUTLIER_REVIEW_STATUS = "OutlierReviewStatus"


@dataclass(frozen=True)
class OutlierAnalysis:
    """Detector output before the user decides whether to exclude any findings."""

    threshold: float
    annotated_frame: pd.DataFrame
    findings: pd.DataFrame
    reference_column: str
    candidate_column: str
    population_columns: tuple[str, ...]
    valid_pair_count: int

    @property
    def flagged_count(self) -> int:
        return len(self.findings)

    @property
    def affected_test_count(self) -> int:
        if self.findings.empty:
            return 0
        columns = [
            column
            for column in ("Test Number", "Test Name")
            if column in self.findings.columns
        ]
        return len(self.findings[columns].drop_duplicates()) if columns else 0

    @property
    def affected_population_count(self) -> int:
        if self.findings.empty or "OutlierPopulation" not in self.findings.columns:
            return 0
        return int(self.findings["OutlierPopulation"].nunique(dropna=False))


@dataclass(frozen=True)
class OutlierReview:
    """Final audit record after the user retains or excludes reviewed rows."""

    threshold: float
    findings: pd.DataFrame
    original_row_count: int
    valid_pair_count: int
    final_row_count: int
    reference_column: str
    candidate_column: str
    population_columns: tuple[str, ...]

    @property
    def flagged_count(self) -> int:
        return len(self.findings)

    @property
    def excluded_count(self) -> int:
        if self.findings.empty or "Excluded" not in self.findings.columns:
            return 0
        return int(self.findings["Excluded"].fillna(False).astype(bool).sum())

    @property
    def retained_flagged_count(self) -> int:
        return self.flagged_count - self.excluded_count

    @property
    def affected_test_count(self) -> int:
        if self.findings.empty:
            return 0
        columns = [
            column
            for column in ("Test Number", "Test Name")
            if column in self.findings.columns
        ]
        return len(self.findings[columns].drop_duplicates()) if columns else 0

    def audit_frame(self) -> pd.DataFrame:
        """Return a report-ready audit table, including detector settings when no rows were flagged."""
        if self.findings.empty:
            return pd.DataFrame([{
                "ReviewStatus": "No outliers detected",
                "Detector": "scaled MAD",
                "MADScaleFactor": MAD_SCALE_FACTOR,
                "MADThreshold": self.threshold,
                "ReferenceColumn": self.reference_column,
                "CandidateColumn": self.candidate_column,
                "OriginalRows": self.original_row_count,
                "ValidPairs": self.valid_pair_count,
                "FinalRows": self.final_row_count,
                "Excluded": False,
            }])
        audit = self.findings.copy()
        audit.insert(0, "ReviewStatus", audit["Excluded"].map({True: "Excluded", False: "Flagged – retained"}))
        audit.insert(1, "Detector", "scaled MAD")
        audit.insert(2, "MADScaleFactor", MAD_SCALE_FACTOR)
        audit.insert(3, "MADThreshold", self.threshold)
        return audit


def _to_numeric(series: pd.Series) -> pd.Series:
    text = series.astype(str).str.strip().replace({"": pd.NA, "nan": pd.NA, "None": pd.NA})
    return pd.to_numeric(
        text.str.replace(" ", "", regex=False).str.replace(",", ".", regex=False),
        errors="coerce",
    )


def _derive_dimensions(frame: pd.DataFrame, profile: CorrelationProfile) -> None:
    for dimension in profile.derived_dimensions:
        def derive(row: pd.Series, *, current: Any = dimension) -> Any:
            if current.when and any(
                row.get(field) not in accepted for field, accepted in current.when.items()
            ):
                return current.default
            match = re.search(
                current.pattern,
                str(row.get(current.source, "")),
                flags=re.IGNORECASE,
            )
            return current.default if match is None else current.value_format.format(
                match.group(current.group)
            )

        frame[dimension.target] = frame.apply(derive, axis=1)


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


def _prepare_frame(frame: pd.DataFrame, profile: CorrelationProfile) -> pd.DataFrame:
    required = {profile.reference_column, profile.candidate_column}
    required.update(
        column
        for column in profile.group_by
        if column not in {dimension.target for dimension in profile.derived_dimensions}
    )
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"Input is missing required columns for outlier review: {missing}")
    reserved = {
        OUTLIER_ROW_ID,
        OUTLIER_INPUT_ROW,
        OUTLIER_FLAGGED,
        OUTLIER_FLAGGED_SERIES,
        OUTLIER_REASON,
        OUTLIER_MAX_SCORE,
        OUTLIER_REVIEW_STATUS,
        "OutlierLabRobustScore",
        "OutlierATERobustScore",
        "OutlierPairedRobustScore",
        "__OutlierPolicyIndex",
    }
    conflicts = sorted(reserved.intersection(frame.columns))
    if conflicts:
        raise ValueError(f"Input contains reserved outlier-review columns: {conflicts}")
    working = frame.copy().reset_index(drop=True)
    _derive_dimensions(working, profile)
    working[OUTLIER_ROW_ID] = pd.Series(range(1, len(working) + 1), index=working.index, dtype="int64")
    working[OUTLIER_INPUT_ROW] = working[OUTLIER_ROW_ID] + 1
    if profile.test_policies:
        working["__OutlierPolicyIndex"] = working.apply(_test_policy_index, axis=1, profile=profile)
    else:
        working["__OutlierPolicyIndex"] = -1
    return working


def _policy(profile: CorrelationProfile, index: int) -> TestPolicy | None:
    return profile.test_policies[index] if index >= 0 else None


def _population_columns(frame: pd.DataFrame, profile: CorrelationProfile) -> tuple[str, ...]:
    candidates = [*profile.group_by]
    for column in ("Test Number", profile.test_name_column or "Test Name"):
        if column in frame.columns and column not in candidates:
            candidates.append(column)
    candidates.append("__OutlierPolicyIndex")
    return tuple(dict.fromkeys(candidates))


def _normal_key(value: Any) -> Any:
    if pd.isna(value):
        return "<blank>"
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _population_label(values: Mapping[str, Any]) -> str:
    return " | ".join(
        f"{column}={_normal_key(value)}"
        for column, value in values.items()
        if column != "__OutlierPolicyIndex"
    ) or "All rows"


def _mad_diagnostics(values: pd.Series, threshold: float) -> tuple[pd.Series, pd.Series, float, float, str]:
    numeric = _to_numeric(values)
    median = float(numeric.median())
    deviations = (numeric - median).abs()
    mad = float(deviations.median())
    tolerance = max(1e-12, abs(median) * 1e-12)
    if math.isfinite(mad) and mad > tolerance:
        scores = deviations / (MAD_SCALE_FACTOR * mad)
        flags = scores > threshold
        status = "scaled MAD"
    else:
        different = deviations > tolerance
        scores = pd.Series(0.0, index=numeric.index, dtype=float)
        scores.loc[different] = math.inf
        flags = different
        status = "MAD=0; non-median values require review" if bool(different.any()) else "MAD=0; constant series"
    scores = scores.where(numeric.notna(), math.nan)
    flags = flags & numeric.notna()
    return scores, flags, median, mad, status


def _theil_sen_residual(x: pd.Series, y: pd.Series) -> pd.Series | None:
    valid = x.notna() & y.notna()
    x_values = x.loc[valid].astype(float)
    y_values = y.loc[valid].astype(float)
    if len(x_values) < 2 or x_values.nunique() < 2:
        return None
    x_list = x_values.tolist()
    y_list = y_values.tolist()
    slopes: list[float] = []
    for left in range(len(x_list) - 1):
        for right in range(left + 1, len(x_list)):
            difference = x_list[right] - x_list[left]
            if difference != 0.0:
                slopes.append((y_list[right] - y_list[left]) / difference)
    if not slopes:
        return None
    slope = float(pd.Series(slopes, dtype=float).median())
    intercept = float((y_values - slope * x_values).median())
    residual = pd.Series(math.nan, index=x.index, dtype=float)
    residual.loc[valid] = y_values - (slope * x_values + intercept)
    return residual


def _paired_signal(
    group: pd.DataFrame,
    profile: CorrelationProfile,
    policy: TestPolicy | None,
    reference: pd.Series,
    candidate: pd.Series,
) -> tuple[pd.Series, str]:
    strategy = normalize_correlation_strategy(policy.strategy if policy else profile.strategy)
    delta = reference - candidate
    if strategy == "Linear":
        residual = _theil_sen_residual(candidate, reference)
        return (residual, "robust Linear residual") if residual is not None else (
            delta,
            "CV-ATE delta (Linear fallback: insufficient ATE variation)",
        )
    if strategy == "Physics-based" and profile.covariate is not None:
        covariate_name = profile.covariate.output_name
        if covariate_name in group.columns:
            covariate = _to_numeric(group[covariate_name])
            residual = _theil_sen_residual(covariate, candidate - reference)
            if residual is not None:
                return residual, "robust Physics-based residual"
        return delta, "CV-ATE delta (Physics fallback: insufficient Kf variation)"
    return delta, "CV-ATE delta"


def _score_text(score: float) -> str:
    if math.isinf(score):
        return "∞ (MAD=0)"
    return f"{score:.3g}"


def _finding_identity_columns(frame: pd.DataFrame, profile: CorrelationProfile) -> tuple[str, ...]:
    requested = (
        "Test Number",
        profile.test_name_column or "Test Name",
        "DUT Nr",
        "Wafer",
        "WAFER",
        "X",
        "Y",
        "DoE split",
        "Insertion Type",
        "Insertion",
        "Temperature",
        *profile.detail_key_columns,
        *profile.group_by,
    )
    return tuple(column for column in dict.fromkeys(requested) if column in frame.columns)


def analyze_outliers(
    frame: pd.DataFrame,
    profile: CorrelationProfile,
    threshold: float = DEFAULT_MAD_THRESHOLD,
) -> OutlierAnalysis:
    """Flag per-test/per-corner Lab, ATE, and paired anomalies using scaled MAD."""
    try:
        threshold = float(threshold)
    except (TypeError, ValueError) as error:
        raise ValueError("MAD threshold must be numeric") from error
    if not math.isfinite(threshold) or threshold <= 0:
        raise ValueError("MAD threshold must be a finite number greater than zero")

    working = _prepare_frame(frame, profile)
    reference_all = _to_numeric(working[profile.reference_column])
    candidate_all = _to_numeric(working[profile.candidate_column])
    valid_pairs = reference_all.notna() & candidate_all.notna()
    if not bool(valid_pairs.any()):
        raise ValueError(
            f"No rows contain numeric values in both '{profile.reference_column}' and "
            f"'{profile.candidate_column}' for outlier review"
        )

    working[OUTLIER_FLAGGED] = False
    working[OUTLIER_FLAGGED_SERIES] = ""
    working[OUTLIER_REASON] = ""
    working[OUTLIER_MAX_SCORE] = math.nan
    working["OutlierLabRobustScore"] = math.nan
    working["OutlierATERobustScore"] = math.nan
    working["OutlierPairedRobustScore"] = math.nan
    working[OUTLIER_REVIEW_STATUS] = "Not flagged"

    population_columns = _population_columns(working, profile)
    valid_working = working.loc[valid_pairs]
    grouped: Iterable[tuple[Any, pd.DataFrame]]
    if len(population_columns) == 1:
        grouped = valid_working.groupby(population_columns[0], dropna=False, sort=True)
    else:
        grouped = valid_working.groupby(list(population_columns), dropna=False, sort=True)
    identity_columns = _finding_identity_columns(working, profile)
    finding_rows: list[dict[str, Any]] = []

    for raw_key, group in grouped:
        key = raw_key if isinstance(raw_key, tuple) else (raw_key,)
        population_values = dict(zip(population_columns, key))
        policy_index = int(population_values.get("__OutlierPolicyIndex", -1))
        selected_policy = _policy(profile, policy_index)
        reference = _to_numeric(group[profile.reference_column])
        candidate = _to_numeric(group[profile.candidate_column])
        paired, paired_metric = _paired_signal(
            group,
            profile,
            selected_policy,
            reference,
            candidate,
        )
        lab_scores, lab_flags, lab_median, lab_mad, lab_status = _mad_diagnostics(reference, threshold)
        ate_scores, ate_flags, ate_median, ate_mad, ate_status = _mad_diagnostics(candidate, threshold)
        paired_scores, paired_flags, paired_median, paired_mad, paired_status = _mad_diagnostics(
            paired,
            threshold,
        )

        working.loc[group.index, "OutlierLabRobustScore"] = lab_scores
        working.loc[group.index, "OutlierATERobustScore"] = ate_scores
        working.loc[group.index, "OutlierPairedRobustScore"] = paired_scores
        flagged_indices = group.index[lab_flags | ate_flags | paired_flags]
        for row_index in flagged_indices:
            series: list[str] = []
            reasons: list[str] = []
            row_scores: list[float] = []
            if bool(lab_flags.loc[row_index]):
                score = float(lab_scores.loc[row_index])
                series.append("Lab/CV")
                reasons.append(f"Lab/CV score {_score_text(score)} > {threshold:g}")
                row_scores.append(score)
            if bool(ate_flags.loc[row_index]):
                score = float(ate_scores.loc[row_index])
                series.append("ATE/TE")
                reasons.append(f"ATE/TE score {_score_text(score)} > {threshold:g}")
                row_scores.append(score)
            if bool(paired_flags.loc[row_index]):
                score = float(paired_scores.loc[row_index])
                series.append("Paired")
                reasons.append(f"{paired_metric} score {_score_text(score)} > {threshold:g}")
                row_scores.append(score)
            flagged_series = ", ".join(series)
            reason = "; ".join(reasons)
            max_score = max(row_scores)
            zero_mad_flag = any(
                status.startswith("MAD=0")
                for status, flagged in (
                    (lab_status, bool(lab_flags.loc[row_index])),
                    (ate_status, bool(ate_flags.loc[row_index])),
                    (paired_status, bool(paired_flags.loc[row_index])),
                )
                if flagged
            )
            if bool(paired_flags.loc[row_index]):
                guidance = "Paired disagreement: verify the measurement source before considering exclusion"
            elif zero_mad_flag:
                guidance = "MAD=0 candidate: indeterminate; retain unless independent evidence proves corruption"
            else:
                guidance = "Raw-series-only flag: likely a valid endpoint unless independent evidence proves corruption"
            working.loc[row_index, OUTLIER_FLAGGED] = True
            working.loc[row_index, OUTLIER_FLAGGED_SERIES] = flagged_series
            working.loc[row_index, OUTLIER_REASON] = reason
            working.loc[row_index, OUTLIER_MAX_SCORE] = max_score
            working.loc[row_index, OUTLIER_REVIEW_STATUS] = "Flagged – retained"

            source = working.loc[row_index]
            finding: dict[str, Any] = {
                OUTLIER_ROW_ID: int(source[OUTLIER_ROW_ID]),
                OUTLIER_INPUT_ROW: int(source[OUTLIER_INPUT_ROW]),
                "TestSet": selected_policy.name if selected_policy else "",
                OUTLIER_FLAGGED_SERIES: flagged_series,
                OUTLIER_REASON: reason,
                "OutlierReviewGuidance": guidance,
                OUTLIER_MAX_SCORE: max_score,
                "OutlierPopulation": _population_label(population_values),
                "LabColumn": profile.reference_column,
                "LabValue": float(reference.loc[row_index]),
                "LabMedian": lab_median,
                "LabMAD": lab_mad,
                "LabRobustScore": float(lab_scores.loc[row_index]),
                "LabMADStatus": lab_status,
                "ATEColumn": profile.candidate_column,
                "ATEValue": float(candidate.loc[row_index]),
                "ATEMedian": ate_median,
                "ATEMAD": ate_mad,
                "ATERobustScore": float(ate_scores.loc[row_index]),
                "ATEMADStatus": ate_status,
                "PairedMetric": paired_metric,
                "PairedValue": float(paired.loc[row_index]),
                "PairedMedian": paired_median,
                "PairedMAD": paired_mad,
                "PairedRobustScore": float(paired_scores.loc[row_index]),
                "PairedMADStatus": paired_status,
            }
            for column in identity_columns:
                finding[column] = source[column]
            if (
                "Test Name" not in finding
                and profile.test_name_column
                and profile.test_name_column in source.index
            ):
                finding["Test Name"] = source[profile.test_name_column]
            finding_rows.append(finding)

    findings = pd.DataFrame(finding_rows)
    if not findings.empty:
        findings = findings.sort_values(
            [column for column in ("TestSet", "Test Number", OUTLIER_INPUT_ROW) if column in findings.columns],
            kind="stable",
        ).reset_index(drop=True)
    working = working.drop(columns="__OutlierPolicyIndex")
    return OutlierAnalysis(
        threshold=threshold,
        annotated_frame=working,
        findings=findings,
        reference_column=profile.reference_column,
        candidate_column=profile.candidate_column,
        population_columns=tuple(
            column for column in population_columns if column != "__OutlierPolicyIndex"
        ),
        valid_pair_count=int(valid_pairs.sum()),
    )


def _pooled_columns(profile: CorrelationProfile, policy: TestPolicy | None) -> tuple[str, ...]:
    requested = (*profile.pooled_columns, *((policy.pooled_columns if policy else ())))
    return tuple(column for column in dict.fromkeys(requested) if column in profile.group_by)


def _group_size_map(frame: pd.DataFrame, profile: CorrelationProfile) -> dict[tuple[Any, ...], int]:
    prepared = frame.copy()
    _derive_dimensions(prepared, profile)
    if profile.test_policies:
        prepared["__OutlierPolicyIndex"] = prepared.apply(_test_policy_index, axis=1, profile=profile)
    else:
        prepared["__OutlierPolicyIndex"] = -1
    reference = _to_numeric(prepared[profile.reference_column])
    candidate = _to_numeric(prepared[profile.candidate_column])
    prepared = prepared.loc[reference.notna() & candidate.notna()].copy()
    sizes: dict[tuple[Any, ...], int] = {}
    policy_indices = range(len(profile.test_policies)) if profile.test_policies else (-1,)
    for policy_index in policy_indices:
        policy = _policy(profile, policy_index)
        policy_frame = prepared.loc[prepared["__OutlierPolicyIndex"] == policy_index]
        group_columns = [
            column
            for column in profile.group_by
            if column not in _pooled_columns(profile, policy)
        ]
        if group_columns:
            grouped = policy_frame.groupby(group_columns, dropna=False, sort=True).size()
            for raw_key, count in grouped.items():
                key = raw_key if isinstance(raw_key, tuple) else (raw_key,)
                sizes[(policy_index, *(_normal_key(value) for value in key))] = int(count)
        else:
            sizes[(policy_index,)] = len(policy_frame)
    return sizes


def _validate_minimum_points(
    original: pd.DataFrame,
    filtered: pd.DataFrame,
    profile: CorrelationProfile,
) -> None:
    before = _group_size_map(original, profile)
    after = _group_size_map(filtered, profile)
    invalid = [
        (key, count, after.get(key, 0))
        for key, count in before.items()
        if count >= profile.minimum_points and after.get(key, 0) < profile.minimum_points
    ]
    if not invalid:
        return
    examples = "; ".join(
        f"{key}: {before_count} → {after_count}"
        for key, before_count, after_count in invalid[:5]
    )
    raise ValueError(
        f"Selected exclusions would reduce {len(invalid)} correlation population(s) below the "
        f"minimum of {profile.minimum_points} points ({examples}). Retain one or more selected rows."
    )


def finalize_outlier_review(
    analysis: OutlierAnalysis,
    profile: CorrelationProfile,
    excluded_row_ids: Iterable[int] = (),
) -> tuple[pd.DataFrame, OutlierReview]:
    """Apply explicitly selected exclusions and return the filtered input plus immutable audit."""
    selected = {int(value) for value in excluded_row_ids}
    available = (
        set(pd.to_numeric(analysis.findings.get(OUTLIER_ROW_ID, pd.Series(dtype=float)), errors="coerce").dropna().astype(int))
        if not analysis.findings.empty
        else set()
    )
    unknown = sorted(selected - available)
    if unknown:
        raise ValueError(f"Outlier row IDs were not present in the review findings: {unknown}")

    reviewed_findings = analysis.findings.copy()
    if reviewed_findings.empty:
        reviewed_findings["Excluded"] = pd.Series(dtype=bool)
    else:
        reviewed_findings["Excluded"] = reviewed_findings[OUTLIER_ROW_ID].astype(int).isin(selected)
    filtered = analysis.annotated_frame.loc[
        ~analysis.annotated_frame[OUTLIER_ROW_ID].astype(int).isin(selected)
    ].copy()
    _validate_minimum_points(analysis.annotated_frame, filtered, profile)
    review = OutlierReview(
        threshold=analysis.threshold,
        findings=reviewed_findings,
        original_row_count=len(analysis.annotated_frame),
        valid_pair_count=analysis.valid_pair_count,
        final_row_count=len(filtered),
        reference_column=analysis.reference_column,
        candidate_column=analysis.candidate_column,
        population_columns=analysis.population_columns,
    )
    return filtered, review


def _same_value(left: Any, right: Any) -> bool:
    if pd.isna(left) and pd.isna(right):
        return True
    try:
        return float(left) == float(right)
    except (TypeError, ValueError):
        return str(left).strip().casefold() == str(right).strip().casefold()


def _findings_for_summary(
    findings: pd.DataFrame,
    summary: pd.Series,
    profile: CorrelationProfile,
) -> pd.DataFrame:
    if findings.empty:
        return findings
    mask = pd.Series(True, index=findings.index)
    if "TestSet" in findings.columns and "TestSet" in summary.index:
        mask &= findings["TestSet"].map(lambda value: _same_value(value, summary["TestSet"]))
    for column in profile.group_by:
        if column not in findings.columns or column not in summary.index:
            continue
        summary_value = summary[column]
        if str(summary_value).strip().upper() == "MERGED":
            continue
        mask &= findings[column].map(lambda value, expected=summary_value: _same_value(value, expected))
    return findings.loc[mask]


def attach_outlier_audit(result: Any, profile: CorrelationProfile, review: OutlierReview) -> Any:
    """Attach group-level counts and the final review object to a CorrelationResult."""
    from .correlation import CorrelationResult

    summary = result.summary.copy()
    for index, row in summary.iterrows():
        relevant = _findings_for_summary(review.findings, row, profile)
        excluded = int(relevant.get("Excluded", pd.Series(False, index=relevant.index)).fillna(False).astype(bool).sum())
        flagged = len(relevant)
        final_count = int(row.get("Count", 0))
        summary.loc[index, "OriginalCount"] = final_count + excluded
        summary.loc[index, "OutlierFlaggedCount"] = flagged
        summary.loc[index, "OutlierRetainedCount"] = flagged - excluded
        summary.loc[index, "OutlierExcludedCount"] = excluded
        summary.loc[index, "FinalCorrelationCount"] = final_count
    return CorrelationResult(summary, result.details.copy(), review)
