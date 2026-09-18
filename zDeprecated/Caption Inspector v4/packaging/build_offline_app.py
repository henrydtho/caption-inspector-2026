#!/usr/bin/env python3
"""Build a fully self-contained Caption Inspector v4.app.

The output depends on macOS and nothing else: no Homebrew, no Python install, no
Hugging Face cache, no network - at build time or ever after. Everything the app
needs is copied inside the bundle and every absolute path pointing back out is
rewritten.

    python3 packaging/build_offline_app.py --models base

What goes in:

    Resources/runtime/Python.framework   the interpreter, pruned
    Resources/vendor/site-packages       faster-whisper and its dependencies
    Resources/vendor/bin, vendor/lib     ffmpeg and ffprobe, with their dylibs
    Resources/models/faster-whisper-*    the transcription weights
    Resources/python                     the app itself

The one online step is `pip install --target`, which happens on the build
machine. Pass `--wheels DIR` to install from a directory of wheels instead, and
the build needs no network either.

Why the relocation matters: a copied Mach-O binary keeps the absolute paths it
was linked against. Copy Homebrew's ffmpeg into a bundle and it still loads
dylibs out of /opt/homebrew, so the bundle works on this machine and nowhere
else. `_relocate` rewrites those references to be relative to the binary's own
location, and re-signs each one, because editing a signed arm64 binary
invalidates its signature and macOS then refuses to load it.
"""

import argparse
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "Caption Inspector v4"
BUNDLE_ID = "com.local.captioninspector.v4"
APP_VERSION = "4.0"

# The app's own sources. Everything else in python/ is either generated or the
# platform library, which is copied separately.
APP_MODULES = [
    "alignment.py",
    "app.py",
    "cancellation.py",
    "caption_cues.py",
    "caption_inspector_app.py",
    "caption_timing.py",
    "cshim.py",
    "drift_report.py",
    "inspection_support.py",
    "launch_app.py",
    "media_probe.py",
    "offline.py",
    "subtitle_formats.py",
    "sync_check.py",
    "sync_cli.py",
    "sync_panel.py",
    "timecode.py",
    "transcribe.py",
    "transcript_check.py",
    "transcript_cli.py",
    "transcript_export.py",
    "transcript_panel.py",
]

SHARED_LIBRARIES = ["libci.1.0.0.dylib", "libci.dylib"]

# faster-whisper pulls ctranslate2, tokenizers, onnxruntime, huggingface_hub and
# numpy. Pinning the top of the tree and letting pip resolve the rest keeps the
# set honest.
PIP_REQUIREMENTS = ["faster-whisper>=1.0"]

# Cut from the interpreter: its test suite, its IDE, its docs, and its own
# site-packages, which is where whatever the developer happened to pip install
# lives. Ours goes in vendor/site-packages instead.
FRAMEWORK_PRUNE = [
    "lib/python{v}/test",
    "lib/python{v}/idlelib",
    "lib/python{v}/tkinter/test",
    "lib/python{v}/lib2to3",
    "lib/python{v}/site-packages",
    "lib/python{v}/ensurepip",
    "lib/python{v}/distutils/tests",
    "share",
    "Resources/English.lproj/Documentation",
    "include",
]

SYSTEM_PREFIXES = ("/usr/lib", "/System/")

# The bundle being built. `_signing_target` stops its search for an enclosing
# bundle here: the output is itself an .app, and without this boundary every
# binary inside it would resolve to the whole app, so nothing nested would ever
# be signed individually.
BUNDLE_ROOT = None


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
# Mach-O relocation
# ---------------------------------------------------------------------------


def _install_names(binary):
    """The binary's own LC_ID_DYLIB values, one per architecture slice."""
    try:
        output = run(["otool", "-D", str(binary)])
    except RuntimeError:
        return set()

    names = set()
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.endswith(":"):
            continue
        names.add(stripped)
    return names


