"""The Transcript & Match tab.

Takes a caption or subtitle file, turns it into a transcript, and - when a video
is supplied - marks every line with how far it sits from the words actually
spoken. The export button writes the same thing as text, Markdown, CSV, JSON,
HTML, or back out as SRT/WebVTT.

The line list is the product here, so it gets the room: a colour per outcome, a
filter to show only the lines that need attention, and a Stop button, because
transcribing a feature is a coffee-length wait and changing your mind about it
should not mean force-quitting the app.
"""

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from cancellation import CancelToken, OperationCancelled
from caption_timing import SIDECAR_EXTENSIONS
from model_picker import ModelPicker
from media_probe import VIDEO_EXTENSIONS, ffprobe_path
from timecode import RATE_LABELS, format_offset_ms
from transcript_export import (
    MATCHED,
    NOT_DIALOGUE,
    OFF,
    STATUS_WORDS,
    TOO_SHORT,
    UNCHECKED,
    UNMATCHED,
    UNTESTABLE,
    format_for_path,
    render,
    write_transcript,
)
from transcript_check import build_transcript
from transcribe import MODEL_SIZES, faster_whisper_available, install_command


STATUS_COLORS = {
    MATCHED: "#1f7a4d",
    OFF: "#9a6212",
    UNMATCHED: "#b3261e",
    UNCHECKED: "#4a5560",
    # Grey, like "not checked": these are not findings.
    NOT_DIALOGUE: "#8a949d",
    TOO_SHORT: "#8a949d",
}

RATE_CHOICES = ["Auto (match the video)"] + [
    f"{label} fps" for _, label in sorted(RATE_LABELS.items(), key=lambda item: item[0])
]

# (label, format key, default extension) for the export dialog.
EXPORT_CHOICES = [
    ("Timecoded transcript (.txt)", "text", ".txt"),
    ("Reading transcript, no timecodes (.txt)", "prose", ".txt"),
    ("Markdown table (.md)", "markdown", ".md"),
    ("Spreadsheet (.csv)", "csv", ".csv"),
    ("Machine-readable (.json)", "json", ".json"),
    ("Web page (.html)", "html", ".html"),
    ("SubRip subtitles (.srt)", "srt", ".srt"),
    ("WebVTT subtitles (.vtt)", "vtt", ".vtt"),
]

CAPTION_PATTERN = " ".join(f"*{extension}" for extension in SIDECAR_EXTENSIONS)


