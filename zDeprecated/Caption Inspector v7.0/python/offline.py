"""Resolve the resources the app needs from inside its own bundle.

The app is built to run with the network switched off, permanently. That is not a
setting - it is a property of where the app looks for things:

    ffmpeg / ffprobe   a vendored copy in the bundle, before PATH
    Whisper weights    a vendored model directory, before the Hugging Face cache
    the hub itself     switched off, so a missing model is an error rather than
                       a silent download

The last point is the one that makes this real. Left to itself, faster-whisper
resolves `"base"` by asking huggingface.co. On a primed machine that call is a
wasted round trip; on a machine with no route it is a hang followed by a
confusing failure. So the bundle ships the weights and this module points at
them by path, and `HF_HUB_OFFLINE` is set before faster-whisper is imported.

Everything degrades: with no bundle, every lookup falls through to the system
install, which is how a checkout of the repo keeps working for development.
"""

import os
import shutil
import sys
from pathlib import Path


# Layout inside a packaged bundle:
#   Caption Inspector v7.0.app/Contents/Resources/
#       python/            the app's own sources
#       vendor/bin/        ffmpeg, ffprobe
#       vendor/lib/        their dylibs
#       vendor/site-packages/
#       models/faster-whisper-base/   config.json, model.bin, tokenizer.json, ...
BUNDLE_MARKER = "caption-inspector-bundle.txt"

MODEL_DIRECTORY_PREFIX = "faster-whisper-"

_ENVIRONMENT_APPLIED = False


def resource_root():
    """The directory holding `vendor/` and `models/`, or None outside a bundle.

    `CAPTION_INSPECTOR_RESOURCES` wins, so the build script can point a test run
    at a staged bundle before the .app is assembled.
    """
    override = os.environ.get("CAPTION_INSPECTOR_RESOURCES")
    if override and (Path(override) / BUNDLE_MARKER).exists():
        return Path(override)

    # python/offline.py -> python/ -> Resources/
    here = Path(__file__).resolve().parent
    for candidate in (here.parent, here.parent.parent):
        if (candidate / BUNDLE_MARKER).exists():
            return candidate

    # Frozen builds put resources beside the executable.
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        for candidate in (executable_dir, executable_dir.parent / "Resources"):
            if (candidate / BUNDLE_MARKER).exists():
                return candidate

    return None


def is_bundled():
    return resource_root() is not None


def vendor_bin():
    root = resource_root()
    if root is None:
        return None
    candidate = root / "vendor" / "bin"
    return candidate if candidate.is_dir() else None


def models_root():
    root = resource_root()
    if root is None:
        return None
    candidate = root / "models"
    return candidate if candidate.is_dir() else None


# ---------------------------------------------------------------- executables


def find_executable(name):
    """Bundled copy first, then PATH.

    The bundled binary is preferred even when a system one exists: the whole
    point of the bundle is that its behaviour does not depend on what happens to
    be installed on the machine.
    """
    bin_dir = vendor_bin()
    if bin_dir:
        candidate = bin_dir / name
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)

    return shutil.which(name)


# --------------------------------------------------------------------- models


def bundled_model_names():
    """Model sizes shipped in the bundle, e.g. ['base', 'tiny']."""
    root = models_root()
    if root is None:
        return []

    names = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if not (child / "model.bin").exists():
            continue
        name = child.name
        if name.startswith(MODEL_DIRECTORY_PREFIX):
            name = name[len(MODEL_DIRECTORY_PREFIX):]
        names.append(name)
    return names


def resolve_model(model_size):
    """Return what to hand `WhisperModel` for `model_size`.

    A vendored directory path when the bundle carries it, otherwise the bare
    name, which faster-whisper resolves against the local Hugging Face cache.
    """
    root = models_root()
    if root is None:
        return model_size

    for candidate in (
        root / f"{MODEL_DIRECTORY_PREFIX}{model_size}",
        root / model_size,
    ):
        if (candidate / "model.bin").exists():
            return str(candidate)

    return model_size


# Where a model was found, in the order the app prefers them.
BUNDLED = "bundled"
CACHED = "cached"
MISSING = "missing"


def model_status(model_size):
    """Where `model_size` lives: BUNDLED, CACHED, or MISSING."""
    root = models_root()
    if root is not None and resolve_model(model_size) != model_size:
        return BUNDLED
    if _in_huggingface_cache(model_size):
        return CACHED
    return MISSING