def _dependencies(binary):
    """Non-system dylib references of `binary`, as written in its header.

    A dylib's own install name appears in `otool -L` output alongside its real
    dependencies, and it must not be treated as one. Rewriting it with
    `-change` turns the library into its own dependency - dyld cannot resolve
    that, and the process loading it is killed with SIGKILL and no diagnostic.
    The install name is rewritten separately, with `-id`.
    """
    try:
        output = run(["otool", "-L", str(binary)])
    except RuntimeError:
        return []

    own = _install_names(binary)

    dependencies = []
    for line in output.splitlines()[1:]:
        stripped = line.strip()
        if not stripped or stripped.endswith(":"):
            continue
        reference = stripped.split(" (")[0].strip()
        if not reference or reference.startswith(SYSTEM_PREFIXES):
            continue
        if reference in own:
            continue
        if reference in dependencies:
            continue
        dependencies.append(reference)
    return dependencies


def _rpaths(binary):
    try:
        output = run(["otool", "-l", str(binary)])
    except RuntimeError:
        return []

    paths = []
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if "LC_RPATH" not in line:
            continue
        for follow in lines[index:index + 6]:
            if "path " in follow:
                paths.append(follow.strip().split("path ", 1)[1].split(" (")[0].strip())
                break
    return paths


def _resolve(reference, binary):
    """Turn a header reference into a real file on disk, or None."""
    holder = Path(binary).resolve().parent

    if reference.startswith("@loader_path"):
        candidate = holder / reference.replace("@loader_path", "").lstrip("/")
        return candidate if candidate.exists() else None

    if reference.startswith("@executable_path"):
        candidate = holder / reference.replace("@executable_path", "").lstrip("/")
        return candidate if candidate.exists() else None

    if reference.startswith("@rpath"):
        tail = reference.replace("@rpath", "").lstrip("/")
        for rpath in _rpaths(binary):
            base = rpath.replace("@loader_path", str(holder)).replace(
                "@executable_path", str(holder)
            )
            candidate = Path(base) / tail
            if candidate.exists():
                return candidate
        return None

    candidate = Path(reference)
    return candidate if candidate.exists() else None


def _signing_target(path):
    """What codesign has to be pointed at to sign `path`.

    A framework's *main* binary cannot be signed on its own - codesign insists on
    sealing the versioned framework directory instead. But that redirection has
    to apply only to the main binary. Every other Mach-O nested inside a
    framework - and the interpreter's extension modules all are - must be signed
    as a plain file. Sealing the enclosing framework does not repair a nested
    .so whose own signature install_name_tool invalidated, and macOS then kills
    the process that loads it with SIGKILL and no diagnostic.
    """
    resolved = Path(path).resolve()
    boundary = Path(BUNDLE_ROOT).resolve() if BUNDLE_ROOT else None

    # `parents` is nearest-first, so the innermost enclosing bundle wins. That
    # matters: the interpreter ships a nested Python.app whose executable is
    # also called "Python", and it must be sealed as that .app rather than
    # mistaken for the enclosing framework's main binary.
    for parent in resolved.parents:
        if boundary is not None and parent == boundary:
            # Reached the bundle we are building; this binary is signed alone.
            return resolved

        if parent.suffix == ".app":
            return parent

        if parent.suffix != ".framework":
            continue

        versions = parent / "Versions"
        is_main_binary = resolved.name == parent.stem and (
            resolved.parent == parent or resolved.parent.parent == versions
        )
        if not is_main_binary:
            return resolved

        if resolved.parent.parent == versions:
            return resolved.parent
        return parent

    return resolved


def _sign(path):
    """Ad-hoc re-sign. Mandatory after install_name_tool on arm64."""
    target = _signing_target(path)
    try:
        run(["codesign", "--force", "--sign", "-", str(target)])
    except RuntimeError as error:
        log(f"warning: could not sign {target.name}: {str(error)[:160]}")


