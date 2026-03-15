"""\
TX test data extractor (flat script, no classes).

Reads all .xlsx and .csv files in an input folder, removes rows 6–13 (1-based),
filters by chip IDs (WAFER/X/Y), filters by tests of interest (supports numeric
ranges), and writes a single Excel output file with one sheet.

Output columns (single sheet):
- Wafer
- X
- Y
- TestName
- TestNumber
- TestValue
- LUT value      (1-3 digit number after 'FwLu' in the test name)
- Temperature   (Hot/Cold/Ambient/Unknown derived from filename)
- SupplyVoltage (VNOM/VMIN/VMAX/Unknown derived from test name)

Examples:
  python TX_Tests_Data_Extractor_Flat.py \
    --input-folder "C:/path/to/raw" \
    --output-xlsx "C:/path/to/out.xlsx" \
    --chips "2,31,5;2,27,8" \
    --tests "52065,53100-53110,IDDQ"

  python TX_Tests_Data_Extractor_Flat.py \
    --input-folder "C:/path/to/raw" \
    --chips-file "C:/path/to/chips.csv" \
    --tests "53000-53999"

Chips file format (CSV/XLSX): columns containing wafer/x/y (header recommended)
"""

import argparse
import re
import sys
from pathlib import Path

import pandas as pd


# =========================
# USER CONFIG (no CLI)
# =========================
# If you run this script without any command-line arguments, the values below
# will be used.

RUN_WITH_IN_CODE_CONFIG = True

INPUT_FOLDER = r"C:/UserData/Infineon/TE_CTRX/CTRX8188/Data_Reviews/CV_TE_Correlation/Raw_Data_TE"  # e.g. r"C:\UserData\Infineon\...\Raw_Data_TE"
#OUTPUT_XLSX = r"C:/UserData/Infineon/TE_CTRX/CTRX8188/Data_Reviews/CV_TE_Correlation/ATE_Extracted_DPLL_PN_Data.xlsx"  # e.g. r"C:\UserData\Infineon\...\Extracted_TX_Data.xlsx" OR a folder path
#OUTPUT_XLSX = r"C:/UserData/Infineon/TE_CTRX/CTRX8188/Data_Reviews/CV_TE_Correlation/ATE_Extracted_LO_Power_Data.xlsx"
#OUTPUT_XLSX = r"C:/UserData/Infineon/TE_CTRX/CTRX8188/Data_Reviews/CV_TE_Correlation/ATE_Extracted_PA_Power_Data.xlsx" 
OUTPUT_XLSX = r"C:/UserData/Infineon/TE_CTRX/CTRX8188/Data_Reviews/CV_TE_Correlation/ATE_Extracted_Kf_Data_DoE.xlsx"  

# Provide chips either as a string list or via a CSV/XLSX file:
CHIPS = r""  # e.g. r"02,25,70;02,35,8"
#CHIPS_FILE = r"C:/UserData/Infineon/TE_CTRX/CTRX8188/Data_Reviews/CV_TE_Correlation/Raw_Data_TE/CTRX8188_CV_TE_Correlation_Chip_IDs_DPLL_PN.xlsx"  # e.g. r"C:\path\to\chips.csv"
CHIPS_FILE = r"C:/UserData/Infineon/TE_CTRX/CTRX8188/Data_Reviews/CV_TE_Correlation/CTRX8188_CV_TE_Correlation_Chip_IDs_LO_Power.xlsx"  
#CHIPS_FILE = r"C:/UserData/Infineon/TE_CTRX/CTRX8188/Data_Reviews/CV_TE_Correlation/CTRX8188_CV_TE_Correlation_Chip_IDs_PA_Power.xlsx"  
#CHIPS_FILE = r"C:/UserData/Infineon/TE_CTRX/CTRX8188/Data_Reviews/CV_TE_Correlation/CTRX8188_CV_TE_Correlation_Chip_IDs_PA_Power_DoE.xlsx"  

