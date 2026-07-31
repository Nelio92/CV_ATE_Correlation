# CorreLaTE: ATE-to-Lab Correlation

![CorreLaTE Signal Bloom logo](src/cv_ate_correlation/assets/correlate-signal-bloom.svg)

**Version:** 0.1.0  
**Author:** Wandji Lionel Wilfried (ES RF D RAD PTE TE4)

CorreLaTE is a profile-driven Python application for extracting ATE data, creating a controlled Lab/CV measurement
handoff, validating returned measurements, fitting ATE-to-Lab correlation models, calculating guard bands, and generating
focused Excel reports and a self-contained HTML sign-off report. The same engine is available through a six-step Tkinter
desktop workflow and a command-line interface. The sixth workflow section applies approved correlation factors to separate
productive ATE data and produces an insertion-aligned HTML yield forecast.

This repository also retains the original CTRX8188 analysis scripts that were migrated from
`Tasks_Automation_Code/IFX_Scripts/8188_CV_ATE_Correlation` with their history. They remain useful as traceable legacy
references and golden-regression baselines while the subsystem-neutral package under `src/cv_ate_correlation` is the
recommended implementation for new workflows.

## About

| Item | Information |
| --- | --- |
| Application | **CorreLaTE: ATE-to-Lab Correlation** |
| Version | `0.1.0` |
| Author | **Wandji Lionel Wilfried (ES RF D RAD PTE TE4)** |
| Interfaces | Six-step desktop GUI and `cv-ate-correlation` CLI using one shared engine |
| Correlation models | Linear OLS, `Mean_Deltas`, `Median_Deltas`, and Physics-based with automatic Kf |
| Guard-band policies | `distribution_sigma`, `max_residuals`, and `mean_deltas` |
| Reports | Focused Excel factors and guard bands, correlation sign-off HTML, and a separate offline correlated-yield forecast HTML |
| Visual identity | **Signal Bloom** — blue ATE and green Lab petals converge around a golden fitted path. The bloom represents scattered measurements becoming one coherent correlated result, while the white points emphasize transparent, traceable data. |

An elegant **About** button occupies the free upper-right area of the desktop header. It opens a compact information dialog
containing the application identity, capability summary, workflow overview, and TE/Lab data-handling guidance. Measurement
processing is local. ATE values, limits, and internal Kf are excluded from the workbook sent to Lab/CV; the separate ATE
manifest remains on the TE side.

![CV ATE Correlation Overview](docs/images/activity-overview.svg)

## What This Repository Covers

The work in this repository supports correlation between characterization / CV data and ATE production-test data for several CTRX8188 use cases:

- DPLL phase-noise correlation
- TXLO power correlation
- TXPA power correlation
- Kf-assisted correlation for PA / LO behavior
- Raw TE data extraction for selected DUTs, wafers, coordinates, and test ranges

At a high level, the current CorreLaTE workflow is:

1. Select a validated built-in profile or create a reusable custom profile for the test sets and insertions of interest.
2. Stream and extract relevant ATE raw data, including automatic Kf test `52046` attachment when configured.
3. Generate a Lab/CV request and a separate internal ATE manifest, then validate and align the returned measurements.
4. Calculate all four model diagnostics while applying the strategy and guard-band policy selected for each test set.
5. Review focused factors and new limits in Excel, then use the self-contained HTML report for test-by-test sign-off with
    embedded model and series plots aligned across insertions.
6. Select production insertions, assign their uncorrelated productive CSV files, apply the approved factors and correlated
    limits, and review empirical yield, failures, and insertion-aligned CDF plots in a separate static HTML report.

![Illustrative Correlation Plot](docs/images/illustrative-correlation-plot.svg)

## CorreLaTE Automated Tool

The installable, subsystem-neutral package is implemented under `src/cv_ate_correlation`.
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

For a profile with a Kf covariate, this same extraction also reads the configured Kf test number (default `52046`), joins
its `Test Value` to the selected correlation rows, and writes the result as the configured output column, normally `Kf`.
The standalone Kf test rows are removed before the extracted workbook is created.

### Generate and Re-import the CV Handoff

Create an editable measurement request and a separate TE-only manifest:

```powershell
cv-ate-correlation request `
    --profile ctrx8188-txlo `
    --input Data/TE_Data_Extraction/ATE_Extracted_LO_Power_Data.xlsx `
    --sheet Extracted_Data `
    --request-output Data/CV_Request_TXLO.xlsx `
    --manifest-output Data/TE_Manifest_TXLO.xlsx
