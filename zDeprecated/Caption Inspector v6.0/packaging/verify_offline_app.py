#!/usr/bin/env python3
"""Prove a built bundle is actually self-contained.

    python3 packaging/verify_offline_app.py "Caption Inspector v6.0.app"

Each check tries to catch a specific way the bundle could be lying about its
independence:

    structure    the pieces are present at all
    leaks        no Mach-O binary still points at Homebrew, the user's home
                 directory, or a system Python
    interpreter  the embedded Python starts with an empty environment, which is
                 what a fresh machine looks like
    imports      faster-whisper and its stack load from the vendored copy
    resources    the app resolves the bundled ffmpeg and bundled weights
    hub          model loading works with the Hugging Face endpoint unreachable
    run          a real Tier 1 + Tier 2 pass over a generated fixture
    distributable  the bundle verifies as a signed app, so Gatekeeper will
                 accept it on a machine that did not build it

The interpreter checks run with `env -i` so nothing from this shell leaks in.
That is the whole point: PATH, PYTHONPATH and HF_HOME all absent.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent

# A reference into any of these from inside the bundle means it is not portable.
LEAK_PREFIXES = (
    "/opt/homebrew",
    "/usr/local/Cellar",
    "/usr/local/opt",
    "/Library/Frameworks/Python.framework",
    str(Path.home()),
)

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"


class Report:
    def __init__(self):
        self.rows = []

    def add(self, status, name, detail=""):
        self.rows.append((status, name, detail))
        marker = {PASS: "  ok  ", FAIL: " FAIL ", WARN: " warn "}[status]
        print(f"[{marker}] {name}")
        if detail:
            for line in str(detail).splitlines():
                print(f"          {line}")

    @property
    def failed(self):
        return any(status == FAIL for status, _, _ in self.rows)


def bundle_python(app):
    return app / "Contents" / "Resources" / "runtime" / "Python.framework" / \
        "Versions" / "Current" / "bin" / "python3"


def run_in_bundle(app, code, extra_env=None, timeout=900):
    """Run `code` in the bundled interpreter with a scrubbed environment.

    `env -i` is the closest thing to a machine that has never had Homebrew or
    python.org Python installed.
    """
    resources = app / "Contents" / "Resources"
    environment = {
        "HOME": os.environ.get("HOME", "/tmp"),
        "PYTHONHOME": str(resources / "runtime" / "Python.framework" / "Versions" / "Current"),
        "PYTHONPATH": f"{resources / 'python'}:{resources / 'vendor' / 'site-packages'}",
        "PATH": f"{resources / 'vendor' / 'bin'}:/usr/bin:/bin",
        "CAPTION_INSPECTOR_RESOURCES": str(resources),
        "HF_HUB_OFFLINE": "1",
        "TOKENIZERS_PARALLELISM": "false",
        # Without this the interpreter writes __pycache__ into its own framework,
        # which adds files to a sealed bundle and invalidates the seal - so the
        # act of verifying would break what it is verifying. The launcher sets
        # the same variable for the same reason.
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    environment.update(extra_env or {})

    command = ["/usr/bin/env", "-i"] + [f"{k}={v}" for k, v in environment.items()]
    command += [str(bundle_python(app)), "-c", code]

    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_structure(app, report):
    resources = app / "Contents" / "Resources"
    required = {
        "launcher": app / "Contents" / "MacOS" / "caption-inspector-app",
        "Info.plist": app / "Contents" / "Info.plist",
        "bundle marker": resources / "caption-inspector-bundle.txt",
        "app sources": resources / "python" / "caption_inspector_app.py",
        "decoder library": resources / "python" / "libci.1.0.0.dylib",
        "interpreter": bundle_python(app),
        "vendored packages": resources / "vendor" / "site-packages" / "faster_whisper",
        "vendored ffmpeg": resources / "vendor" / "bin" / "ffmpeg",
        "vendored ffprobe": resources / "vendor" / "bin" / "ffprobe",
        "models": resources / "models",
    }

    missing = [name for name, path in required.items() if not path.exists()]
    if missing:
        report.add(FAIL, "Bundle structure", "missing: " + ", ".join(missing))
        return False

    models = [p.name for p in (resources / "models").iterdir() if p.is_dir()]
    report.add(PASS, "Bundle structure", f"models embedded: {', '.join(models) or 'none'}")
    return bool(models)


# Mach-O magic, thin and universal, both endiannesses.
_MACHO_MAGIC = {
    b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce",
    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
}


def _is_macho(path):
    """Read the magic rather than guessing from the name.

    Guessing catches shell scripts sitting in a bin/ directory, and reports them
    as unsigned binaries - noise that buries the real findings.
    """
    try:
        with open(path, "rb") as handle:
            return handle.read(4) in _MACHO_MAGIC
    except OSError:
        return False


def _macho_files(root):
    for path in Path(root).rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if _is_macho(path):
            yield path


def check_leaks(app, report):
    """No binary in the bundle may reference a path outside it."""
    leaks = []
    scanned = 0

    for binary in _macho_files(app):
        scanned += 1
        result = subprocess.run(["otool", "-L", str(binary)], capture_output=True, text=True)
        if result.returncode != 0:
            continue
        for line in result.stdout.splitlines()[1:]:
            reference = line.strip().split(" (")[0].strip()
            if not reference or reference.endswith(":"):
                continue
            if reference.startswith(LEAK_PREFIXES):
                leaks.append(f"{binary.relative_to(app)} -> {reference}")

    if leaks:
        detail = "\n".join(leaks[:12])
        if len(leaks) > 12:
            detail += f"\n... and {len(leaks) - 12} more"
        report.add(FAIL, f"No external references ({scanned} binaries scanned)", detail)
        return False

    report.add(PASS, f"No external references ({scanned} binaries scanned)")
    return True


def check_signatures(app, report):
    """Every Mach-O in the bundle must carry a valid signature.

    This is the check for the failure mode that costs the most time to diagnose:
    relocating a binary invalidates its signature, and macOS then kills the
    loading process with SIGKILL and no message. An invalid signature here is
    the cause of an app that dies silently on launch.
    """
    invalid = []
    unsigned = 0
    scanned = 0

    for binary in _macho_files(app):
        scanned += 1
        result = subprocess.run(
            ["codesign", "--verify", str(binary)], capture_output=True, text=True
        )
        if result.returncode == 0:
            continue

        reason = (result.stderr or "").strip().splitlines()
        detail = reason[0].split(": ", 1)[-1] if reason else "unknown"

        # An unsigned binary is normal - most PyPI wheels ship unsigned .so
        # files and macOS loads them happily. A *modified* signature is the
        # fatal case: the kernel kills the loading process.
        if "not signed at all" in detail:
            unsigned += 1
            continue

        invalid.append(f"{binary.relative_to(app)}: {detail}")

    if invalid:
        detail = "\n".join(invalid[:12])
        if len(invalid) > 12:
            detail += f"\n... and {len(invalid) - 12} more"
        report.add(FAIL, f"Code signatures valid ({scanned} binaries)", detail)
        return False

    report.add(
        PASS,
        f"Code signatures valid ({scanned} binaries)",
        f"{unsigned} carry no signature, which is normal for PyPI wheels and loads fine"
        if unsigned else "",
    )
    return True


def check_distributable(app, report):
    """The bundle must verify as a signed app, not merely contain signed parts.

    Two things fail this in ways that are invisible until someone else tries to
    open the app:

    A script as the main executable signs as "app bundle with generic".
    `codesign --verify` fails on it and Gatekeeper refuses it on any machine that
    downloaded it, no matter how carefully the contents were signed.

    A dangling symlink anywhere inside fails verification of the *whole bundle*,
    reported against the bundle rather than the link, so the app reads as
    unsigned.
    """
    dangling = [
        str(path.relative_to(app))
        for path in app.rglob("*")
        if path.is_symlink() and not path.exists()
    ]
    if dangling:
        report.add(
            FAIL,
            "Bundle is distributable",
            "dangling symlinks fail verification of the whole bundle:\n"
            + "\n".join(dangling[:8]),
        )
        return False

    described = subprocess.run(
        ["codesign", "-dv", str(app)], capture_output=True, text=True
    )
    fmt = ""
    for line in (described.stderr or "").splitlines():
        if line.startswith("Format="):
            fmt = line.split("=", 1)[1].strip()

    if "Mach-O" not in fmt:
        report.add(
            FAIL,
            "Bundle is distributable",
            f"main executable is not Mach-O (Format={fmt or 'unknown'}). Gatekeeper "
            "rejects such a bundle on any machine that downloads it.",
        )
        return False

    verified = subprocess.run(
        ["codesign", "--verify", "--strict", str(app)], capture_output=True, text=True
    )
    if verified.returncode != 0:
        report.add(
            FAIL,
            "Bundle is distributable",
            (verified.stderr or "").strip()[-600:],
        )
        return False

    report.add(PASS, "Bundle is distributable", f"Format={fmt}, signature verifies")
    return True


def check_interpreter(app, report):
    result = run_in_bundle(app, "import sys; print(sys.version.split()[0]); print(sys.prefix)")
    if result.returncode != 0:
        report.add(FAIL, "Embedded interpreter starts", result.stderr.strip()[-800:])
        return False
    lines = result.stdout.strip().splitlines()
    report.add(PASS, "Embedded interpreter starts", f"Python {lines[0]}")
    return True


def check_imports(app, report):
    code = (
        "import faster_whisper, ctranslate2, tokenizers, numpy\n"
        "print('faster_whisper', faster_whisper.__file__)\n"
        "print('ctranslate2', ctranslate2.__version__)\n"
        "import tkinter\n"
        "root = tkinter.Tk(); root.withdraw()\n"
        "print('tkinter', tkinter.TkVersion, 'window created')\n"
        "root.destroy()\n"
    )
    result = run_in_bundle(app, code)
    if result.returncode != 0:
        detail = result.stderr.strip()[-1200:]
        if result.returncode == -9 or result.returncode == 137:
            detail = (
                "the interpreter was killed by the kernel (SIGKILL), which means one of the "
                "loaded binaries has an invalid code signature - see the signature check above"
            )
        report.add(FAIL, "Vendored dependencies import", detail)
        return False

    from_bundle = str(app) in result.stdout
    report.add(
        PASS if from_bundle else WARN,
        "Vendored dependencies import",
        result.stdout.strip() if from_bundle else
        "imported, but not from inside the bundle:\n" + result.stdout.strip(),
    )
    return True


def check_resources(app, report):
    code = (
        "import offline\n"
        "print('bundled:', offline.is_bundled())\n"
        "print('ffprobe:', offline.find_executable('ffprobe'))\n"
        "print('models:', offline.bundled_model_names())\n"
    )
    result = run_in_bundle(app, code)
    if result.returncode != 0:
        report.add(FAIL, "App resolves bundled resources", result.stderr.strip()[-800:])
        return False

    output = result.stdout
    resolved_inside = str(app) in output and "bundled: True" in output
    report.add(
        PASS if resolved_inside else FAIL,
        "App resolves bundled resources",
        output.strip(),
    )
    return resolved_inside


def check_hub_unreachable(app, report):
    """Load the model with the hub pointed at a dead port."""
    code = (
        "import offline; offline.apply_environment()\n"
        "from faster_whisper import WhisperModel\n"
        "import offline as o\n"
        "names = o.bundled_model_names()\n"
        "ref = o.resolve_model(names[0])\n"
        "WhisperModel(ref, device='auto', compute_type='int8')\n"
        "print('loaded', names[0], 'from', ref)\n"
    )
    result = run_in_bundle(
        app, code, extra_env={"HF_ENDPOINT": "http://127.0.0.1:9", "HF_HUB_OFFLINE": "1"}
    )
    if result.returncode != 0:
        report.add(FAIL, "Model loads with the network unreachable", result.stderr.strip()[-1200:])
        return False
    report.add(PASS, "Model loads with the network unreachable", result.stdout.strip())
    return True


FIXTURE_CODE = r"""
import subprocess, sys, os
from pathlib import Path
work = Path(os.environ["VERIFY_WORK"])
work.mkdir(parents=True, exist_ok=True)

