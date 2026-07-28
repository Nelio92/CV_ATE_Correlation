"""Initial CTRX8188 profiles; all subsystem knowledge is isolated here."""

from __future__ import annotations

from .models import (
    ConditionalDimension,
    CorrelationProfile,
    CovariateProfile,
    DerivedField,
    ExtractionProfile,
    GuardBandProfile,
    MatchCase,
    RegexField,
    RequirementRule,
    TestSelector,
)


_COMMON_COLUMNS = (
    "DUT Nr", "Wafer", "X", "Y", "DoE split", "Test Number", "Test Name",
    "Test Value", "LUT value", "Low", "High", "Unit", "Temperature",
    "Voltage corner", "Frequency_GHz", "Insertion Type",
)

_DERIVED_FIELDS = (
    DerivedField("Insertion Type", "filename", (
        MatchCase("S11P", "FE"), MatchCase("S21P", "FE"), MatchCase("S31P", "FE"),
    ), "BE"),
    DerivedField("Temperature", "filename", (
        MatchCase(r"S1|HT", 135, "regex"), MatchCase("S2", -40), MatchCase(r"S3|RT", 25, "regex"),
    )),
    DerivedField("Voltage corner", "test_name", (
        MatchCase("095", "VMIN"), MatchCase("105", "VMAX"), MatchCase("100", "VNOM"),
    )),
    DerivedField("Frequency_GHz", "test_name", (
        MatchCase(r"(?<!\d)81(?!\d)", 81, "regex"), MatchCase("80p5", 80.5),
        MatchCase(r"(?<!\d)77(?!\d)", 77, "regex"), MatchCase("76p5", 76.5),
        MatchCase(r"(?<!\d)76(?!\d)", 76, "regex"),
    )),
)

_LUT = RegexField("LUT value", "test_name", r"FwLu(?P<lut>\d{1,3})(?!\d)", "lut", "int", "")


def _extraction(
    name: str,
    selector: TestSelector,
    *,
    include_lut: bool = True,
    extra_derived: tuple[DerivedField, ...] = (),
    metadata_value_maps: dict | None = None,
    derived_value_maps: dict | None = None,
) -> ExtractionProfile:
    columns = _COMMON_COLUMNS if include_lut else tuple(column for column in _COMMON_COLUMNS if column != "LUT value")
    return ExtractionProfile(
        name=name,
        selector=selector,
        output_columns=columns,
        derived_fields=(*_DERIVED_FIELDS, *extra_derived),
        regex_fields=(_LUT,) if include_lut else (),
        coordinate_fallback={"WAFER": "62007", "X": "62008", "Y": "62009"},
        metadata_value_maps=metadata_value_maps or {},
        derived_value_maps=derived_value_maps or {},
    )


EXTRACTION_PROFILES = {
    "ctrx8188-dpll": _extraction(
        "CTRX8188 DPLL phase noise",
        TestSelector(exact=(52047, 52064, 52065, 52095, 52104, 52105), ranges=((52004, 52009),)),
        include_lut=False,
        metadata_value_maps={"DoE split": {"TT": "POR"}},
    ),
    "ctrx8188-kf": _extraction("CTRX8188 Kf", TestSelector(exact=(52046, 52084, 52094))),
    "ctrx8188-txlo": _extraction(
        "CTRX8188 TXLO power",
        TestSelector(ranges=((57006, 57009), (57039, 57051), (57099, 57111), (57159, 57171),
                             (57219, 57231), (57279, 57291), (57339, 57351))),
    ),
    "ctrx8188-txpa": _extraction(
        "CTRX8188 TXPA power",
            TestSelector(exact=(52046, 52084, 52094), ranges=((53171, 53290), (53719, 53838), (54139, 54258),
                             (54489, 54608), (55139, 55258), (55489, 55608))),
    ),
}

_DETAIL_KEYS = ("DUT Nr", "Wafer", "X", "Y", "Temperature", "Voltage corner", "Frequency_GHz", "Test Number")
_COVARIATE = CovariateProfile("Test Value", ("DUT Nr", "Temperature"), "Kf")

