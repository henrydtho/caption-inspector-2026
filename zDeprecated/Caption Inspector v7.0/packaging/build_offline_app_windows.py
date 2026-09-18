#!/usr/bin/env python3
"""Build a fully self-contained Caption Inspector v7.0 bundle for Windows.

Run this ON A WINDOWS MACHINE (it produces a Windows PE binary; it cannot be
cross-built from macOS or Linux):

    py -3 packaging\\build_offline_app_windows.py --models base

The output depends on nothing at runtime: no Python install, no MSYS2, no
Hugging Face cache, no network - everything the app needs is copied next to
the generated .exe.

    CaptionInspector\\CaptionInspector.exe        PyInstaller-frozen launcher
    CaptionInspector\\libci.1.0.0.dll              the decoder, built via MSYS2
    CaptionInspector\\vendor\\bin\\ffmpeg.exe, ffprobe.exe, and their DLLs
    CaptionInspector\\models\\faster-whisper-<size>\\...
    CaptionInspector\\caption-inspector-bundle.txt seals the folder as a bundle

This mirrors `packaging/build_offline_app.py` (the macOS build) in spirit, but
uses PyInstaller to freeze the interpreter instead of hand-relocating a
framework build - PE dependency walking is handled well enough by PyInstaller
for the interpreter itself; `ffmpeg.exe`/`ffprobe.exe` and `libci.dll` are
vendored here because PyInstaller only looks at what Python imports, not at
outside binaries this script hands it with --add-binary.

Prerequisites on the build machine:
    - Python 3.12 (python.org installer, with tcl/tk) on PATH as `py`.
    - MSYS2 with the UCRT64 clang/make/pkg-config/ffmpeg packages, so
      `libci.1.0.0.dll` can be (re)built and so ffmpeg's DLLs can be vendored.
      `windows\\Install-And-Launch-CaptionInspector.ps1` sets this up.
"""

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "CaptionInspector"
APP_VERSION = "7.0"
BUNDLE_MARKER = "caption-inspector-bundle.txt"

MSYS2_ROOT = Path(r"C:\msys64")
MSYS2_UCRT64_BIN = MSYS2_ROOT / "ucrt64" / "bin"
MSYS2_USR_BIN = MSYS2_ROOT / "usr" / "bin"

PIP_REQUIREMENTS = ["pyinstaller>=6.0", "faster-whisper>=1.0"]

# Extension modules pulled in dynamically (plugins, native libs) that a static
# import scan will not find on its own.
PYINSTALLER_COLLECT_ALL = [
    "faster_whisper",
    "ctranslate2",
    "tokenizers",
    "onnxruntime",
    "huggingface_hub",
]

# Never vendor these - they ship with Windows itself, and copying them next to
# an app is a well-known way to shadow the OS's own copy with a stale one.
SYSTEM_DLL_PREFIXES = (
    "api-ms-win-", "kernel32", "user32", "advapi32", "ntdll", "msvcrt",
    "ole32", "oleaut32", "shell32", "shlwapi", "ws2_32", "gdi32", "comdlg32",
    "comctl32", "rpcrt4", "sechost", "bcrypt", "crypt32", "version", "winmm",
    "setupapi", "cfgmgr32", "imm32", "wldap32", "userenv", "psapi", "iphlpapi",
    "netapi32", "dnsapi", "secur32", "mswsock", "ncrypt", "powrprof",
    "propsys", "windows.storage", "wtsapi32", "dwmapi", "uxtheme",
)


def log(message):
    print(f"  {message}", flush=True)


def run(command, **kwargs):
    completed = subprocess.run(command, capture_output=True, text=True, **kwargs)
    if completed.returncode != 0:
        raise RuntimeError(
            f"{' '.join(str(part) for part in command)} failed:\n"
            f"{(completed.stderr or completed.stdout or '').strip()[-2000:]}"
        )
    return completed.stdout


# ---------------------------------------------------------------------------
# Shared library
# ---------------------------------------------------------------------------


def ensure_shared_library(force_rebuild=False):
    dll = REPO_ROOT / "python" / "libci.1.0.0.dll"
    if dll.exists() and not force_rebuild:
        log(f"using existing {dll}")
        return dll

    bash = MSYS2_USR_BIN / "bash.exe"
    if not bash.exists():
        raise SystemExit(
            "libci.1.0.0.dll is missing and MSYS2 was not found at C:\\msys64.\n"
            "Run windows\\Install-And-Launch-CaptionInspector.ps1 once to install "
            "MSYS2 and the clang/make/pkg-config/ffmpeg packages, then re-run this "
            "script."
        )

    log("building libci.1.0.0.dll via MSYS2 (clang, from src\\Makefile)")
    build_command = (
        "export PATH=/ucrt64/bin:$PATH; "
        f"cd '{REPO_ROOT.as_posix()}/src'; make sharedlib"
    )
    run([str(bash), "-lc", build_command])

    if not dll.exists():
        raise SystemExit(f"Build finished but {dll} still does not exist.")
    return dll


