#!/usr/bin/env python3
"""Package a built bundle into something you can actually send someone.

    python3 packaging/make_distributable.py "Caption Inspector v5.5.app"

Produces, in `packaging/dist/`:

    Caption Inspector v5-<version>-<arch>.dmg    what to send
    Caption Inspector v5-<version>-<arch>.zip    for anything that dislikes DMGs
    SHA256SUMS.txt                               so the recipient can check it
    FIRST-LAUNCH.txt                             what the recipient has to do

Two details decide whether the thing that arrives still works:

**Never use `zip`.** A `.app` with an embedded Python.framework is full of
symlinks (`Versions/Current`, the framework stubs), and plain `zip` follows them,
producing an archive that unpacks into a broken bundle several hundred MB larger
than it should be. `ditto -c -k --sequesterRsrc --keepParent` preserves symlinks,
permissions and the code signature. macOS's own Archive Utility does the right
thing on the receiving end.

**Quarantine.** Anything arriving by download, email or a file-sharing service
gets tagged `com.apple.quarantine`, and Gatekeeper refuses to launch an ad-hoc
signed app so tagged. Signing with a Developer ID and notarizing is the only fix
that scales; `--identity` wires that up. Without it, the recipient has to strip
the attribute by hand, and FIRST-LAUNCH.txt tells them how.
"""

import argparse
import hashlib
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = REPO_ROOT / "packaging" / "dist"


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


def bundle_metadata(app):
    plist_path = app / "Contents" / "Info.plist"
    if not plist_path.exists():
        raise SystemExit(f"{app} has no Info.plist - is that really an app bundle?")

    plist = plistlib.loads(plist_path.read_bytes())
    marker = app / "Contents" / "Resources" / "caption-inspector-bundle.txt"
    if not marker.exists():
        raise SystemExit(
            f"{app} carries no bundle marker, so it is not a self-contained build.\n"
            "It is probably the lightweight launcher bundle, which needs Python and "
            "FFmpeg on the target machine and is not distributable.\n"
            "Build the real one first:  make offline-app"
        )

    return {
        "name": plist.get("CFBundleName", app.stem),
        "version": plist.get("CFBundleShortVersionString", "0"),
        "architecture": plist.get("CaptionInspectorArchitecture", "unknown"),
        "minimum_macos": plist.get("LSMinimumSystemVersion", "11.0"),
    }


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


def sign_bundle(app, identity, entitlements=None):
    """Sign with a Developer ID, inside-out, with the hardened runtime.

    `--deep` is deliberately not used: Apple deprecated it and it signs nested
    code with the *outer* options, which is how notarization submissions come
    back rejected for a nested dylib missing the hardened runtime flag. Signing
    the nested Mach-O files first and the bundle last is the supported order.
    """
    log(f"signing with identity: {identity}")

    magic = {
        b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce",
        b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
    }

    def is_macho(path):
        try:
            with open(path, "rb") as handle:
                return handle.read(4) in magic
        except OSError:
            return False

    # Deepest first, so a nested framework is sealed before whatever contains it.
    nested = sorted(
        (path for path in app.rglob("*") if path.is_file() and not path.is_symlink()
         and is_macho(path)),
        key=lambda path: len(path.parts),
        reverse=True,
    )

    options = ["--force", "--options", "runtime", "--timestamp", "--sign", identity]
    if entitlements:
        options += ["--entitlements", str(entitlements)]

    for path in nested:
        try:
            run(["codesign", *options, str(path)])
        except RuntimeError as error:
            log(f"warning: could not sign {path.name}: {str(error)[:140]}")

    run(["codesign", *options, str(app)])
    run(["codesign", "--verify", "--strict", "--verbose=2", str(app)])
    log(f"signed and verified ({len(nested)} nested binaries)")


def notarize(archive, profile):
    """Submit to Apple and staple the ticket to the bundle."""
    log("submitting for notarization (this takes a few minutes)")
    output = run([
        "xcrun", "notarytool", "submit", str(archive),
        "--keychain-profile", profile, "--wait",
    ])
    print(output)
    if "status: Accepted" not in output:
        raise SystemExit(
            "Notarization did not come back Accepted. Inspect the log with:\n"
            "  xcrun notarytool log <submission-id> --keychain-profile "
            f"{profile}"
        )


def staple(app):
    log("stapling the notarization ticket")
    run(["xcrun", "stapler", "staple", str(app)])
    run(["xcrun", "stapler", "validate", str(app)])


# ---------------------------------------------------------------------------
# Archives
# ---------------------------------------------------------------------------


def make_zip(app, destination):
    """ditto, never zip - see the module docstring."""
    if destination.exists():
        destination.unlink()
    run([
        "ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
        str(app), str(destination),
    ])
    log(f"wrote {destination.name} ({destination.stat().st_size / 1e6:.0f} MB)")
    return destination


def make_dmg(app, destination, volume_name, extras=None):
    """A read-only compressed DMG with an /Applications shortcut."""
    if destination.exists():
        destination.unlink()

    staging = destination.parent / "_dmg_staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    log("staging the disk image")
    run(["ditto", str(app), str(staging / app.name)])
    (staging / "Applications").symlink_to("/Applications")

    for extra in extras or []:
        shutil.copy2(extra, staging / extra.name)

    run([
        "hdiutil", "create",
        "-volname", volume_name,
        "-srcfolder", str(staging),
        "-ov", "-format", "UDZO",
        str(destination),
    ])
    shutil.rmtree(staging, ignore_errors=True)
    log(f"wrote {destination.name} ({destination.stat().st_size / 1e6:.0f} MB)")
    return destination


