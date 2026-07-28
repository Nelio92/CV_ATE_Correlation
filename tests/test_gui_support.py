from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from cv_ate_correlation.gui import (
    GROUPING_CONDITION_OPTIONS,
    compile_grouping_conditions,
    correlation_group_columns,
    grouping_condition_definitions,
    grouping_condition_state,
    validate_insertion_definitions,
    workbook_sheet_names,
)


def test_workbook_sheet_names_preserves_workbook_order(tmp_path: Path) -> None:
    workbook = tmp_path / "workflow.xlsx"
    with pd.ExcelWriter(workbook, engine="openpyxl") as writer:
        pd.DataFrame({"value": [1]}).to_excel(writer, sheet_name="Extracted_Data", index=False)
        pd.DataFrame({"value": [2]}).to_excel(writer, sheet_name="Correlation_Input", index=False)

    assert workbook_sheet_names(workbook) == ("Extracted_Data", "Correlation_Input")


def test_profile_editor_exposes_standard_grouping_conditions() -> None:
    assert tuple(option.label for option in GROUPING_CONDITION_OPTIONS) == (
        "DUT Nr",
        "Test Number",
        "Frequency",
        "Supply Corner",
        "Channel",
        "Digital Control",
    )

    state = grouping_condition_state({
        "group_by": (
            "DUT Nr, Test Number, Frequency_GHz, Voltage corner, "
            "PA Channel, LUT value"
        ),
    })
    assert state == {
        "dut_nr": (True, "DUT Nr"),
        "test_number": (True, "Test Number"),
        "frequency": (True, "Frequency_GHz"),
        "supply_corner": (True, "Voltage corner"),
        "channel": (True, "PA Channel"),
        "digital_control": (True, "LUT value"),
    }
    assert correlation_group_columns(("Test Number", "Frequency_GHz")) == (
        "Test Number",
        "Frequency_GHz",
        "Temperature",
    )
    assert correlation_group_columns(("Test Number", "Temperature")) == (
        "Test Number",
        "Temperature",
    )


def test_profile_editor_restores_custom_grouping_column_names() -> None:
    state = grouping_condition_state({
        "group_by": "RF Frequency, Supply State",
        "grouping_frequency_column": "RF Frequency",
        "grouping_supply_corner_column": "Supply State",
    })
    assert state["frequency"] == (True, "RF Frequency")
    assert state["supply_corner"] == (True, "Supply State")
    assert state["channel"] == (False, "Channel")


def test_every_insertion_requires_existing_raw_data(tmp_path: Path) -> None:
    s1 = tmp_path / "S1.csv"
    s1.write_text("header", encoding="utf-8")
    validated = validate_insertion_definitions([
        {"name": "S1", "group": "FE", "temperature": "135", "raw_files": [str(s1)]},
    ])
    assert validated == [{
        "name": "S1",
        "group": "FE",
        "temperature": 135.0,
        "raw_files": [str(s1.resolve())],
    }]

    with pytest.raises(ValueError, match="at least one corresponding raw test-data file"):
        validate_insertion_definitions([
            {"name": "S2", "group": "BE", "temperature": "-40", "raw_files": []},
        ])


def test_raw_file_cannot_be_assigned_to_two_insertions(tmp_path: Path) -> None:
    raw = tmp_path / "shared.csv"
    raw.write_text("header", encoding="utf-8")
    with pytest.raises(ValueError, match="assigned to both"):
        validate_insertion_definitions([
            {"name": "S1", "group": "FE", "temperature": 135, "raw_files": [str(raw)]},
            {"name": "S2", "group": "BE", "temperature": -40, "raw_files": [str(raw)]},
        ])


def test_guided_grouping_identification_compiles_without_user_regex() -> None:
    definitions = [
        {
            "label": "Supply Corner",
            "column": "Voltage corner",
            "enabled": True,
            "source": "filename",
            "method": "mapping",
            "expression": "095 => VMIN\n105 => VMAX",
            "default": "Unknown",
            "cast": "str",
        },
        {
            "label": "Digital Control",
            "column": "LUT value",
            "enabled": True,
            "source": "test_name",
            "method": "number_after",
            "expression": "FwLu",
            "default": "",
            "cast": "int",
        },
    ]
    group_by, condition_rules, regex_rules = compile_grouping_conditions(definitions)
    assert group_by == ("Voltage corner", "LUT value")
    assert "Voltage corner ; filename ; contains ; 095 ; VMIN ; Unknown" in condition_rules
    assert "Voltage corner ; filename ; contains ; 105 ; VMAX ; Unknown" in condition_rules
    assert "LUT value ; test_name ; FwLu" in regex_rules
    assert " ; 1 ; int ; " in regex_rules


def test_legacy_saved_temperature_condition_is_removed() -> None:
    definitions = grouping_condition_definitions({
        "grouping_conditions": [
            {
                "key": "temperature",
                "label": "Temperature",
                "column": "Temperature",
                "enabled": True,
            },
            {
                "key": "test_number",
                "label": "Test Number",
                "column": "Test Number",
                "enabled": True,
                "source": "existing",
                "method": "existing",
            },
        ],
    })
    assert all(item["column"] != "Temperature" for item in definitions)


def test_custom_grouping_condition_supports_optional_advanced_regex() -> None:
    definitions = grouping_condition_definitions({
        "group_by": "Test Number, Bias Mode",
        "regex_rules": r"Bias Mode ; test_name ; BIAS_(\w+) ; 1 ; str ; Unknown",
    })
    bias = next(item for item in definitions if item["column"] == "Bias Mode")
    assert bias["custom"] is True
    assert bias["method"] == "regex"
    assert bias["expression"] == r"BIAS_(\w+)"

    group_by, _condition_rules, regex_rules = compile_grouping_conditions(definitions)
    assert group_by == ("Test Number", "Bias Mode")
    assert r"Bias Mode ; test_name ; BIAS_(\w+) ; 1 ; str ; Unknown" == regex_rules
