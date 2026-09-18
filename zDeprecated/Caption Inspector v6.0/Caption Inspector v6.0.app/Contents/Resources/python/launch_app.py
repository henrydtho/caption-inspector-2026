#!/usr/bin/env python3

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DESKTOP_APP_PATH = REPO_ROOT / "python" / "caption_inspector_app.py"
WEB_APP_PATH = REPO_ROOT / "python" / "app.py"


def _library_candidates():
    if sys.platform.startswith("win"):
        return [
            REPO_ROOT / "python" / "libci.1.0.0.dll",
            REPO_ROOT / "python" / "libci.dll",
        ]

    if sys.platform.startswith("linux"):
        return [
            REPO_ROOT / "python" / "libci.so",
            REPO_ROOT / "python" / "libci.1.0.0.so",
        ]

    return [
        REPO_ROOT / "python" / "libci.1.0.0.dylib",
        REPO_ROOT / "python" / "libci.dylib",
    ]


def _default_library_path():
    for candidate in _library_candidates():
        if candidate.exists():
            return candidate

    return _library_candidates()[0]


def ensure_streamlit_installed():
    if importlib.util.find_spec("streamlit") is not None:
        return

    print("Streamlit is not installed.", file=sys.stderr)
    print(
        "Install it with: python3 -m pip install -r python/requirements-app.txt",
        file=sys.stderr,
    )
    raise SystemExit(1)


def ensure_shared_library(force_rebuild=False):
    library_path = _default_library_path()
    if library_path.exists() and not force_rebuild:
        return

    print("Building shared library for the app...")
    subprocess.run(["make", "sharedlib"], cwd=REPO_ROOT, check=True)


def ensure_tkinter_installed():
    if importlib.util.find_spec("tkinter") is not None:
        return

    print("Tkinter is not available in this Python installation.", file=sys.stderr)
    raise SystemExit(1)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Launch the local Caption Inspector app.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate app prerequisites and exit without starting Streamlit.",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Launch the Streamlit web UI instead of the desktop app.",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Rebuild the shared library before launching the app.",
    )
    parser.add_argument(
        "app_args",
        nargs=argparse.REMAINDER,
        help="Extra arguments passed through to the selected app after '--'.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    ensure_shared_library(force_rebuild=args.rebuild)
    if args.web:
        ensure_streamlit_installed()
    else:
        ensure_tkinter_installed()

    if args.check:
        library_path = _default_library_path()
        print("Caption Inspector app prerequisites are ready.")
        print(f"Desktop app: {DESKTOP_APP_PATH}")
        print(f"Web app: {WEB_APP_PATH}")
        print(f"Library: {library_path}")
        return

    app_args = list(args.app_args)
    if app_args and app_args[0] == "--":
        app_args = app_args[1:]

    if args.web:
        command = [sys.executable, "-m", "streamlit", "run", str(WEB_APP_PATH)] + app_args
    else:
        command = [sys.executable, str(DESKTOP_APP_PATH)] + app_args

    raise SystemExit(subprocess.call(command, cwd=REPO_ROOT))


if __name__ == "__main__":
    main()