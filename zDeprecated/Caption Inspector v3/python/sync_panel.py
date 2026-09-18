"""The Sync QC tab of the v3 desktop app.

Two buttons matter here. Tier 1 is the one that gets pressed on every delivery -
seconds, no audio analysis, and it catches the frame-rate mislabels. Tier 2 is
the one that gets pressed when Tier 1 passes but the file still looks wrong, and
it produces the number that goes back to the vendor.
"""

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from caption_timing import SIDECAR_EXTENSIONS
from drift_report import _sample_x, headline_sentence, write_report
from media_probe import VIDEO_EXTENSIONS, ffprobe_path
from sync_check import FAIL, INFO, PASS, WARN, tier1_check, tier2_check
from timecode import RATE_LABELS, format_offset_ms
from transcribe import MODEL_SIZES, faster_whisper_available, install_command, install_faster_whisper


STATUS_COLORS = {
    PASS: "#1f7a4d",
    WARN: "#9a6212",
    FAIL: "#b3261e",
    INFO: "#4a5560",
}

STATUS_WORDS = {PASS: "PASS", WARN: "REVIEW", FAIL: "FAIL", INFO: "NOTE"}

RATE_CHOICES = ["Auto (match the video)"] + [
    f"{label} fps" for _, label in sorted(RATE_LABELS.items(), key=lambda item: item[0])
]