import shutil
ffmpeg = shutil.which("ffmpeg")
print("ffmpeg in use:", ffmpeg)

# A short program with real speech, built with the bundled ffmpeg only.
lines = ["welcome back to the evening broadcast",
         "our correspondent reports from the harbour tonight",
         "shipping containers stacked along the northern pier",
         "the mayor declined to comment this afternoon"]
inputs, filters, mixes = [], [], []
for i, text in enumerate(lines):
    aiff = work / f"l{i}.aiff"
    subprocess.run(["/usr/bin/say", "-r", "170", "-o", str(aiff), text], check=True)
    inputs += ["-i", str(aiff)]
    delay = int((3.0 + i * 8.0) * 1000)
    filters.append(f"[{i}:a]adelay={delay}|{delay},aresample=48000[a{i}]")
    mixes.append(f"[a{i}]")
graph = ";".join(filters) + ";" + "".join(mixes) + f"amix=inputs={len(lines)}:duration=longest:normalize=0[out]"
subprocess.run([ffmpeg, "-v", "error", "-y", *inputs, "-filter_complex", graph,
                "-map", "[out]", "-ac", "1", "-ar", "48000", str(work / "d.wav")], check=True)
subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=160x90:r=25",
                "-i", str(work / "d.wav"), "-t", "36", "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", "-timecode", "00:58:30:00",
                str(work / "p.mov")], check=True)

