# CV_ATE_Correlation

Repository dedicated to the CTRX8188 CV versus ATE correlation activity that was previously kept in `Tasks_Automation_Code/IFX_Scripts/8188_CV_ATE_Correlation`.

This migration was done with preserved git history for the transferred folder, then completed with repository-local documentation and visuals.

![CV ATE Correlation Overview](docs/images/activity-overview.svg)

## What This Activity Covers

The work in this repository supports correlation between characterization / CV data and ATE production-test data for several CTRX8188 use cases:

- DPLL phase-noise correlation
- TXLO power correlation
- TXPA power correlation
- Kf-assisted correlation for PA / LO behavior
- Raw TE data extraction for selected DUTs, wafers, coordinates, and test ranges

At a high level, the activity follows this flow:

1. Extract relevant ATE raw data for the DUT population and tests of interest.
2. Build Excel workbooks that align CV and ATE measurements on common keys such as DUT number, wafer, X/Y, temperature, voltage corner, frequency, and test number.
3. Run one of the correlation scripts depending on the parameter family under study.
4. Review the generated Excel summaries, row-level residual tables, updated limits, and per-group plots.

![Illustrative Correlation Plot](docs/images/illustrative-correlation-plot.svg)

## CorreLaTE Automated Tool

The repository now includes **CorreLaTE: ATE-to-Lab Correlation**, an installable, subsystem-neutral package under `src/cv_ate_correlation`.
It provides one shared calculation engine through both a command-line interface and a lightweight desktop GUI.
Campaign-specific details—including selected tests, dimensions, grouping, strategy, limits, special requirements,
covariate keys, and guard-band directions—are isolated in profiles rather than hard-coded in the engine.

### Installation

From this folder, install the package and test dependencies into the active Python environment:

```powershell
python -m pip install -e ".[test]"
```

List the installed extraction and correlation profiles:

```powershell
cv-ate-correlation profiles
```

This displays both the validated, read-only CTRX8188 profiles and any profiles created by the current user.

### Extract Raw TE Data

The legacy-wide CSV adapter streams the input records instead of loading the complete 1.3 GB campaign into memory:

```powershell
cv-ate-correlation extract `
    --profile ctrx8188-txlo `
    --input-folder Data/Raw_Data_TE `
    --chip-manifest Data/TE_Data_Extraction/CTRX8188_CV_TE_Correlation_Chip_IDs_LO_Power.xlsx `
    --output Data/TE_Data_Extraction/ATE_Extracted_LO_Power_Data_New.xlsx
```

### Generate and Re-import the CV Handoff

Create a protected measurement request and a separate TE-only manifest:

```powershell
cv-ate-correlation request `
    --profile ctrx8188-txlo `
    --input Data/TE_Data_Extraction/ATE_Extracted_LO_Power_Data.xlsx `
    --sheet Extracted_Data `
    --request-output Data/CV_Request_TXLO.xlsx `
    --manifest-output Data/TE_Manifest_TXLO.xlsx
```

The request contains immutable `Measurement_Request_ID` + `Repeat_Index` keys and an unlocked yellow CV value column.
ATE values and limits are omitted from that workbook. The internal manifest retains the ATE values and must remain on the TE side.

After the CV owner completes and returns the request, validate full one-to-one coverage and create the aligned correlation input:

```powershell
cv-ate-correlation import-results `
    --profile ctrx8188-txlo `
    --returned Data/CV_Request_TXLO_Returned.xlsx `
    --manifest Data/TE_Manifest_TXLO.xlsx `
    --output Data/TXLO_Correlation_Input.xlsx
```

Import rejects missing, unknown, or duplicate request keys and blank/non-numeric CV measurements. Descriptive fields from the
returned workbook are not trusted during alignment; only the validated key and CV result are merged into the TE manifest.

### Generate a Correlation Report

For profiles without an auxiliary covariate:

```powershell
cv-ate-correlation correlate `
    --profile ctrx8188-dpll `
    --input Data/TE_Data_Extraction/ATE_Extracted_DPLL_PN_Data.xlsx `
    --sheet FE_Filtered `
    --output Data/Outputs/DPLL_Correlation_New.xlsx `
    --plots Data/Outputs/DPLL_Plots_New
```

Kf-assisted profiles additionally require an explicit lookup workbook and sheet:

```powershell
cv-ate-correlation correlate `
    --profile ctrx8188-txpa `
    --input Data/TE_Data_Extraction/ATE_Extracted_PA_Power_Data_DoE.xlsx `
    --sheet FE_Filtered `
    --covariate-input Data/TE_Data_Extraction/ATE_Extracted_PA_Power_Data_DoE.xlsx `
    --covariate-sheet KF_FE `
    --output Data/Outputs/TXPA_Correlation_New.xlsx `
    --plots Data/Outputs/TXPA_Plots_New