_TXLO_RULE = RequirementRule(
    when={"LO IDAC": (112,)}, lower=9.0, upper=16.0,
    lower_residual="minimum", upper_residual="maximum",
    method="requirements+abs(min residual)/-abs(max residual)",
)
_PHYSICS_TXLO_RULE = RequirementRule(
    when={"LO IDAC": (112,)}, lower=9.0, upper=16.0,
    lower_residual="maximum", upper_residual="minimum",
    method="requirements+abs(max residual)/-abs(min residual)",
)
_TXPA_VMIN_RULE = RequirementRule(
    when={"LUT value": (255,), "Voltage corner": ("VMIN",)}, lower=10.0, upper=16.0,
    lower_residual="minimum", upper_residual="maximum",
    bypass_when={"Insertion Type": ("BE",)},
    method="requirements+abs(min residual)/-abs(max residual)",
    bypass_method="REQ_MIN/REQ_MAX (BE)",
)
_TXPA_RULE = RequirementRule(
    when={"LUT value": (255,)}, lower=10.0, upper=16.0,
    lower_residual="maximum", upper_residual="minimum",
    bypass_when={"Insertion Type": ("BE",)},
    method="requirements+abs(max residual)/-abs(min residual)",
    bypass_method="REQ_MIN/REQ_MAX (BE)",
)

CORRELATION_PROFILES = {
    "ctrx8188-dpll": CorrelationProfile(
        name="CTRX8188 DPLL phase noise",
        strategy="mean_delta", reference_column="CV_PN_DIV8", candidate_column="ATE_PN_DIV8",
        group_by=("Test Number", "Voltage corner", "Frequency_GHz", "Temperature"),
        lower_limit_column="Low", upper_limit_column="High", unit_column="Unit",
        detail_key_columns=_DETAIL_KEYS,
        guard_band=GuardBandProfile(kind="shifted_upper_limit"),
    ),
    "ctrx8188-txlo": CorrelationProfile(
        name="CTRX8188 TXLO power",
        strategy="median_offset", reference_column="CV_LO_Power", candidate_column="ATE_LO_Power",
        group_by=("Test Number", "Voltage corner", "Frequency_GHz", "Temperature", "LO IDAC"),
        lower_limit_column="Low", upper_limit_column="High", unit_column="Unit",
        detail_key_columns=_DETAIL_KEYS,
        guard_band=GuardBandProfile(kind="distribution_sigma", rules=(_TXLO_RULE,)),
        covariate=_COVARIATE,
        covariate_guard_band=GuardBandProfile(kind="distribution_sigma", rules=(_PHYSICS_TXLO_RULE,)),
    ),
    "ctrx8188-txpa": CorrelationProfile(
        name="CTRX8188 TXPA power",
        strategy="median_offset", reference_column="CV_PA_Power", candidate_column="ATE_PA_Power",
        group_by=("LUT value", "Voltage corner", "Frequency_GHz", "Temperature", "PA Channel"),
        lower_limit_column="Low", upper_limit_column="High", unit_column="Unit",
        detail_key_columns=_DETAIL_KEYS,
        derived_dimensions=(ConditionalDimension(
            target="PA Channel", source="Test Name", pattern=r"TX(?P<channel>[1-8])(?!\d)",
            group="channel", value_format="TX{}", when={"LUT value": (255,)}, default="ALL",
        ),),
        guard_band=GuardBandProfile(kind="distribution_sigma", rules=(_TXPA_VMIN_RULE, _TXPA_RULE)),
        covariate=_COVARIATE,
        covariate_guard_band=GuardBandProfile(kind="distribution_sigma", rules=(_TXPA_RULE,)),
    ),
}


def get_extraction_profile(name: str) -> ExtractionProfile:
    try:
        return EXTRACTION_PROFILES[name]
    except KeyError as error:
        raise KeyError(f"Unknown extraction profile '{name}'. Choices: {', '.join(EXTRACTION_PROFILES)}") from error


def get_correlation_profile(name: str) -> CorrelationProfile:
    try:
        return CORRELATION_PROFILES[name]
    except KeyError as error:
        raise KeyError(f"Unknown correlation profile '{name}'. Choices: {', '.join(CORRELATION_PROFILES)}") from error