# Tests: comma/semicolon separated tokens; ranges allowed
#TESTS = r"52004-52009,52047,52064-52065,52095,52104-52105"  # e.g. r"52065,52085,53100-53105"
#TESTS = r"57006-57009,57039-57051,57099-57111,57159-57171,57219-57231,57279-57291,57339-57351"  
#TESTS = r"53171-53290,53719-53838,54139-54258,54489-54608,55139-55258,55489-55608" 
#TESTS = r"52046,52084,52094,53171-53290,53719-53838,54139-54258,54489-54608,55139-55258,55489-55608"
TESTS = r"52046,52084,52094" 

# Excel only:
SHEET = r""  # e.g. r"Sheet1" (leave empty for first sheet)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Extract TX test data for specific devices (WAFER/X/Y) from all .xlsx/.csv files in a folder."
    )
    parser.add_argument(
        "--input-folder",
        required=True,
        help="Folder containing raw data files (.xlsx or .csv).",
    )
    parser.add_argument(
        "--output-xlsx",
        required=True,
        help="Path to the consolidated Excel output file (.xlsx).",
    )
    parser.add_argument(
        "--chips",
        default="",
        help=(
            "Chip list in the format 'WAFER,X,Y;WAFER,X,Y'. Example: '2,31,5;2,27,8'. "
            "You can also use ':' as separator inside a chip id."
        ),
    )
    parser.add_argument(
        "--chips-file",
        default="",
        help="Optional chips file (CSV or XLSX) containing chip IDs (columns include wafer/x/y).",
    )
    parser.add_argument(
        "--tests",
        required=True,
        help=(
            "Tests of interest. Comma/semicolon-separated tokens. "
            "Tokens can be test numbers (e.g. 52065), ranges (e.g. 53100-53110), "
            "or substrings to match in test name (e.g. IDDQ)."
        ),
    )
    parser.add_argument(
        "--sheet",
        default="",
        help="Optional sheet name (Excel only). If omitted, reads the first sheet.",
    )

    if RUN_WITH_IN_CODE_CONFIG and len(sys.argv) == 1:
        args = argparse.Namespace(
            input_folder=INPUT_FOLDER,
            output_xlsx=OUTPUT_XLSX,
            chips=CHIPS,
            chips_file=CHIPS_FILE,
            tests=TESTS,
            sheet=SHEET,
        )
        if not str(args.input_folder).strip():
            raise SystemExit("INPUT_FOLDER is empty. Set it in the USER CONFIG block at the top of the script.")
        if not str(args.output_xlsx).strip():
            raise SystemExit("OUTPUT_XLSX is empty. Set it in the USER CONFIG block at the top of the script.")
        if not str(args.tests).strip():
            raise SystemExit("TESTS is empty. Set it in the USER CONFIG block at the top of the script.")
    else:
        args = parser.parse_args()

    input_folder = Path(args.input_folder)
    output_xlsx = Path(args.output_xlsx)

    # Allow providing an output folder instead of a full file path
    if output_xlsx.suffix.lower() != ".xlsx":
        output_xlsx = output_xlsx / "Extracted_TX_Data.xlsx"

    if not input_folder.is_dir():
        raise SystemExit(f"Input folder not found: {input_folder}")

    # -------------------------
    # Parse chips
    # -------------------------
    def _normalize_wafer(value: str) -> str:
        s = str(value).strip().strip('"').strip("'")
        if s.isdigit():
            # Remove leading zeros consistently (e.g. "002" -> "2", "02" -> "2")
            return str(int(s))
        return s

    target_chips = set()
    chip_metadata = {}  # (wafer,x,y) -> {"DoE split": str, "DUT Nr": str}

    if args.chips_file:
        before_count = len(target_chips)
        chips_file_in = Path(str(args.chips_file))

        def _resolve_existing_chips_file(p: Path) -> Path | None:
            candidates: list[Path] = []
            # 1) As provided
            candidates.append(p)
            # 2) Relative to current working directory
            if not p.is_absolute():
                candidates.append(Path.cwd() / p)
            # 3) Same folder as input data (common workflow)
            candidates.append(input_folder / p.name)
            # 4) Same folder as this script
            try:
                candidates.append(Path(__file__).resolve().parent / p.name)
            except Exception:
                pass

            for c in candidates:
                try:
                    if c.is_file():
                        return c
                except Exception:
                    continue

            # 5) Recursive search by filename inside input folder
            try:
                for found in input_folder.rglob(p.name):
                    if found.is_file():
                        return found
            except Exception:
                pass

            return None

        chips_file = _resolve_existing_chips_file(chips_file_in)
        if chips_file is None:
            tried = [
                str(chips_file_in),
                str((Path.cwd() / chips_file_in) if not chips_file_in.is_absolute() else chips_file_in),
                str(input_folder / chips_file_in.name),
            ]
            try:
                tried.append(str(Path(__file__).resolve().parent / chips_file_in.name))
            except Exception:
                pass
            tried_msg = "\n  - ".join(tried)
            raise SystemExit(
                "Chips file not found. Update CHIPS_FILE / --chips-file to the correct location.\n"
                f"Provided: {chips_file_in}\n"
                f"Tried:\n  - {tried_msg}"
            )

        # Robust chips parsing: chips files can be CSV (often semicolon-delimited) or Excel.
        # Example seen in the field: Wafer;X;Y;DoE split
        def _read_chips_df(path: Path) -> pd.DataFrame:
            if path.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
                try:
                    return pd.read_excel(path, sheet_name=0)
                except Exception as e:
                    raise SystemExit(f"Failed to read chips Excel file: {path} ({e})")

            # Quick delimiter guess from the first line
            try:
                first_line = path.read_text(encoding="utf-8-sig", errors="ignore").splitlines()[0]
            except Exception:
                first_line = ""
            if first_line.count(";") > first_line.count(","):
                sep = ";"
            else:
                sep = ","

            # If neither ';' nor ',' appear, chips file is often whitespace-aligned.
            if first_line.count(";") == 0 and first_line.count(",") == 0:
                text = path.read_text(encoding="utf-8-sig", errors="ignore")
                lines = [ln for ln in text.splitlines() if ln.strip()]
                if not lines:
                    return pd.DataFrame()

                header_tokens = re.split(r"\s+", lines[0].strip())
                headers = []
                i = 0
                while i < len(header_tokens):
                    t = header_tokens[i]
                    t_up = t.upper()
                    nxt = header_tokens[i + 1] if i + 1 < len(header_tokens) else ""
                    nxt_up = nxt.upper()
                    if t_up == "DUT" and nxt_up == "NR":
                        headers.append("DUT Nr")
                        i += 2
                        continue
                    if t_up == "DOE" and nxt_up == "SPLIT":
                        headers.append("DoE split")
                        i += 2
                        continue
                    headers.append(t)
                    i += 1

                records = []
                for ln in lines[1:]:
                    parts = re.split(r"\s+", ln.strip())
                    if len(parts) < len(headers):
                        parts = parts + [""] * (len(headers) - len(parts))
                    records.append(parts[: len(headers)])
                return pd.DataFrame(records, columns=headers)

            # Try reading with the guessed delimiter; fall back to sniffing
            try:
                return pd.read_csv(path, sep=sep, engine="python")
            except Exception:
                return pd.read_csv(path, sep=None, engine="python")

        chips_df = _read_chips_df(chips_file)
        chips_df.columns = [str(c).strip().lower() for c in chips_df.columns]

        # Map flexible headers
        def _find_col(candidates):
            for cand in candidates:
                if cand in chips_df.columns:
                    return cand
            # fallback: partial matches
            for col in chips_df.columns:
                for cand in candidates:
                    if cand in col:
                        return col
            return None

        wafer_col = _find_col(["wafer", "waf"])
        x_col = _find_col(["x"])
        y_col = _find_col(["y"])
        doe_col = _find_col(["doe split", "doe_split", "doe", "split"])
        dut_col = _find_col(["dut nr", "dut_nr", "dut", "dut number", "dut no", "dutnum", "dut#", "dut "])

        # Headerless or unrecognized: assume first 3 columns are wafer,x,y
        if wafer_col is None or x_col is None or y_col is None:
            if chips_df.shape[1] >= 3:
                wafer_col, x_col, y_col = chips_df.columns[:3]
            else:
                # Single-column files: allow rows like "02;31;5" or "02,31,5"
                if chips_df.shape[1] == 1:
                    only_col = chips_df.columns[0]
                    records = []
                    for v in chips_df[only_col].astype(str).tolist():
                        parts = [p.strip() for p in re.split(r"[;,\s]+", v) if p.strip()]
                        if len(parts) >= 3:
                            records.append(parts[:3])
                    chips_df = pd.DataFrame(records, columns=["wafer", "x", "y"])
                    wafer_col, x_col, y_col = "wafer", "x", "y"
                else:
                    raise SystemExit(
                        f"Could not parse chips file columns in: {chips_file}. Found columns: {list(chips_df.columns)}"
                    )

        for _, r in chips_df.iterrows():
            wafer = _normalize_wafer(r[wafer_col])
            if wafer == "" or wafer.lower() == "nan":
                continue
            try:
                x_val = int(float(r[x_col]))
                y_val = int(float(r[y_col]))
            except Exception:
                continue
            key = (wafer, x_val, y_val)
            target_chips.add(key)
            meta = chip_metadata.get(key, {})
            if doe_col is not None:
                doe_val = str(r[doe_col]).strip()
                if doe_val.lower() == "nan":
                    doe_val = ""
                if (not meta.get("DoE split")) and doe_val:
                    meta["DoE split"] = doe_val

            if dut_col is not None:
                dut_val = str(r[dut_col]).strip()
                if dut_val.lower() == "nan":
                    dut_val = ""
                if (not meta.get("DUT Nr")) and dut_val:
                    meta["DUT Nr"] = dut_val

            if meta:
                chip_metadata[key] = meta

        loaded_from_file = len(target_chips) - before_count
        print(f"Loaded {loaded_from_file} chips from chips file: {chips_file}")

    if args.chips.strip():
        for chunk in re.split(r"[;\n]+", args.chips.strip()):
            chunk = chunk.strip()
            if not chunk:
                continue
            parts = [p.strip() for p in re.split(r"[:,]", chunk) if p.strip()]
            if len(parts) != 3:
                raise SystemExit(
                    f"Malformed chip token '{chunk}'. Expected 'WAFER,X,Y' (or 'WAFER:X:Y')."
                )
            wafer = parts[0]
            wafer = _normalize_wafer(wafer)
            try:
                x_val = int(float(parts[1]))
                y_val = int(float(parts[2]))
            except Exception:
                raise SystemExit(
                    f"Malformed chip token '{chunk}'. X/Y must be numbers."
                )
            key = (wafer, x_val, y_val)
            target_chips.add(key)

    if not target_chips:
        raise SystemExit("No valid chips provided. Use --chips and/or --chips-file.")

    # -------------------------
    # Parse tests
    # -------------------------
    test_ranges = []  # list[tuple[int,int]]
    test_numbers = set()  # set[int]
    test_substrings = []  # list[str]

    for token in re.split(r"[;,\n]+", args.tests.strip()):
        token = token.strip()
        if not token:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
        if m:
            a = int(m.group(1))
            b = int(m.group(2))
            if a > b:
                a, b = b, a
            test_ranges.append((a, b))
            continue
        if re.fullmatch(r"\d+", token):
            test_numbers.add(int(token))
            continue
        test_substrings.append(token.lower())

    if not (test_ranges or test_numbers or test_substrings):
        raise SystemExit("No valid tests provided in --tests.")

    # -------------------------
    # Gather files
    # -------------------------
    files = []
    for pattern in ("*.xlsx", "*.csv"):
        files.extend(sorted(input_folder.glob(pattern)))

    # Skip Office temp files and avoid reading the output file as an input
    try:
        output_xlsx_resolved = output_xlsx.resolve()
    except Exception:
        output_xlsx_resolved = None

    filtered_files = []
    for p in files:
        if p.name.startswith("~$"):
            continue
        if output_xlsx_resolved is not None:
            try:
                if p.resolve() == output_xlsx_resolved:
                    continue
            except Exception:
                pass
        filtered_files.append(p)
    files = filtered_files

    if not files:
        raise SystemExit(f"No .xlsx or .csv files found in: {input_folder}")

    extracted_rows = []

    _FWLU_RE = re.compile(r"FwLu(?P<lut>\d{1,3})(?!\d)", flags=re.IGNORECASE)

    def _extract_lut_value(test_name: str):
        """Return LUT value (int) from 'FwLuNN'/'FwLuNNN' substring, else ''."""
        m = _FWLU_RE.search(str(test_name) if test_name is not None else "")
        if not m:
            return ""
        try:
            return int(m.group("lut"))
        except Exception:
            return ""

    def _normalize_header_cell(value):
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return ""
        if isinstance(value, (int,)):
            return str(value)
        if isinstance(value, float):
            if value.is_integer():
                return str(int(value))
            return str(value)
        return str(value).strip()

    def _parse_first_two_lines_csv(path: Path):
        with open(path, "r", encoding="latin1", errors="ignore") as f:
            line1 = f.readline().rstrip("\n\r")
            line2 = f.readline().rstrip("\n\r")
        header = line1.split(";")
        test_names = line2.split(";")
        if len(test_names) < len(header):
            test_names = test_names + [""] * (len(header) - len(test_names))
        return header, test_names

    def _find_label_row_index(raw_df: pd.DataFrame, label_col: str, label: str):
        s = raw_df[label_col].astype(str).str.strip().str.upper()
        try:
            return int(s[s == label.upper()].index[0])
        except Exception:
            return None

    def _select_test_numbers(header_cells, test_name_cells):
        selected = []
        for idx, h in enumerate(header_cells):
            hs = str(h).strip()
            if not hs.isdigit():
                continue
            tn = int(hs)

            matches_number = tn in test_numbers
            if (not matches_number) and test_ranges:
                for a, b in test_ranges:
                    if a <= tn <= b:
                        matches_number = True
                        break

            matches_substring = False
            if test_substrings:
                tn_name = str(test_name_cells[idx]).strip().lower() if idx < len(test_name_cells) else ""
                for sub in test_substrings:
                    if sub in tn_name:
                        matches_substring = True
                        break

            if matches_number or matches_substring:
                selected.append(tn)
        return selected

    for file_path in files:
        name_upper = file_path.name.upper()

        insertion_type = "FE" if any(tag in name_upper for tag in ("S11P", "S21P", "S31P")) else "BE"

        if ("S1" in name_upper) or ("HT" in name_upper):
            temperature = "135"
        elif "S2" in name_upper:
            temperature = "-40"
        elif ("S3" in name_upper) or ("RT" in name_upper):
            temperature = "25"
        else:
            temperature = "Unknown"

        # Raw file format (as in the provided sample):
        # - Row 1: column headers (meta columns + many numeric test numbers)
        # - Row 2: test names aligned to those columns
        # - Data starts on row 3
        # - Rows 6–13 (1-based) must be removed before processing
        if file_path.suffix.lower() == ".csv":
            header_cells, test_name_cells = _parse_first_two_lines_csv(file_path)

            # Locate key meta columns by name in header row
            def _idx_of(col_name: str):
                try:
                    return next(i for i, c in enumerate(header_cells) if str(c).strip().upper() == col_name)
                except StopIteration:
                    return None

            wafer_idx = _idx_of("WAFER")
            x_idx = _idx_of("X")
            y_idx = _idx_of("Y")
            testnr_idx = _idx_of("TEST NR")
            if testnr_idx is None:
                # Sometimes appears as 'Test Nr'
                testnr_idx = next(
                    (i for i, c in enumerate(header_cells) if str(c).strip().upper() == "TEST NR" or str(c).strip().upper() == "TEST NR." or str(c).strip().upper() == "TEST NR"),
                    None,
                )

            if wafer_idx is None or x_idx is None or y_idx is None:
                continue

            selected_test_numbers = _select_test_numbers(header_cells, test_name_cells)
            if not selected_test_numbers:
                continue

            # usecols indices: WAFER/X/Y + all selected test number columns
            selected_test_indices = [i for i, c in enumerate(header_cells) if str(c).strip().isdigit() and int(str(c).strip()) in set(selected_test_numbers)]
            base_usecols = [wafer_idx, x_idx, y_idx]
            if testnr_idx is not None:
                base_usecols.append(testnr_idx)
            usecols = sorted(set(base_usecols + selected_test_indices))

            # BE special case: coordinates are stored in test-number columns 62007/62008/62009
            # (while WAFER/X/Y columns are empty)
            if insertion_type == "BE":
                for meta_tn in (62007, 62008, 62009):
                    meta_s = str(meta_tn)
                    try:
                        meta_idx = next(i for i, c in enumerate(header_cells) if str(c).strip() == meta_s)
                    except StopIteration:
                        meta_idx = None
                    if meta_idx is not None:
                        usecols.append(meta_idx)
                usecols = sorted(set(usecols))

            raw = pd.read_csv(
                file_path,
                header=None,
                sep=";",
                engine="python",
                encoding="latin1",
                usecols=usecols,
                skiprows=list(range(5, 13)),
            )

            # Rebuild header/test-name arrays aligned to usecols
            header_cells_uc = [header_cells[i] if i < len(header_cells) else "" for i in usecols]
            test_name_cells_uc = [test_name_cells[i] if i < len(test_name_cells) else "" for i in usecols]
            header_cells_norm = [_normalize_header_cell(v) for v in header_cells_uc]
            test_name_cells_norm = [str(v).strip() for v in test_name_cells_uc]

            if len(raw) < 3:
                continue

            raw.columns = header_cells_norm
            # Build test-name and limit maps from the metadata rows (rows 2-5 in the file)
            test_name_map = {header_cells_norm[i]: test_name_cells_norm[i] for i in range(len(header_cells_norm))}
            limits_map = {}

            # Prefer row-based labels if Test Nr column is present
            testnr_col = next((c for c in raw.columns if str(c).strip().upper() == "TEST NR"), None)
            if testnr_col is not None:
                idx_test_name = _find_label_row_index(raw, testnr_col, "TEST NAME")
                idx_low = _find_label_row_index(raw, testnr_col, "LOW")
                idx_high = _find_label_row_index(raw, testnr_col, "HIGH")
                idx_unit = _find_label_row_index(raw, testnr_col, "UNIT")

                if idx_test_name is not None:
                    for c in raw.columns:
                        if str(c).strip().isdigit():
                            v = raw.at[idx_test_name, c] if idx_test_name in raw.index else ""
                            vs = str(v).strip()
                            if vs.lower() == "nan":
                                vs = ""
                            if vs:
                                test_name_map[str(c).strip()] = vs

                def _val(idx, col):
                    if idx is None:
                        return ""
                    try:
                        v = raw.at[idx, col]
                    except Exception:
                        return ""
                    vs = str(v).strip()
                    return "" if vs.lower() == "nan" else vs

                for c in raw.columns:
                    cs = str(c).strip()
                    if not cs.isdigit():
                        continue
                    limits_map[cs] = {
                        "Low": _val(idx_low, c),
                        "High": _val(idx_high, c),
                        "Unit": _val(idx_unit, c),
                    }

                # Data starts after the UNIT row when present, else after first 5 rows
                if idx_unit is not None:
                    data_start = idx_unit + 1
                else:
                    data_start = 5
            else:
                limits_map = {}
                data_start = 5

            df = raw.iloc[data_start:].copy()

        else:
            read_kwargs = {"header": None}
            if args.sheet:
                read_kwargs["sheet_name"] = args.sheet
            try:
                raw = pd.read_excel(file_path, **read_kwargs)
            except PermissionError:
                continue
            raw = raw.drop(index=list(range(5, 13)), errors="ignore").reset_index(drop=True)

            if len(raw) < 3:
                continue

            header_cells_norm = [_normalize_header_cell(v) for v in raw.iloc[0].tolist()]
            raw.columns = header_cells_norm

            # Build test-name and limit maps from metadata rows (search by label in 'Test Nr')
            testnr_col = next((c for c in raw.columns if str(c).strip().upper() == "TEST NR"), None)
            test_name_map = {}
            limits_map = {}
            data_start = 5

            if testnr_col is not None:
                idx_test_name = _find_label_row_index(raw, testnr_col, "TEST NAME")
                idx_low = _find_label_row_index(raw, testnr_col, "LOW")
                idx_high = _find_label_row_index(raw, testnr_col, "HIGH")
                idx_unit = _find_label_row_index(raw, testnr_col, "UNIT")

                def _val(idx, col):
                    if idx is None:
                        return ""
                    try:
                        v = raw.at[idx, col]
                    except Exception:
                        return ""
                    vs = str(v).strip()
                    return "" if vs.lower() == "nan" else vs

                if idx_test_name is not None:
                    for c in raw.columns:
                        if str(c).strip().isdigit():
                            vs = _val(idx_test_name, c)
                            if vs:
                                test_name_map[str(c).strip()] = vs

                for c in raw.columns:
                    cs = str(c).strip()
                    if not cs.isdigit():
                        continue
                    limits_map[cs] = {
                        "Low": _val(idx_low, c),
                        "High": _val(idx_high, c),
                        "Unit": _val(idx_unit, c),
                    }

                if idx_unit is not None:
                    data_start = idx_unit + 1

            df = raw.iloc[data_start:].copy()

        # Drop fully empty rows
        df = df.dropna(how="all").reset_index(drop=True)
        if df.empty:
            continue

        # Identify Wafer/X/Y columns (must exist in header row)
        cols = [str(c) for c in df.columns]
        wafer_col = next((c for c in cols if c.strip().upper() == "WAFER"), None)
        x_col = next((c for c in cols if c.strip().upper() == "X"), None)
        y_col = next((c for c in cols if c.strip().upper() == "Y"), None)

        if wafer_col is None or x_col is None or y_col is None:
            continue

        # Normalize chip ID columns
        df[wafer_col] = df[wafer_col].astype(str).map(_normalize_wafer)
        wafer_clean = df[wafer_col].astype(str).str.strip()
        df.loc[wafer_clean.eq("") | wafer_clean.str.lower().eq("nan"), wafer_col] = pd.NA
        df[x_col] = pd.to_numeric(df[x_col], errors="coerce")
        df[y_col] = pd.to_numeric(df[y_col], errors="coerce")

        # BE special case: if WAFER/X/Y are missing, take them from 62007/62008/62009 when present
        if insertion_type == "BE":
            if all(str(t) in df.columns for t in ("62007", "62008", "62009")):
                wafer_fb = df["62007"].astype(str).map(_normalize_wafer)
                wafer_fb_clean = wafer_fb.astype(str).str.strip()
                wafer_fb = wafer_fb.mask(wafer_fb_clean.eq("") | wafer_fb_clean.str.lower().eq("nan"), pd.NA)
                df[wafer_col] = df[wafer_col].fillna(wafer_fb)

                x_fb = pd.to_numeric(df["62008"], errors="coerce")
                y_fb = pd.to_numeric(df["62009"], errors="coerce")
                df[x_col] = df[x_col].fillna(x_fb)
                df[y_col] = df[y_col].fillna(y_fb)

        df = df.dropna(subset=[wafer_col, x_col, y_col]).copy()
        if df.empty:
            continue

        df[x_col] = df[x_col].astype(float).astype(int)
        df[y_col] = df[y_col].astype(float).astype(int)

        # Filter to target chips
        chip_mask = df.apply(lambda r: (r[wafer_col], int(r[x_col]), int(r[y_col])) in target_chips, axis=1)
        df = df.loc[chip_mask].copy()
        if df.empty:
            continue

        # Determine selected test columns: headers that are numeric test numbers
        excluded = {wafer_col, x_col, y_col}
        if insertion_type == "BE":
            excluded.update({"62007", "62008", "62009"})
        test_cols = [c for c in df.columns if c not in excluded and str(c).strip().isdigit()]
        if not test_cols:
            continue

        selected_set = set()
        selected_set.update(test_numbers)
        for a, b in test_ranges:
            selected_set.update(range(a, b + 1))

        selected_test_cols = []
        for c in test_cols:
            tn = int(str(c).strip())
            if tn in selected_set:
                selected_test_cols.append(c)
                continue

            if test_substrings:
                tn_name = str(test_name_map.get(str(c).strip(), "")).lower()
                if any(sub in tn_name for sub in test_substrings):
                    selected_test_cols.append(c)

        if not selected_test_cols:
            continue

        # Emit rows (chip x test)
        for _, r in df.iterrows():
            wafer = _normalize_wafer(r[wafer_col])
            x_val = int(r[x_col])
            y_val = int(r[y_col])
            doe_split = chip_metadata.get((wafer, x_val, y_val), {}).get("DoE split", "")
            dut_nr = chip_metadata.get((wafer, x_val, y_val), {}).get("DUT Nr", "")

            for test_col in selected_test_cols:
                test_number_int = int(str(test_col).strip())
                test_name = str(test_name_map.get(str(test_col).strip(), "")).strip() or str(test_col)
                test_value = r[test_col]

                lut_value = _extract_lut_value(test_name)

                lim = limits_map.get(str(test_col).strip(), {})
                test_low = lim.get("Low", "")
                test_high = lim.get("High", "")
                test_unit = lim.get("Unit", "")

                # Supply voltage (per spec: substring match in test name)
                if "095" in test_name:
                    supply_voltage = "VMIN"
                elif "105" in test_name:
                    supply_voltage = "VMAX"
                elif "100" in test_name:
                    supply_voltage = "VNOM"
                else:
                    supply_voltage = "Unknown"

                test_name_lc = test_name.lower()

                # Frequency (GHz) (substring match in test name)
                if re.search(r"(?<!\d)81(?!\d)", test_name_lc):
                    frequency_ghz = 81
                elif "80p5" in test_name_lc:
                    frequency_ghz = 80.5
                elif re.search(r"(?<!\d)77(?!\d)", test_name_lc):
                    frequency_ghz = 77
                elif "76p5" in test_name_lc:
                    frequency_ghz = 76.5
                elif re.search(r"(?<!\d)76(?!\d)", test_name_lc):
                    frequency_ghz = 76
                else:
                    frequency_ghz = "Unknown"

                extracted_rows.append(
                    {
                        "DUT Nr": dut_nr,
                        "Wafer": wafer,
                        "X": x_val,
                        "Y": y_val,
                        "DoE split": doe_split,
                        "Test Number": test_number_int,
                        "Test Name": test_name,
                        "Test Value": test_value,
                        "LUT value": lut_value,
                        "Low": test_low,
                        "High": test_high,
                        "Unit": test_unit,
                        "Temperature": temperature,
                        "Voltage corner": supply_voltage,
                        "Frequency_GHz": frequency_ghz,
                        "Insertion Type": insertion_type,
                    }
                )

    if not extracted_rows:
        raise SystemExit("No data extracted. Check chip IDs, test selectors, and file format assumptions.")

    out_df = pd.DataFrame(extracted_rows)

    output_xlsx.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_excel(output_xlsx, index=False, sheet_name="Extracted_Data")

    print(f"Wrote {len(out_df)} rows to: {output_xlsx}")