def checksums(paths, destination):
    lines = []
    for path in paths:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        lines.append(f"{digest.hexdigest()}  {path.name}")

    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"wrote {destination.name}")
    return destination


FIRST_LAUNCH_SIGNED = """Caption Inspector {version} ({architecture})

Requires macOS {minimum_macos} or later on a Mac that runs {architecture} code.

To install: open the .dmg and drag Caption Inspector v5 to Applications.
Then launch it from Applications like any other app.

Nothing else is needed. The app carries its own Python, FFmpeg and speech model;
it does not require Homebrew, does not install anything, and never connects to
the internet.
"""

FIRST_LAUNCH_UNSIGNED = """Caption Inspector {version} ({architecture})

Requires macOS {minimum_macos} or later on a Mac that runs {architecture} code.

To install: open the .dmg and drag Caption Inspector v5 to Applications.

FIRST LAUNCH - this build is not signed with an Apple Developer ID, so macOS
will refuse to open it until you clear the download flag. Open Terminal and run:

    xattr -dr com.apple.quarantine "/Applications/Caption Inspector v5.5.app"

Then launch it from Applications as normal. You only do this once.

Right-clicking and choosing Open does NOT work for this app: the flag has to be
cleared from the nested libraries too, which only the command above does.

Nothing else is needed. The app carries its own Python, FFmpeg and speech model;
it does not require Homebrew, does not install anything, and never connects to
the internet.
"""


def write_first_launch(destination, metadata, signed):
    template = FIRST_LAUNCH_SIGNED if signed else FIRST_LAUNCH_UNSIGNED
    destination.write_text(template.format(**metadata), encoding="utf-8")
    log(f"wrote {destination.name}")
    return destination


# ---------------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("app", nargs="?", help="Path to the built .app bundle")
    parser.add_argument("--output", default=str(DIST_DIR), help="Where to write the artifacts")
    parser.add_argument(
        "--identity",
        help='Developer ID for signing, e.g. "Developer ID Application: Name (TEAMID)"',
    )
    parser.add_argument(
        "--notarize-profile",
        help="notarytool keychain profile; implies --identity and staples the ticket",
    )
    parser.add_argument("--entitlements", help="Entitlements plist for signing")
    parser.add_argument("--skip-verify", action="store_true", help="Skip the pre-flight verification")
    parser.add_argument("--zip-only", action="store_true", help="Do not build a DMG")
    args = parser.parse_args(argv)

    if sys.platform != "darwin":
        raise SystemExit("This packages a macOS .app and only runs on macOS.")

    app = Path(args.app) if args.app else REPO_ROOT / "Caption Inspector v5.5.app"
    app = app.resolve()
    if not app.exists():
        raise SystemExit(
            f"No bundle at {app}\n"
            "Build one first:  make offline-app"
        )

    metadata = bundle_metadata(app)
    print(
        f"Packaging {metadata['name']} {metadata['version']} "
        f"for {metadata['architecture']}, macOS {metadata['minimum_macos']}+\n"
    )

    # Verify before packaging, never after: an archive of a broken bundle is
    # worse than no archive, because it looks finished.
    if not args.skip_verify:
        log("verifying the bundle is self-contained")
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "packaging" / "verify_offline_app.py"),
             str(app), "--skip-run"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(result.stdout)
            raise SystemExit(
                "The bundle failed verification, so it has not been packaged.\n"
                "Fix it, or re-run with --skip-verify if you know what you are doing."
            )
        log("verified")

    identity = args.identity
    if args.notarize_profile and not identity:
        raise SystemExit("--notarize-profile needs --identity: Apple will not notarize ad-hoc code.")

    if identity:
        sign_bundle(app, identity, Path(args.entitlements) if args.entitlements else None)

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    stem = f"{metadata['name']}-{metadata['version']}-{metadata['architecture']}".replace(" ", "-")
    first_launch = write_first_launch(
        out_dir / "FIRST-LAUNCH.txt", metadata, signed=bool(args.notarize_profile)
    )

    artifacts = []
    zip_path = make_zip(app, out_dir / f"{stem}.zip")
    artifacts.append(zip_path)

    if args.notarize_profile:
        notarize(zip_path, args.notarize_profile)
        staple(app)
        # The stapled ticket lives in the bundle, so the archives are rebuilt.
        zip_path = make_zip(app, out_dir / f"{stem}.zip")

    if not args.zip_only:
        artifacts.append(
            make_dmg(app, out_dir / f"{stem}.dmg", f"{metadata['name']}", extras=[first_launch])
        )

    checksums(artifacts, out_dir / "SHA256SUMS.txt")

    print()
    print(f"Artifacts in {out_dir}:")
    for path in sorted(out_dir.iterdir()):
        if path.is_file():
            print(f"  {path.name:<52} {path.stat().st_size / 1e6:8.1f} MB")

    print()
    if args.notarize_profile:
        print("Signed, notarized and stapled. Recipients can open it straight from the DMG.")
    elif identity:
        print(
            "Signed but NOT notarized. macOS will still warn on a downloaded copy;\n"
            "add --notarize-profile to finish the job."
        )
    else:
        print(
            "Ad-hoc signed only. On any Mac that downloads this, Gatekeeper will block it\n"
            "until the recipient clears the quarantine flag - FIRST-LAUNCH.txt explains how.\n"
            "For real distribution, sign and notarize:\n"
            '  --identity "Developer ID Application: NAME (TEAMID)" --notarize-profile PROFILE'
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
