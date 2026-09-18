#!/usr/bin/env python3

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from cshim import resolve_caption_converter_library
from inspection_support import (
    DECODED_TYPES,
    SUBTITLE_FAMILY,
    SUBTITLE_TYPES,
    SUPPORTED_TYPES,
    decode_file,
    format_subtitle_cues,
    format_visible_caption_cues,
    text_preview,
    track_summary_rows,
)
from compare_panel import ComparePanel
from frame_rate_detect import detect_frame_rate
from pdf_export import write_text_as_pdf
from sync_panel import SyncQCPanel
from timecode import RATE_LABELS
from transcript_panel import TranscriptPanel


APP_VERSION = "7.0"

VALID_SCC_FRAME_RATES = (2397, 2400, 2500, 2997, 3000, 5000, 5994, 6000)

# The frame rate is read from the file by default. The explicit choices stay,
# because a file that states nothing and has too few rows to infer from still
# has to be decodable by someone who knows what it is.
AUTO_RATE = "Auto (read from the file)"
RATE_CHOICES = [AUTO_RATE] + [
    f"{label} fps" for _, label in sorted(RATE_LABELS.items(), key=lambda item: item[0])
]

# Only SCC needs the frame rate spinner. Every other input either carries its
# own rate or does not count in frames at all.
FRAME_RATE_SENSITIVE_SUFFIXES = (".scc",)

_ALL_PATTERN = " ".join(f"*.{name}" for name in SUPPORTED_TYPES)
_MEDIA_PATTERN = " ".join(f"*.{name}" for name in DECODED_TYPES)
_SUBTITLE_PATTERN = " ".join(f"*.{name}" for name in SUBTITLE_TYPES)


class CaptionInspectorDesktopApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"Caption Inspector v{APP_VERSION}")
        self.root.geometry("1240x860")
        self.root.minsize(1000, 720)

        self.selected_file = tk.StringVar()
        self.rate_choice = tk.StringVar(value=AUTO_RATE)
        self.detected_rate_text = tk.StringVar(value="")
        self.detected_rate_code = None
        self.status_text = tk.StringVar(value="Choose a supported asset to begin.")
        self.summary_text = tk.StringVar(value="No file checked yet.")
        self.results_mode = tk.StringVar(value="Visible caption cues")
        self.raw_debug_mode = tk.BooleanVar(value=False)
        self.current_track_key = None
        self.track_lookup = {}
        self.decoded_tracks = None
        # Bumped by Stop. A decode that finishes after its generation has moved
        # on has its result dropped - see `_stop_check`.
        self._decode_generation = 0
        self._decoding = False

        self._build_layout()
        self._refresh_runtime_status()
        self.root.after(150, self._activate_window)

    def _build_layout(self):
        self.root.configure(bg="#f4efe8")

        shell = ttk.Frame(self.root, padding=(16, 14, 16, 8))
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(1, weight=1)

        header = ttk.Frame(shell)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        ttk.Label(
            header, text="Caption Inspector", font=("Helvetica", 22, "bold")
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="Decode caption tracks, and check that a delivery is actually in sync with its video.",
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Label(header, text=f"v{APP_VERSION}", foreground="#8a949d").grid(row=0, column=1, sticky="e")

        notebook = ttk.Notebook(shell)
        notebook.grid(row=1, column=0, sticky="nsew", pady=(14, 0))

        inspector_tab = ttk.Frame(notebook)
        notebook.add(inspector_tab, text="  Inspect Captions  ")
        notebook.add(SyncQCPanel(notebook), text="  Sync QC  ")
        notebook.add(ComparePanel(notebook), text="  Compare Subtitles  ")
        notebook.add(TranscriptPanel(notebook), text="  Transcript & Match  ")

        self._build_inspector_tab(inspector_tab)

    def _build_inspector_tab(self, parent):
        outer = ttk.Frame(parent, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        controls = ttk.LabelFrame(outer, text="Check File", padding=14)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        controls.columnconfigure(1, weight=1)

        ttk.Label(controls, text="Asset").grid(row=0, column=0, sticky="w")
        file_entry = ttk.Entry(controls, textvariable=self.selected_file)
        file_entry.grid(row=0, column=1, sticky="ew", padx=(10, 10))

        browse_button = ttk.Button(controls, text="Browse...", command=self._browse_file)
        browse_button.grid(row=0, column=2, sticky="ew")

        ttk.Label(controls, text="Frame rate").grid(row=1, column=0, sticky="w", pady=(12, 0))
        ttk.Combobox(
            controls,
            textvariable=self.rate_choice,
            values=RATE_CHOICES,
            state="readonly",
            width=24,
        ).grid(row=1, column=1, sticky="w", padx=(10, 10), pady=(12, 0))

        buttons = ttk.Frame(controls)
        buttons.grid(row=1, column=2, sticky="ew", pady=(12, 0))
        buttons.columnconfigure(0, weight=1)

        self.check_button = ttk.Button(buttons, text="Run Check", command=self._run_check)
        self.check_button.grid(row=0, column=0, sticky="ew")

        self.stop_button = ttk.Button(
            buttons, text="Stop", command=self._stop_check, state="disabled"
        )
        self.stop_button.grid(row=0, column=1, sticky="ew", padx=(8, 0))

        ttk.Label(
            controls,
            textvariable=self.detected_rate_text,
            wraplength=760,
            justify="left",
            foreground="#52616f",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))

        content = ttk.PanedWindow(outer, orient="horizontal")
        content.grid(row=1, column=0, sticky="nsew")

        left_panel = ttk.Frame(content, padding=(0, 0, 12, 0))
        right_panel = ttk.Frame(content)
        content.add(left_panel, weight=1)
        content.add(right_panel, weight=3)

        left_panel.rowconfigure(2, weight=1)
        left_panel.columnconfigure(0, weight=1)

        runtime_frame = ttk.LabelFrame(left_panel, text="Runtime", padding=12)
        runtime_frame.grid(row=0, column=0, sticky="ew")
        runtime_frame.columnconfigure(0, weight=1)
        self.runtime_label = ttk.Label(runtime_frame, text="")
        self.runtime_label.grid(row=0, column=0, sticky="w")
        self.library_label = ttk.Label(runtime_frame, text="", wraplength=300)
        self.library_label.grid(row=1, column=0, sticky="w", pady=(8, 0))

        summary_frame = ttk.LabelFrame(left_panel, text="Summary", padding=12)
        summary_frame.grid(row=1, column=0, sticky="ew", pady=(12, 12))
        summary_frame.columnconfigure(0, weight=1)
        self.summary_label = ttk.Label(summary_frame, textvariable=self.summary_text, wraplength=300, justify="left")
        self.summary_label.grid(row=0, column=0, sticky="w")

        track_frame = ttk.LabelFrame(left_panel, text="Tracks", padding=12)
        track_frame.grid(row=2, column=0, sticky="nsew")
        track_frame.rowconfigure(0, weight=1)
        track_frame.columnconfigure(0, weight=1)

        self.track_list = tk.Listbox(track_frame, exportselection=False)
        self.track_list.grid(row=0, column=0, sticky="nsew")
        self.track_list.bind("<<ListboxSelect>>", self._on_track_selected)

        right_panel.rowconfigure(1, weight=1)
        right_panel.rowconfigure(2, weight=1)
        right_panel.columnconfigure(0, weight=1)

        status_frame = ttk.Frame(right_panel)
        status_frame.grid(row=0, column=0, sticky="ew")
        status_frame.columnconfigure(0, weight=1)
        ttk.Label(status_frame, textvariable=self.status_text).grid(row=0, column=0, sticky="w")
        ttk.Label(status_frame, text="Results mode").grid(row=0, column=1, sticky="e", padx=(12, 8))
        results_mode_combo = ttk.Combobox(
            status_frame,
            textvariable=self.results_mode,
            values=("Timeline", "Visible caption cues"),
            state="readonly",
            width=22,
        )
        results_mode_combo.grid(row=0, column=2, sticky="e")
        results_mode_combo.bind("<<ComboboxSelected>>", self._on_results_mode_changed)
        raw_debug_toggle = ttk.Checkbutton(
            status_frame,
            text="Raw debug mode",
            variable=self.raw_debug_mode,
            command=self._on_raw_debug_mode_changed,
        )
        raw_debug_toggle.grid(row=0, column=3, sticky="e", padx=(12, 0))

        results_frame = ttk.LabelFrame(right_panel, text="Test Results", padding=12)
        results_frame.grid(row=1, column=0, sticky="nsew", pady=(12, 12))
        results_frame.rowconfigure(1, weight=1)
        results_frame.columnconfigure(0, weight=1)

        results_toolbar = ttk.Frame(results_frame)
        results_toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(
            results_toolbar, text="Export PDF...", command=self._export_results_pdf
        ).pack(side="right", padx=(8, 0))
        ttk.Button(
            results_toolbar, text="Export Text...", command=self._export_results_text
        ).pack(side="right")

        self.results_text = ScrolledText(results_frame, wrap="word", font=("Menlo", 11))
        self.results_text.grid(row=1, column=0, sticky="nsew")
        self.results_text.insert("1.0", "Track details and transcripts will appear here after a check runs.")
        self.results_text.configure(state="disabled")

        log_frame = ttk.LabelFrame(right_panel, text="Decoder Output", padding=12)
        log_frame.grid(row=2, column=0, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

        self.log_text = ScrolledText(log_frame, wrap="word", font=("Menlo", 10))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.insert("1.0", "Decoder messages will appear here after the test runs.")
        self.log_text.configure(state="disabled")

    def _refresh_runtime_status(self):
        library_path = Path(resolve_caption_converter_library())
        if library_path.exists():
            self.runtime_label.config(text="Shared library detected")
        else:
            self.runtime_label.config(text="Shared library missing")
        self.library_label.config(text=str(library_path))

    def _activate_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.root.attributes("-topmost", True)
        self.root.after(400, lambda: self.root.attributes("-topmost", False))

    def _browse_file(self):
        selected_path = filedialog.askopenfilename(
            title="Select a caption, subtitle, or media file",
            filetypes=[
                ("Supported files", _ALL_PATTERN),
                ("Subtitle files", _SUBTITLE_PATTERN),
                ("Media and broadcast captions", _MEDIA_PATTERN),
                ("All files", "*.*"),
            ],
        )
        if selected_path:
            self.selected_file.set(selected_path)
            self.status_text.set("File selected. Reading its frame rate...")
            self._detect_rate(selected_path)

    def _detect_rate(self, selected_path):
        """Read the rate off the file and report it, rather than asking for it.

        On a worker thread: a long SCC is a few thousand timecodes to parse, and
        blocking the browse dialog on that is how a file picker feels broken.
        """
        self.detected_rate_code = None
        self.detected_rate_text.set("Reading the file's frame rate...")

        def work():
            try:
                detection = detect_frame_rate(selected_path)
            except Exception as error:
                self.root.after(0, self._on_rate_detected, None, str(error))
                return
            self.root.after(0, self._on_rate_detected, detection, None)

        threading.Thread(target=work, daemon=True).start()

    def _on_rate_detected(self, detection, error):
        if error:
            self.detected_rate_text.set(f"Frame rate could not be read: {error}")
            self.status_text.set("File selected. Ready to run the check.")
            return

        self.detected_rate_code = detection.rate_code
        lines = [detection.headline()]
        lines.extend(detection.notes[:3])
        self.detected_rate_text.set("  ".join(lines))

        if detection.conflict:
            self.status_text.set("File selected. The rate it states disagrees with its stamps.")
        elif detection.rate_code is None and detection.timebase == "timecode":
            self.status_text.set(
                "File selected. No frame rate could be read - choose one before running the check."
            )
        else:
            self.status_text.set("File selected. Ready to run the check.")

    def _chosen_rate_code(self):
        """The explicit choice, or None when the file's own rate should be used."""
        choice = self.rate_choice.get()
        if choice.startswith("Auto"):
            return None
        label = choice.replace(" fps", "").strip()
        for code, code_label in RATE_LABELS.items():
            if code_label == label:
                return code
        return None

    def _parse_frame_rate(self):
        """The x100 rate to hand the decoder, 0 when it does not need one."""
        chosen = self._chosen_rate_code()
        if chosen is not None:
            return chosen
        return self.detected_rate_code or 0

    def _validate_before_decode(self, selected_path, frame_rate):
        suffix = Path(selected_path).suffix.lower()
        if suffix == ".scc" and frame_rate not in VALID_SCC_FRAME_RATES:
            raise ValueError(
                "This SCC states no frame rate and its timecodes are not enough to infer one.\n\n"
                "Pick a rate from the Frame rate list and run the check again."
            )

    def _run_check(self):
        selected_path = self.selected_file.get().strip()
        if not selected_path:
            messagebox.showwarning("Caption Inspector", "Choose a file before running the check.")
            return

        if not Path(selected_path).exists():
            messagebox.showerror("Caption Inspector", "The selected file does not exist.")
            return

        try:
            frame_rate = self._parse_frame_rate()
            self._validate_before_decode(selected_path, frame_rate)
        except ValueError as error:
            messagebox.showerror("Caption Inspector", str(error))
            self.status_text.set("Check blocked by input validation.")
            return

        self._decoding = True
        self._decode_generation += 1
        generation = self._decode_generation
        self.check_button.config(state="disabled")
        self.stop_button.config(state="normal")
        self.status_text.set("Running caption check...")
        self._set_text(self.results_text, "Running caption check...")
        self._set_text(self.log_text, "Collecting decoder output...")

        worker = threading.Thread(
            target=self._decode_worker, args=(selected_path, frame_rate, generation), daemon=True
        )
        worker.start()

    def _stop_check(self):
        """Abandon the running decode.

        The decode is one blocking call into the C library, which offers no way
        to interrupt it, so this cannot cut the decode short. What it does do is
        give the tab back immediately and guarantee the abandoned decode's
        result is discarded rather than appearing minutes later over whatever
        you did next. The status line says exactly that rather than implying the
        work stopped.
        """
        if not self._decoding:
            return
        self._decode_generation += 1
        self._decoding = False
        self.check_button.config(state="normal")
        self.stop_button.config(state="disabled")
        self.status_text.set(
            "Stopped. The decoder finishes in the background and its result is discarded."
        )
        self._set_text(self.results_text, "Stopped before the decode finished.")

    def _decode_worker(self, selected_path, frame_rate, generation):
        try:
            tracks, logs = decode_file(selected_path, frame_rate, capture_logs=True)
        except Exception as error:
            self.root.after(0, self._handle_decode_failure, str(error), generation)
            return

        self.root.after(0, self._handle_decode_success, selected_path, tracks, logs, generation)

    def _is_current(self, generation):
        return generation == self._decode_generation

    def _finish_decode(self):
        self._decoding = False
        self.check_button.config(state="normal")
        self.stop_button.config(state="disabled")

    def _handle_decode_failure(self, message, generation=None):
        if generation is not None and not self._is_current(generation):
            return
        self._finish_decode()
        self.status_text.set("Check failed.")
        self._set_text(self.results_text, f"The file check failed:\n\n{message}")
        self._set_text(self.log_text, "No decoder output captured.")
        messagebox.showerror("Caption Inspector", message)

    def _handle_decode_success(self, selected_path, tracks, logs, generation=None):
        if generation is not None and not self._is_current(generation):
            return
        self._finish_decode()
        self.decoded_tracks = tracks
        self.track_lookup = {}
        self.track_list.delete(0, tk.END)

        summary_rows = track_summary_rows(tracks)
        total_tracks = len(summary_rows)
        total_events = sum(row["events"] for row in summary_rows)
        total_text_events = sum(row["text_events"] for row in summary_rows)

        self.summary_text.set(
            f"File: {Path(selected_path).name}\n"
            f"Tracks found: {total_tracks}\n"
            f"Events: {total_events}\n"
            f"Text events: {total_text_events}"
        )

        for family, family_tracks in tracks.items():
            for track_name, rows in family_tracks.items():
                label = f"{family} / {track_name} ({len(rows)} events)"
                self.track_lookup[label] = (family, track_name)
                self.track_list.insert(tk.END, label)

        if self.track_list.size() > 0:
            self.track_list.selection_set(0)
            self._show_track(self.track_list.get(0))
            self.status_text.set("Check finished. Select any track to inspect its results.")
        else:
            self.current_track_key = None
            self._set_text(self.results_text, "The check completed, but no caption tracks were produced for this file.")
            self.status_text.set("Check finished. No caption tracks were found.")

        self._set_text(self.log_text, logs.strip() or "The decoder did not emit any console output.")

    def _on_track_selected(self, _event):
        selection = self.track_list.curselection()
        if not selection:
            return
        self._show_track(self.track_list.get(selection[0]))

    def _on_results_mode_changed(self, _event):
        if self.current_track_key:
            self._show_track(self.current_track_key)

    def _on_raw_debug_mode_changed(self):
        if self.current_track_key:
            self._show_track(self.current_track_key)

    def _show_track(self, label):
        if label not in self.track_lookup or not self.decoded_tracks:
            return

        family, track_name = self.track_lookup[label]
        rows = self.decoded_tracks[family][track_name]
        transcript = text_preview(rows)

        if self.raw_debug_mode.get():
            lines = [
                f"Track: {family} / {track_name}",
                f"Events: {len(rows)}",
                "",
                "Raw debug timeline:",
            ]

            for row in rows:
                lines.append(f"{row['time']}  {row['type']}  {row['event']}")

            if transcript:
                lines.extend(["", "Transcript:", transcript])

            self.current_track_key = label
            self._set_text(self.results_text, "\n".join(lines))
            return

        if family == SUBTITLE_FAMILY:
            self.current_track_key = label
            self._set_text(self.results_text, self._subtitle_view(family, track_name, rows))
            return

        if self.results_mode.get() == "Visible caption cues" and family == "CEA-608":
            cue_text, cue_count = format_visible_caption_cues(rows, track_name)
            lines = [
                f"Track: {family} / {track_name}",
                f"Visible cues: {cue_count}",
                "",
                cue_text,
            ]
            self.current_track_key = label
            self._set_text(self.results_text, "\n".join(lines))
            return

        if self.results_mode.get() == "Visible caption cues" and family != "CEA-608":
            lines = [
                f"Track: {family} / {track_name}",
                "",
                "Visible caption cue mode is currently available for CEA-608 tracks "
                "and subtitle files only.",
            ]
            self.current_track_key = label
            self._set_text(self.results_text, "\n".join(lines))
            return

        lines = [
            f"Track: {family} / {track_name}",
            f"Events: {len(rows)}",
            "",
            "Timeline:",
        ]

        for row in rows:
            lines.append(f"{row['time']}  {row['type']}  {row['event']}")

        if transcript:
            lines.extend(["", "Transcript:", transcript])

        self.current_track_key = label
        self._set_text(self.results_text, "\n".join(lines))

    def _subtitle_view(self, family, track_name, rows):
        """Render a text subtitle track.

        A subtitle file states both ends of every cue, so both views show real
        times rather than reconstructed ones: cue mode reads as a spotting list,
        timeline mode as in/out pairs.
        """
        if self.results_mode.get() == "Visible caption cues":
            cue_text, cue_count = format_subtitle_cues(rows)
            return "\n".join(
                [f"Track: {family} / {track_name}", f"Cues: {cue_count}", "", cue_text]
            )

        lines = [f"Track: {family} / {track_name}", f"Cues: {len(rows)}", "", "Timeline:"]
        for row in rows:
            out_stamp = row.get("end_time") or ""
            body = " / ".join(part for part in str(row["event"]).splitlines() if part)
            lines.append(f"{row['time']} -> {out_stamp}  {body}")

        transcript = text_preview(rows)
        if transcript:
            lines.extend(["", "Transcript:", transcript])
        return "\n".join(lines)

    def _set_text(self, widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", tk.END)
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _current_results_text(self):
        return self.results_text.get("1.0", "end-1c")

    def _default_export_stem(self):
        selected_path = self.selected_file.get().strip()
        return Path(selected_path).stem if selected_path else "caption_inspector_results"

    def _export_results_text(self):
        content = self._current_results_text()
        if not content.strip():
            messagebox.showinfo("Caption Inspector", "There are no results to export yet.")
            return

        destination = filedialog.asksaveasfilename(
            title="Export results as text",
            defaultextension=".txt",
            initialfile=f"{self._default_export_stem()}.txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not destination:
            return

        try:
            Path(destination).write_text(content, encoding="utf-8")
        except OSError as error:
            messagebox.showerror("Caption Inspector", f"Could not save the text file:\n\n{error}")
            return

        self.status_text.set(f"Results exported to {Path(destination).name}.")

    def _export_results_pdf(self):
        content = self._current_results_text()
        if not content.strip():
            messagebox.showinfo("Caption Inspector", "There are no results to export yet.")
            return

        destination = filedialog.asksaveasfilename(
            title="Export results as PDF",
            defaultextension=".pdf",
            initialfile=f"{self._default_export_stem()}.pdf",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
        )
        if not destination:
            return

        try:
            write_text_as_pdf(content, destination, source_path=self.selected_file.get().strip())
        except OSError as error:
            messagebox.showerror("Caption Inspector", f"Could not save the PDF file:\n\n{error}")
            return

        self.status_text.set(f"Results exported to {Path(destination).name}.")


def main():
    root = tk.Tk()
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    app = CaptionInspectorDesktopApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()