class TranscriptPanel(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=16)

        self.caption_path = tk.StringVar()
        self.video_path = tk.StringVar()
        self.rate_choice = tk.StringVar(value=RATE_CHOICES[0])
        self.model_size = tk.StringVar(value="base")
        self.language = tk.StringVar(value="")
        self.tolerance_ms = tk.StringVar(value="200")
        self.export_choice = tk.StringVar(value=EXPORT_CHOICES[0][0])
        self.only_problems = tk.BooleanVar(value=False)
        self.status_text = tk.StringVar(value="Choose a caption or subtitle file to transcribe.")
        self.verdict_text = tk.StringVar(value="No transcript yet")
        self.counts_text = tk.StringVar(value="")

        self.result = None
        self._busy = False
        self._cancel = None

        self._build()
        self._refresh_capabilities()

    # ------------------------------------------------------------------ layout

    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        inputs = ttk.LabelFrame(self, text="Files", padding=12)
        inputs.grid(row=0, column=0, sticky="ew")
        inputs.columnconfigure(1, weight=1)

        ttk.Label(inputs, text="Captions").grid(row=0, column=0, sticky="w")
        ttk.Entry(inputs, textvariable=self.caption_path).grid(row=0, column=1, sticky="ew", padx=10)
        ttk.Button(inputs, text="Browse...", command=self._browse_captions).grid(row=0, column=2)

        ttk.Label(inputs, text="Video (optional)").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(inputs, textvariable=self.video_path).grid(
            row=1, column=1, sticky="ew", padx=10, pady=(10, 0)
        )
        ttk.Button(inputs, text="Browse...", command=self._browse_video).grid(
            row=1, column=2, pady=(10, 0)
        )

        ttk.Label(
            inputs,
            text="Without a video the transcript is still produced; the match column reads "
            "\"not checked\".",
            foreground="#6b7681",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))

        options = ttk.LabelFrame(self, text="Options", padding=12)
        options.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        for column in (1, 3, 5):
            options.columnconfigure(column, weight=1)

        ttk.Label(options, text="Read timecodes at").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            options, textvariable=self.rate_choice, values=RATE_CHOICES, state="readonly", width=20
        ).grid(row=0, column=1, sticky="w", padx=(8, 18))

        ttk.Label(options, text="Model").grid(row=0, column=2, sticky="w")
        self.model_picker = ModelPicker(options, self.model_size, width=18)
        self.model_picker.grid(row=0, column=3, sticky="w", padx=(8, 18))

        ttk.Label(options, text="Match tolerance (ms)").grid(row=0, column=4, sticky="w")
        ttk.Spinbox(
            options, from_=0, to=5000, increment=50, textvariable=self.tolerance_ms, width=8
        ).grid(row=0, column=5, sticky="w", padx=(8, 0))

        ttk.Label(options, text="Language (blank = auto)").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(options, textvariable=self.language, width=8).grid(
            row=1, column=1, sticky="w", padx=(8, 18), pady=(10, 0)
        )

        ttk.Checkbutton(
            options,
            text="Show only lines that need attention",
            variable=self.only_problems,
            command=self._render,
        ).grid(row=1, column=2, columnspan=2, sticky="w", pady=(10, 0), padx=(8, 0))

        actions = ttk.Frame(self)
        actions.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        actions.columnconfigure(5, weight=1)

        self.build_button = ttk.Button(
            actions, text="Build Transcript", command=self._run
        )
        self.build_button.grid(row=0, column=0)

        # Stop is created disabled and enabled for the duration of a run, so it
        # is never a button that looks live but does nothing.
        self.stop_button = ttk.Button(
            actions, text="Stop", command=self._stop, state="disabled"
        )
        self.stop_button.grid(row=0, column=1, padx=(10, 0))

        ttk.Combobox(
            actions,
            textvariable=self.export_choice,
            values=[label for label, _, _ in EXPORT_CHOICES],
            state="readonly",
            width=34,
        ).grid(row=0, column=2, padx=(18, 0))

        self.export_button = ttk.Button(
            actions, text="Export...", command=self._export, state="disabled"
        )
        self.export_button.grid(row=0, column=3, padx=(10, 0))

        self.copy_button = ttk.Button(
            actions, text="Copy", command=self._copy, state="disabled"
        )
        self.copy_button.grid(row=0, column=4, padx=(10, 0))

        self.progress = ttk.Progressbar(actions, mode="indeterminate", length=160)
        self.progress.grid(row=0, column=6, sticky="e")

        results = ttk.Frame(self)
        results.grid(row=3, column=0, sticky="nsew", pady=(14, 0))
        results.columnconfigure(0, weight=1)
        results.rowconfigure(2, weight=1)

        banner = ttk.Frame(results)
        banner.grid(row=0, column=0, sticky="ew")
        banner.columnconfigure(1, weight=1)

        self.verdict_label = tk.Label(
            banner,
            textvariable=self.counts_text,
            font=("Helvetica", 13, "bold"),
            fg="#ffffff",
            bg=STATUS_COLORS[UNCHECKED],
            padx=14,
            pady=6,
        )
        self.verdict_label.grid(row=0, column=0, sticky="w")

        ttk.Label(banner, textvariable=self.verdict_text, wraplength=760, justify="left").grid(
            row=0, column=1, sticky="w", padx=(14, 0)
        )

        ttk.Label(results, textvariable=self.status_text, foreground="#52616f").grid(
            row=1, column=0, sticky="w", pady=(8, 6)
        )

        body = ttk.LabelFrame(results, text="Transcript", padding=8)
        body.grid(row=2, column=0, sticky="nsew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        self.transcript_text = ScrolledText(body, wrap="word", font=("Menlo", 11), height=20)
        self.transcript_text.grid(row=0, column=0, sticky="nsew")
        self.transcript_text.configure(state="disabled")
        for status, color in STATUS_COLORS.items():
            self.transcript_text.tag_configure(status, foreground=color, font=("Menlo", 11, "bold"))
        self.transcript_text.tag_configure("meta", foreground="#8a949d")
        self.transcript_text.tag_configure("speaker", font=("Menlo", 11, "bold"))
        self.transcript_text.tag_configure("body", foreground="#1c2024")
        self.transcript_text.tag_configure("note", foreground="#9a6212")

    # ------------------------------------------------------------ capabilities

    def _refresh_capabilities(self):
        if not ffprobe_path():
            self.status_text.set(
                "ffprobe was not found. A transcript can still be built from the caption file; "
                "checking it against a video needs FFmpeg."
            )

    # -------------------------------------------------------------- file input

    def _browse_captions(self):
        selected = filedialog.askopenfilename(
            title="Select a caption or subtitle file",
            filetypes=[
                ("Caption and subtitle files", CAPTION_PATTERN),
                ("Video with embedded captions", "*.mov *.mp4 *.ts *.mpg"),
                ("All files", "*.*"),
            ],
        )
        if selected:
            self.caption_path.set(selected)
            self._autofill_video(selected)

    def _browse_video(self):
        pattern = " ".join(f"*{extension}" for extension in VIDEO_EXTENSIONS)
        selected = filedialog.askopenfilename(
            title="Select the video to check against",
            filetypes=[("Video files", pattern), ("All files", "*.*")],
        )
        if selected:
            self.video_path.set(selected)

    def _autofill_video(self, caption_selection):
        """Deliveries usually put the video next to the sidecar with the same stem."""
        if self.video_path.get().strip():
            return
        stem = Path(caption_selection).with_suffix("")
        for extension in VIDEO_EXTENSIONS:
            candidate = Path(f"{stem}{extension}")
            if candidate.exists():
                self.video_path.set(str(candidate))
                self.status_text.set(f"Found a matching video: {candidate.name}")
                return

    # ------------------------------------------------------------------ running

    def _int_option(self, variable, default):
        try:
            return int(float(variable.get().strip()))
        except (ValueError, AttributeError):
            return default

    def _selected_rate_code(self):
        choice = self.rate_choice.get()
        if choice.startswith("Auto"):
            return None
        label = choice.replace(" fps", "").strip()
        for code, code_label in RATE_LABELS.items():
            if code_label == label:
                return code
        return None

    def _snapshot_options(self):
        """Read the Tk variables on the main thread; workers get a plain dict."""
        return {
            "caption": self.caption_path.get().strip(),
            "video": self.video_path.get().strip() or None,
            "rate_code": self._selected_rate_code(),
            "model_size": self.model_size.get(),
            "language": self.language.get().strip() or None,
            "tolerance_ms": float(self._int_option(self.tolerance_ms, 200)),
        }

    def _set_busy(self, busy, message=None):
        self._busy = busy
        self.build_button.config(state="disabled" if busy else "normal")
        self.stop_button.config(state="normal" if busy else "disabled")
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()
        if message:
            self.status_text.set(message)

    def _run(self):
        if self._busy:
            return

        options = self._snapshot_options()
        if not options["caption"]:
            messagebox.showwarning("Transcript", "Choose a caption or subtitle file first.")
            return
        if not Path(options["caption"]).exists():
            messagebox.showerror("Transcript", "The caption file does not exist.")
            return
        if options["video"] and not Path(options["video"]).exists():
            messagebox.showerror("Transcript", "The video file does not exist.")
            return

        if options["video"] and not self.model_picker.is_ready():
            if not messagebox.askyesno(
                "Transcript",
                self.model_picker.explain()
                + "\n\nBuild the transcript from the caption file alone?\n"
                "The lines will be produced, marked \"not checked\".",
            ):
                return
            options["video"] = None

        if options["video"] and not faster_whisper_available():
            if not messagebox.askyesno(
                "Transcript",
                "Checking the transcript against the video needs faster-whisper, which is not "
                "installed.\n\nBuild the transcript without checking it?\n\n"
                "To install it, use the Sync QC tab's install button, or run:\n"
                + " ".join(install_command()),
            ):
                return
            options["video"] = None

        self._cancel = CancelToken()
        token = self._cancel
        self._set_busy(True, "Reading caption cues...")

        def report(message):
            self.after(0, self.status_text.set, message)

        def work():
            try:
                result = build_transcript(
                    options["caption"],
                    options["video"],
                    rate_code=options["rate_code"],
                    model_size=options["model_size"],
                    language=options["language"],
                    tolerance_ms=options["tolerance_ms"],
                    progress=report,
                    cancel=token,
                )
            except OperationCancelled:
                self.after(0, self._on_stopped)
                return
            except Exception as error:  # a crash here must not wedge the UI
                self.after(0, self._on_failure, str(error))
                return
            self.after(0, self._on_done, result)

        threading.Thread(target=work, daemon=True).start()

    def _stop(self):
        if not self._busy or self._cancel is None:
            return
        self._cancel.cancel()
        self.stop_button.config(state="disabled")
        self.status_text.set("Stopping...")

    # ----------------------------------------------------------------- results

    def _on_stopped(self):
        self._set_busy(False, "Stopped. Nothing was written.")
        self.verdict_text.set("Stopped before the transcript was finished.")
        self.counts_text.set("STOPPED")
        self.verdict_label.config(bg=STATUS_COLORS[UNCHECKED])

    def _on_failure(self, message):
        self._set_busy(False, "The transcript could not be built.")
        self.verdict_text.set(message)
        self.counts_text.set("ERROR")
        self.verdict_label.config(bg=STATUS_COLORS[UNMATCHED])
        messagebox.showerror("Transcript", message)

    def _on_done(self, result):
        self.result = result
        self._set_busy(False, f"{len(result.lines)} lines transcribed.")
        has_lines = bool(result.lines)
        self.export_button.config(state="normal" if has_lines else "disabled")
        self.copy_button.config(state="normal" if has_lines else "disabled")
        self._render()

    def _banner_status(self, result):
        if not result.lines:
            return UNMATCHED
        if not result.checked:
            return UNCHECKED
        # Untestable lines are deliberately not counted here; they are not faults.
        if result.unmatched or result.out_of_tolerance:
            return OFF if result.matched else UNMATCHED
        return MATCHED

    def _render(self):
        result = self.result
        widget = self.transcript_text
        widget.configure(state="normal")
        widget.delete("1.0", "end")

        if result is None:
            widget.configure(state="disabled")
            return

        status = self._banner_status(result)
        self.verdict_label.config(bg=STATUS_COLORS[status])
        self.verdict_text.set(result.verdict_sentence())

        if result.checked:
            counts = (
                f"{result.matched} in sync / {result.out_of_tolerance} off / "
                f"{result.unmatched} missing"
            )
            if result.untestable:
                counts += f" / {result.untestable} n/a"
            self.counts_text.set(counts)
        else:
            self.counts_text.set(f"{len(result.lines)} lines")

        for error in result.errors:
            widget.insert("end", f"Error: {error}\n", UNMATCHED)
        for note in result.notes:
            widget.insert("end", f"Note: {note}\n", "note")
        if result.errors or result.notes:
            widget.insert("end", "\n")

        only_problems = self.only_problems.get()
        shown = 0
        for line in result.lines:
            if only_problems and (line.status in (MATCHED, UNCHECKED)
                                  or line.status in UNTESTABLE):
                continue
            shown += 1

            widget.insert("end", f"{line.index:04d}  {line.timecode}  ", "meta")
            if line.status == UNCHECKED:
                widget.insert("end", "\n")
            elif line.status in UNTESTABLE:
                widget.insert("end", f"{STATUS_WORDS[line.status]}\n", line.status)
            elif line.status == UNMATCHED:
                widget.insert("end", "not found in audio\n", UNMATCHED)
            else:
                widget.insert(
                    "end",
                    f"{format_offset_ms(line.offset)}  "
                    f"(confidence {line.confidence:.2f})  {STATUS_WORDS[line.status]}\n",
                    line.status,
                )

            if line.speaker:
                widget.insert("end", f"      {line.speaker}: ", "speaker")
                widget.insert("end", f"{line.text.splitlines()[0]}\n", "body")
                for extra in line.text.splitlines()[1:]:
                    widget.insert("end", f"      {extra}\n", "body")
            else:
                for text_line in line.text.splitlines() or [""]:
                    widget.insert("end", f"      {text_line}\n", "body")
            widget.insert("end", "\n")

        if only_problems and shown == 0:
            widget.insert(
                "end",
                "Every line matched the dialogue inside tolerance. Uncheck the filter to see "
                "the full transcript.\n",
                MATCHED,
            )

        widget.configure(state="disabled")
        widget.see("1.0")

    # ------------------------------------------------------------------ export

    def _selected_export(self):
        label = self.export_choice.get()
        for candidate, key, extension in EXPORT_CHOICES:
            if candidate == label:
                return key, extension
        return "text", ".txt"

    def _export(self):
        if not self.result or not self.result.lines:
            return

        key, extension = self._selected_export()
        default_name = Path(self.result.caption_path).stem + "-transcript" + extension
        target = filedialog.asksaveasfilename(
            title="Export transcript",
            defaultextension=extension,
            initialfile=default_name,
            filetypes=[("All files", "*.*")],
        )
        if not target:
            return

        # The dropdown is the intent; the typed extension only decides the
        # format when it disagrees with a dropdown left on its default.
        chosen = key
        by_suffix = format_for_path(target, default=None)
        if by_suffix and by_suffix != key and Path(target).suffix.lower() != extension:
            chosen = by_suffix

        try:
            written = write_transcript(target, self.result, chosen)
        except OSError as error:
            messagebox.showerror("Transcript", f"Could not write the transcript: {error}")
            return

        self.status_text.set(f"Exported {chosen} transcript to {written.name}.")

    def _copy(self):
        if not self.result or not self.result.lines:
            return
        key, _ = self._selected_export()
        try:
            payload = render(self.result, key)
        except ValueError as error:
            messagebox.showerror("Transcript", str(error))
            return
        self.clipboard_clear()
        self.clipboard_append(payload)
        self.status_text.set(f"Copied the {key} transcript to the clipboard.")