```

The request worksheet and all of its cells are unprotected so CV users can select all cells and freely use Excel filters,
grouping, and multi-level `Data > Sort`. A warning comment on the gray `Measurement_Request_ID` header and a warning on the
Instructions sheet state that request IDs must not be modified. `Repeat_Index` also remains part of the validated key and
must not be changed. The yellow CV value column is the intended measurement-entry area. ATE values and limits are omitted
from that workbook. The internal manifest retains the ATE values and must remain on the TE side. Strict import validation
still rejects changed, missing, unknown, or duplicated request keys.

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
    --mad-threshold 12 `
    --output Data/Outputs/DPLL_Correlation_New.xlsx `
    --html-report Data/Outputs/DPLL_Correlation_Signoff.html
```

Kf-assisted profiles use the Kf column embedded during raw extraction; no separate lookup workbook is required:

```powershell
cv-ate-correlation correlate `
    --profile ctrx8188-txpa `
    --input Data/TXPA_Correlation_Input.xlsx `
    --sheet Correlation_Input `
    --output Data/Outputs/TXPA_Correlation_New.xlsx `
    --html-report Data/Outputs/TXPA_Correlation_Signoff.html
```

Each report contains:

- `Correlation_Factors`
- `Guard_Bands`
- `Correlation_Summary`
- `Correlated_Data`
- `Outlier_Review` when the pre-correlation review is attached

`Correlation_Factors` is intentionally focused on the selected strategy for each test set: offset strategies report only
their single correlation factor, while Linear and Physics-based strategies report only factors A/B. The factor cells use a
light-green bold highlight. `Guard_Bands` likewise reports only the selected policy's method and newly calculated limits;
its method and populated new-limit cells use the same highlight. Full all-model diagnostics and original inputs remain
available in `Correlation_Summary` and `Correlated_Data`.

The optional HTML output is the primary human-review and sign-off artifact. It is one offline file with embedded styling,
Signal Bloom branding, and compressed plot images—there is no companion plot folder and no network dependency. Its opening
summary records the profile, applied strategies and guard-band policies, affected tests, population and sample counts,
grouping corners, available models, pooling, Kf configuration, and invalid limit-window count. Each following section covers
one affected test or one explicitly marked merged/pooled test family. It prints every contributing test number and name,
then two focused tables for factors and correlated limits by insertion, followed by model plots and series plots aligned
horizontally across insertions. Sections are searchable and collapsible, and plots can be enlarged for review meetings.
The Excel workbook remains the numerical authority for complete diagnostics and row-level values.

### Forecast Productive Yield

After Section 5 factors and correlated limits are approved, a separate productive-data campaign can be forecast without
altering the original production CSVs. Each CSV is explicitly assigned to one insertion already defined in the selected
Section 1 profile:

```powershell
cv-ate-correlation forecast-yield `
    --profile ctrx8144-txpa `
    --correlation-report Data/Outputs/TXPA_Correlation_New.xlsx `
    --productive-input S1=Data/Productive/S1_Lot_A.csv `
    --productive-input S1=Data/Productive/S1_Lot_B.csv `
    --productive-input B1=Data/Productive/B1_Lot_A.csv `
    --html-report Data/Outputs/TXPA_Yield_Forecast.html