# WebVTT, so the new parser is exercised inside the bundle too.
def stamp(x):
    return f"{int(x//3600):02d}:{int(x%3600//60):02d}:{x%60:06.3f}"
vtt = ["WEBVTT", ""]
for i, text in enumerate(lines):
    s = 3.0 + i * 8.0
    vtt += [str(i + 1), f"{stamp(s)} --> {stamp(s + 3.0)}", f"<v Reporter>{text}", ""]
(work / "p.vtt").write_text("\n".join(vtt), encoding="utf-8")

from sync_check import tier1_check, tier2_check
from transcript_check import build_transcript

t1 = tier1_check(str(work / "p.vtt"), str(work / "p.mov"))
print("tier1 verdict:", t1.verdict, "| checks:", len(t1.checks), "| errors:", t1.errors)

t2 = tier2_check(str(work / "p.vtt"), str(work / "p.mov"), model_size=sys.argv[1] if len(sys.argv) > 1 else "base",
                 language="en", transcript_cache=False)
print("tier2 verdict:", t2.verdict, "| errors:", t2.errors)
if t2.tier2:
    print("median offset ms: %.0f" % (t2.tier2["median_offset"] * 1000))
    print("matched: %d/%d" % (t2.tier2["matched"], t2.tier2["cues"]))

