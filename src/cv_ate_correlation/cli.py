"""Command-line interface for the shared engine."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from . import profiles_8188 as profile_registry
from .correlation import attach_covariate, correlate_frame
from .excel import write_dataframe_workbook
from .extraction import LegacyWideTeCsvAdapter
from .handoff import import_measurement_results, create_measurement_request
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

    correlate = subparsers.add_parser("correlate", help="Generate factors, guard-bands, details, and plots")
    correlate.add_argument("--profile", required=True, choices=sorted(CORRELATION_PROFILES))
    correlate.add_argument("--input", required=True, type=Path)
    correlate.add_argument("--sheet", required=True)
    correlate.add_argument("--covariate-input", type=Path)
    correlate.add_argument("--covariate-sheet")
    correlate.add_argument("--output", required=True, type=Path)
    correlate.add_argument("--plots", type=Path)

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

    profile = get_correlation_profile(args.profile)
    frame = pd.read_excel(args.input, sheet_name=args.sheet)
    if profile.covariate:
        if not args.covariate_input or not args.covariate_sheet:
            raise SystemExit("This profile requires --covariate-input and --covariate-sheet")
        lookup = pd.read_excel(args.covariate_input, sheet_name=args.covariate_sheet)
        frame = attach_covariate(frame, lookup, profile)
    result = correlate_frame(frame, profile)
    write_excel_report(result, profile, args.output)
    plot_count = write_plots(result, profile, args.plots) if args.plots else 0
    print(f"Wrote {len(result.summary)} groups and {plot_count} plots to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
