"""Command-line interface for the shared engine."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from . import profiles_8188 as profile_registry
from .correlation import attach_covariate, attach_covariate_from_test_rows, correlate_frame
from .excel import write_dataframe_workbook
from .extraction import LegacyWideTeCsvAdapter
from .handoff import import_measurement_results, create_measurement_request
from .html_report import write_html_report
from .outliers import (
    DEFAULT_MAD_THRESHOLD,
    analyze_outliers,
    attach_outlier_audit,
    finalize_outlier_review,
)
from .profile_store import profile_store_path
from .profiles_8188 import (
    CORRELATION_PROFILES,
    EXTRACTION_PROFILES,
    builtin_profile_ids,
    get_correlation_profile,
    get_extraction_profile,
    refresh_profiles,
)
from .reporting import write_excel_report, write_plots
from .yield_forecast import (
    forecast_yield,
    load_productive_csv_inputs,
    validate_productive_insertion_inputs,
)
from .yield_forecast_report import write_yield_forecast_html


def _parse_productive_input(value: str) -> tuple[str, Path]:
    """Parse INSERTION=CSV used by the forecast CLI."""
    insertion, separator, path = value.partition("=")
    insertion = insertion.strip()
    path = path.strip()
    if not separator or not insertion or not path:
        raise argparse.ArgumentTypeError(
            "productive input must use INSERTION=CSV, for example S1=C:/data/lot.csv"
        )
    return insertion, Path(path)


def _build_parser() -> argparse.ArgumentParser:
    refresh_profiles()
    parser = argparse.ArgumentParser(
        prog="cv-ate-correlation",
        description="CorreLaTE: profile-driven ATE-to-Lab correlation",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("profiles", help="List installed profiles")

    extract = subparsers.add_parser("extract", help="Extract selected records from legacy wide ATE CSV files")
    extract.add_argument("--profile", required=True, choices=sorted(EXTRACTION_PROFILES))
    extract.add_argument("--input-folder", required=True, type=Path)
    extract.add_argument("--chip-manifest", required=True, type=Path)
    extract.add_argument("--output", required=True, type=Path)

    correlate = subparsers.add_parser(
        "correlate", help="Generate factors, guard-bands, details, and an HTML sign-off report"
    )
    correlate.add_argument("--profile", required=True, choices=sorted(CORRELATION_PROFILES))
    correlate.add_argument("--input", required=True, type=Path)
    correlate.add_argument("--sheet", required=True)
    correlate.add_argument(
        "--covariate-input", type=Path,
        help="Legacy fallback lookup workbook; current extractions embed Kf automatically",
    )
    correlate.add_argument("--covariate-sheet", help="Legacy fallback lookup sheet")
    correlate.add_argument("--output", required=True, type=Path)
    correlate.add_argument(
        "--mad-threshold",
        type=float,
        default=DEFAULT_MAD_THRESHOLD,
        help="Scaled-MAD outlier review threshold (default: 12); detection never excludes rows automatically",
    )
    correlate.add_argument(
        "--exclude-outlier-row",
        type=int,
        action="append",
        default=[],
        metavar="ROW_ID",
        help="Explicit OutlierRowId to exclude after review; repeat for multiple rows",
    )
    correlate.add_argument(
        "--html-report", type=Path,
        help="Optional self-contained offline HTML report with embedded plots",
    )
    correlate.add_argument("--plots", type=Path, help=argparse.SUPPRESS)

    forecast = subparsers.add_parser(
        "forecast-yield",
        help="Forecast productive yield from Section 5 factors and insertion CSVs",
    )
    forecast.add_argument("--profile", required=True, choices=sorted(CORRELATION_PROFILES))
    forecast.add_argument("--correlation-report", required=True, type=Path)
    forecast.add_argument("--correlation-sheet", default="Correlation_Summary")
    forecast.add_argument(
        "--productive-input",
        required=True,
        action="append",
        type=_parse_productive_input,
        metavar="INSERTION=CSV",
        help="Productive raw CSV assigned to a Section 1 insertion; repeat for files and insertions",
    )
    forecast.add_argument("--html-report", required=True, type=Path)

    request = subparsers.add_parser("request", help="Generate an editable CV request and TE-only ATE manifest")
    request.add_argument("--profile", required=True, choices=sorted(CORRELATION_PROFILES))
    request.add_argument("--input", required=True, type=Path)
    request.add_argument("--sheet", required=True)
    request.add_argument("--candidate-value-column", default="Test Value")
    request.add_argument("--request-output", required=True, type=Path)
    request.add_argument("--manifest-output", required=True, type=Path)

    imported = subparsers.add_parser("import-results", help="Validate a returned CV request and align it one-to-one")
    imported.add_argument("--profile", required=True, choices=sorted(CORRELATION_PROFILES))
    imported.add_argument("--returned", required=True, type=Path)
    imported.add_argument("--returned-sheet", default="Measurement_Request")
    imported.add_argument("--manifest", required=True, type=Path)
    imported.add_argument("--manifest-sheet", default="ATE_Manifest")
    imported.add_argument("--output", required=True, type=Path)

    subparsers.add_parser("gui", help="Open the lightweight desktop interface")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "profiles":
        builtins = set(builtin_profile_ids())
        print("Built-in profiles:", ", ".join(sorted(builtins)))
        print("Custom profiles:", ", ".join(sorted(set(CORRELATION_PROFILES) - builtins)) or "(none)")
        print("Custom profile store:", profile_store_path())
        if profile_registry.PROFILE_LOAD_ERROR:
            print("Profile store warning:", profile_registry.PROFILE_LOAD_ERROR)
        return 0
    if args.command == "gui":
        from .gui import launch

        launch()
        return 0
    if args.command == "extract":
        result = LegacyWideTeCsvAdapter().extract(
            args.input_folder, args.chip_manifest, get_extraction_profile(args.profile)
        )
        correlation_profile = CORRELATION_PROFILES.get(args.profile)
        if correlation_profile is not None and correlation_profile.covariate is not None:
            result = attach_covariate_from_test_rows(result, correlation_profile)
        write_dataframe_workbook(args.output, {"Extracted_Data": result})
        print(f"Wrote {len(result)} extracted rows to {args.output}")
        return 0
    if args.command == "request":
        profile = get_correlation_profile(args.profile)
        frame = pd.read_excel(args.input, sheet_name=args.sheet)
        request, _manifest = create_measurement_request(
            frame, profile, args.request_output, args.manifest_output,
            candidate_value_column=args.candidate_value_column,
        )
        print(f"Wrote {len(request)} requests to {args.request_output} and the TE manifest to {args.manifest_output}")
        return 0
    if args.command == "import-results":
        profile = get_correlation_profile(args.profile)
        result = import_measurement_results(
            args.returned, args.manifest, profile,
            returned_sheet=args.returned_sheet, manifest_sheet=args.manifest_sheet,
        )
        write_dataframe_workbook(args.output, {"Correlation_Input": result})
        print(f"Wrote {len(result)} validated one-to-one rows to {args.output}")
        return 0
    if args.command == "forecast-yield":
        correlation_profile = get_correlation_profile(args.profile)
        extraction_profile = get_extraction_profile(args.profile)
        files_by_insertion: dict[str, list[Path]] = {}
        for insertion, path in args.productive_input:
            files_by_insertion.setdefault(insertion, []).append(path)
        definitions = [
            {
                "name": insertion.name,
                "selected": insertion.name in files_by_insertion,
                "files": files_by_insertion.get(insertion.name, []),
            }
            for insertion in extraction_profile.insertions
        ]
        unknown = sorted(
            set(files_by_insertion) - {item.name for item in extraction_profile.insertions}
        )
        if unknown:
            raise SystemExit(
                f"Unknown insertion(s) for profile '{args.profile}': {', '.join(unknown)}"
            )
        assignments = validate_productive_insertion_inputs(
            definitions,
            extraction_profile.insertions,
        )
        factors = pd.read_excel(
            args.correlation_report,
            sheet_name=args.correlation_sheet,
        )
        production = load_productive_csv_inputs(
            assignments,
            extraction_profile,
            correlation_profile,
        )
        result = forecast_yield(production, factors, correlation_profile)
        plots = write_yield_forecast_html(
            result,
            correlation_profile,
            args.html_report,
        )
        samples = int(result.summary["SampleCount"].sum())
        failures = int(result.summary["FailCount"].sum())
        print(
            f"Forecasted {samples} productive samples with {failures} failure(s); "
            f"wrote {plots} CDF plots to {args.html_report}"
        )
        return 0

    profile = get_correlation_profile(args.profile)
    frame = pd.read_excel(args.input, sheet_name=args.sheet)
    if profile.covariate and profile.covariate.output_name not in frame.columns:
        if args.covariate_input and args.covariate_sheet:
            lookup = pd.read_excel(args.covariate_input, sheet_name=args.covariate_sheet)
            frame = attach_covariate(frame, lookup, profile)
        elif "Test Number" in frame.columns and pd.to_numeric(
            frame["Test Number"], errors="coerce"
        ).eq(profile.covariate.test_number).any():
            frame = attach_covariate_from_test_rows(frame, profile)
        else:
            raise SystemExit(
                f"Input is missing embedded covariate column '{profile.covariate.output_name}'. "
                f"Rerun raw extraction with this profile so Kf test {profile.covariate.test_number} is attached."
            )
    analysis = analyze_outliers(frame, profile, args.mad_threshold)
    frame, outlier_review = finalize_outlier_review(
        analysis,
        profile,
        args.exclude_outlier_row,
    )
    result = attach_outlier_audit(correlate_frame(frame, profile), profile, outlier_review)
    write_excel_report(result, profile, args.output)
    embedded_plot_count = (
        write_html_report(result, profile, args.html_report) if args.html_report else 0
    )
    legacy_plot_count = write_plots(result, profile, args.plots) if args.plots else 0
    message = f"Wrote {len(result.summary)} groups to {args.output}"
    message += (
        f"; outlier review flagged {outlier_review.flagged_count} and explicitly excluded "
        f"{outlier_review.excluded_count} raw sample(s)"
    )
    if args.html_report:
        message += f" and {embedded_plot_count} embedded plots to {args.html_report}"
    if args.plots:
        message += f"; also wrote {legacy_plot_count} legacy PNG diagnostics to {args.plots}"
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