def collect_and_relocate(binaries, library_dir, rpath_for):
    """Copy every non-system dependency next to the binaries and rewrite refs.

    `binaries` are files already inside the bundle. `library_dir` is where the
    dylibs are gathered. `rpath_for(path)` returns the LC_RPATH to add to a
    given binary so `@rpath/<name>` resolves to `library_dir`.

    Runs until the dependency closure is empty, so a dylib pulled in by another
    dylib is handled too.
    """
    library_dir.mkdir(parents=True, exist_ok=True)
    pending = list(binaries)
    seen = set()
    copied = {}
    # Every binary whose header we altered. Adding an LC_RPATH counts: it
    # invalidates the signature just as a -change does, and an unsigned-but-
    # modified dylib is fatal at load time.
    touched = []

    while pending:
        binary = pending.pop()
        key = str(binary)
        if key in seen:
            continue
        seen.add(key)

        changes = []
        for reference in _dependencies(binary):
            resolved = _resolve(reference, binary)
            if resolved is None:
                continue

            # Already inside the bundle's own library directory: leave it be.
            try:
                if Path(resolved).resolve().parent == library_dir.resolve():
                    if not reference.startswith("@rpath"):
                        changes.append((reference, f"@rpath/{Path(resolved).name}"))
                    continue
            except OSError:
                pass

            name = Path(resolved).name
            destination = library_dir / name
            if name not in copied:
                shutil.copy2(Path(resolved).resolve(), destination)
                destination.chmod(0o755)
                copied[name] = destination
                pending.append(destination)

            changes.append((reference, f"@rpath/{name}"))

        for old, new in changes:
            run(["install_name_tool", "-change", old, new, str(binary)])

        if binary.parent.resolve() == library_dir.resolve():
            run(["install_name_tool", "-id", f"@rpath/{binary.name}", str(binary)])

        _ensure_rpath(binary, rpath_for(binary))
        touched.append(binary)

    # Signed after every header edit is finished, so nothing is signed twice or
    # signed and then modified again.
    for binary in touched:
        _sign(binary)

    return copied


def _ensure_rpath(binary, rpath):
    if not rpath:
        return
    if rpath in _rpaths(binary):
        return
    try:
        run(["install_name_tool", "-add_rpath", rpath, str(binary)])
    except RuntimeError:
        pass


# ---------------------------------------------------------------------------
# Python framework
# ---------------------------------------------------------------------------


def framework_version_dir():
    """The Python.framework/Versions/X.Y this interpreter runs from."""
    prefix = Path(sysconfig.get_config_var("prefix") or sys.prefix).resolve()
    if "Python.framework" not in str(prefix):
        raise SystemExit(
            "This build needs a framework Python (the python.org installer).\n"
            f"The interpreter running this script is {sys.executable}, whose prefix is\n"
            f"  {prefix}\n"
            "which is not inside a Python.framework, so there is no relocatable "
            "interpreter to embed.\n"
            "Install Python from python.org and re-run this script with it."
        )
    return prefix


def copy_framework(destination, thin=False):
    """Copy and prune the interpreter, then make it position-independent."""
    source = framework_version_dir()
    version = f"{sys.version_info.major}.{sys.version_info.minor}"

    target_root = destination / "Python.framework" / "Versions" / version
    target_root.parent.mkdir(parents=True, exist_ok=True)

    log(f"copying interpreter from {source}")
    shutil.copytree(source, target_root, symlinks=True, dirs_exist_ok=True)

    for pattern in FRAMEWORK_PRUNE:
        victim = target_root / pattern.format(v=version)
        if victim.is_dir():
            shutil.rmtree(victim, ignore_errors=True)
        elif victim.exists():
            victim.unlink()

    for cache in target_root.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    for static in target_root.rglob("*.a"):
        static.unlink(missing_ok=True)

    _prune_console_scripts(target_root, version)

    # Framework/Versions/Current -> X.Y, so the launcher can be version-agnostic.
    current = destination / "Python.framework" / "Versions" / "Current"
    if current.is_symlink() or current.exists():
        current.unlink()
    current.symlink_to(version)

    if thin:
        _thin_binaries(target_root)

    # Pruned before relocation: a loose *Config.sh inside Tcl.framework makes
    # codesign refuse to seal it, so removing them first keeps the signing pass
    # from emitting failures it then has to repair.
    _prune_nested_frameworks(target_root)
    remove_broken_symlinks(target_root)
    _relocate_framework(target_root, source, version)
    _seal_frameworks(target_root, version)
    return target_root


