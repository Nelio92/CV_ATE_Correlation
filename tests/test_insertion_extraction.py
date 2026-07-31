from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from cv_ate_correlation.extraction import LegacyWideTeCsvAdapter
from cv_ate_correlation.models import (
    DerivedField,
    ExtractionProfile,
    InsertionProfile,
    MatchCase,
    TestSelector as ProfileTestSelector,
)


def write_raw_export(path: Path) -> None:
    rows = [
        "WAFER;X;Y;101",
        ";;;Leakage Current",
        ";;;-1",
        ";;;1",
        ";;;A",
        *[";;;" for _ in range(8)],
        "1;2;3;0.25",
    ]
    path.write_text("\n".join(rows), encoding="latin1")


def write_be_raw_export(path: Path, *, include_primary_columns: bool, tn_prefix: bool = False) -> None:
    primary_headers = ["WAFER", "X", "Y"] if include_primary_columns else []
    fuse_headers = [f"TN{number}" if tn_prefix else number for number in ("62007", "62008", "62009")]
    headers = [*primary_headers, *fuse_headers, "101"]
    empty_primary = [""] * len(primary_headers)
    rows = [
        ";".join(headers),
        ";".join([""] * (len(headers) - 1) + ["Leakage Current"]),
        ";".join([""] * (len(headers) - 1) + ["-1"]),
        ";".join([""] * (len(headers) - 1) + ["1"]),
        ";".join([""] * (len(headers) - 1) + ["A"]),
        *[";".join([""] * len(headers)) for _ in range(8)],
        ";".join([*empty_primary, "1", "2", "3", "0.25"]),
    ]
    path.write_text("\n".join(rows), encoding="latin1")


def test_insertion_assignment_derives_group_name_and_temperature(tmp_path: Path) -> None:
    raw_file = tmp_path / "arbitrary_name.csv"
    write_raw_export(raw_file)
    manifest = tmp_path / "chips.csv"
    pd.DataFrame({
        "Wafer": [1], "X": [2], "Y": [3], "DUT Nr": [7], "DoE split": ["TT"],
    }).to_csv(manifest, index=False)
    profile = ExtractionProfile(
        name="Insertion extraction",
        selector=ProfileTestSelector(exact=(101,)),
        output_columns=(
            "DUT Nr",
            "Test Number",
            "Test Value",
            "Insertion",
            "Insertion Type",
            "Temperature",
        ),
        derived_fields=(
            DerivedField("Temperature", "test_name", (MatchCase("Leakage", -40),), 25),
        ),
        insertions=(InsertionProfile("S1", "FE", 135.0, (str(raw_file),)),),
    )

    result = LegacyWideTeCsvAdapter().extract(tmp_path / "unused", manifest, profile)

    assert result.to_dict("records") == [{
        "DUT Nr": 7,
        "Test Number": 101,
        "Test Value": 0.25,
        "Insertion": "S1",
        "Insertion Type": "FE",
        "Temperature": 135.0,
    }]


@pytest.mark.parametrize(
    ("include_primary_columns", "tn_prefix"),
    [(True, False), (False, False), (False, True)],
)
def test_be_insertion_uses_default_fuse_coordinate_fallback(
    tmp_path: Path,
    include_primary_columns: bool,
    tn_prefix: bool,
) -> None:
    raw_file = tmp_path / "be.csv"
    write_be_raw_export(
        raw_file,
        include_primary_columns=include_primary_columns,
        tn_prefix=tn_prefix,
    )
    manifest = tmp_path / "chips.csv"
    pd.DataFrame({
        "Wafer": [1], "X": [2], "Y": [3], "DUT Nr": [8], "DoE split": ["SS"],
    }).to_csv(manifest, index=False)
    profile = ExtractionProfile(
        name="BE FUSE coordinate fallback",
        selector=ProfileTestSelector(exact=(101,)),
        output_columns=("DUT Nr", "Wafer", "X", "Y", "Test Number", "Test Value"),
        insertions=(InsertionProfile("S4", "BE", 25.0, (str(raw_file),)),),
    )

    result = LegacyWideTeCsvAdapter().extract(tmp_path / "unused", manifest, profile)

    assert result.to_dict("records") == [{
        "DUT Nr": 8,
        "Wafer": 1,
        "X": 2,
        "Y": 3,
        "Test Number": 101,
        "Test Value": 0.25,
    }]
