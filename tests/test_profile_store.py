from __future__ import annotations

import json

import pandas as pd
import pytest

from cv_ate_correlation.correlation import correlate_frame
from cv_ate_correlation.gui import APPLICATION_TITLE
from cv_ate_correlation.profile_store import (
    delete_custom_profile,
    load_custom_profile_specs,
    load_custom_profiles,
    parse_condition_rules,
    parse_regex_rules,
    parse_test_selector,
    profile_spec_to_models,
    save_custom_profile_spec,
)
from cv_ate_correlation.profiles_8188 import (
    CORRELATION_PROFILES,
    EXTRACTION_PROFILES,
    refresh_profiles,
)


def custom_spec() -> dict[str, object]:
    return {
        "display_name": "Generic current correlation",
        "tests": "101, 200-202, LeakageCurrent",
        "group_by": "Test Number, Temperature, Voltage corner",
        "strategy": "median_offset",
        "reference_column": "Lab Current",
        "candidate_column": "Test Value",
        "minimum_points": "3",
        "detail_key_columns": "DUT Nr, Wafer, X, Y",
        "additional_output_columns": "Temperature, Voltage corner",
        "coordinate_columns": "WAFER, X, Y",
        "coordinate_fallback": "WAFER=62007, X=62008, Y=62009",
        "insertion_field": "Insertion Type",
        "fallback_insertion_values": "BE",
        "condition_rules": (
            "Temperature ; filename ; contains ; HOT ; 125 ; 25\n"
            "Voltage corner ; test_name ; regex ; VMIN|095 ; VMIN ; VNOM"
        ),
        "regex_rules": "Channel ; test_name ; CH(?P<channel>\\d+) ; channel ; int ; 0",
        "lower_limit_column": "Low",
        "upper_limit_column": "High",
        "unit_column": "Unit",
        "test_name_column": "Test Name",
        "guard_band_kind": "distribution_sigma",
        "sigma_multiplier": "6",
        "covariate_value_column": "",
        "covariate_merge_keys": "",
        "covariate_output_name": "Covariate",
        "insertions": [{
            "name": "S1",
            "group": "FE",
            "temperature": 125,
            "raw_files": [__file__],
        }],
    }


def test_compact_profile_fields_are_parsed() -> None:
    assert APPLICATION_TITLE == "CorreLaTE: ATE-to-Lab Correlation"
    selector = parse_test_selector("101, 200-202, LeakageCurrent")
    assert selector.matches(101, "anything")
    assert selector.matches(201, "anything")
    assert selector.matches(999, "My LeakageCurrent measurement")
    assert not selector.matches(999, "unrelated")

    conditions = parse_condition_rules(custom_spec()["condition_rules"])
    regex = parse_regex_rules(custom_spec()["regex_rules"])
    assert [rule.target for rule in conditions] == ["Temperature", "Voltage corner"]
    assert regex[0].target == "Channel"
    assert regex[0].group == "channel"


def test_custom_profile_builds_extraction_and_correlation_models() -> None:
    extraction, correlation = profile_spec_to_models("my-current", custom_spec())
    assert extraction.name == "Generic current correlation"
    assert extraction.coordinate_fallback == {"WAFER": "62007", "X": "62008", "Y": "62009"}
    assert "Temperature" in extraction.output_columns
    assert "Voltage corner" in extraction.output_columns
    assert extraction.insertions[0].name == "S1"
    assert extraction.insertions[0].group == "FE"
    assert extraction.insertions[0].temperature == 125
    assert correlation.reference_column == "Lab Current"
    assert correlation.candidate_column == "Test Value"
    assert correlation.group_by == ("Test Number", "Voltage corner", "Insertion", "Temperature")
    assert correlation.minimum_points == 3
    assert correlation.covariate is None


def test_be_fuse_coordinate_fallback_is_the_custom_profile_default() -> None:
    spec = custom_spec()
    spec["coordinate_fallback"] = ""
    spec["fallback_insertion_values"] = ""

    extraction, _correlation = profile_spec_to_models("my-current", spec)

    assert extraction.coordinate_fallback == {"WAFER": "62007", "X": "62008", "Y": "62009"}
    assert extraction.fallback_insertion_values == ("BE",)


