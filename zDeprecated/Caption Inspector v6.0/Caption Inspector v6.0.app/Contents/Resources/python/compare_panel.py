"""The Compare Subtitles tab.

Two files for the same episode, and the question "what changed?". The panel is
built around the answer being *short*: a delivery with four script edits should
show four lines, not nine hundred, so the differences pane is filtered by what
kind of change it is and defaults to hiding the ones nobody asked about.

Nothing here asks for a frame rate. Both files are read for the rate they are
actually in, and it is reported back rather than requested - a mislabelled rate
is a finding, not a setting.
"""

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from cancellation import CancelToken, OperationCancelled
from caption_timing import SIDECAR_EXTENSIONS
from inspection_support import DECODED_TYPES
from subtitle_compare import (
    DEFAULT_TOLERANCE_MS,
    format_baseline,
    FORMATTING,
    ONLY_A,
    ONLY_B,
    TEXT,
    TIMING,
    WORDING,
    compare_subtitles,
    write_comparison_report,
)
from sync_panel import STATUS_COLORS, STATUS_WORDS
from sync_check import FAIL, INFO, PASS, WARN
from subtitle_formats import format_label
from timecode import format_offset_ms, format_seconds


# Sidecars plus the containers, because "compare the SCC against the SRT" and
# "compare what is actually in the MP4 against the sidecar" are the same job.
COMPARABLE_EXTENSIONS = tuple(SIDECAR_EXTENSIONS) + tuple(
    f".{name}" for name in DECODED_TYPES if f".{name}" not in SIDECAR_EXTENSIONS
)
CAPTION_PATTERN = " ".join(f"*{extension}" for extension in COMPARABLE_EXTENSIONS)

# The filters, and which difference kinds each one lets through. "Everything"
# is deliberately not the default: a file re-exported from another tool changes
# every smart quote in the programme, and that buries the four real edits.
FILTERS = {
    "Script changes only": (WORDING,),
    "Script and timing": (WORDING, TIMING, ONLY_A, ONLY_B),
    "Everything": (WORDING, FORMATTING, TIMING, ONLY_A, ONLY_B),
    "Added and removed only": (ONLY_A, ONLY_B),
}
DEFAULT_FILTER = "Script and timing"

_KIND_TAGS = {
    TIMING: "timing",
    ONLY_A: "only_a",
    ONLY_B: "only_b",
}

_KIND_WORDS = {
    TIMING: "RETIMED",
    ONLY_A: "ONLY IN A",
    ONLY_B: "ONLY IN B",
}


