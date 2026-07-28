from __future__ import annotations

from pathlib import Path

import pandas as pd

from cv_ate_correlation.gui import workbook_sheet_names


def test_workbook_sheet_names_preserves_workbook_order(tmp_path: Path) -> None:
    workbook = tmp_path / "workflow.xlsx"
    with pd.ExcelWriter(workbook, engine="openpyxl") as writer:
        pd.DataFrame({"value": [1]}).to_excel(writer, sheet_name="Extracted_Data", index=False)
        pd.DataFrame({"value": [2]}).to_excel(writer, sheet_name="Correlation_Input", index=False)

    assert workbook_sheet_names(workbook) == ("Extracted_Data", "Correlation_Input")