def test_insertions_at_the_same_temperature_are_separate_correlation_groups() -> None:
    spec = custom_spec()
    spec["insertions"] = [
        {"name": "S1", "group": "FE", "temperature": 125, "raw_files": [__file__]},
        {"name": "B1", "group": "BE", "temperature": 125, "raw_files": [str(__file__) + ".other"]},
    ]
    _extraction, correlation = profile_spec_to_models("my-current", spec)
    frame = pd.DataFrame([
        {
            "DUT Nr": dut,
            "Test Number": 101,
            "Voltage corner": "VMIN",
            "Insertion": insertion,
            "Temperature": 125,
            "Lab Current": dut + 0.1,
            "Test Value": dut,
            "Test Name": "LeakageCurrent",
        }
        for insertion in ("S1", "B1")
        for dut in range(1, 12)
    ])

    result = correlate_frame(frame, correlation)

    assert len(result.summary) == 2
    assert set(result.summary["Insertion"]) == {"S1", "B1"}
    assert set(result.summary["Count"]) == {11}


def test_profile_supports_multiple_test_specific_policies(tmp_path) -> None:
    spec = custom_spec()
    spec["test_sets"] = [
        {
            "name": "Phase noise",
            "tests": "101, 200-202",
            "strategy": "mean_delta",
            "guard_band_kind": "shifted_upper_limit",
            "sigma_multiplier": 6,
        },
        {
            "name": "Leakage",
            "tests": "LeakageCurrent, 301",
            "strategy": "median_offset",
            "guard_band_kind": "distribution_sigma",
            "sigma_multiplier": 4,
        },
    ]

    extraction, correlation = profile_spec_to_models("my-current", spec)

    assert extraction.selector.matches(201, "unrelated")
    assert extraction.selector.matches(999, "LeakageCurrent hot")
    assert [policy.name for policy in correlation.test_policies] == ["Phase noise", "Leakage"]
    assert correlation.test_policies[0].strategy == "mean_delta"
    assert correlation.test_policies[0].guard_band.kind == "shifted_upper_limit"
    assert correlation.test_policies[1].strategy == "median_offset"
    assert correlation.test_policies[1].guard_band.sigma_multiplier == 4

    store = tmp_path / "profiles.json"
    save_custom_profile_spec("my-current", spec, store)
    reloaded_extraction, reloaded_correlation = load_custom_profiles(store)
    assert reloaded_extraction["my-current"].selector.matches(301, "unrelated")
    assert [policy.name for policy in reloaded_correlation["my-current"].test_policies] == [
        "Phase noise",
        "Leakage",
    ]


def test_custom_profiles_round_trip_through_json_store(tmp_path) -> None:
    path = tmp_path / "profiles.json"
    save_custom_profile_spec("my-current", custom_spec(), path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert "my-current" in load_custom_profile_specs(path)

    extraction, correlation = load_custom_profiles(path)
    assert set(extraction) == {"my-current"}
    assert set(correlation) == {"my-current"}
    assert delete_custom_profile("my-current", path)
    assert load_custom_profile_specs(path) == {}
    assert not delete_custom_profile("my-current", path)


def test_custom_profile_is_merged_into_runtime_registries(tmp_path, monkeypatch) -> None:
    with monkeypatch.context() as environment:
        environment.setenv("CORRELATE_PROFILE_STORE", str(tmp_path / "profiles.json"))
        save_custom_profile_spec("my-current", custom_spec())
        refresh_profiles(strict=True)
        assert "my-current" in EXTRACTION_PROFILES
        assert "my-current" in CORRELATION_PROFILES
    refresh_profiles(strict=True)


def test_built_in_ids_cannot_be_overwritten(tmp_path) -> None:
    for profile_id in ("ctrx8188-dpll", "ctrx8188-kf"):
        with pytest.raises(ValueError, match="read-only"):
            save_custom_profile_spec(profile_id, custom_spec(), tmp_path / "profiles.json")


def test_invalid_profile_is_rejected() -> None:
    spec = custom_spec()
    spec["tests"] = "300-200"
    with pytest.raises(ValueError, match="descending"):
        profile_spec_to_models("my-current", spec)

    spec = custom_spec()
    spec["covariate_value_column"] = "Kf"
    with pytest.raises(ValueError, match="must either both be set"):
        profile_spec_to_models("my-current", spec)