class ComparePanel(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=16)

        self.path_a = tk.StringVar()
        self.path_b = tk.StringVar()
        self.tolerance_ms = tk.StringVar(value=str(DEFAULT_TOLERANCE_MS))
        self.filter_choice = tk.StringVar(value=DEFAULT_FILTER)
        self.status_text = tk.StringVar(value="Choose two subtitle files for the same episode.")
        self.verdict_text = tk.StringVar(value="No comparison run yet")
        self.headline_text = tk.StringVar(value="")
        self.rate_a_text = tk.StringVar(value="")
        self.rate_b_text = tk.StringVar(value="")

        self.result = None
        self._busy = False
        self._cancel = None

        self._build()

    # ------------------------------------------------------------------ layout

    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        inputs = ttk.LabelFrame(self, text="Files", padding=12)
        inputs.grid(row=0, column=0, sticky="ew")
        inputs.columnconfigure(1, weight=1)

        ttk.Label(inputs, text="File A").grid(row=0, column=0, sticky="w")
        ttk.Entry(inputs, textvariable=self.path_a).grid(row=0, column=1, sticky="ew", padx=10)
        ttk.Button(inputs, text="Browse...", command=lambda: self._browse(self.path_a, "A")).grid(
            row=0, column=2
        )
        ttk.Label(inputs, textvariable=self.rate_a_text, foreground="#52616f").grid(
            row=1, column=1, sticky="w", padx=10
        )

        ttk.Label(inputs, text="File B").grid(row=2, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(inputs, textvariable=self.path_b).grid(row=2, column=1, sticky="ew", padx=10, pady=(10, 0))
        ttk.Button(inputs, text="Browse...", command=lambda: self._browse(self.path_b, "B")).grid(
            row=2, column=2, pady=(10, 0)
        )
        ttk.Label(inputs, textvariable=self.rate_b_text, foreground="#52616f").grid(
            row=3, column=1, sticky="w", padx=10
        )

        ttk.Label(
            inputs,
            text="Differences are described as B relative to A, so put the approved file in A.",
            foreground="#52616f",
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))

        options = ttk.LabelFrame(self, text="Options", padding=12)
        options.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        options.columnconfigure(4, weight=1)

        ttk.Label(options, text="Treat cues as simultaneous within (ms)").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(
            options, from_=0, to=5000, increment=50, textvariable=self.tolerance_ms, width=8
        ).grid(row=0, column=1, sticky="w", padx=(8, 18))

        ttk.Label(options, text="Show").grid(row=0, column=2, sticky="w")
        filter_box = ttk.Combobox(
            options,
            textvariable=self.filter_choice,
            values=list(FILTERS),
            state="readonly",
            width=24,
        )
        filter_box.grid(row=0, column=3, sticky="w", padx=(8, 0))
        filter_box.bind("<<ComboboxSelected>>", lambda _event: self._render_differences())

        actions = ttk.Frame(self)
        actions.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        actions.columnconfigure(3, weight=1)

        self.compare_button = ttk.Button(actions, text="Compare", command=self._run_compare)
        self.compare_button.grid(row=0, column=0)

        self.stop_button = ttk.Button(actions, text="Stop", command=self._stop, state="disabled")
        self.stop_button.grid(row=0, column=1, padx=(10, 0))

        self.report_button = ttk.Button(
            actions, text="Save Report...", command=self._save_report, state="disabled"
        )
        self.report_button.grid(row=0, column=2, padx=(10, 0))

        self.progress = ttk.Progressbar(actions, mode="indeterminate", length=160)
        self.progress.grid(row=0, column=4, sticky="e")

        results = ttk.Frame(self)
        results.grid(row=3, column=0, sticky="nsew", pady=(14, 0))
        results.columnconfigure(0, weight=1)
        results.rowconfigure(2, weight=1)

        banner = ttk.Frame(results)
        banner.grid(row=0, column=0, sticky="ew")
        banner.columnconfigure(1, weight=1)

        self.verdict_label = tk.Label(
            banner,
            textvariable=self.verdict_text,
            font=("Helvetica", 13, "bold"),
            fg="#ffffff",
            bg=STATUS_COLORS[INFO],
            padx=14,
            pady=6,
        )
        self.verdict_label.grid(row=0, column=0, sticky="w")
        ttk.Label(banner, textvariable=self.headline_text, wraplength=760, justify="left").grid(
            row=0, column=1, sticky="w", padx=(14, 0)
        )

        ttk.Label(results, textvariable=self.status_text, foreground="#52616f").grid(
            row=1, column=0, sticky="w", pady=(8, 6)
        )

        panes = ttk.PanedWindow(results, orient="vertical")
        panes.grid(row=2, column=0, sticky="nsew")

        check_frame = ttk.LabelFrame(panes, text="Checks", padding=8)
        check_frame.rowconfigure(0, weight=1)
        check_frame.columnconfigure(0, weight=1)
        self.check_text = ScrolledText(check_frame, wrap="word", font=("Menlo", 11), height=10)
        self.check_text.grid(row=0, column=0, sticky="nsew")
        self.check_text.configure(state="disabled")
        for status, color in STATUS_COLORS.items():
            self.check_text.tag_configure(status, foreground=color, font=("Menlo", 11, "bold"))
        self.check_text.tag_configure("detail", foreground="#52616f")
        panes.add(check_frame, weight=2)

        diff_frame = ttk.LabelFrame(panes, text="Differences", padding=8)
        diff_frame.rowconfigure(0, weight=1)
        diff_frame.columnconfigure(0, weight=1)
        self.diff_text = ScrolledText(diff_frame, wrap="word", font=("Menlo", 11), height=16)
        self.diff_text.grid(row=0, column=0, sticky="nsew")
        self.diff_text.configure(state="disabled")
        self.diff_text.tag_configure("heading", font=("Menlo", 11, "bold"))
        self.diff_text.tag_configure("when", foreground="#52616f")
        self.diff_text.tag_configure("timing", foreground=STATUS_COLORS[INFO], font=("Menlo", 11, "bold"))
        self.diff_text.tag_configure("only_a", foreground=STATUS_COLORS[FAIL], font=("Menlo", 11, "bold"))
        self.diff_text.tag_configure("only_b", foreground=STATUS_COLORS[PASS], font=("Menlo", 11, "bold"))
        self.diff_text.tag_configure("wording", foreground=STATUS_COLORS[WARN], font=("Menlo", 11, "bold"))
        self.diff_text.tag_configure("formatting", foreground="#52616f", font=("Menlo", 11, "bold"))
        # Struck through as well as coloured: colour alone is not a difference
        # everyone can see, and a red word next to a green one is exactly the
        # pair that colour blindness collapses.
        self.diff_text.tag_configure("removed", foreground="#b3261e", overstrike=True)
        self.diff_text.tag_configure("added", foreground="#1f7a4d")
        panes.add(diff_frame, weight=3)

    # -------------------------------------------------------------- file input

    def _browse(self, variable, side):
        selected = filedialog.askopenfilename(
            title=f"Select subtitle file {side}",
            filetypes=[("Subtitle and caption files", CAPTION_PATTERN), ("All files", "*.*")],
        )
        if not selected:
            return
        variable.set(selected)
        self._show_rate(selected, side)
        if side == "A" and not self.path_b.get().strip():
            self._suggest_counterpart(selected)

    def _suggest_counterpart(self, selection):
        """A second version usually sits beside the first with a suffix on the stem."""
        source = Path(selection)
        for candidate in sorted(source.parent.glob(f"{source.stem}*")):
            if candidate == source or candidate.suffix.lower() not in COMPARABLE_EXTENSIONS:
                continue
            self.path_b.set(str(candidate))
            self._show_rate(str(candidate), "B")
            self.status_text.set(f"Found a likely second version: {candidate.name}")
            return

    def _show_rate(self, path, side):
        """Read the frame rate off the file and say so, without being asked."""
        variable = self.rate_a_text if side == "A" else self.rate_b_text
        variable.set("Reading...")

        def work():
            from frame_rate_detect import detect_frame_rate

            try:
                detection = detect_frame_rate(path)
                text = f"{format_label(detection.kind)} - {detection.headline()}"
            except Exception as error:
                text = str(error)
            self.after(0, variable.set, text)

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ running

    def _tolerance(self):
        try:
            return max(0, int(float(self.tolerance_ms.get().strip())))
        except (ValueError, AttributeError):
            return DEFAULT_TOLERANCE_MS

    def _validate(self):
        first, second = self.path_a.get().strip(), self.path_b.get().strip()
        if not first or not second:
            messagebox.showwarning("Compare Subtitles", "Choose both files first.")
            return None
        for path in (first, second):
            if not Path(path).exists():
                messagebox.showerror("Compare Subtitles", f"{Path(path).name} does not exist.")
                return None
        if Path(first).resolve() == Path(second).resolve():
            messagebox.showwarning("Compare Subtitles", "Both boxes point at the same file.")
            return None
        return first, second

    def _set_busy(self, busy, message=None):
        self._busy = busy
        self.compare_button.config(state="disabled" if busy else "normal")
        self.stop_button.config(state="normal" if busy else "disabled")
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()
        if message:
            self.status_text.set(message)

    def _run_compare(self):
        selection = self._validate()
        if not selection or self._busy:
            return

        first, second = selection
        tolerance = self._tolerance()
        self._cancel = CancelToken()
        token = self._cancel
        self._set_busy(True, "Comparing...")
        self.report_button.config(state="disabled")

        def work():
            try:
                result = compare_subtitles(first, second, tolerance_ms=tolerance, cancel=token)
            except OperationCancelled:
                self.after(0, self._on_stopped)
                return
            except Exception as error:  # a crash here must not wedge the tab
                self.after(0, self._on_failure, str(error))
                return
            self.after(0, self._on_done, result)

        threading.Thread(target=work, daemon=True).start()

    def _stop(self):
        if self._cancel:
            self._cancel.cancel()

    def _on_stopped(self, message="Stopped."):
        self._set_busy(False, message)

    def _on_failure(self, message):
        self._set_busy(False, "The comparison failed.")
        self._set_verdict(FAIL, "Comparison failed")
        self.headline_text.set(message)
        messagebox.showerror("Compare Subtitles", message)

    def _on_done(self, result):
        self.result = result
        self._set_busy(False, "Comparison finished.")

        if result.errors:
            self._set_verdict(FAIL, "Could not read")
            self.headline_text.set(" ".join(result.errors))
            self._render_checks()
            self._render_differences()
            return

        self._set_verdict(result.verdict, STATUS_WORDS[result.verdict])
        self.headline_text.set(self._headline(result))
        if result.file_a and result.file_a.detection:
            self.rate_a_text.set(
                f"{format_label(result.file_a.kind)} - {result.file_a.detection.headline()}"
            )
        if result.file_b and result.file_b.detection:
            self.rate_b_text.set(
                f"{format_label(result.file_b.kind)} - {result.file_b.detection.headline()}"
            )
        self._render_checks()
        self._render_differences()
        self.report_button.config(state="normal")

    def _headline(self, result):
        stats = result.stats
        if not result.differences():
            return "The two files are identical."
        parts = []
        if stats.get("wording_changed"):
            parts.append(f"{stats['wording_changed']} script changes")
        if stats.get("formatting_changed"):
            parts.append(f"{stats['formatting_changed']} formatting-only changes")
        if stats.get("retimed"):
            parts.append(f"{stats['retimed']} retimed")
        if stats.get("only_in_a"):
            parts.append(f"{stats['only_in_a']} only in A")
        if stats.get("only_in_b"):
            parts.append(f"{stats['only_in_b']} only in B")
        return f"{stats.get('identical', 0)} cues identical; " + ", ".join(parts) + "."

    def _set_verdict(self, status, word):
        self.verdict_text.set(word)
        self.verdict_label.config(bg=STATUS_COLORS.get(status, STATUS_COLORS[INFO]))

    # ---------------------------------------------------------------- rendering

    def _write(self, widget, chunks):
        widget.configure(state="normal")
        widget.delete("1.0", tk.END)
        for text, tag in chunks:
            widget.insert(tk.END, text, tag or ())
        widget.configure(state="disabled")

    def _render_checks(self):
        if not self.result:
            return
        chunks = []
        for error in self.result.errors:
            chunks.append((f"[{STATUS_WORDS[FAIL]}] ", FAIL))
            chunks.append((f"{error}\n", None))
        for check in self.result.checks:
            chunks.append((f"[{STATUS_WORDS[check.status]}] ", check.status))
            chunks.append((f"{check.name}: {check.headline}\n", None))
            for detail in check.detail:
                chunks.append((f"        {detail}\n", "detail"))
        self._write(self.check_text, chunks or [("No checks were run.\n", None)])

    def _render_differences(self):
        if not self.result:
            return

        allowed = FILTERS.get(self.filter_choice.get(), FILTERS[DEFAULT_FILTER])
        shown = [diff for diff in self.result.differences() if self._diff_key(diff) in allowed]

        if not shown:
            total = len(self.result.differences())
            message = (
                "The two files are identical.\n"
                if not total
                else f"No differences of this kind. {total} differences are hidden by the filter.\n"
            )
            self._write(self.diff_text, [(message, "detail")])
            return

        chunks = []
        for diff in shown:
            chunks.extend(self._difference_chunks(diff))
        self._write(self.diff_text, chunks)

    def _diff_key(self, diff):
        """What the filter matches on - a text change splits by what changed."""
        if diff.kind == TEXT:
            return diff.change
        return diff.kind

    def _difference_chunks(self, diff):
        key = self._diff_key(diff)
        cue = diff.cue_a or diff.cue_b
        reference = " / ".join(
            part
            for part in (
                f"A#{diff.index_a}" if diff.index_a else None,
                f"B#{diff.index_b}" if diff.index_b else None,
            )
            if part
        )

        if diff.kind == TEXT:
            word = "SCRIPT CHANGE" if diff.change == WORDING else "FORMATTING"
            tag = "wording" if diff.change == WORDING else "formatting"
        else:
            word = _KIND_WORDS[diff.kind]
            tag = _KIND_TAGS[diff.kind]

        chunks = [(f"{word}  ", tag), (f"{reference} at {format_seconds(cue.start)}\n", "when")]

        if diff.kind == ONLY_A:
            chunks.append((f"  - {diff.cue_a.text}\n\n", "removed"))
            return chunks
        if diff.kind == ONLY_B:
            chunks.append((f"  + {diff.cue_b.text}\n\n", "added"))
            return chunks
        if diff.kind == TIMING:
            shift = format_offset_ms(diff.relative_start)
            if diff.relative_end is not None:
                shift += f", out {format_offset_ms(diff.relative_end)}"
            if abs(diff.baseline) > 0.001:
                shift += f" (relative to the file-wide {format_baseline(diff.baseline)})"
            chunks.append((f"  in {shift}\n", "when"))
            chunks.append((f"  = {diff.cue_a.text}\n\n", None))
            return chunks

        if diff.relative_start is not None and abs(diff.relative_start) > 0.001:
            chunks.append((f"  in {format_offset_ms(diff.relative_start)}\n", "when"))
        chunks.append(("  ", None))
        for op, text in diff.words:
            if not text:
                continue
            if op == "equal":
                chunks.append((text + " ", None))
            elif op == "delete":
                chunks.append((text + " ", "removed"))
            else:
                chunks.append((text + " ", "added"))
        chunks.append(("\n\n", None))
        return chunks

    # ------------------------------------------------------------------ report

    def _save_report(self):
        if not self.result:
            return
        target = filedialog.asksaveasfilename(
            title="Save comparison report",
            defaultextension=".html",
            filetypes=[("HTML report", "*.html"), ("Text report", "*.txt"), ("JSON", "*.json")],
        )
        if not target:
            return
        try:
            write_comparison_report(target, self.result)
        except OSError as error:
            messagebox.showerror("Compare Subtitles", f"Could not write the report: {error}")
            return
        self.status_text.set(f"Report written to {Path(target).name}.")