# ---------------------------------------------------------------------------
# DLL dependency closure (the PE equivalent of the macOS otool walk)
# ---------------------------------------------------------------------------


def _objdump():
    for candidate in (
        shutil.which("objdump"),
        MSYS2_UCRT64_BIN / "objdump.exe",
        MSYS2_USR_BIN / "objdump.exe",
    ):
        if candidate and Path(candidate).exists():
            return str(candidate)
    raise SystemExit(
        "objdump was not found (checked PATH and C:\\msys64). It ships with "
        "MSYS2's UCRT64 toolchain and is needed to find ffmpeg's and libci's "
        "dependency DLLs."
    )


def _direct_dependencies(binary):
    output = run([_objdump(), "-p", str(binary)])
    names = []
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("DLL Name:"):
            names.append(line.split(":", 1)[1].strip())
    return names


def _is_system_dll(name):
    lowered = name.lower()
    return lowered.startswith(SYSTEM_DLL_PREFIXES)


def resolve_dependency_closure(binaries, search_dirs):
    """All non-system DLLs `binaries` need, transitively, as resolved paths.

    `search_dirs` is checked in order for each dependency name; MSYS2 keeps a
    package's shared libraries together in one bin directory, so this rarely
    needs to look further than ucrt64\\bin.
    """
    pending = list(binaries)
    seen_binaries = set()
    resolved = {}

    while pending:
        binary = Path(pending.pop())
        key = str(binary).lower()
        if key in seen_binaries:
            continue
        seen_binaries.add(key)

        for name in _direct_dependencies(binary):
            if _is_system_dll(name):
                continue
            if name.lower() in resolved:
                continue

            found = None
            for directory in search_dirs:
                candidate = Path(directory) / name
                if candidate.exists():
                    found = candidate
                    break

            if found is None:
                log(f"warning: could not locate dependency '{name}' of {binary.name}")
                continue

            resolved[name.lower()] = found
            pending.append(found)

    return list(resolved.values())


# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------


def find_ffmpeg_tools(ffmpeg_dir=None):
    search_dirs = []
    if ffmpeg_dir:
        search_dirs.append(Path(ffmpeg_dir))
    search_dirs.append(MSYS2_UCRT64_BIN)

    tools = {}
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        found = None
        on_path = shutil.which(name)
        if on_path:
            found = Path(on_path)
        else:
            for directory in search_dirs:
                candidate = directory / name
                if candidate.exists():
                    found = candidate
                    break
        if found is None:
            raise SystemExit(
                f"{name} was not found on PATH or in C:\\msys64\\ucrt64\\bin.\n"
                "Install it (it is one of the packages "
                "windows\\Install-And-Launch-CaptionInspector.ps1 sets up), or pass "
                "--ffmpeg-dir pointing at a folder that has it."
            )
        tools[name] = found
        log(f"found {name} at {found}")
    return tools


def vendor_ffmpeg(bin_dir, ffmpeg_tools, extra_binaries):
    bin_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for name, source in ffmpeg_tools.items():
        destination = bin_dir / name
        shutil.copy2(source, destination)
        copied.append(destination)
        log(f"vendored {name}")

    search_dirs = [MSYS2_UCRT64_BIN] + [Path(p).parent for p in ffmpeg_tools.values()]
    dependencies = resolve_dependency_closure(copied + extra_binaries, search_dirs)
    for dependency in dependencies:
        destination = bin_dir / dependency.name
        if destination.exists():
            continue
        shutil.copy2(dependency, destination)
    log(f"vendored {len(dependencies)} dependency DLL(s) into {bin_dir}")


# ---------------------------------------------------------------------------
# Models (identical logic to the macOS build - the HF cache layout is the same)
# ---------------------------------------------------------------------------


def _huggingface_snapshot(model_size):
    home = os.environ.get("HF_HOME")
    hub = Path(home) / "hub" if home else Path.home() / ".cache" / "huggingface" / "hub"
    for entry in sorted(hub.glob(f"models--*faster-whisper-{model_size}")):
        snapshots = entry / "snapshots"
        if not snapshots.is_dir():
            continue
        for snapshot in sorted(snapshots.iterdir()):
            if (snapshot / "model.bin").exists():
                return snapshot
    return None


