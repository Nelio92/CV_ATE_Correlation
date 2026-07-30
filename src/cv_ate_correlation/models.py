"""Validated, subsystem-neutral configuration contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, TypeAlias


CorrelationStrategy: TypeAlias = Literal[
    "Linear",
    "Mean_Deltas",
    "Median_Deltas",
    "Physics-based",
    "mean_delta",
    "median_offset",
    "physics_based",
    "legacy_mean_delta",
]
GuardBandKind: TypeAlias = Literal[
    "distribution_sigma",
    "Max_residuals",
    "max_residuals",
    "shifted_upper_limit",
]


def normalize_correlation_strategy(
    value: str,
) -> Literal["Linear", "Mean_Deltas", "Median_Deltas", "Physics-based"]:
    """Return the user-facing strategy name, accepting persisted legacy aliases."""
    aliases = {
        "linear": "Linear",
        "mean_deltas": "Mean_Deltas",
        "mean_delta": "Mean_Deltas",
        "legacy_mean_delta": "Mean_Deltas",
        "median_deltas": "Median_Deltas",
        "median_offset": "Median_Deltas",
        "physics-based": "Physics-based",
        "physics_based": "Physics-based",
    }
    try:
        return aliases[str(value).strip().casefold()]  # type: ignore[return-value]
    except KeyError as error:
        raise ValueError(f"Unknown correlation strategy: {value}") from error


def normalize_guard_band_kind(
    value: str,
    *,
    migrate_legacy_shifted: bool = False,
) -> Literal["distribution_sigma", "Max_residuals", "shifted_upper_limit"]:
    """Return a canonical policy name and optionally migrate old custom profiles."""
    normalized = str(value).strip().casefold()
    aliases = {
        "distribution_sigma": "distribution_sigma",
        "max_residuals": "Max_residuals",
        "shifted_upper_limit": "Max_residuals" if migrate_legacy_shifted else "shifted_upper_limit",
    }
    try:
        return aliases[normalized]  # type: ignore[return-value]
    except KeyError as error:
        raise ValueError(f"Unknown guard-band policy: {value}") from error


DEFAULT_COORDINATE_FALLBACK: Mapping[str, str] = {
    "WAFER": "62007",
    "X": "62008",
    "Y": "62009",
}


@dataclass(frozen=True)
class TestSelector:
    exact: tuple[int, ...] = ()
    ranges: tuple[tuple[int, int], ...] = ()
    name_contains: tuple[str, ...] = ()

    def matches(self, number: int, name: str) -> bool:
        if number in self.exact:
            return True
        if any(start <= number <= end for start, end in self.ranges):
            return True
        lowered = str(name).lower()
        return any(token.lower() in lowered for token in self.name_contains)


@dataclass(frozen=True)
class MatchCase:
    pattern: str
    value: Any
    mode: Literal["contains", "regex"] = "contains"


@dataclass(frozen=True)
class DerivedField:
    target: str
    source: Literal["filename", "test_name"]
    cases: tuple[MatchCase, ...]
    default: Any = "Unknown"


@dataclass(frozen=True)
class RegexField:
    target: str
    source: Literal["filename", "test_name"]
    pattern: str
    group: str | int = 1
    cast: Literal["str", "int", "float"] = "str"
    default: Any = ""


@dataclass(frozen=True)
class InsertionProfile:
    name: str
    group: Literal["FE", "BE"]
    temperature: float
    raw_files: tuple[str, ...]


@dataclass(frozen=True)
class ExtractionProfile:
    name: str
    selector: TestSelector
    output_columns: tuple[str, ...]
    derived_fields: tuple[DerivedField, ...] = ()
    regex_fields: tuple[RegexField, ...] = ()
    coordinate_columns: tuple[str, str, str] = ("WAFER", "X", "Y")
    coordinate_fallback: Mapping[str, str] = field(
        default_factory=lambda: dict(DEFAULT_COORDINATE_FALLBACK)
    )
    insertion_field: str = "Insertion Type"
    fallback_insertion_values: tuple[str, ...] = ("BE",)
    metadata_value_maps: Mapping[str, Mapping[Any, Any]] = field(default_factory=dict)
    derived_value_maps: Mapping[str, Mapping[Any, Any]] = field(default_factory=dict)
    insertions: tuple[InsertionProfile, ...] = ()


@dataclass(frozen=True)
class ConditionalDimension:
    target: str
    source: str
    pattern: str
    group: str | int = 1
    value_format: str = "{}"
    when: Mapping[str, tuple[Any, ...]] = field(default_factory=dict)
    default: Any = "ALL"


@dataclass(frozen=True)
class RequirementRule:
    when: Mapping[str, tuple[Any, ...]]
    lower: float
    upper: float
    lower_residual: Literal["minimum", "maximum"] = "minimum"
    upper_residual: Literal["minimum", "maximum"] = "maximum"
    bypass_when: Mapping[str, tuple[Any, ...]] = field(default_factory=dict)
    method: str = "requirements with residual guard-band"
    bypass_method: str = "requirements without residual guard-band"


@dataclass(frozen=True)
class GuardBandProfile:
    kind: GuardBandKind
    sigma_multiplier: float = 6.0
    rules: tuple[RequirementRule, ...] = ()
    factor_multiplier: float = -1.0
    residual_multiplier: float = -1.0
    requirement_min: float | None = None
    requirement_max: float | None = None


@dataclass(frozen=True)
class CovariateProfile:
    value_column: str
    merge_keys: tuple[str, ...]
    output_name: str = "Covariate"
    test_number: int = 52046


@dataclass(frozen=True)
class TestPolicy:
    name: str
    selector: TestSelector
    strategy: CorrelationStrategy
    guard_band: GuardBandProfile
    pooled_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class CorrelationProfile:
    name: str
    strategy: CorrelationStrategy
    reference_column: str
    candidate_column: str
    group_by: tuple[str, ...]
    minimum_points: int = 5
    lower_limit_column: str | None = None
    upper_limit_column: str | None = None
    unit_column: str | None = None
    test_name_column: str | None = "Test Name"
    detail_key_columns: tuple[str, ...] = ()
    derived_dimensions: tuple[ConditionalDimension, ...] = ()
    guard_band: GuardBandProfile = GuardBandProfile(kind="distribution_sigma")
    covariate: CovariateProfile | None = None
    covariate_guard_band: GuardBandProfile | None = None
    test_policies: tuple[TestPolicy, ...] = ()
    pooled_columns: tuple[str, ...] = ()