def model_is_available(model_size):
    """True when this model can be loaded without a network."""
    return model_status(model_size) != MISSING


def available_models(candidates):
    """The subset of `candidates` this machine can actually load right now."""
    return [size for size in candidates if model_is_available(size)]


def network_allowed():
    """Whether this install is permitted to fetch a model it does not have.

    A packaged bundle never is - that is the point of it. A working tree is,
    because a developer adding a model expects it to just download.
    """
    if os.environ.get("CAPTION_INSPECTOR_ALLOW_NETWORK") == "1":
        return True
    return not is_bundled()


def describe_missing_model(model_size, known_sizes=()):
    """Say what is wrong and exactly how to fix it.

    "Not available locally" on its own leaves someone stuck: the next step
    depends on whether they are running a sealed bundle or a working tree, and
    they should not have to know that.
    """
    have = available_models(known_sizes) if known_sizes else bundled_model_names()
    have_text = ", ".join(have) if have else "none"

    lines = [
        f"The {model_size!r} transcription model is not installed on this machine.",
        f"Models available right now: {have_text}.",
    ]

    if is_bundled():
        lines += [
            "",
            "This is a self-contained build, so it will not download one. To add "
            f"{model_size!r} to it, run this against the app on a machine with a network:",
            "",
            f"    python3 packaging/stage_model.py --model {model_size} \\",
            "        \"/Applications/Caption Inspector v7.0.app\"",
            "",
            "Or build a bundle that carries it from the start:",
            "",
            f"    make offline-app MODELS=base,{model_size}",
        ]
    elif network_allowed():
        lines += [
            "",
            f"Download it once with:",
            "",
            f"    python3 -c \"from faster_whisper import WhisperModel; "
            f"WhisperModel('{model_size}')\"",
        ]
    else:
        lines += [
            "",
            "Model downloads are switched off for this process. Set "
            "CAPTION_INSPECTOR_ALLOW_NETWORK=1 to allow one, or choose a model "
            "listed above.",
        ]

    return "\n".join(lines)


def _in_huggingface_cache(model_size):
    home = os.environ.get("HF_HOME")
    hub = Path(home) / "hub" if home else Path.home() / ".cache" / "huggingface" / "hub"
    if not hub.is_dir():
        return False

    # faster-whisper's published repos are Systran/faster-whisper-<size>.
    for entry in hub.glob(f"models--*faster-whisper-{model_size}"):
        snapshots = entry / "snapshots"
        if not snapshots.is_dir():
            continue
        for snapshot in snapshots.iterdir():
            if (snapshot / "model.bin").exists():
                return True
    return False


# ---------------------------------------------------------------- environment


def apply_environment():
    """Pin the process to local resources. Safe and cheap to call repeatedly.

    Must run before `faster_whisper` is imported, because huggingface_hub reads
    HF_HUB_OFFLINE at import time.
    """
    global _ENVIRONMENT_APPLIED
    if _ENVIRONMENT_APPLIED:
        return

    # An explicit opt-out, for a developer who wants to pull a new model.
    if os.environ.get("CAPTION_INSPECTOR_ALLOW_NETWORK") == "1":
        _ENVIRONMENT_APPLIED = True
        return

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    # Stops huggingface_hub spending a second on a symlink-support probe.
    os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")

    bin_dir = vendor_bin()
    if bin_dir:
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}" + os.environ.get("PATH", "")

    _ENVIRONMENT_APPLIED = True


def status_lines():
    """What the app resolved, for the runtime panel and `--check`."""
    lines = []
    root = resource_root()
    if root is None:
        lines.append("Running from the repository (not a packaged bundle).")
    else:
        lines.append(f"Bundled resources: {root}")

    for name in ("ffmpeg", "ffprobe"):
        found = find_executable(name)
        if not found:
            lines.append(f"{name}: not found")
        elif vendor_bin() and str(found).startswith(str(vendor_bin())):
            lines.append(f"{name}: bundled")
        else:
            lines.append(f"{name}: system ({found})")

    bundled = bundled_model_names()
    if bundled:
        lines.append(f"Bundled transcription models: {', '.join(bundled)}")
    else:
        lines.append("Bundled transcription models: none")

    offline = os.environ.get("HF_HUB_OFFLINE") == "1"
    lines.append(
        "Model downloads: disabled (offline)" if offline else "Model downloads: allowed"
    )
    return lines
