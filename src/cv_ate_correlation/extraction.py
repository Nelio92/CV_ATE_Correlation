"""Streaming adapters for legacy, very-wide ATE exports."""

from __future__ import annotations

import csv
import math
import re
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .chip_manifest import CHIP_MANIFEST_COLUMNS
from .models import DerivedField, ExtractionProfile, InsertionProfile, RegexField


def _normalize_wafer(value: Any) -> str:
    text = str(value).strip().strip('"').strip("'")
    normalized = text.replace(",", ".")
    try:
        number = float(normalized)
    except ValueError:
        return text
    if math.isfinite(number) and number.is_integer():
        return str(int(number))
    return text


def _parse_number(value: Any) -> Any:
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return ""
    normalized = text.replace(",", ".")
    try:
        number = float(normalized)
    except ValueError:
        return text
    if math.isfinite(number) and number.is_integer():
        return int(number)
    return number


def _is_missing(value: Any) -> bool:
    return str(value).strip().lower() in {"", "nan", "none"}


def _derive(rule: DerivedField, *, filename: str, test_name: str) -> Any:
    source = filename if rule.source == "filename" else test_name
    for case in rule.cases:
        if case.mode == "regex":
            if re.search(case.pattern, source, flags=re.IGNORECASE):
                return case.value
        elif case.pattern.lower() in source.lower():
            return case.value
    return rule.default


def _extract_regex(rule: RegexField, *, filename: str, test_name: str) -> Any:
    source = filename if rule.source == "filename" else test_name
    match = re.search(rule.pattern, source, flags=re.IGNORECASE)
    if not match:
        return rule.default
    value: Any = match.group(rule.group)
    try:
        if rule.cast == "int":
            return int(value)
        if rule.cast == "float":
            return float(value)
    except (TypeError, ValueError):
        return rule.default
    return str(value)


def read_chip_manifest(path: Path) -> tuple[set[tuple[str, int, int]], dict[tuple[str, int, int], dict[str, str]]]:
    """Read and validate the five required chip identity and split columns."""
    if path.suffix.lower() in {".xlsx", ".xlsm", ".xls"}:
        frame = pd.read_excel(path, sheet_name=0)
    else:
        frame = pd.read_csv(path, sep=None, engine="python")

    def normalize_header(value: Any) -> str:
        return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).split())

    aliases = {
        "DUT Nr": ("dut nr", "dut number", "dut no", "dut id", "dut"),
        "Wafer": ("wafer", "wafer nr", "wafer number", "wafer id", "waf"),
        "X": ("x", "x coordinate", "die x"),
        "Y": ("y", "y coordinate", "die y"),
        "DoE split": ("doe split", "split", "process split", "corner split", "process corner"),
    }
    available = {normalize_header(column): column for column in frame.columns}
    resolved = {
        canonical: next((available[name] for name in names if name in available), None)
        for canonical, names in aliases.items()
    }
    missing_columns = [column for column in CHIP_MANIFEST_COLUMNS if resolved[column] is None]
    if missing_columns:
        raise ValueError(
            "Chip manifest is missing required column(s): "
            f"{', '.join(missing_columns)}. Use all five template columns: "
            f"{', '.join(CHIP_MANIFEST_COLUMNS)}."
        )

    def missing(value: Any) -> bool:
        return pd.isna(value) or str(value).strip().casefold() in {"", "nan", "none"}

    def metadata_text(value: Any) -> str:
        if isinstance(value, float) and math.isfinite(value) and value.is_integer():
            return str(int(value))
        return str(value).strip()

    def coordinate(value: Any, name: str) -> int:
        try:
            number = float(str(value).strip().replace(",", "."))
        except (TypeError, ValueError) as error:
            raise ValueError(f"{name} must be an integer") from error
        if not math.isfinite(number) or not number.is_integer():
            raise ValueError(f"{name} must be an integer")
        return int(number)

    chips: set[tuple[str, int, int]] = set()
    metadata: dict[tuple[str, int, int], dict[str, str]] = {}
    dut_rows: dict[str, int] = {}
    chip_rows: dict[tuple[str, int, int], int] = {}
    problems: list[str] = []
    for frame_index, row in frame.iterrows():
        excel_row = int(frame_index) + 2 if isinstance(frame_index, int) else str(frame_index)
        values = {column: row[resolved[column]] for column in CHIP_MANIFEST_COLUMNS}
        if all(missing(value) for value in values.values()):
            continue
        missing_values = [column for column, value in values.items() if missing(value)]
        if missing_values:
            problems.append(f"row {excel_row}: missing {', '.join(missing_values)}")
            continue
        wafer = _normalize_wafer(values["Wafer"])
        try:
            x_value = coordinate(values["X"], "X")
            y_value = coordinate(values["Y"], "Y")
        except ValueError as error:
            problems.append(f"row {excel_row}: {error}")
            continue
        dut = metadata_text(values["DUT Nr"])
        split = metadata_text(values["DoE split"])
        key = (wafer, x_value, y_value)
        normalized_dut = dut.casefold()
        if normalized_dut in dut_rows:
            problems.append(
                f"row {excel_row}: duplicate DUT Nr '{dut}' (first used on row {dut_rows[normalized_dut]})"
            )
            continue
        if key in chip_rows:
            problems.append(
                f"row {excel_row}: duplicate Wafer/X/Y {key} (first used on row {chip_rows[key]})"
            )
            continue
        dut_rows[normalized_dut] = int(excel_row)
        chip_rows[key] = int(excel_row)
        chips.add(key)
        metadata[key] = {"DUT Nr": dut, "DoE split": split}
    if problems:
        preview = "; ".join(problems[:8])
        remainder = f"; and {len(problems) - 8} more" if len(problems) > 8 else ""
        raise ValueError(f"Chip manifest contains invalid required data: {preview}{remainder}")
    if not chips:
        raise ValueError(
            "Chip manifest contains no chip rows. Populate DUT Nr, Wafer, X, Y, and DoE split before running Section 2."
        )
    return chips, metadata