def remove_broken_symlinks(root):
    """Delete symlinks whose target does not exist.

    Pruning the interpreter leaves a few behind - `Headers` pointed into the
    `include` directory that was removed, and the framework ships an
    `etc/openssl/cert.pem` link into a path that does not travel with it. A
    dangling link is not cosmetic: `codesign --verify` fails on the whole bundle
    with "No such file or directory", naming the bundle rather than the link, so
    the app reads as unsigned to Gatekeeper and cannot be distributed.
    """
    removed = []
    for path in Path(root).rglob("*"):
        if path.is_symlink() and not path.exists():
            removed.append(str(path.relative_to(root)))
            path.unlink(missing_ok=True)

    if removed:
        log(f"removed {len(removed)} dangling symlink(s): {', '.join(removed[:4])}")
    return removed


def _prune_nested_frameworks(root):
    """Strip build-time files out of the nested Tcl/Tk frameworks.

    Headers, pkgconfig data and tclConfig.sh are useless at runtime, and they
    actively break signing: codesign treats a loose .sh inside a framework as
    unsigned nested code and refuses to seal the bundle at all.
    """
    frameworks = root / "Frameworks"
    if not frameworks.is_dir():
        return

    removed = 0
    for framework in frameworks.glob("*.framework"):
        for version_dir in (framework / "Versions").glob("*"):
            if version_dir.is_symlink() or not version_dir.is_dir():
                continue
            victims = ["Headers", "PrivateHeaders", "pkgconfig", "_CodeSignature"]
            for name in victims:
                target = version_dir / name
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                    removed += 1
            for config in list(version_dir.glob("*Config.sh")):
                config.unlink(missing_ok=True)
                removed += 1

        for name in ("Headers", "PrivateHeaders"):
            link = framework / name
            if link.is_symlink():
                link.unlink()
        for config in list(framework.glob("*Config.sh")):
            if config.is_symlink():
                config.unlink()

    log(f"pruned {removed} build-time items from the nested frameworks")


def _seal_frameworks(root, version):
    """Sign nested frameworks, then the interpreter's own, innermost first.

    Signing outermost first would be undone immediately: sealing a framework
    covers everything nested inside it, so any later change invalidates the
    outer seal.
    """
    frameworks = root / "Frameworks"
    if frameworks.is_dir():
        for framework in sorted(frameworks.glob("*.framework")):
            for version_dir in sorted((framework / "Versions").glob("*")):
                if version_dir.is_symlink() or not version_dir.is_dir():
                    continue
                _sign(version_dir)

    _sign(root)
    log("sealed the interpreter frameworks")


def _prune_console_scripts(root, version):
    """Keep only the interpreter in bin/.

    The rest are console entry points for whatever the build machine happened to
    have installed - pip, streamlit, pyinstaller - and each one carries an
    absolute shebang pointing back at the original framework. They are dead
    weight in the bundle and they are references to the outside world.
    """
    keep = {
        "python3", f"python{version}", f"python{version}-config",
        "python3-config", "python", "pythonw",
    }
    bin_dir = root / "bin"
    if not bin_dir.is_dir():
        return

    removed = 0
    for item in list(bin_dir.iterdir()):
        if item.name in keep:
            continue
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
        else:
            item.unlink(missing_ok=True)
        removed += 1

    log(f"removed {removed} console scripts from the interpreter's bin/")


def _thin_binaries(root):
    """Drop the x86_64 half of the universal2 binaries."""
    log("thinning universal binaries to arm64")
    count = 0
    for path in list(root.rglob("*.so")) + list(root.rglob("*.dylib")):
        try:
            info = run(["lipo", "-info", str(path)])
        except RuntimeError:
            continue
        if "x86_64" not in info or "arm64" not in info:
            continue
        try:
            run(["lipo", str(path), "-thin", "arm64", "-output", str(path)])
            _sign(path)
            count += 1
        except RuntimeError:
            continue
    log(f"thinned {count} binaries")


