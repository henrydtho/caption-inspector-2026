#!/usr/bin/env python3

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from cshim import resolve_caption_converter_library
from inspection_support import SUPPORTED_TYPES, decode_file, format_visible_caption_cues, text_preview, track_summary_rows


VALID_SCC_FRAME_RATES = (2397, 2400, 2500, 2997, 3000, 5000, 5994, 6000)


class CaptionInspectorDesktopApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Caption Inspector")
        self.root.geometry("1240x820")
        self.root.minsize(980, 700)

        self.selected_file = tk.StringVar()
        self.frame_rate = tk.StringVar(value="0")
        self.status_text = tk.StringVar(value="Choose a supported asset to begin.")
        self.summary_text = tk.StringVar(value="No file checked yet.")
        self.results_mode = tk.StringVar(value="Visible caption cues")
        self.raw_debug_mode = tk.BooleanVar(value=False)
        self.current_track_key = None
        self.track_lookup = {}
        self.decoded_tracks = None

        self._build_layout()
        self._refresh_runtime_status()
        self.root.after(150, self._activate_window)

    def _build_layout(self):
        self.root.configure(bg="#f4efe8")

        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        title = ttk.Label(header, text="Caption Inspector Desktop", font=("Helvetica", 22, "bold"))
        title.grid(row=0, column=0, sticky="w")

        subtitle = ttk.Label(
            header,
            text="Pick a media or caption file, run the decoder, and inspect the generated track events in one window.",
        )
        subtitle.grid(row=1, column=0, sticky="w", pady=(6, 0))

        controls = ttk.LabelFrame(outer, text="Check File", padding=14)
        controls.grid(row=1, column=0, sticky="ew", pady=(14, 14))
        controls.columnconfigure(1, weight=1)

        ttk.Label(controls, text="Asset").grid(row=0, column=0, sticky="w")
        file_entry = ttk.Entry(controls, textvariable=self.selected_file)
        file_entry.grid(row=0, column=1, sticky="ew", padx=(10, 10))

        browse_button = ttk.Button(controls, text="Browse...", command=self._browse_file)
        browse_button.grid(row=0, column=2, sticky="ew")

        ttk.Label(controls, text="Frame rate x100").grid(row=1, column=0, sticky="w", pady=(12, 0))
        frame_rate_spin = ttk.Spinbox(controls, from_=0, to=6000, increment=100, textvariable=self.frame_rate, width=12)
        frame_rate_spin.grid(row=1, column=1, sticky="w", padx=(10, 10), pady=(12, 0))

        self.check_button = ttk.Button(controls, text="Run Check", command=self._run_check)
        self.check_button.grid(row=1, column=2, sticky="ew", pady=(12, 0))

        ttk.Label(
            controls,
            text="Use 2400 for SCC-style inputs when the file needs an explicit frame rate.",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))

        content = ttk.PanedWindow(outer, orient="horizontal")
        content.grid(row=2, column=0, sticky="nsew")

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
        results_frame.rowconfigure(0, weight=1)
        results_frame.columnconfigure(0, weight=1)

        self.results_text = ScrolledText(results_frame, wrap="word", font=("Menlo", 11))
        self.results_text.grid(row=0, column=0, sticky="nsew")
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
            title="Select a caption or media file",
            filetypes=[("Supported files", "*.ts *.mpg *.mp4 *.mcc *.scc *.mov"), ("All files", "*.*")],
        )
        if selected_path:
            self.selected_file.set(selected_path)
            if Path(selected_path).suffix.lower() == ".scc" and self.frame_rate.get().strip() in ("", "0"):
                self.frame_rate.set("2400")
                self.status_text.set("SCC file selected. Frame rate defaulted to 2400.")
            else:
                self.status_text.set("File selected. Ready to run the check.")

    def _parse_frame_rate(self):
        raw_value = self.frame_rate.get().strip()
        if raw_value == "":
            return 0

        try:
            return int(raw_value)
        except ValueError as error:
            raise ValueError("Frame rate must be a whole number such as 2400 or 2997.") from error

    def _validate_before_decode(self, selected_path, frame_rate):
        suffix = Path(selected_path).suffix.lower()
        if suffix == ".scc" and frame_rate not in VALID_SCC_FRAME_RATES:
            valid_values = ", ".join(str(value) for value in VALID_SCC_FRAME_RATES)
            raise ValueError(f"SCC files require a valid frame rate. Use one of: {valid_values}.")

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

        self.check_button.config(state="disabled")
        self.status_text.set("Running caption check...")
        self._set_text(self.results_text, "Running caption check...")
        self._set_text(self.log_text, "Collecting decoder output...")

        worker = threading.Thread(target=self._decode_worker, args=(selected_path, frame_rate), daemon=True)
        worker.start()

    def _decode_worker(self, selected_path, frame_rate):
        try:
            tracks, logs = decode_file(selected_path, frame_rate, capture_logs=True)
        except Exception as error:
            self.root.after(0, self._handle_decode_failure, str(error))
            return

        self.root.after(0, self._handle_decode_success, selected_path, tracks, logs)

    def _handle_decode_failure(self, message):
        self.check_button.config(state="normal")
        self.status_text.set("Check failed.")
        self._set_text(self.results_text, f"The file check failed:\n\n{message}")
        self._set_text(self.log_text, "No decoder output captured.")
        messagebox.showerror("Caption Inspector", message)

    def _handle_decode_success(self, selected_path, tracks, logs):
        self.check_button.config(state="normal")
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
                "Visible caption cue mode is currently available for CEA-608 tracks only.",
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

    def _set_text(self, widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", tk.END)
        widget.insert("1.0", text)
        widget.configure(state="disabled")


def main():
    root = tk.Tk()
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    app = CaptionInspectorDesktopApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()