"""A model chooser that only offers models that actually exist.

The Sync QC and Transcript tabs both let you pick a transcription model. Until
now both listed all five sizes unconditionally, whether or not the machine had
them - so choosing `medium` on a build that ships only `base` looked fine, and
failed after the video had been probed and the audio extracted, with a message
about staging weights into a bundle directory.

That is the wrong place to find out. The list of models is knowable before
anything is pressed, so it is shown up front:

    base (bundled)          shipped inside this app
    large-v3 (installed)    in the local cache
    medium (not installed)  selectable, but says so, and offers to fetch it

Downloading is the one thing here that touches the network, and it happens only
when someone presses the button. A packaged bundle does not offer it at all.
"""

import threading
import tkinter as tk
from tkinter import messagebox, ttk

from offline import (
    BUNDLED,
    CACHED,
    describe_missing_model,
    model_status,
    network_allowed,
)
from transcribe import MODEL_SIZES, TranscriptionError, download_model


STATUS_SUFFIX = {
    BUNDLED: " (bundled)",
    CACHED: " (installed)",
}
MISSING_SUFFIX = " (not installed)"


def label_for(size):
    return f"{size}{STATUS_SUFFIX.get(model_status(size), MISSING_SUFFIX)}"


def size_for(label):
    """Recover the bare model size from a display label."""
    return (label or "").split(" (")[0].strip()


class ModelPicker(ttk.Frame):
    """Combobox of model sizes, annotated with what is installed.

    `variable` holds the bare size (`"base"`), never the decorated label, so
    callers and saved options are unaffected by how this chooses to present it.
    """

    def __init__(self, parent, variable, on_change=None, width=22):
        super().__init__(parent)

        self.variable = variable
        self._on_change = on_change
        self._busy = False

        self._label = tk.StringVar(value=label_for(variable.get()))

        self.combo = ttk.Combobox(
            self, textvariable=self._label, state="readonly", width=width
        )
        self.combo.grid(row=0, column=0, sticky="w")
        self.combo.bind("<<ComboboxSelected>>", self._selected)

        # Only offered where a download is possible; inside a sealed bundle
        # there is nothing this button could honestly do.
        self.get_button = ttk.Button(self, text="Get model...", command=self._download)
        self.get_button.grid(row=0, column=1, padx=(8, 0))

        self.refresh()

    # ------------------------------------------------------------------ state

    def refresh(self):
        """Re-read what is installed and redraw the list."""
        self.combo.configure(values=[label_for(size) for size in MODEL_SIZES])
        self._label.set(label_for(self.variable.get()))
        self._sync_button()

    def _sync_button(self):
        size = self.variable.get()
        missing = model_status(size) not in (BUNDLED, CACHED)
        if missing and network_allowed() and not self._busy:
            self.get_button.grid()
            self.get_button.config(state="normal", text=f"Get {size}...")
        elif missing and not network_allowed():
            # A sealed build cannot fetch anything; saying so beats a dead button.
            self.get_button.grid_remove()
        else:
            self.get_button.grid_remove()

    def selected_size(self):
        return size_for(self._label.get())

    def is_ready(self):
        return model_status(self.selected_size()) in (BUNDLED, CACHED)

    def explain(self):
        return describe_missing_model(self.selected_size(), MODEL_SIZES)

    # ----------------------------------------------------------------- events

    def _selected(self, _event=None):
        self.variable.set(self.selected_size())
        self._sync_button()
        if self._on_change:
            self._on_change(self.variable.get())

    def _download(self):
        if self._busy:
            return

        size = self.variable.get()
        if not messagebox.askyesno(
            "Download model",
            f"Download the {size!r} transcription model?\n\n"
            "This is the only part of the app that uses the network, and it happens "
            "once - afterwards the model is used from disk.\n\n"
            f"Larger models are a slower download: tiny is about 75 MB, base 150 MB, "
            "and large-v3 close to 3 GB.",
        ):
            return

        self._busy = True
        self.get_button.config(state="disabled", text="Downloading...")

        def work():
            try:
                download_model(size)
            except TranscriptionError as error:
                self.after(0, self._finished, size, str(error))
                return
            except Exception as error:  # network stacks raise all sorts
                self.after(0, self._finished, size, f"The download failed: {error}")
                return
            self.after(0, self._finished, size, None)

        threading.Thread(target=work, daemon=True).start()

    def _finished(self, size, error):
        self._busy = False
        self.refresh()
        if error:
            messagebox.showerror("Download model", error)
        else:
            messagebox.showinfo("Download model", f"The {size} model is ready to use.")
        if self._on_change:
            self._on_change(self.variable.get())
