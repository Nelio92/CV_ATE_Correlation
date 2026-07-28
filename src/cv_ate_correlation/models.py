"""Validated, subsystem-neutral configuration contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping


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
class ExtractionProfile:
    name: str
    selector: TestSelector
    output_columns: tuple[str, ...]
    derived_fields: tuple[DerivedField, ...] = ()
    regex_fields: tuple[RegexField, ...] = ()
    coordinate_columns: tuple[str, str, str] = ("WAFER", "X", "Y")
    coordinate_fallback: Mapping[str, str] = field(default_factory=dict)
    insertion_field: str = "Insertion Type"
    fallback_insertion_values: tuple[str, ...] = ("BE",)
    metadata_value_maps: Mapping[str, Mapping[Any, Any]] = field(default_factory=dict)
    derived_value_maps: Mapping[str, Mapping[Any, Any]] = field(default_factory=dict)


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
    kind: Literal["distribution_sigma", "shifted_upper_limit"]
    sigma_multiplier: float = 6.0
    rules: tuple[RequirementRule, ...] = ()
    factor_multiplier: float = -1.0
    residual_multiplier: float = -1.0


@dataclass(frozen=True)
class CovariateProfile:
    value_column: str
    merge_keys: tuple[str, ...]
    output_name: str = "Covariate"


@dataclass(frozen=True)
class CorrelationProfile:
    name: str
    strategy: Literal["mean_delta", "median_offset"]
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