def copy_models(models_dir, sizes):
    models_dir.mkdir(parents=True, exist_ok=True)
    staged = []

    for size in sizes:
        snapshot = _huggingface_snapshot(size)
        if snapshot is None:
            raise SystemExit(
                f"No cached weights found for the {size!r} model.\n"
                "Download it once on a machine with a network:\n"
                f"  py -3 -c \"from faster_whisper import WhisperModel; "
                f"WhisperModel('{size}')\"\n"
                "then re-run this build."
            )

        destination = models_dir / f"faster-whisper-{size}"
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True)

        for item in snapshot.iterdir():
            shutil.copy2(item.resolve(), destination / item.name)

        size_mb = sum(f.stat().st_size for f in destination.iterdir()) / 1e6
        log(f"staged model {size} ({size_mb:.0f} MB)")
        staged.append(size)

    return staged


# ---------------------------------------------------------------------------
# PyInstaller
# ---------------------------------------------------------------------------


def install_build_dependencies():
    log("installing PyInstaller and faster-whisper (build-time only)")
    run([sys.executable, "-m", "pip", "install", "--upgrade", "pip"])
    run([sys.executable, "-m", "pip", "install", "--upgrade"] + PIP_REQUIREMENTS)


def run_pyinstaller(work_dir, dist_dir):
    entry = REPO_ROOT / "packaging" / "windows_entry.py"
    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", APP_NAME,
        "--onedir", "--windowed",
        "--paths", str(REPO_ROOT / "python"),
        "--distpath", str(dist_dir),
        "--workpath", str(work_dir),
        "--specpath", str(work_dir),
    ]
    for module in PYINSTALLER_COLLECT_ALL:
        command += ["--collect-all", module]
    command.append(str(entry))

    log("running PyInstaller (this takes a few minutes)")
    subprocess.run(command, check=True, cwd=REPO_ROOT)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def write_bundle_marker(bundle_dir):
    marker = bundle_dir / BUNDLE_MARKER
    marker.write_text(
        f"Caption Inspector v{APP_VERSION} - self-contained Windows bundle\n"
        "Do not delete this file: it tells the app it is running from a sealed "
        "bundle, so it should not try to reach the network or fall back to "
        "whatever ffmpeg/model happens to be installed on this machine.\n"
    )


def make_zip(bundle_dir, zip_path):
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in bundle_dir.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(bundle_dir.parent))

    log(f"wrote {zip_path} ({zip_path.stat().st_size / 1e6:.0f} MB)")
    return zip_path


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--models", default="base",
        help="Comma-separated model sizes to bundle, e.g. base,medium. "
             "Each must already be downloaded into the local Hugging Face cache.",
    )
    parser.add_argument("--ffmpeg-dir", default=None, help="Folder containing ffmpeg.exe/ffprobe.exe.")
    parser.add_argument("--force-rebuild-dll", action="store_true", help="Rebuild libci.1.0.0.dll even if present.")
    parser.add_argument("--zip-only", action="store_true", help="Skip re-running PyInstaller; just re-zip an existing dist folder.")
    return parser.parse_args()


def main():
    if not sys.platform.startswith("win"):
        raise SystemExit("This script produces a Windows binary and must be run on Windows.")

    args = parse_args()
    sizes = [size.strip() for size in args.models.split(",") if size.strip()]

    dist_root = REPO_ROOT / "packaging" / "dist"
    work_dir = REPO_ROOT / "packaging" / "build"
    bundle_dir = dist_root / APP_NAME

    if not args.zip_only:
        dll = ensure_shared_library(force_rebuild=args.force_rebuild_dll)
        install_build_dependencies()
        run_pyinstaller(work_dir, dist_root)

        exe = bundle_dir / f"{APP_NAME}.exe"
        if not exe.exists():
            raise SystemExit(f"PyInstaller finished but {exe} does not exist.")

        shutil.copy2(dll, bundle_dir / dll.name)
        log(f"vendored {dll.name} beside {exe.name}")

        ffmpeg_tools = find_ffmpeg_tools(args.ffmpeg_dir)
        vendor_ffmpeg(bundle_dir / "vendor" / "bin", ffmpeg_tools, extra_binaries=[bundle_dir / dll.name])
        copy_models(bundle_dir / "models", sizes)
        write_bundle_marker(bundle_dir)

    zip_path = make_zip(bundle_dir, dist_root / f"Caption-Inspector-v{APP_VERSION}-windows-x64.zip")

    log("")
    log(f"Done. {bundle_dir} is self-contained; {zip_path.name} is what to hand to another machine.")
    log("Sanity-check on a clean Windows machine before distributing it:")
    log(f"  1. Unzip and double-click {APP_NAME}.exe.")
    log("  2. Open a file from each format (.mcc, .scc, .ts/.mp4) and confirm it decodes.")
    log("  3. Try a transcription job and confirm it runs with no network access.")


if __name__ == "__main__":
    main()
