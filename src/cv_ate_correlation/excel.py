"""Consistent Excel workbook formatting for every CorreLaTE output."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

ACCENT_1_BLUE = "4472C4"
HEADER_FONT_COLOR = "FFFFFF"
DEFAULT_MIN_WIDTH = 10
DEFAULT_MAX_WIDTH = 60


def format_worksheet(
    worksheet: Worksheet,
    *,
    min_width: int = DEFAULT_MIN_WIDTH,
    max_width: int = DEFAULT_MAX_WIDTH,
) -> None:
    """Apply Accent 1 headers, filters, frozen headers, and bounded autofit."""
    if worksheet.max_row < 1 or worksheet.max_column < 1:
        return

    header_fill = PatternFill(fill_type="solid", fgColor=ACCENT_1_BLUE)
    for cell in worksheet[1]:
        cell.fill = header_fill
        cell.font = Font(color=HEADER_FONT_COLOR, bold=True)
        cell.alignment = Alignment(vertical="center")

    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions
    worksheet.sheet_view.showGridLines = False

    for column_index in range(1, worksheet.max_column + 1):
        longest = 0
        for cells in worksheet.iter_rows(
            min_col=column_index,
            max_col=column_index,
            min_row=1,
            max_row=worksheet.max_row,
        ):
            value = cells[0].value
            if value is None:
                continue
            lines = str(value).splitlines() or [""]
            longest = max(longest, *(len(line) for line in lines))
        width = min(max(longest + 2, min_width), max_width)
        worksheet.column_dimensions[get_column_letter(column_index)].width = width


def format_workbook(path: str | Path) -> None:
    """Format every non-empty worksheet in an existing Excel workbook."""
    destination = Path(path)
    workbook = load_workbook(destination)
    for worksheet in workbook.worksheets:
        format_worksheet(worksheet)
    workbook.save(destination)


def write_dataframe_workbook(
    path: str | Path,
    sheets: Mapping[str, pd.DataFrame],
) -> Path:
    """Write DataFrames and apply the standard CorreLaTE Excel presentation."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(destination, engine="openpyxl") as writer:
        for sheet_name, frame in sheets.items():
            frame.to_excel(writer, index=False, sheet_name=sheet_name)
    format_workbook(destination)
    return destination