```

`--productive-input INSERTION=CSV` is repeatable. The selected insertion supplies its Section 1 FE/BE identity and
temperature while the productive CSV supplies all DUT measurements. The streaming raw-data adapter extracts only the
configured tests. It does not require the characterization chip manifest and does not reuse the characterization files
assigned in Section 1.

For every productive row, CorreLaTE selects the approved Section 5 factor using its configured test set and all unpooled
grouping conditions. Missing, ambiguous, stale, or duplicate factor matches stop the run rather than silently applying a
different factor. The adapter treats `Test Value` as the canonical raw measurement and also recognizes a configured alias
such as `Test Values`. It resolves aliases per row, rejects conflicting numeric duplicates, and never converts missing
measurements to zero. If a campaign contains both valid and blank/non-numeric test cells, only the unusable rows are skipped;
their count is shown in the command result and HTML input-quality warning. A campaign with no usable measurements stops with
file and candidate-column diagnostics. Correlated productive values are calculated as follows:

- `Linear`: $CV_{forecast}=a\,ATE+b$
- `Mean_Deltas` and `Median_Deltas`: $CV_{forecast}=ATE+b$
- `Physics-based`: $CV_{forecast}=ATE-(\alpha K_f+\beta)$; productive Kf rows must be present in the CSV campaign

The forecast PASS rule uses inclusive correlated limits:

$$
LTL_{correlated}\le CV_{forecast}\le UTL_{correlated}.
$$

The self-contained HTML report provides overall and per-test yield percentages, PASS/FAIL counts, below-LTL and above-UTL
counts, minimum, mean, standard deviation, percentiles, median, maximum, and Cpk when defined. Every productive sample is
drawn as an empirical CDF marker rather than a connecting line. Failing samples use red `X` markers, every insertion with
a failure receives a red plot border, and every test with at least one failure receives a red expanded review section and
index entry. CDF plots for the same test and conditions are kept in one horizontally scrollable insertion row. The result
is an empirical forecast from the supplied productive samples, not a guarantee of future manufacturing yield.

### Desktop GUI

Launch the GUI with:

```powershell
cv-ate-correlation gui
```

The GUI and CLI use the same engine and persistent profile registry. The Signal Bloom mark appears beside the CorreLaTE
wordmark and is also used as the application window icon. The desktop interface provides six guided workflow tabs:

1. `Profiles` — create, validate, update, or delete a reusable custom profile.
2. `Extract TE` — select an extraction profile, chip manifest, and output workbook. Built-in profiles also select a
    raw-data folder; custom profiles use the files assigned to their insertions.
3. `Create CV Request` — generate the editable CV workbook and separate internal ATE manifest.
4. `Import CV Results` — validate returned request coverage and produce the one-to-one aligned input.
5. `Correlate` — review configurable scaled-MAD findings before fitting, optionally exclude explicitly selected rows,
    then generate the Excel report and optional self-contained HTML sign-off report using the Kf retained in the aligned input.
6. `Forecast Yield` — select one or more insertions defined in Section 1, assign productive raw CSV files independently
    to each selected insertion, load the Section 5 `Correlation_Summary`, and generate the separate correlated-yield HTML.

The upper-right `About` button opens a focused dialog with the application version, author, supported models and guard-band
policies, report outputs, expanded Signal Bloom meaning, workflow summary, and safe TE/Lab handoff guidance. Keeping About
in the header leaves the notebook dedicated to the six numbered operational steps.

Workflow file fields are visually classified to prevent direction mistakes:

- blue `INPUT` labels and blue-tinted fields select files or folders that must already exist
- green `OUTPUT` labels and green-tinted fields select new destinations that CorreLaTE will generate
- input buttons use `Open…`/`Select…`; output workbook buttons use `Save as…`

In Step 3, both the CV request and internal ATE manifest are outputs. Keep the manifest internally and load that generated
file as an input in Step 4. The CV request is the file sent to Lab/CV and loaded again after its result column is completed.

The profile editor is subsystem-neutral. A user can define:

- insertions before test identity, with a name such as `S1`, fixed `FE` or `BE` group, numeric temperature, and one
    or more corresponding raw-data files selected with `Browse…`
- one or more named test sets, each containing exact test numbers, inclusive ranges, or test-name fragments
- an independent correlation strategy and guard-band policy for every test set
- optional **Merge/pool parameters** for every test set, accepting any enabled grouping columns
- independently selectable grouping conditions for `DUT Nr`, `Test Number`, `Frequency`, `Supply Corner`, `Channel`,
    and `Digital Control`
- filename- or test-name-based identification configured inside each grouping condition
- additional user-defined grouping conditions created with the `Add…` button
- Lab/reference and ATE/candidate columns and minimum points per group
- limit, unit, and detail-key columns
- `distribution_sigma`, `max_residuals`, or `mean_deltas` guard-band behavior, with per-test-set `REQ_MIN` and `REQ_MAX`
    inputs for both inward requirement-limit policies
- automatic Physics/Kf extraction, with editable raw value column, merge keys, output name, and Kf test number
    (default `52046`)

Each grouping condition has an editable input-column name. The defaults use common ATE names such as `Frequency_GHz` and
`Voltage corner`; alternatives such as `PA Channel`, `LUT value`, or a project-specific column can be entered directly. Its
identification source and rule are configured in the same box. Every manually entered field in the GUI includes a short
description and example value; browse controls and fixed dropdown lists are self-describing and therefore omit those hints.
`Insertion` and `Temperature` are not selectable grouping conditions: CorreLaTE derives both and automatically includes them
in correlation grouping. Keeping the insertion name prevents separate campaigns that share a temperature—such as FE `S1`
and BE `B1` at 135 °C—from being combined into one plot. Profile validation requires at least one existing raw file for
every insertion and rejects duplicate insertion names or a raw file assigned to more than one insertion. Plot titles include
the insertion and explicit sample count.

For `BE` insertions, CorreLaTE automatically falls back to the FUSE metadata columns when the normal chip-coordinate values
are blank or their columns are absent: test number `62007` supplies `WAFER`, `62008` supplies `X`, and `62009` supplies `Y`.
These mappings and the `BE` applicability are the defaults for new and existing custom profiles. The three values are visible
and editable in the profile editor under `Insertions` → `BE coordinate fallback (FUSE module)`, allowing each silicon profile
to use its own FUSE test numbers.

Custom profiles are stored per Windows user in `%APPDATA%\CorreLaTE\profiles.json`. They are loaded by both the GUI and CLI,
so a saved profile immediately appears in all extraction, handoff, import, and correlation selectors. The built-in CTRX8188
profiles are read-only and retain their golden-regression behavior.

The compact test-selection field in each test set accepts entries such as `101, 200-220, LeakageCurrent`. Use `Add…` to
create another set when some tests require different calculations. Every set shows the underlying equations beside its
selectors:

- `Linear`: OLS fit $CV_{pred}=a\,ATE+b$; the correlation factors are slope $a$ and intercept $b$
- `Mean_Deltas`: $CV_{pred}=ATE+\operatorname{mean}(CV-ATE)$
- `Median_Deltas`: $CV_{pred}=ATE+\operatorname{median}(CV-ATE)$
- `Physics-based`: $CV_{pred}=ATE-(\alpha K_f+\beta)$, fitted from $ATE-CV=\alpha K_f+\beta$
- `distribution_sigma`: $limits=\operatorname{mean}(ATE_{corrected})\pm k\sigma(ATE_{corrected})$
- `max_residuals`: $LTL_{new}=REQ_{MIN}+|r|_{max}$ and $UTL_{new}=REQ_{MAX}-|r|_{max}$
- `mean_deltas`: $LTL_{new}=REQ_{MIN}+|\overline{CV-ATE}|$ and
    $UTL_{new}=REQ_{MAX}-|\overline{CV-ATE}|$

`max_residuals` is the canonical spelling used by the GUI, newly saved profiles, Excel, and HTML. Existing profile files
using `Max_residuals` remain readable and are normalized to lowercase. The `mean_deltas` guard-band is distinct from the
`Mean_Deltas` correlation strategy: it uses the absolute raw average CV-minus-ATE bias as a symmetric inward requirement
margin regardless of the selected primary correlation model. It is therefore a bias-based tightening policy and is less
conservative than protecting against the largest observed post-correction residual.

#### Pre-correlation outlier review

Step 5 performs an auditable outlier review before any production model is fitted. The editable threshold defaults to
$n=12$ and uses the normal-consistency-scaled median absolute deviation:

$$
MAD=\operatorname{median}\left(|x_i-\operatorname{median}(x)|\right),\qquad
s_{MAD}=1.4826\,MAD,
$$

$$
\frac{|x_i-\operatorname{median}(x)|}{s_{MAD}}>n.
$$

Detection runs independently within every unpooled test/corner population so legitimate shifts between tests,
insertions, temperatures, supply corners, channels, or other enabled dimensions are not compared globally. CorreLaTE
reviews three signals independently:

1. the raw Lab/CV series;
2. the raw ATE/TE series;
3. the paired disagreement—robust preliminary residuals for Linear and Physics-based strategies, or the centered
   $CV-ATE$ delta for offset strategies.

The paired signal distinguishes a DUT that is extreme on both systems but still follows the correlation from a genuinely
discordant pair. When $MAD=0$, constant values are not flagged; any non-median deviation is shown with an infinite robust
score and an explicit `MAD=0` review status rather than silently divided by zero.

The review window always reports whether findings exist and, when present, lists the count, test number/name, DUT and wafer
coordinates, DoE split, insertion, temperature, enabled corners, measured values, robust scores, and reasons. A statistical
flag is a review candidate—not proof that a sample is invalid. **Filtering is disabled by default**, no row is preselected,
and continuing with all data preserves every sample. To filter, the user must first enable manual exclusions for that run,
select each finding explicitly, and confirm it. CorreLaTE blocks a selection that would reduce any previously valid
correlation population below the configured minimum sample count.

The aligned input is never modified. Retained flagged rows remain marked in `Correlated_Data`; every finding and its final
retained/excluded decision is written to the companion Excel report's `Outlier_Review` worksheet; and group summaries
record original, flagged, excluded, and final counts. The detailed outlier audit is intentionally excluded from the HTML
sign-off report and must be reviewed separately. When findings exist, the HTML displays only a prominent warning with the
flagged, retained, and excluded totals and a reference to `Outlier_Review`; when no items are flagged, no outlier warning is
shown. The CLI follows the same safe default and accepts repeated `--exclude-outlier-row ROW_ID` arguments only for
explicit, traceable exclusions after a review run.

All four model predictions and residuals are calculated for comparison. `Mean_Deltas` and `Median_Deltas` are intentionally
offset-only models with fixed slope 1; unlike OLS, they do not rotate to follow the point cloud. A low or negative R² therefore
means that an offset-only model is unsuitable for that group, not that its mean or median factor was calculated incorrectly.
Physics-based results are shown when the automatically extracted Kf data has enough numeric variation; selecting
Physics-based as the primary strategy requires complete Kf coverage.

#### Why production Linear remains OLS instead of Deming regression

The production `Linear` strategy intentionally uses ordinary least squares (OLS). OLS treats ATE as the predictor and
minimizes the vertical CV/Lab prediction error

$$
\sum_i\left(CV_i-(a\,ATE_i+b)\right)^2.
$$

That objective matches CorreLaTE's operational use: predict the Lab/CV value from an observed ATE value and quantify the
resulting vertical residual used by correlation diagnostics and guard-banding.

Deming regression is an errors-in-variables method. It is appropriate when both ATE and Lab/CV measurements have known
measurement uncertainty, and it minimizes an error-weighted orthogonal distance rather than vertical prediction error. Its
fit requires a defensible error-variance ratio

$$
\lambda=\frac{\operatorname{Var}(\text{Lab/CV measurement error})}
                                    {\operatorname{Var}(\text{ATE measurement error})}.
$$

Setting $\lambda=1$ assumes equal error variances; it does not estimate them. The evaluated 39,600-row production-aligned
campaign contains 450 populations of 88 samples but only one observation per measurement request (`Repeat_Index=1`), so
$\lambda$ cannot be estimated from those data. Equal-variance Deming was therefore evaluated only as a sensitivity study:

- OLS produced lower or equal vertical RMSE in all 450 populations, which is expected from the OLS objective.
- 384 of 450 populations had $|r|<0.7$; in these weakly associated populations equal-variance Deming frequently produced
    unstable and sometimes extreme slopes.
- In the 66 populations with $|r|\ge0.7$, the median maximum OLS-to-Deming prediction difference was only 0.0772 measurement
    units and the median Deming/OLS vertical-RMSE ratio was 1.014.
- In the 35 populations with $|r|\ge0.9$, the median maximum prediction difference fell to 0.0469 measurement units.

Consequently, Deming does **not** replace OLS and is not exposed as a production strategy or guard-band input. It can remain
an isolated sensitivity analysis for method-comparison studies. Production adoption would first require repeatability or
metrology data for both systems, a justified value of $\lambda$, and acceptance criteria demonstrating an improvement for
the intended prediction task. Equal-variance Deming results must always be labeled as an unverified $\lambda=1$ assumption.

### Automatic Kf input for the Physics-based model

Automatic Physics/Kf is enabled by default for custom profiles. Step 2 identifies and joins Kf directly from the assigned
raw data without a separate workbook or manually supplied covariate. Its advanced profile fields are:

1. **Kf raw value column** is the numeric result on the raw Kf row and defaults to `Test Value`.
2. **Kf merge keys** identify which Kf belongs to each correlation row and default to
    `DUT Nr, Temperature, Insertion` for custom insertion profiles.
3. **Kf output name** is the attached internal column name and defaults to `Kf`.
4. **Kf test number** identifies the raw Kf test and defaults to `52046`.
5. Save the profile and run Step 2. CorreLaTE adds the Kf test to the raw extraction selector automatically, joins its value,
   verifies complete coverage, and removes the Kf rows. The `Kf` column is retained in the internal manifest and aligned
   correlation input, but omitted from the workbook sent to Lab/CV.

Each merge-key combination must resolve to exactly one numeric Kf. Repeated identical rows are accepted, but conflicting Kf
values for the same keys are rejected. Missing matches also stop extraction with the unmatched row count. For each correlation group, CorreLaTE fits
$ATE-CV=\alpha K_f+\beta$ and calculates $CV_{pred}=ATE-(\alpha K_f+\beta)$. Kf is therefore an explanatory input to the
physics model, not the CV/reference measurement itself.

When eight test numbers are pooled across 11 DUTs and the merge keys identify DUT plus measurement conditions, each DUT's
Kf is joined to all eight matching test rows. The Physics-based fit therefore uses the same 88-row pooled population and
produces one shared $\alpha$, $\beta$, and guard band for that test set.

Correlation workbooks include the applied test-set name, strategy, and guard-band policy for traceability. A row must match
exactly one test set; CorreLaTE rejects unmatched or overlapping definitions rather than silently choosing a policy.

**Merge/pool parameters** removes selected dimensions from the factor/guard-band grouping only for that test set. The
original dimensions remain on every row in `Correlated_Data`. For example, pooling `Test Number, Channel` combines eight
TXPA channel tests measured on 11 DUTs into one 88-sample population for each frequency, supply corner, digital control,
insertion, and temperature. All eight test numbers receive the same correlation factor and guard band. Summary, factor,
and guard-band sheets show `MERGED`, the pooled parameter names, every merged value, and value counts; plot titles show the
merged values and `Samples=88`. Different test sets can pool different parameters without combining rows across sets.

Each correlation population produces two embedded figures in the HTML report. The series figure contains raw and all
correlated series with the new policy limits; the models figure contains the CV-vs-ATE model comparison and all model
residuals. Figures are rendered directly in memory and embedded as compressed data URIs, so normal sign-off generation does
not leave individual PNG files or FE/BE plot folders behind. Within a test-family section, equivalent correlation conditions
form one review row and all insertion plots stay on that same horizontal row. Plot annotations include sample counts,
mean/median/max deltas, residual deviations, R² values, physics α/β, invalid limit-window warnings, and DoE segment labels
when available. The correlated-series subplot omits original test-limit and base-requirement lines and legend entries; its
vertical range is calculated from all model predictions and the applicable new limits so the model traces remain visible.

Every enabled grouping condition defines a separate factor/guard-band population. Device identifiers such as `DUT Nr`
normally belong in **Detail key columns**, not **Grouping conditions**, unless a separate correlation factor per DUT is
intentional and enough repeated measurements exist for every DUT. If all groups are below **Minimum points/group**, the
error now reports valid-pair count, group count, largest group, group-size distribution, and which individual grouping
dimensions are likely over-fragmenting the data. CorreLaTE does not silently merge scientific conditions or lower the
minimum sample requirement.

Every Excel workbook generated by the package—extracted data, CV request, internal manifest, aligned input, and correlation
report—uses consistent presentation on every sheet: autofitted column widths, blue Accent 1 headers with bold white text,
frozen header rows, and enabled header filters. Widths are capped to keep exceptionally long values manageable.
In all four correlation-report sheets, `Test Name` is placed immediately after `Test Number` whenever a test-number column is
present.

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
outside the Tk event loop, with a shared progress/status area keeping the interface responsive. Every workflow tab and profile
subpage has a vertical scrollbar and mouse-wheel support so all fields remain reachable in a small window.

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

- DPLL Linear/mean-delta factors, shifted limits, and worst-case guard-bands
- TXLO/TXPA Median_Deltas factors, residuals, corrected limits, and special requirement policies
- Kf-assisted coefficients, fit metrics, residuals, and limits
- four-model calculations, requirement-based `max_residuals` and `mean_deltas` limits, scaled-MAD outlier review/auditing,
    direct in-memory figure rendering, and the offline HTML
    test-family/insertion sign-off structure
- DPLL, Kf, TXLO, and combined TXPA raw extraction against the committed 8188 workbooks
- direct DPLL parity against a freshly executed legacy extraction script

The suite includes profile parsing, insertion validation and extraction, persistence, built-in protection, runtime registry
integration, branding assets, and About metadata validation. Eight DPLL cells for FE wafer 15, X=14, Y=6 at 135 °C
differ between the current raw CSV and the historical extracted workbook. The regression records this as source-data drift and
separately proves that the new streaming adapter matches fresh output from the legacy script exactly (750 rows × 15 columns).

## Repository Contents

| File | Purpose |
| --- | --- |
| `src/cv_ate_correlation/` | Current CorreLaTE package: shared models, extraction, handoff, correlation, guard-band, reporting, CLI, GUI, and profile registry. |
| `src/cv_ate_correlation/assets/` | Selected Signal Bloom branding in SVG, 64/256 px transparent PNG, and multi-resolution ICO formats. |
| `tests/` | Unit, workflow, reporting, branding, and campaign-backed golden regression coverage. |
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