class SyncQCPanel(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=16)

        self.video_path = tk.StringVar()
        self.caption_path = tk.StringVar()
        self.rate_choice = tk.StringVar(value=RATE_CHOICES[0])
        self.tolerance_frames = tk.StringVar(value="2")
        self.tolerance_ms = tk.StringVar(value="200")
        self.model_size = tk.StringVar(value="base")
        self.language = tk.StringVar(value="")
        self.status_text = tk.StringVar(value="Choose a video and a caption file.")
        self.verdict_text = tk.StringVar(value="No check run yet")
        self.headline_text = tk.StringVar(value="")

        self.tier1_result = None
        self.tier2_result = None
        self._busy = False

        self._build()
        self._refresh_capabilities()

    # ------------------------------------------------------------------ layout

    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        inputs = ttk.LabelFrame(self, text="Files", padding=12)
        inputs.grid(row=0, column=0, sticky="ew")
        inputs.columnconfigure(1, weight=1)

        ttk.Label(inputs, text="Video").grid(row=0, column=0, sticky="w")
        ttk.Entry(inputs, textvariable=self.video_path).grid(row=0, column=1, sticky="ew", padx=10)
        ttk.Button(inputs, text="Browse...", command=self._browse_video).grid(row=0, column=2)

        ttk.Label(inputs, text="Captions").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(inputs, textvariable=self.caption_path).grid(row=1, column=1, sticky="ew", padx=10, pady=(10, 0))
        ttk.Button(inputs, text="Browse...", command=self._browse_captions).grid(row=1, column=2, pady=(10, 0))

        options = ttk.LabelFrame(self, text="Options", padding=12)
        options.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        for column in (1, 3, 5):
            options.columnconfigure(column, weight=1)

        ttk.Label(options, text="Read timecodes at").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            options, textvariable=self.rate_choice, values=RATE_CHOICES, state="readonly", width=20
        ).grid(row=0, column=1, sticky="w", padx=(8, 18))

        ttk.Label(options, text="Tail tolerance (frames)").grid(row=0, column=2, sticky="w")
        ttk.Spinbox(options, from_=0, to=120, textvariable=self.tolerance_frames, width=6).grid(
            row=0, column=3, sticky="w", padx=(8, 18)
        )

        ttk.Label(options, text="Offset tolerance (ms)").grid(row=0, column=4, sticky="w")
        ttk.Spinbox(options, from_=0, to=5000, increment=50, textvariable=self.tolerance_ms, width=8).grid(
            row=0, column=5, sticky="w", padx=(8, 0)
        )

        ttk.Label(options, text="Tier 2 model").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Combobox(
            options, textvariable=self.model_size, values=list(MODEL_SIZES), state="readonly", width=20
        ).grid(row=1, column=1, sticky="w", padx=(8, 18), pady=(10, 0))

        ttk.Label(options, text="Language (blank = auto)").grid(row=1, column=2, sticky="w", pady=(10, 0))
        ttk.Entry(options, textvariable=self.language, width=8).grid(
            row=1, column=3, sticky="w", padx=(8, 18), pady=(10, 0)
        )

        actions = ttk.Frame(self)
        actions.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        actions.columnconfigure(4, weight=1)

        self.tier1_button = ttk.Button(actions, text="Run Tier 1 (fast)", command=self._run_tier1)
        self.tier1_button.grid(row=0, column=0)

        self.full_button = ttk.Button(actions, text="Run Full Check (Tier 1 + 2)", command=self._run_full)
        self.full_button.grid(row=0, column=1, padx=(10, 0))

        self.report_button = ttk.Button(actions, text="Save Report...", command=self._save_report, state="disabled")
        self.report_button.grid(row=0, column=2, padx=(10, 0))

        self.install_button = ttk.Button(actions, text="Install faster-whisper", command=self._install_engine)
        self.install_button.grid(row=0, column=3, padx=(10, 0))

        self.progress = ttk.Progressbar(actions, mode="indeterminate", length=160)
        self.progress.grid(row=0, column=5, sticky="e")

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

        detail_frame = ttk.LabelFrame(panes, text="Checks", padding=8)
        detail_frame.rowconfigure(0, weight=1)
        detail_frame.columnconfigure(0, weight=1)
        self.detail_text = ScrolledText(detail_frame, wrap="word", font=("Menlo", 11), height=16)
        self.detail_text.grid(row=0, column=0, sticky="nsew")
        self.detail_text.configure(state="disabled")
        for status, color in STATUS_COLORS.items():
            self.detail_text.tag_configure(status, foreground=color, font=("Menlo", 11, "bold"))
        self.detail_text.tag_configure("detail", foreground="#52616f")
        self.detail_text.tag_configure("section", font=("Menlo", 11, "bold"))
        panes.add(detail_frame, weight=3)

        plot_frame = ttk.LabelFrame(panes, text="Offset across the program (Tier 2)", padding=8)
        plot_frame.rowconfigure(0, weight=1)
        plot_frame.columnconfigure(0, weight=1)
        self.plot_canvas = tk.Canvas(plot_frame, height=180, bg="#fbfaf8", highlightthickness=0)
        self.plot_canvas.grid(row=0, column=0, sticky="nsew")
        self.plot_canvas.bind("<Configure>", lambda _event: self._draw_plot())
        panes.add(plot_frame, weight=2)

    # ------------------------------------------------------------ capabilities

    def _refresh_capabilities(self):
        if not ffprobe_path():
            self.status_text.set(
                "ffprobe was not found on PATH. Install FFmpeg (brew install ffmpeg) to run sync checks."
            )
        if faster_whisper_available():
            self.install_button.grid_remove()
        else:
            self.install_button.grid()

    # -------------------------------------------------------------- file input

    def _browse_video(self):
        pattern = " ".join(f"*{extension}" for extension in VIDEO_EXTENSIONS)
        selected = filedialog.askopenfilename(
            title="Select the delivered video",
            filetypes=[("Video files", pattern), ("All files", "*.*")],
        )
        if selected:
            self.video_path.set(selected)
            self._autofill_captions(selected)

    def _browse_captions(self):
        pattern = " ".join(f"*{extension}" for extension in SIDECAR_EXTENSIONS)
        selected = filedialog.askopenfilename(
            title="Select the caption sidecar",
            filetypes=[("Caption files", pattern), ("All files", "*.*")],
        )
        if selected:
            self.caption_path.set(selected)

    def _autofill_captions(self, video_selection):
        """Deliveries usually ship the sidecar next to the video with the same stem."""
        if self.caption_path.get().strip():
            return
        stem = Path(video_selection).with_suffix("")
        for extension in SIDECAR_EXTENSIONS:
            candidate = Path(f"{stem}{extension}")
            if candidate.exists():
                self.caption_path.set(str(candidate))
                self.status_text.set(f"Found a matching caption file: {candidate.name}")
                return

    # ------------------------------------------------------------------ running

    def _selected_rate_code(self):
        choice = self.rate_choice.get()
        if choice.startswith("Auto"):
            return None
        label = choice.replace(" fps", "").strip()
        for code, code_label in RATE_LABELS.items():
            if code_label == label:
                return code
        return None

    def _int_option(self, variable, default):
        try:
            return int(float(variable.get().strip()))
        except (ValueError, AttributeError):
            return default

    def _snapshot_options(self):
        """Read every Tk variable up front, on the main thread.

        Tk variables belong to the interpreter thread; touching one from a worker
        raises "main thread is not in main loop". The workers below get a plain
        dict instead.
        """
        return {
            "rate_code": self._selected_rate_code(),
            "tolerance_frames": self._int_option(self.tolerance_frames, 2),
            "tolerance_ms": float(self._int_option(self.tolerance_ms, 200)),
            "model_size": self.model_size.get(),
            "language": self.language.get().strip() or None,
        }

    def _validate_inputs(self, need_video=True):
        caption = self.caption_path.get().strip()
        if not caption:
            messagebox.showwarning("Sync QC", "Choose a caption file first.")
            return None

        if not Path(caption).exists():
            messagebox.showerror("Sync QC", "The caption file does not exist.")
            return None

        video = self.video_path.get().strip()
        if need_video and not video:
            messagebox.showwarning(
                "Sync QC",
                "Choose a video file. Without it the check can only verify the caption file's "
                "internal consistency.",
            )
            return None

        if video and not Path(video).exists():
            messagebox.showerror("Sync QC", "The video file does not exist.")
            return None

        return caption, (video or None)

    def _set_busy(self, busy, message=None):
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.tier1_button.config(state=state)
        self.full_button.config(state=state)
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()
        if message:
            self.status_text.set(message)

    def _run_tier1(self):
        selection = self._validate_inputs(need_video=False)
        if not selection or self._busy:
            return

        caption, video = selection
        options = self._snapshot_options()
        self._set_busy(True, "Running Tier 1...")
        self.tier2_result = None

        def work():
            try:
                result = tier1_check(
                    caption,
                    video,
                    rate_code=options["rate_code"],
                    tolerance_frames=options["tolerance_frames"],
                )
            except Exception as error:  # a crash here must not wedge the UI
                self.after(0, self._on_failure, str(error))
                return
            self.after(0, self._on_tier1_done, result)

        threading.Thread(target=work, daemon=True).start()

    def _run_full(self):
        selection = self._validate_inputs(need_video=True)
        if not selection or self._busy:
            return

        caption, video = selection
        if not faster_whisper_available():
            messagebox.showinfo(
                "Sync QC",
                "Tier 2 needs faster-whisper, which is not installed.\n\n"
                "Use the Install faster-whisper button, or run:\n"
                + " ".join(install_command())
                + "\n\nTier 1 works without it.",
            )
            return

        options = self._snapshot_options()
        self._set_busy(True, "Running Tier 1...")

        def work():
            try:
                tier1 = tier1_check(
                    caption,
                    video,
                    rate_code=options["rate_code"],
                    tolerance_frames=options["tolerance_frames"],
                )
            except Exception as error:
                self.after(0, self._on_failure, str(error))
                return

            self.after(0, self._on_tier1_done, tier1, False)

            if tier1.errors:
                self.after(0, self._set_busy, False, "Tier 1 could not run; Tier 2 skipped.")
                return

            def report(message):
                self.after(0, self.status_text.set, message)

            try:
                tier2 = tier2_check(
                    caption,
                    video,
                    rate_code=options["rate_code"] or tier1.rate_code,
                    model_size=options["model_size"],
                    language=options["language"],
                    tolerance_ms=options["tolerance_ms"],
                    max_cues=600,
                    progress=report,
                )
            except Exception as error:
                self.after(0, self._on_failure, str(error))
                return

            self.after(0, self._on_tier2_done, tier1, tier2)

        threading.Thread(target=work, daemon=True).start()

    def _install_engine(self):
        if self._busy:
            return
        if not messagebox.askyesno(
            "Install faster-whisper",
            "This installs faster-whisper into your Python user site so Tier 2 can transcribe "
            "dialogue locally. It downloads roughly 100 MB of packages.\n\nContinue?",
        ):
            return

        self._set_busy(True, "Installing faster-whisper...")
        self.install_button.config(state="disabled")

        def work():
            try:
                install_faster_whisper(progress=lambda message: self.after(0, self.status_text.set, message))
            except Exception as error:
                self.after(0, self._on_failure, str(error))
                self.after(0, self.install_button.config, {"state": "normal"})
                return
            self.after(0, self._on_install_done)

        threading.Thread(target=work, daemon=True).start()

    # ----------------------------------------------------------------- results

    def _on_install_done(self):
        self._set_busy(False, "faster-whisper installed. Tier 2 is available in this session's Python.")
        self.install_button.config(state="normal")
        self._refresh_capabilities()

    def _on_failure(self, message):
        self._set_busy(False, "The check failed.")
        self._write_lines([("FAIL", "Error", message, [])])
        messagebox.showerror("Sync QC", message)

    def _on_tier1_done(self, result, finished=True):
        self.tier1_result = result
        if finished:
            self.tier2_result = None
            self._set_busy(False, "Tier 1 finished.")
        self._render()

    def _on_tier2_done(self, tier1, tier2):
        self.tier1_result = tier1
        self.tier2_result = tier2
        self._set_busy(False, "Full check finished.")
        self._render()

    def _render(self):
        tier1 = self.tier1_result
        tier2 = self.tier2_result
        if not tier1:
            return

        verdicts = [result.verdict for result in (tier1, tier2) if result]
        order = {INFO: 0, PASS: 1, WARN: 2, FAIL: 3}
        verdict = max(verdicts, key=lambda value: order[value]) if verdicts else INFO

        self.verdict_text.set(STATUS_WORDS[verdict])
        self.verdict_label.config(bg=STATUS_COLORS[verdict])
        self.headline_text.set(headline_sentence(tier1, tier2))
        self.report_button.config(state="normal")

        rows = []
        if tier1.errors:
            rows.extend(("FAIL", "Error", error, []) for error in tier1.errors)

        if tier1.checks:
            rows.append((None, "TIER 1 - TIMECODE AND FRAME RATE MATH", None, []))
            rows.extend((check.status, check.name, check.headline, check.detail) for check in tier1.checks)

        if tier2:
            rows.append((None, "TIER 2 - AUDIO-VERIFIED SYNC", None, []))
            rows.extend(("FAIL", "Error", error, []) for error in tier2.errors)
            rows.extend((check.status, check.name, check.headline, check.detail) for check in tier2.checks)

            if tier2.tier2:
                summary = tier2.tier2
                drift = summary["drift"]
                rows.append(
                    (
                        None,
                        "MEASUREMENTS",
                        None,
                        [
                            f"Cues matched to dialogue: {summary['matched']} of {summary['cues']}",
                            f"Median offset: {format_offset_ms(summary['median_offset'])}",
                            f"Drift rate: {drift['slope_ms_per_minute']:+.1f} ms/minute "
                            f"({drift['slope_ms_per_minute'] * 10:+.0f} ms per 10 minutes)",
                            f"Accumulated across program: {format_offset_ms(drift['total_drift_seconds'])}",
                            f"Implied timing ratio: {drift['implied_ratio']:.5f}",
                            f"Fit r-squared: {drift['r_squared']:.3f}",
                        ],
                    )
                )

        self._write_lines(rows)
        self._draw_plot()

    def _write_lines(self, rows):
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", tk.END)

        for status, name, headline, detail in rows:
            if status is None:
                self.detail_text.insert(tk.END, f"\n{name}\n", "section")
                for line in detail:
                    self.detail_text.insert(tk.END, f"    {line}\n", "detail")
                continue

            self.detail_text.insert(tk.END, f"[{STATUS_WORDS.get(status, status):<6}] ", status)
            self.detail_text.insert(tk.END, f"{name}: {headline}\n")
            for line in detail:
                self.detail_text.insert(tk.END, f"         {line}\n", "detail")

        self.detail_text.configure(state="disabled")

    def _draw_plot(self):
        """Scatter of offset against timeline position, drawn on the canvas."""
        canvas = self.plot_canvas
        canvas.delete("all")

        summary = self.tier2_result.tier2 if self.tier2_result else None
        samples = (summary or {}).get("samples") or []
        width = canvas.winfo_width() or 700
        height = canvas.winfo_height() or 180

        if len(samples) < 2:
            canvas.create_text(
                width / 2,
                height / 2,
                text="Run a full check to plot caption offset against position in the program.",
                fill="#8a949d",
                font=("Helvetica", 11),
            )
            return

        pad_left, pad_right, pad_top, pad_bottom = 58, 14, 14, 26
        plot_width = max(10, width - pad_left - pad_right)
        plot_height = max(10, height - pad_top - pad_bottom)

        xs = [_sample_x(sample) for sample in samples]
        ys = [sample["offset"] * 1000.0 for sample in samples]
        x_min, x_max = min(xs), max(xs)
        if x_max <= x_min:
            return

        span = max(abs(min(ys)), abs(max(ys)), 120.0) * 1.15

        def to_x(value):
            return pad_left + (value - x_min) / (x_max - x_min) * plot_width

        def to_y(value):
            return pad_top + (span - value) / (2 * span) * plot_height

        canvas.create_rectangle(
            pad_left, pad_top, pad_left + plot_width, pad_top + plot_height, outline="#dfd9d2"
        )

        for step in range(5):
            value = span - step * (2 * span) / 4.0
            y = to_y(value)
            zero = abs(value) < 1e-6
            canvas.create_line(
                pad_left, y, pad_left + plot_width, y,
                fill="#17222c" if zero else "#e8e3dc",
                dash=() if zero else (3, 3),
            )
            canvas.create_text(pad_left - 8, y, text=f"{value:+.0f}", anchor="e", fill="#7a848d", font=("Menlo", 9))

        for sample in samples:
            x = to_x(_sample_x(sample))
            y = to_y(max(-span, min(span, sample["offset"] * 1000.0)))
            canvas.create_oval(x - 2, y - 2, x + 2, y + 2, fill="#4a5560", outline="")

        drift = summary["drift"]
        start_y = to_y(max(-span, min(span, (drift["intercept"] + drift["slope"] * x_min) * 1000.0)))
        end_y = to_y(max(-span, min(span, (drift["intercept"] + drift["slope"] * x_max) * 1000.0)))
        canvas.create_line(to_x(x_min), start_y, to_x(x_max), end_y, fill="#c25a1e", width=2)

        canvas.create_text(
            pad_left + 6,
            pad_top + 10,
            text=f"{drift['slope_ms_per_minute']:+.0f} ms/min",
            anchor="w",
            fill="#c25a1e",
            font=("Helvetica", 10, "bold"),
        )
        canvas.create_text(
            width / 2,
            height - 8,
            text="Position in program (offset in ms; positive means captions lead the dialogue)",
            fill="#8a949d",
            font=("Helvetica", 9),
        )

    # ------------------------------------------------------------------ export

    def _save_report(self):
        if not self.tier1_result:
            return

        default_name = Path(self.caption_path.get()).stem + "-sync-report.html"
        selected = filedialog.asksaveasfilename(
            title="Save sync QC report",
            defaultextension=".html",
            initialfile=default_name,
            filetypes=[("HTML report", "*.html"), ("Text report", "*.txt"), ("JSON", "*.json")],
        )
        if not selected:
            return

        try:
            written = write_report(selected, self.tier1_result, self.tier2_result)
        except OSError as error:
            messagebox.showerror("Sync QC", f"Could not write the report: {error}")
            return

        self.status_text.set(f"Report saved to {written}")