class LegacyWideTeCsvAdapter:
    """Extract selected tests without loading multi-gigabyte exports into memory."""

    def extract_productive_files(
        self,
        files: Iterable[Path],
        profile: ExtractionProfile,
        insertion: InsertionProfile,
    ) -> pd.DataFrame:
        """Extract every device in productive CSVs for one explicitly selected insertion."""
        resolved = [Path(path).expanduser().resolve() for path in files]
        if not resolved:
            raise ValueError(f"Insertion '{insertion.name}' needs at least one productive CSV file")
        missing = [str(path) for path in resolved if not path.is_file()]
        if missing:
            raise FileNotFoundError(
                f"Productive files for insertion '{insertion.name}' do not exist: {missing}"
            )
        rows: list[dict[str, Any]] = []
        for path in resolved:
            extracted = list(self._extract_file(path, None, {}, profile, insertion))
            for row in extracted:
                row["Productive Source File"] = str(path)
            rows.extend(extracted)
        if not rows:
            raise ValueError(
                f"No productive rows matched the configured tests for insertion '{insertion.name}'"
            )
        return pd.DataFrame(rows).reindex(
            columns=[*profile.output_columns, "Productive Source File"]
        )

    def extract(self, input_folder: Path, chip_manifest: Path, profile: ExtractionProfile) -> pd.DataFrame:
        chips, chip_metadata = read_chip_manifest(chip_manifest)
        rows: list[dict[str, Any]] = []
        insertion_by_path: dict[Path, InsertionProfile] = {}
        if profile.insertions:
            files: list[Path] = []
            missing_files: list[str] = []
            for insertion in profile.insertions:
                for raw_file in insertion.raw_files:
                    path = Path(raw_file).expanduser()
                    if not path.is_absolute():
                        path = input_folder / path
                    path = path.resolve()
                    if not path.is_file():
                        missing_files.append(str(path))
                        continue
                    files.append(path)
                    insertion_by_path[path] = insertion
            if missing_files:
                raise FileNotFoundError(f"Configured insertion files do not exist: {missing_files}")
            files = sorted(dict.fromkeys(files))
        else:
            files = sorted(input_folder.glob("*.csv"))
        if not files:
            raise FileNotFoundError(f"No CSV files found in {input_folder}")
        for path in files:
            insertion = insertion_by_path.get(path.resolve())
            rows.extend(self._extract_file(path, chips, chip_metadata, profile, insertion))
        if not rows:
            raise ValueError("No data matched the selected chips and tests")
        frame = pd.DataFrame(rows)
        return frame.reindex(columns=list(profile.output_columns))

    def _extract_file(
        self,
        path: Path,
        chips: set[tuple[str, int, int]] | None,
        metadata: dict[tuple[str, int, int], dict[str, str]],
        profile: ExtractionProfile,
        insertion: InsertionProfile | None = None,
    ) -> Iterable[dict[str, Any]]:
        with path.open("r", encoding="latin1", errors="ignore", newline="") as handle:
            reader = csv.reader(handle, delimiter=";")
            physical_rows: list[list[str]] = []
            for _ in range(13):
                try:
                    physical_rows.append(next(reader))
                except StopIteration:
                    return []
            if len(physical_rows) < 5:
                return []

            header, names, lows, highs, units = physical_rows[:5]
            index_by_name = {str(value).strip().upper(): index for index, value in enumerate(header)}

            file_values = {
                rule.target: _derive(rule, filename=path.name, test_name="")
                for rule in profile.derived_fields
                if rule.source == "filename"
            }
            for rule in profile.regex_fields:
                if rule.source == "filename":
                    file_values[rule.target] = _extract_regex(rule, filename=path.name, test_name="")
            for field, value_map in profile.derived_value_maps.items():
                if field in file_values:
                    file_values[field] = value_map.get(file_values[field], file_values[field])
            if insertion is not None:
                file_values[profile.insertion_field] = insertion.group
                file_values["Insertion"] = insertion.name
                file_values["Temperature"] = insertion.temperature

            fallback_indexes: dict[str, int] = {}
            insertion_value = str(file_values.get(profile.insertion_field, "")).strip().casefold()
            fallback_values = {
                str(value).strip().casefold() for value in profile.fallback_insertion_values
            }
            if insertion_value in fallback_values:
                for coordinate, test_number in profile.coordinate_fallback.items():
                    normalized_test_number = str(test_number).strip().upper()
                    index = index_by_name.get(normalized_test_number)
                    if index is None and normalized_test_number.isdigit():
                        index = index_by_name.get(f"TN{normalized_test_number}")
                    if index is not None:
                        fallback_indexes[coordinate.upper()] = index

            coordinate_indexes = tuple(
                index_by_name.get(name.upper()) for name in profile.coordinate_columns
            )
            required_coordinates = tuple(name.upper() for name in profile.coordinate_columns)
            if any(
                primary_index is None and coordinate not in fallback_indexes
                for coordinate, primary_index in zip(required_coordinates, coordinate_indexes)
            ):
                return []
            wafer_index, x_index, y_index = coordinate_indexes

            selected: list[tuple[int, int, str]] = []
            for index, cell in enumerate(header):
                text = str(cell).strip()
                if not text.isdigit():
                    continue
                number = int(text)
                name = str(names[index]).strip() if index < len(names) else ""
                if profile.selector.matches(number, name):
                    selected.append((index, number, name or text))
            if not selected:
                return []

            output: list[dict[str, Any]] = []
            for data_row in reader:
                def get(index: int | None) -> str:
                    return data_row[index].strip() if index is not None and index < len(data_row) else ""

                wafer = _normalize_wafer(get(wafer_index))
                x_raw, y_raw = get(x_index), get(y_index)
                if _is_missing(wafer) and "WAFER" in fallback_indexes:
                    wafer = _normalize_wafer(get(fallback_indexes["WAFER"]))
                if _is_missing(x_raw) and "X" in fallback_indexes:
                    x_raw = get(fallback_indexes["X"])
                if _is_missing(y_raw) and "Y" in fallback_indexes:
                    y_raw = get(fallback_indexes["Y"])
                try:
                    x_value, y_value = int(float(x_raw)), int(float(y_raw))
                except (TypeError, ValueError):
                    continue
                chip_key = (wafer, x_value, y_value)
                if chips is not None and chip_key not in chips:
                    continue

                chip_values = metadata.get(chip_key, {})
                for test_index, test_number, test_name in selected:
                    record: dict[str, Any] = {
                        "DUT Nr": _parse_number(chip_values.get("DUT Nr", "")),
                        "Wafer": _parse_number(wafer),
                        "X": x_value,
                        "Y": y_value,
                        "DoE split": chip_values.get("DoE split", ""),
                        "Test Number": test_number,
                        "Test Name": test_name,
                        "Test Value": _parse_number(get(test_index)),
                        "Low": _parse_number(lows[test_index] if test_index < len(lows) else ""),
                        "High": _parse_number(highs[test_index] if test_index < len(highs) else ""),
                        "Unit": str(units[test_index]).strip() if test_index < len(units) else "",
                        **file_values,
                    }
                    for field, value_map in profile.metadata_value_maps.items():
                        if field in record:
                            record[field] = value_map.get(record[field], record[field])
                    for rule in profile.derived_fields:
                        if rule.source == "test_name":
                            record[rule.target] = _derive(rule, filename=path.name, test_name=test_name)
                    for rule in profile.regex_fields:
                        if rule.source == "test_name":
                            record[rule.target] = _extract_regex(rule, filename=path.name, test_name=test_name)
                    if insertion is not None:
                        record[profile.insertion_field] = insertion.group
                        record["Insertion"] = insertion.name
                        record["Temperature"] = insertion.temperature
                    output.append(record)
            return output