tr = build_transcript(str(work / "p.vtt"), str(work / "p.mov"), model_size="base", language="en")
print("transcript lines:", len(tr.lines), "| checked:", tr.checked)
print("verdict:", tr.verdict_sentence())
print("ALL_STAGES_OK")
"""


def check_real_run(app, report, work_dir):
    result = run_in_bundle(
        app,
        FIXTURE_CODE,
        extra_env={
            "VERIFY_WORK": str(work_dir),
            "HF_ENDPOINT": "http://127.0.0.1:9",
            "HF_HUB_OFFLINE": "1",
        },
    )
    if result.returncode != 0 or "ALL_STAGES_OK" not in result.stdout:
        report.add(
            FAIL,
            "End-to-end run, network unreachable",
            (result.stdout[-1500:] + "\n" + result.stderr[-1500:]).strip(),
        )
        return False

    report.add(PASS, "End-to-end run, network unreachable", result.stdout.strip())
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("app", help="Path to the built .app bundle")
    parser.add_argument(
        "--work",
        default="/tmp/caption-inspector-verify",
        help="Scratch directory for the generated fixture",
    )
    parser.add_argument("--skip-run", action="store_true", help="Skip the end-to-end pass")
    args = parser.parse_args(argv)

    app = Path(args.app).resolve()
    if not app.exists():
        raise SystemExit(f"No bundle at {app}")

    print(f"Verifying {app}\n")
    report = Report()

    check_structure(app, report)
    check_leaks(app, report)
    check_signatures(app, report)
    check_distributable(app, report)
    if check_interpreter(app, report):
        check_imports(app, report)
        check_resources(app, report)
        check_hub_unreachable(app, report)
        if not args.skip_run:
            check_real_run(app, report, Path(args.work))

    print()
    if report.failed:
        print("RESULT: this bundle is NOT self-contained. See the failures above.")
        return 1
    print("RESULT: the bundle runs entirely from its own resources, with no network.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