def _relocate_framework(root, original_prefix, version):
    """Rewrite every absolute reference back into the original framework.

    The copy preserves the internal layout, so each absolute reference
    `<prefix>/lib/libX.dylib` becomes a `@loader_path`-relative walk from
    whichever binary holds it. Nothing else has to change: the `@rpath` and
    `@loader_path` references in the tree are already relative and move with it.
    """
    prefix = str(original_prefix).rstrip("/") + "/"
    log("relocating interpreter references")

    binaries = []
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if path.suffix in (".so", ".dylib") or path.name.startswith("python") or path.name in (
            "Python", "Tcl", "Tk"
        ):
            binaries.append(path)

    changed = 0
    for binary in binaries:
        changes = []
        for reference in _dependencies(binary):
            if not reference.startswith(prefix):
                continue
            relative_target = reference[len(prefix):]
            steps = os.path.relpath(root / relative_target, binary.parent)
            changes.append((reference, f"@loader_path/{steps}"))

        # The install name is rewritten with -id, never with -change.
        if any(name.startswith(prefix) for name in _install_names(binary)):
            run(["install_name_tool", "-id", f"@loader_path/{binary.name}", str(binary)])
            changes.append(("<id>", "rewritten"))

        for old, new in changes:
            if old == "<id>":
                continue
            run(["install_name_tool", "-change", old, new, str(binary)])

        if changes:
            _sign(binary)
            changed += 1

    log(f"relocated {changed} interpreter binaries")


# ---------------------------------------------------------------------------
# Dependencies and models
# ---------------------------------------------------------------------------


def install_wheels(target, wheel_dir=None):
    target.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable, "-m", "pip", "install",
        "--target", str(target),
        "--no-compile",
        "--upgrade",
    ]
    if wheel_dir:
        command += ["--no-index", "--find-links", str(wheel_dir)]
        log(f"installing dependencies from {wheel_dir} (offline)")
    else:
        log("installing dependencies from PyPI (the one online step)")
    command += PIP_REQUIREMENTS

    run(command)

    for cache in target.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    for junk in list(target.glob("*.dist-info/RECORD")) + list(target.glob("bin")):
        if junk.is_dir():
            shutil.rmtree(junk, ignore_errors=True)


