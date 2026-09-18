"""PyInstaller entry point for the self-contained Windows build.

This is the actual script PyInstaller compiles, instead of pointing it at
`caption_inspector_app.py` directly. It has to run before anything else is
imported: `cshim.py` resolves the path to `libci.*.dll` at import time, and the
FFmpeg/model lookups in `offline.py` need `CAPTION_INSPECTOR_RESOURCES` set
before `transcribe.py` is imported. By the time `caption_inspector_app` is
imported below, both are already in place.

Layout this expects beside the frozen `.exe` (built by
`build_offline_app_windows.py`):

    CaptionInspector.exe
    libci.1.0.0.dll
    caption-inspector-bundle.txt        marks this folder as a sealed bundle
    vendor\\bin\\ffmpeg.exe, ffprobe.exe, and their dependency DLLs
    models\\faster-whisper-<size>\\...
"""

import os
import sys
from pathlib import Path


def _bootstrap():
    if not getattr(sys, "frozen", False):
        return

    exe_dir = Path(sys.executable).resolve().parent

    dll = exe_dir / "libci.1.0.0.dll"
    if dll.exists():
        os.environ.setdefault("CAPTION_INSPECTOR_LIBRARY", str(dll))

    os.environ.setdefault("CAPTION_INSPECTOR_RESOURCES", str(exe_dir))

    # Lets ffmpeg.exe (launched as a subprocess) and libci.dll (loaded via
    # ctypes) both find their dependency DLLs without installing anything or
    # touching the system PATH permanently.
    vendor_bin = exe_dir / "vendor" / "bin"
    if vendor_bin.is_dir():
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(str(vendor_bin))
        os.environ["PATH"] = str(vendor_bin) + os.pathsep + os.environ.get("PATH", "")


_bootstrap()

from caption_inspector_app import main  # noqa: E402  (must follow _bootstrap)

if __name__ == "__main__":
    main()