```

Each report contains:

- `Correlation_Factors`
- `Guard_Bands`
- `Correlation_Summary`
- `Correlated_Data`

### Desktop GUI

Launch the GUI with:

```powershell
cv-ate-correlation gui
```

The GUI and CLI use the same engine and persistent profile registry. The desktop interface provides five guided tabs:

1. `Profiles` — create, validate, update, or delete a reusable custom profile.
2. `Extract TE` — select an extraction profile, chip manifest, and output workbook. Built-in profiles also select a
    raw-data folder; custom profiles use the files assigned to their insertions.
3. `Create CV Request` — generate the protected CV workbook and separate internal ATE manifest.
4. `Import CV Results` — validate returned request coverage and produce the one-to-one aligned input.
5. `Correlate` — generate the Excel report and optional PNG plots, including explicit covariate selection.

The profile editor is subsystem-neutral. A user can define:

- insertions before test identity, with a name such as `S1`, fixed `FE` or `BE` group, numeric temperature, and one
    or more corresponding raw-data files selected with `Browse…`
- exact test numbers, inclusive test-number ranges, and test-name fragments
- independently selectable grouping conditions for `DUT Nr`, `Test Number`, `Frequency`, `Supply Corner`, `Channel`,
    and `Digital Control`
- filename- or test-name-based identification configured inside each grouping condition
- additional user-defined grouping conditions created with the `Add…` button
- Lab/reference and ATE/candidate columns, mean-delta or median-offset strategy, and minimum points per group
- limit, unit, and detail-key columns
- distribution-sigma or shifted-upper-limit guard-band behavior
- optional covariate value, merge keys, and output name

Each grouping condition has an editable input-column name. The defaults use common ATE names such as `Frequency_GHz` and
`Voltage corner`; alternatives such as `PA Channel`, `LUT value`, or a project-specific column can be entered directly. Its
identification source and rule are configured in the same box. Every manually entered field in the GUI includes a short
description and example value; browse controls and fixed dropdown lists are self-describing and therefore omit those hints.
`Temperature` is not a selectable grouping condition: CorreLaTE derives it from each insertion and automatically includes it
in correlation grouping. Profile validation requires at least one existing raw file for every insertion and rejects duplicate
insertion names or a raw file assigned to more than one insertion.

For `BE` insertions, CorreLaTE automatically falls back to the FUSE metadata columns when the normal chip-coordinate values
are blank or their columns are absent: test number `62007` supplies `WAFER`, `62008` supplies `X`, and `62009` supplies `Y`.
These mappings and the `BE` applicability are the defaults for new and existing custom profiles.

Custom profiles are stored per Windows user in `%APPDATA%\CorreLaTE\profiles.json`. They are loaded by both the GUI and CLI,
so a saved profile immediately appears in all extraction, handoff, import, and correlation selectors. The built-in CTRX8188
profiles are read-only and retain their golden-regression behavior.

The compact `Tests` field accepts entries such as `101, 200-220, LeakageCurrent`.

Regular expressions are not required for normal profile creation. The identification-method dropdown provides:

- `Use existing column` when the source workbook already contains the grouping value
- `Text mappings (no regex)` for literal rules, entered one per line, such as:

```text
HOT => 125
RT => 25
095 => VMIN
```

- `Number after prefix (no regex)` where entering `FwLu` extracts `255` from `FwLu255`
- `Advanced regex` as an optional expert mode for cases the guided methods cannot express; for example:

```text
CH(\d+)
```

The GUI translates the two no-regex methods into validated internal extraction rules when the profile is saved.

Workbook sheet selectors are populated automatically after browsing. Long-running extraction, correlation, and plotting work runs
outside the Tk event loop, with a shared progress/status area keeping the interface responsive.

### Regression Validation

Run the fast strategy and report regressions:

```powershell
python -m pytest -m "not slow" -q
```

Run all regressions, including the complete raw-data campaign:

```powershell
python -m pytest -q
```

The current suite validates:

- DPLL mean-delta factors, shifted limits, and worst-case guard-bands
- TXLO/TXPA median-offset factors, residuals, corrected limits, and special requirement policies
- Kf-assisted coefficients, fit metrics, residuals, and limits
- DPLL, Kf, TXLO, and combined TXPA raw extraction against the committed 8188 workbooks
- direct DPLL parity against a freshly executed legacy extraction script

The complete suite currently contains 32 tests, including profile parsing, insertion validation and extraction, persistence,
built-in protection, and runtime registry integration. Eight DPLL cells for FE wafer 15, X=14, Y=6 at 135 °C
differ between the current raw CSV and the historical extracted workbook. The regression records this as source-data drift and
separately proves that the new streaming adapter matches fresh output from the legacy script exactly (750 rows × 15 columns).

## Repository Contents

| File | Purpose |
| --- | --- |
| `Tests_Data_Extractor_Flat.py` | Extracts targeted ATE raw data from `.xlsx` and `.csv` inputs for selected DUTs and test ranges, then writes a consolidated Excel workbook. |
| `CV_ATE_Correlation_DPLL.py` | Performs delta-based CV ↔ ATE correlation for DPLL phase-noise data and derives updated ATE high limits plus worst-case guard-band limits. |
| `CV_ATE_Correlation_Linear_TXLO_TXPA.py` | Performs offset-only median-delta correlation for TXLO / TXPA power data and derives correlated limits plus residual-based guard-bands. |
| `CV_ATE_Correlation_ModelX_TXLO_TXPA.py` | Extends the TXLO / TXPA power flow with a model-based correlation using both offset correction and a Kf-based physics-informed model. |

## Activity Breakdown

### 1. Raw TE Data Extraction

`Tests_Data_Extractor_Flat.py` is the preparation step for the downstream analysis.

What it does:

- reads all `.xlsx` and `.csv` files from an input folder
- removes non-data header / spacer rows used in the TE export format
- filters for selected devices using `Wafer`, `X`, and `Y`
- filters the requested tests using explicit numbers, ranges, or name fragments
- normalizes metadata such as temperature, LUT value, supply corner, and optional DoE split / DUT number information
- writes a single consolidated Excel workbook for later correlation processing

Typical output columns include:

- `Wafer`
- `X`
- `Y`
- `TestName`
- `TestNumber`
- `TestValue`
- `LUT value`
- `Temperature`
- `SupplyVoltage`

### 2. DPLL Phase-Noise Correlation

`CV_ATE_Correlation_DPLL.py` handles the DPLL phase-noise use case when CV and ATE values are stored in the same sheet.

Per correlation group, it computes:

$$
\Delta = CV - ATE
$$

$$
ATE_{High,New} = ATE_{High,Old} - \operatorname{mean}(\Delta)
$$

$$
WC_{GB} = \max_i\left|\Delta_i - \operatorname{mean}(\Delta)\right|
$$

$$
ATE_{High,WC} = ATE_{High,New} - WC_{GB}
$$

Grouping is done by test case through:

- `Test Number`
- `Voltage corner`
- `Frequency_GHz`
- `Temperature`

Outputs:

- Excel summary with one row per group
- Excel detail table with row-level deltas
- PNG plots per group showing CV, ATE, old limits, updated limits, and worst-case limits

### 3. Linear TXLO / TXPA Correlation

`CV_ATE_Correlation_Linear_TXLO_TXPA.py` uses a robust offset-only model based on the median delta between CV and ATE.

Core relation:

$$
\Delta = CV - ATE
$$

$$
CF = \operatorname{median}(\Delta)
$$

$$
ATE_{correlated} = ATE + CF
$$

Residuals are then computed as:

$$
Residual = \Delta - CF
$$

This flow is used for TXLO and TXPA power correlation, with automatic grouping support for either:

- TXLO by `Test Number`, `Voltage corner`, `Frequency_GHz`, `Temperature`
- TXPA by `LUT value`, `Voltage corner`, `Frequency_GHz`, `Temperature`

The script also derives new correlated limits and handles requirement-based special cases such as:

- LO maximum power at `LO IDAC = 112`
- PA maximum power at `LUT value = 255`

Outputs:

- factor / limit summary sheet
- row-level correlated data sheet
- group plots with raw values, corrected values, regression view, and residuals

### 4. ModelX / Kf-Assisted TXLO / TXPA Correlation

`CV_ATE_Correlation_ModelX_TXLO_TXPA.py` expands the offset-only approach with a second model that uses Kf as an explanatory parameter.

Two models are evaluated per group:

1. Offset-only model

$$
CV_{pred} = ATE + \operatorname{median}(CV - ATE)
$$

1. Kf-assisted model

$$
CV_{pred} = ATE - (\alpha K_f + \beta)
$$

with coefficients fitted from:

$$
ATE - CV = \alpha K_f + \beta
$$

This script merges an additional Kf sheet into the CV/ATE dataset and compares model quality using residuals and goodness-of-fit metrics.

Outputs:

- group-level coefficient and fit summary workbook
- row-level workbook with predicted values and residuals
- per-group PNG plots for raw/correlated views and model residual analysis

## Expected Inputs

The transferred scripts are analysis utilities, not standalone datasets. They expect local input workbooks such as:

- extracted ATE workbooks
- CV / ATE aligned correlation workbooks
- DUT chip-list workbooks
- optional Kf lookup sheets

Most of the script configuration is done directly inside the files through the `USER CONFIG` block at the top of each script.

## Dependencies

The scripts mainly rely on:

- `pandas`
- `matplotlib`
- `openpyxl` via `pandas.ExcelWriter`

Recommended setup:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install pandas matplotlib openpyxl
```

## Important Notes

- The original source folder contained scripts only. It did not contain the confidential raw CV / ATE workbooks or pre-generated plot folders.
- For that reason, this repository documents the activity and transferred the script logic, but it does not include measured-data Excel files or real generated plots.
- The SVG images in `docs/images/` are illustrative repository visuals created for documentation. They are not measurement results.
- Several scripts still contain the original absolute analysis paths inside their `USER CONFIG` section. Update those paths locally before running them in a new environment.

## Transfer Summary

This repository was populated from:

- source repository: `Tasks_Automation_Code`
- source path: `IFX_Scripts/8188_CV_ATE_Correlation`

The folder history was preserved during migration before the new README and documentation assets were added.
