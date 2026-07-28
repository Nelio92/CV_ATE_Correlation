"""Lightweight desktop interface for all shared CV/ATE workflows."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import pandas as pd

from .correlation import attach_covariate, correlate_frame
from .extraction import LegacyWideTeCsvAdapter
from .handoff import MANIFEST_SHEET, REQUEST_SHEET, create_measurement_request, import_measurement_results
from .profiles_8188 import (
    CORRELATION_PROFILES,
    EXTRACTION_PROFILES,
    get_correlation_profile,
    get_extraction_profile,
)
from .reporting import write_excel_report, write_plots


Action = Callable[[], str]


def workbook_sheet_names(path: str | Path) -> tuple[str, ...]:
    """Return workbook sheets for GUI selectors without keeping the file open."""
    return tuple(pd.ExcelFile(Path(path)).sheet_names)


class CorrelationDesktopApp:
    """Four-step Tkinter shell around the pure extraction/correlation engine."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("CV ↔ ATE Correlation")
        self.root.geometry("1020x690")
        self.root.minsize(900, 620)

        style = ttk.Style(root)
        style.configure("Heading.TLabel", font=("Segoe UI", 12, "bold"))
        style.configure("Hint.TLabel", foreground="#555555")

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=(12, 6))

        self.status = tk.StringVar(value="Ready")
        status_frame = ttk.Frame(root, padding=(12, 4, 12, 10))
        status_frame.pack(fill="x")
        status_frame.columnconfigure(0, weight=1)
        ttk.Label(status_frame, textvariable=self.status).grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(status_frame, mode="indeterminate", length=210)
        self.progress.grid(row=0, column=1, sticky="e")

        self._job_results: queue.Queue[tuple[bool, str]] = queue.Queue()
        self._active_button: ttk.Button | None = None

        self._build_extraction_tab()
        self._build_request_tab()
        self._build_import_tab()
        self._build_correlation_tab()

    def _make_tab(self, title: str, heading: str, hint: str) -> ttk.Frame:
        outer = ttk.Frame(self.notebook, padding=18)
        outer.columnconfigure(0, weight=1)
        self.notebook.add(outer, text=title)
        ttk.Label(outer, text=heading, style="Heading.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(outer, text=hint, style="Hint.TLabel", wraplength=850).grid(
            row=1, column=0, pady=(4, 14), sticky="w"
        )
        form = ttk.Frame(outer)
        form.grid(row=2, column=0, sticky="nsew")
        form.columnconfigure(1, weight=1)
        return form

    @staticmethod
    def _add_label(form: ttk.Frame, row: int, text: str) -> None:
        ttk.Label(form, text=text).grid(row=row, column=0, padx=(0, 10), pady=7, sticky="w")

    def _add_combo(
        self,
        form: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        values: tuple[str, ...] | list[str],
        *,
        readonly: bool = False,
    ) -> ttk.Combobox:
        self._add_label(form, row, label)
        combo = ttk.Combobox(
            form,
            textvariable=variable,
            values=values,
            state="readonly" if readonly else "normal",
        )
        combo.grid(row=row, column=1, padx=(0, 8), pady=7, sticky="ew")
        return combo

    def _add_path(
        self,
        form: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        command: Callable[[], None],
        *,
        button_text: str = "Browse…",
    ) -> tuple[ttk.Entry, ttk.Button]:
        self._add_label(form, row, label)
        entry = ttk.Entry(form, textvariable=variable)
        entry.grid(row=row, column=1, padx=(0, 8), pady=7, sticky="ew")
        button = ttk.Button(form, text=button_text, command=command)
        button.grid(row=row, column=2, pady=7, sticky="ew")
        return entry, button

    @staticmethod
    def _require(**values: str) -> None:
        missing = [label for label, value in values.items() if not value.strip()]
        if missing:
            raise ValueError(f"Required: {', '.join(missing)}")

    def _validate_required(self, **values: str) -> bool:
        try:
            self._require(**values)
        except ValueError as error:
            messagebox.showerror("Missing information", str(error), parent=self.root)
            return False
        return True

    def _choose_open(
        self,
        path_variable: tk.StringVar,
        sheet_variable: tk.StringVar | None = None,
        sheet_combo: ttk.Combobox | None = None,
    ) -> None:
        value = filedialog.askopenfilename(
            parent=self.root,
            filetypes=[("Excel workbooks", "*.xlsx *.xlsm *.xls"), ("All files", "*.*")],
        )
        if not value:
            return
        path_variable.set(value)
        if sheet_variable is None or sheet_combo is None:
            return
        try:
            sheets = workbook_sheet_names(value)
        except Exception as error:
            sheet_combo.configure(values=())
            sheet_variable.set("")
            messagebox.showerror("Cannot read workbook", str(error), parent=self.root)
            return
        sheet_combo.configure(values=sheets)
        if sheets:
            sheet_variable.set(sheets[0])

    def _choose_save(self, variable: tk.StringVar, title: str) -> None:
        value = filedialog.asksaveasfilename(
            parent=self.root,
            title=title,
            defaultextension=".xlsx",
            filetypes=[("Excel workbooks", "*.xlsx")],
        )
        if value:
            variable.set(value)

    def _choose_folder(self, variable: tk.StringVar, title: str) -> None:
        value = filedialog.askdirectory(parent=self.root, title=title)
        if value:
            variable.set(value)

    def _start_job(self, button: ttk.Button, description: str, action: Action) -> None:
        if self._active_button is not None:
            messagebox.showwarning(
                "Operation in progress",
                "Wait for the current operation to finish.",
                parent=self.root,
            )
            return
        self._active_button = button
        button.state(["disabled"])
        self.status.set(description)
        self.progress.start(12)

        def worker() -> None:
            try:
                self._job_results.put((True, action()))
            except Exception as error:  # The main Tk thread displays workflow errors.
                self._job_results.put((False, f"{type(error).__name__}: {error}"))

        threading.Thread(target=worker, daemon=True).start()
        self.root.after(100, self._poll_job)

    def _poll_job(self) -> None:
        if self._active_button is None:
            return
        try:
            success, text = self._job_results.get_nowait()
        except queue.Empty:
            self.root.after(100, self._poll_job)
            return
        self.progress.stop()
        self._active_button.state(["!disabled"])
        self._active_button = None
        self.status.set(text if success else "Operation failed")
        if success:
            messagebox.showinfo("Complete", text, parent=self.root)
        else:
            messagebox.showerror("Operation failed", text, parent=self.root)

    def _build_extraction_tab(self) -> None:
        form = self._make_tab(
            "1 · Extract TE",
            "Extract normalized ATE measurements",
            "Streams legacy wide TE CSV files, filters the selected devices and tests, and writes Extracted_Data.",
        )
        profile = tk.StringVar(value=next(iter(EXTRACTION_PROFILES)))
        raw_folder = tk.StringVar()
        chip_manifest = tk.StringVar()
        output = tk.StringVar()
        self._add_combo(form, 0, "Extraction profile", profile, sorted(EXTRACTION_PROFILES), readonly=True)
        self._add_path(
            form,
            1,
            "Raw TE folder",
            raw_folder,
            lambda: self._choose_folder(raw_folder, "Select raw TE data folder"),
            button_text="Select…",
        )
        self._add_path(
            form,
            2,
            "Chip manifest",
            chip_manifest,
            lambda: self._choose_open(chip_manifest),
        )
        self._add_path(
            form,
            3,
            "Extracted workbook",
            output,
            lambda: self._choose_save(output, "Save extracted ATE workbook"),
        )

        def run() -> None:
            values = {
                "profile": profile.get(),
                "raw TE folder": raw_folder.get(),
                "chip manifest": chip_manifest.get(),
                "output workbook": output.get(),
            }
            if not self._validate_required(**values):
                return

            def action() -> str:
                frame = LegacyWideTeCsvAdapter().extract(
                    Path(values["raw TE folder"]),
                    Path(values["chip manifest"]),
                    get_extraction_profile(values["profile"]),
                )
                destination = Path(values["output workbook"])
                destination.parent.mkdir(parents=True, exist_ok=True)
                frame.to_excel(destination, index=False, sheet_name="Extracted_Data")
                return f"Extracted {len(frame):,} rows to {destination}."

            self._start_job(run_button, "Extracting raw TE data…", action)

        run_button = ttk.Button(form, text="Run extraction", command=run)
        run_button.grid(row=4, column=1, pady=(18, 0), sticky="e")

    def _build_request_tab(self) -> None:
        form = self._make_tab(
            "2 · Create CV Request",
            "Create a protected CV measurement request",
            "The editable request excludes ATE values. A separate internal manifest retains ATE data for one-to-one alignment.",
        )
        profile = tk.StringVar(value=next(iter(CORRELATION_PROFILES)))
        source = tk.StringVar()
        sheet = tk.StringVar()
        value_column = tk.StringVar(value="Test Value")
        request_output = tk.StringVar()
        manifest_output = tk.StringVar()
        self._add_combo(form, 0, "Correlation profile", profile, sorted(CORRELATION_PROFILES), readonly=True)
        sheet_combo = self._add_combo(form, 2, "Input sheet", sheet, ())
        self._add_path(
            form,
            1,
            "Extracted workbook",
            source,
            lambda: self._choose_open(source, sheet, sheet_combo),
        )
        self._add_label(form, 3, "ATE value column")
        ttk.Entry(form, textvariable=value_column).grid(row=3, column=1, padx=(0, 8), pady=7, sticky="ew")
        self._add_path(
            form,
            4,
            "CV request workbook",
            request_output,
            lambda: self._choose_save(request_output, "Save CV measurement request"),
        )
        self._add_path(
            form,
            5,
            "Internal ATE manifest",
            manifest_output,
            lambda: self._choose_save(manifest_output, "Save internal ATE manifest"),
        )

        def run() -> None:
            values = {
                "profile": profile.get(),
                "input workbook": source.get(),
                "input sheet": sheet.get(),
                "ATE value column": value_column.get(),
                "CV request workbook": request_output.get(),
                "ATE manifest workbook": manifest_output.get(),
            }
            if not self._validate_required(**values):
                return
            if Path(values["CV request workbook"]).resolve() == Path(values["ATE manifest workbook"]).resolve():
                messagebox.showerror(
                    "Invalid output paths",
                    "The CV request and internal ATE manifest must use different files.",
                    parent=self.root,
                )
                return

            def action() -> str:
                frame = pd.read_excel(Path(values["input workbook"]), sheet_name=values["input sheet"])
                request, _manifest = create_measurement_request(
                    frame,
                    get_correlation_profile(values["profile"]),
                    Path(values["CV request workbook"]),
                    Path(values["ATE manifest workbook"]),
                    candidate_value_column=values["ATE value column"],
                )
                return f"Created {len(request):,} CV requests and the separate internal ATE manifest."

            self._start_job(run_button, "Creating protected CV request…", action)

        run_button = ttk.Button(form, text="Create request", command=run)
        run_button.grid(row=6, column=1, pady=(18, 0), sticky="e")

    def _build_import_tab(self) -> None:
        form = self._make_tab(
            "3 · Import CV Results",
            "Validate and align returned CV measurements",
            "Rejects duplicate, missing, or unknown request keys and merges only validated CV values into the ATE manifest.",
        )
        profile = tk.StringVar(value=next(iter(CORRELATION_PROFILES)))
        returned = tk.StringVar()
        returned_sheet = tk.StringVar(value=REQUEST_SHEET)
        manifest = tk.StringVar()
        manifest_sheet = tk.StringVar(value=MANIFEST_SHEET)
        output = tk.StringVar()
        self._add_combo(form, 0, "Correlation profile", profile, sorted(CORRELATION_PROFILES), readonly=True)
        returned_combo = self._add_combo(form, 2, "Returned sheet", returned_sheet, ())
        self._add_path(
            form,
            1,
            "Returned CV workbook",
            returned,
            lambda: self._choose_open(returned, returned_sheet, returned_combo),
        )
        manifest_combo = self._add_combo(form, 4, "Manifest sheet", manifest_sheet, ())
        self._add_path(
            form,
            3,
            "Internal ATE manifest",
            manifest,
            lambda: self._choose_open(manifest, manifest_sheet, manifest_combo),
        )
        self._add_path(
            form,
            5,
            "Aligned correlation input",
            output,
            lambda: self._choose_save(output, "Save aligned correlation input"),
        )

        def run() -> None:
            values = {
                "profile": profile.get(),
                "returned workbook": returned.get(),
                "returned sheet": returned_sheet.get(),
                "ATE manifest": manifest.get(),
                "manifest sheet": manifest_sheet.get(),
                "output workbook": output.get(),
            }
            if not self._validate_required(**values):
                return

            def action() -> str:
                frame = import_measurement_results(
                    Path(values["returned workbook"]),
                    Path(values["ATE manifest"]),
                    get_correlation_profile(values["profile"]),
                    returned_sheet=values["returned sheet"],
                    manifest_sheet=values["manifest sheet"],
                )
                destination = Path(values["output workbook"])
                destination.parent.mkdir(parents=True, exist_ok=True)
                frame.to_excel(destination, index=False, sheet_name="Correlation_Input")
                return f"Validated and aligned {len(frame):,} one-to-one rows to {destination}."

            self._start_job(run_button, "Validating returned CV measurements…", action)

        run_button = ttk.Button(form, text="Import results", command=run)
        run_button.grid(row=6, column=1, pady=(18, 0), sticky="e")

    def _build_correlation_tab(self) -> None:
        form = self._make_tab(
            "4 · Correlate",
            "Generate factors, guard-bands, report, and plots",
            "Uses the shared engine. Covariate fields are enabled only for profiles that require them.",
        )
        profile = tk.StringVar(value=next(iter(CORRELATION_PROFILES)))
        source = tk.StringVar()
        sheet = tk.StringVar()
        covariate_source = tk.StringVar()
        covariate_sheet = tk.StringVar()
        report = tk.StringVar()
        plots = tk.StringVar()
        covariate_hint = tk.StringVar()
        self._add_combo(form, 0, "Correlation profile", profile, sorted(CORRELATION_PROFILES), readonly=True)
        sheet_combo = self._add_combo(form, 2, "Input sheet", sheet, ())
        self._add_path(
            form,
            1,
            "Correlation input",
            source,
            lambda: self._choose_open(source, sheet, sheet_combo),
        )
        covariate_combo = self._add_combo(form, 4, "Covariate sheet", covariate_sheet, ())
        covariate_entry, covariate_button = self._add_path(
            form,
            3,
            "Covariate workbook",
            covariate_source,
            lambda: self._choose_open(covariate_source, covariate_sheet, covariate_combo),
        )
        ttk.Label(form, textvariable=covariate_hint, style="Hint.TLabel").grid(
            row=4, column=2, pady=7, sticky="w"
        )
        self._add_path(
            form,
            5,
            "Excel report",
            report,
            lambda: self._choose_save(report, "Save correlation report"),
        )
        self._add_path(
            form,
            6,
            "Plots folder (optional)",
            plots,
            lambda: self._choose_folder(plots, "Select plot output folder"),
            button_text="Select…",
        )

        def update_covariate_state(*_args: object) -> None:
            required = get_correlation_profile(profile.get()).covariate is not None
            state = ["!disabled"] if required else ["disabled"]
            covariate_entry.state(state)
            covariate_combo.state(state)
            covariate_button.state(state)
            covariate_hint.set("Required" if required else "Not used")

        def run() -> None:
            values = {
                "profile": profile.get(),
                "input workbook": source.get(),
                "input sheet": sheet.get(),
                "report workbook": report.get(),
                "covariate workbook": covariate_source.get(),
                "covariate sheet": covariate_sheet.get(),
                "plots folder": plots.get(),
            }
            if not self._validate_required(**{
                key: values[key]
                for key in ("profile", "input workbook", "input sheet", "report workbook")
            }):
                return
            selected = get_correlation_profile(values["profile"])
            if selected.covariate:
                if not self._validate_required(**{
                    "covariate workbook": values["covariate workbook"],
                    "covariate sheet": values["covariate sheet"],
                }):
                    return

            def action() -> str:
                frame = pd.read_excel(Path(values["input workbook"]), sheet_name=values["input sheet"])
                if selected.covariate:
                    lookup = pd.read_excel(
                        Path(values["covariate workbook"]),
                        sheet_name=values["covariate sheet"],
                    )
                    frame = attach_covariate(frame, lookup, selected)
                result = correlate_frame(frame, selected)
                destination = Path(values["report workbook"])
                write_excel_report(result, selected, destination)
                plot_count = 0
                if values["plots folder"].strip():
                    plot_count = write_plots(result, selected, Path(values["plots folder"]))
                return (
                    f"Generated {len(result.summary):,} correlation groups and "
                    f"{plot_count:,} plots in {destination}."
                )

            self._start_job(run_button, "Calculating correlations and generating outputs…", action)

        run_button = ttk.Button(form, text="Generate report", command=run)
        run_button.grid(row=7, column=1, pady=(18, 0), sticky="e")
        profile.trace_add("write", update_covariate_state)
        update_covariate_state()


def launch() -> None:
    root = tk.Tk()
    CorrelationDesktopApp(root)
    root.mainloop()