def copy_ffmpeg(bin_dir, lib_dir):
    """Vendor ffmpeg and ffprobe with their entire dylib closure."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    copied = []

    for name in ("ffmpeg", "ffprobe"):
        source = shutil.which(name)
        if not source:
            raise SystemExit(
                f"{name} was not found on PATH, so it cannot be vendored into the bundle.\n"
                "Install FFmpeg on the build machine (brew install ffmpeg) and re-run."
            )
        destination = bin_dir / name
        shutil.copy2(Path(source).resolve(), destination)
        destination.chmod(0o755)
        copied.append(destination)
        log(f"vendored {name} from {source}")

    libraries = collect_and_relocate(
        copied,
        lib_dir,
        rpath_for=lambda path: (
            "@loader_path/../lib" if path.parent.name == "bin" else "@loader_path"
        ),
    )
    log(f"vendored {len(libraries)} support libraries for ffmpeg")
    return libraries


def relocate_decoder_library(resources):
    """Point libci at the bundle's own ffmpeg dylibs.

    The decoder is built against whatever FFmpeg the build machine has, so a
    straight copy still loads libavformat out of /opt/homebrew. Its dependencies
    are gathered into the same vendor/lib as ffmpeg's, which dedupes them since
    both were linked against the same install.
    """
    library_dir = resources / "vendor" / "lib"
    decoders = [
        path for path in (resources / "python").iterdir()
        if path.suffix == ".dylib" or path.name.endswith(".dylib")
    ]
    if not decoders:
        return {}

    collected = collect_and_relocate(
        decoders,
        library_dir,
        # python/ and vendor/lib/ are siblings under Resources.
        rpath_for=lambda path: (
            "@loader_path/../vendor/lib" if path.parent.name == "python" else "@loader_path"
        ),
    )
    log(f"relocated the decoder library against {len(collected)} bundled libraries")
    return collected


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
    """Copy model weights out of the Hugging Face cache into the bundle."""
    models_dir.mkdir(parents=True, exist_ok=True)
    staged = []

    for size in sizes:
        snapshot = _huggingface_snapshot(size)
        if snapshot is None:
            raise SystemExit(
                f"No cached weights found for the {size!r} model.\n"
                "Download it once on a machine with a network:\n"
                f"  python3 -c \"from faster_whisper import WhisperModel; "
                f"WhisperModel('{size}')\"\n"
                "then re-run this build."
            )

        destination = models_dir / f"faster-whisper-{size}"
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True)

        for item in snapshot.iterdir():
            # The cache stores blobs as symlinks; resolve them so the bundle
            # does not point back into the user's cache directory.
            shutil.copy2(item.resolve(), destination / item.name)

        size_mb = sum(f.stat().st_size for f in destination.iterdir()) / 1e6
        log(f"staged model {size} ({size_mb:.0f} MB)")
        staged.append(size)

    return staged


# ---------------------------------------------------------------------------
# Bundle assembly
# ---------------------------------------------------------------------------


LAUNCHER_SOURCE = Path(__file__).resolve().parent / "launcher.c"


def bundle_architecture():
    """The architecture this build targets.

    ffmpeg comes from the build machine's Homebrew and the wheels come from pip;
    neither ships universal binaries. An Intel build has to be made on an Intel
    Mac, running this same script.
    """
    return platform.machine()


def write_launcher(macos_dir, architecture):
    """Compile the bundle's main executable.

    A shell script here would be simpler and would work locally, but it makes the
    bundle undistributable: codesign classifies such a bundle as "app bundle with
    generic", `codesign --verify` fails on it, and Gatekeeper rejects it on any
    machine that downloaded it. A Mach-O main executable signs, verifies and
    notarizes normally.
    """
    if not LAUNCHER_SOURCE.exists():
        raise SystemExit(f"The launcher source is missing: {LAUNCHER_SOURCE}")

    macos_dir.mkdir(parents=True, exist_ok=True)
    launcher = macos_dir / "caption-inspector-app"

    run([
        "clang",
        "-arch", architecture,
        "-mmacosx-version-min=11.0",
        "-O2",
        "-Wall",
        "-Wextra",
        "-o", str(launcher),
        str(LAUNCHER_SOURCE),
    ])
    launcher.chmod(0o755)
    _sign(launcher)
    log(f"compiled the {architecture} launcher")
    return launcher


def write_info_plist(contents_dir, architecture):
    plist = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": APP_NAME,
        "CFBundleExecutable": "caption-inspector-app",
        "CFBundleIconFile": "AppIcon",
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": APP_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": APP_VERSION,
        "CFBundleVersion": APP_VERSION.split(".")[0],
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication",
        # No outbound connections, declared.
        "NSAppTransportSecurity": {"NSAllowsArbitraryLoads": False},
        # Everything vendored inside is this architecture only.
        "LSArchitecturePriority": [architecture],
        "CaptionInspectorArchitecture": architecture,
        "CaptionInspectorOffline": True,
    }
    (contents_dir / "Info.plist").write_bytes(plistlib.dumps(plist))


def copy_app_sources(resources):
    target = resources / "python"
    target.mkdir(parents=True, exist_ok=True)

    for name in APP_MODULES:
        source = REPO_ROOT / "python" / name
        if not source.exists():
            raise SystemExit(f"Expected app module is missing: {source}")
        shutil.copy2(source, target / name)

    found_library = False
    for name in SHARED_LIBRARIES:
        source = REPO_ROOT / "python" / name
        if source.exists():
            shutil.copy2(source, target / name)
            found_library = True

    if not found_library:
        raise SystemExit(
            "The caption decoder library was not found in python/.\n"
            "Build it first with `make sharedlib`, then re-run this script."
        )

    log(f"copied {len(APP_MODULES)} app modules and the decoder library")
    return target


def directory_size_mb(path):
    total = 0
    for item in Path(path).rglob("*"):
        if item.is_file() and not item.is_symlink():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total / 1e6


def build(output, models, thin, wheel_dir, skip_sign, force=False):
    global BUNDLE_ROOT
    app = Path(output)
    BUNDLE_ROOT = app

    if app.exists():
        # The repo ships a lightweight bundle at this same path - a launcher
        # script that runs the app from the working tree. Rebuilding over it
        # would destroy it silently, so anything without our marker needs
        # --force. Several hundred MB landing in a synced folder unannounced is
        # the other half of why this asks first.
        marker = app / "Contents" / "Resources" / "caption-inspector-bundle.txt"
        if not marker.exists() and not force:
            raise SystemExit(
                f"There is already a bundle at\n  {app}\n"
                "and it was not produced by this script (it carries no bundle marker).\n"
                "That is almost certainly the lightweight launcher bundle checked into the "
                "repo, which runs the app from the working tree.\n\n"
                "Build somewhere else:\n"
                "  python3 packaging/build_offline_app.py --output ~/Desktop/'Caption Inspector v4.app'\n"
                "or replace it deliberately:\n"
                "  python3 packaging/build_offline_app.py --force"
            )
        log(f"removing the previous bundle at {app}")
        shutil.rmtree(app)

    contents = app / "Contents"
    resources = contents / "Resources"
    resources.mkdir(parents=True)

    # The marker is how `offline.py` knows it is running inside a bundle.
    (resources / "caption-inspector-bundle.txt").write_text(
        f"{APP_NAME} {APP_VERSION}\n"
        f"architecture={bundle_architecture()}\n"
        "macos_minimum=11.0\n"
        "Self-contained build. Resources in this directory are the only ones the "
        "app uses; it makes no network connections.\n",
        encoding="utf-8",
    )

    architecture = bundle_architecture()
    copy_app_sources(resources)
    write_info_plist(contents, architecture)
    write_launcher(contents / "MacOS", architecture)

    icon_source = REPO_ROOT / "zDeprecated" / "Caption Inspector v3" / \
        "Caption Inspector v3.app" / "Contents" / "Resources" / "AppIcon.icns"
    if icon_source.exists():
        shutil.copy2(icon_source, resources / "AppIcon.icns")
        log("copied the app icon")

    copy_framework(resources / "runtime", thin=thin)
    install_wheels(resources / "vendor" / "site-packages", wheel_dir)
    # ffmpeg first: it populates vendor/lib with the av* dylibs that the decoder
    # library also needs, so the decoder's pass finds them already there.
    copy_ffmpeg(resources / "vendor" / "bin", resources / "vendor" / "lib")
    relocate_decoder_library(resources)
    copy_models(resources / "models", models)

    # Anything the wheels or ffmpeg brought in. Swept before signing, since a
    # dangling link fails verification of the whole bundle.
    remove_broken_symlinks(resources / "vendor")
    remove_broken_symlinks(resources / "python")

    if not skip_sign:
        log("signing the bundle (ad-hoc)")
        try:
            run(["codesign", "--force", "--deep", "--sign", "-", str(app)])
        except RuntimeError as error:
            log(f"warning: bundle signing failed: {str(error)[:200]}")

    print()
    print(f"Built {app}")
    print(f"  architecture: {bundle_architecture()}  (an Intel build must be made on an Intel Mac)")
    print(f"  total size: {directory_size_mb(app):.0f} MB")
    for part in ("runtime", "vendor/site-packages", "vendor/bin", "vendor/lib", "models", "python"):
        location = resources / part
        if location.exists():
            print(f"    {part:24s} {directory_size_mb(location):7.0f} MB")
    print()
    print("Verify it with:")
    print(f"  python3 packaging/verify_offline_app.py '{app}'")
    return app


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output",
        default=str(REPO_ROOT / f"{APP_NAME}.app"),
        help="Where to write the bundle",
    )
    parser.add_argument(
        "--models",
        default="base",
        help="Comma-separated model sizes to embed (default: base)",
    )
    parser.add_argument(
        "--thin",
        action="store_true",
        help="Drop the x86_64 slice, roughly halving the interpreter (Apple Silicon only)",
    )
    parser.add_argument(
        "--wheels",
        help="Install dependencies from this directory of wheels instead of PyPI",
    )
    parser.add_argument("--skip-sign", action="store_true", help="Skip the ad-hoc codesign pass")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing bundle that this script did not build",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if sys.platform != "darwin":
        raise SystemExit("This build script produces a macOS .app and only runs on macOS.")

    models = [size.strip() for size in args.models.split(",") if size.strip()]
    build(args.output, models, args.thin, args.wheels, args.skip_sign, args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
