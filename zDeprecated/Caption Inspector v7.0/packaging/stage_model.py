#!/usr/bin/env python3
"""Add a transcription model to an already-built bundle.

    python3 packaging/stage_model.py --model medium "/Applications/Caption Inspector v7.0.app"

A self-contained bundle only carries the models it was built with, and it will
not download more - that is the point of it. Rebuilding to add one takes several
minutes and re-downloads every wheel; this copies the weights in and re-seals the
bundle, which takes seconds.

**Adding a file to a signed bundle invalidates its signature.** macOS then kills
the app on launch with SIGKILL and no message, so the re-sign at the end is not a
tidy-up step - skip it and you have bricked the app. If the bundle was signed
with a Developer ID, re-signing ad-hoc would silently downgrade it and break
Gatekeeper on every other machine, so that case stops and asks.

The weights come from the local Hugging Face cache. `--download` fetches them
first if they are not there; that is the only step that uses the network, and it
runs on the machine doing the staging, not on the machine running the app.
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "python"))

MODEL_DIRECTORY_PREFIX = "faster-whisper-"


def log(message):
    print(f"  {message}", flush=True)


def run(command, **kwargs):
    completed = subprocess.run(command, capture_output=True, text=True, **kwargs)
    if completed.returncode != 0:
        raise RuntimeError(
            f"{' '.join(str(part) for part in command)} failed:\n"
            f"{(completed.stderr or completed.stdout or '').strip()[-1500:]}"
        )
    return completed.stdout


def resources_of(app):
    resources = app / "Contents" / "Resources"
    marker = resources / "caption-inspector-bundle.txt"
    if not marker.exists():
        raise SystemExit(
            f"{app} is not a self-contained build (no bundle marker).\n"
            "Only bundles produced by packaging/build_offline_app.py carry their own "
            "models; the lightweight launcher bundle uses whatever is installed on the "
            "machine."
        )
    return resources


def signing_authority(app):
    """The identity the bundle is currently signed with, or None for ad-hoc."""
    described = subprocess.run(
        ["codesign", "-dvv", str(app)], capture_output=True, text=True
    )
    for line in (described.stderr or "").splitlines():
        if line.startswith("Authority="):
            return line.split("=", 1)[1].strip()
    return None


def cache_snapshot(model_size):
    """Locate `model_size` in the local Hugging Face cache."""
    import os

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


def fetch(model_size):
    """Download the weights into the local cache."""
    log(f"downloading the {model_size} model into the local cache")
    import os

    environment = dict(os.environ)
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
        environment.pop(name, None)

    completed = subprocess.run(
        [sys.executable, "-c",
         f"from faster_whisper import WhisperModel; WhisperModel('{model_size}')"],
        capture_output=True, text=True, env=environment,
    )
    if completed.returncode != 0:
        raise SystemExit(
            f"Could not download the {model_size} model:\n"
            f"{(completed.stderr or completed.stdout or '').strip()[-1500:]}"
        )


def stage(app, model_size, allow_download, identity):
    resources = resources_of(app)
    models = resources / "models"
    models.mkdir(parents=True, exist_ok=True)

    destination = models / f"{MODEL_DIRECTORY_PREFIX}{model_size}"
    if destination.exists():
        log(f"{model_size} is already staged; replacing it")
        shutil.rmtree(destination)

    snapshot = cache_snapshot(model_size)
    if snapshot is None:
        if not allow_download:
            raise SystemExit(
                f"No cached weights for the {model_size!r} model on this machine.\n"
                "Re-run with --download to fetch them, or download once with:\n"
                f"  python3 -c \"from faster_whisper import WhisperModel; "
                f"WhisperModel('{model_size}')\""
            )
        fetch(model_size)
        snapshot = cache_snapshot(model_size)
        if snapshot is None:
            raise SystemExit(
                f"The {model_size} download reported success but no weights appeared in "
                "the cache."
            )

    destination.mkdir(parents=True)
    for item in snapshot.iterdir():
        # Cache entries are symlinks into blobs; resolve them so the bundle does
        # not point back at the staging machine's cache directory.
        if item.is_dir():
            continue
        shutil.copy2(item.resolve(), destination / item.name)

    size_mb = sum(f.stat().st_size for f in destination.iterdir()) / 1e6
    log(f"staged {model_size} ({size_mb:.0f} MB) into {destination.relative_to(app)}")

    # Adding files broke the seal. Re-sign, or the app is killed on launch.
    log("re-signing the bundle")
    options = ["--force", "--sign", identity or "-"]
    if identity:
        options = ["--force", "--options", "runtime", "--timestamp", "--sign", identity]
    run(["codesign", *options, str(app)])
    run(["codesign", "--verify", "--strict", str(app)])
    log("signature verified")

    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("app", help="Path to the self-contained .app bundle")
    parser.add_argument("--model", required=True, help="Model size to add, e.g. medium")
    parser.add_argument(
        "--download",
        action="store_true",
        help="Fetch the weights if this machine does not already have them",
    )
    parser.add_argument(
        "--identity",
        help="Re-sign with this Developer ID instead of ad-hoc (required if the bundle "
             "was Developer ID signed)",
    )
    args = parser.parse_args(argv)

    if sys.platform != "darwin":
        raise SystemExit("This edits a macOS .app and only runs on macOS.")

    app = Path(args.app).resolve()
    if not app.exists():
        raise SystemExit(f"No bundle at {app}")

    from transcribe import MODEL_SIZES

    if args.model not in MODEL_SIZES:
        raise SystemExit(
            f"Unknown model size {args.model!r}. Choose one of: {', '.join(MODEL_SIZES)}."
        )

    authority = signing_authority(app)
    if authority and not args.identity:
        raise SystemExit(
            f"This bundle is signed by:\n  {authority}\n\n"
            "Re-signing it ad-hoc would break Gatekeeper for everyone you sent it to.\n"
            "Either pass the same identity:\n"
            f'  python3 packaging/stage_model.py --model {args.model} '
            f'--identity "{authority}" "{app}"\n'
            "or build a fresh bundle that includes the model:\n"
            f"  make offline-app MODELS=base,{args.model}"
        )

    print(f"Staging {args.model} into {app.name}\n")
    stage(app, args.model, args.download, args.identity)

    print()
    print(f"Done. Verify the bundle still runs offline with:")
    print(f"  python3 packaging/verify_offline_app.py '{app}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
