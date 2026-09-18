# Offline packaging

Builds a `Caption Inspector v4.app` that depends on macOS and nothing else. No
Homebrew, no Python install, no Hugging Face cache, no network — not on first
launch, not ever.

```
make offline-app            # build with the base model
make verify-offline-app     # prove the result is self-contained
make dist                   # package it into a DMG + zip to send
```

Or directly, with options:

```
python3 packaging/build_offline_app.py --models base,tiny --thin
python3 packaging/verify_offline_app.py "Caption Inspector v4.app"
```

## What ends up inside

| Location under `Contents/Resources/` | Contents | Size |
|---|---|---|
| `runtime/Python.framework` | The interpreter, pruned of its test suite, IDLE, docs and site-packages | ~70 MB |
| `vendor/site-packages` | faster-whisper, ctranslate2, tokenizers, onnxruntime, numpy | ~180 MB |
| `vendor/bin`, `vendor/lib` | ffmpeg and ffprobe with their full dylib closure | ~37 MB |
| `models/faster-whisper-*` | Transcription weights | 75 MB (tiny) / 148 MB (base) / 2.9 GB (large-v3) |
| `python/` | The app, plus the `libci` decoder library | ~1 MB |

A base-model build is about **435 MB**, compressing to a 260 MB DMG or a 232 MB
zip. `--thin` drops the x86_64 slice from the interpreter, saving roughly 25 MB.

### Architecture

**A build is only as portable as the machine that made it.** Homebrew's ffmpeg
and pip's wheels are single-architecture, so of the 206 Mach-O files in an
Apple Silicon build, 114 are arm64-only — ffmpeg, its dylibs, `libci`, and every
wheel. Only the Python framework is universal.

So an `arm64` build does not run on an Intel Mac, and there is no way to make one
that does from a single machine. To ship both, run this same script on an Intel
Mac; the artifacts are named with their architecture (`...-arm64.dmg`) so the two
do not get confused.

The bundle states its architecture in `Info.plist` and in the bundle marker, and
the compiled launcher checks it at startup — an unsupported Mac gets a sentence
it can act on rather than a dyld "incompatible architecture" error thrown from
somewhere deep inside an import.

## Build requirements

The **build machine** needs a framework Python (the python.org installer — a
Homebrew or pyenv Python has no relocatable framework to embed), FFmpeg on PATH,
the decoder library built (`make sharedlib`), and the model weights already in
the Hugging Face cache:

```
python3 -c "from faster_whisper import WhisperModel; WhisperModel('base')"
```

The only step that reaches the network is `pip install --target`. To remove even
that, stage the wheels once and build from them:

```
python3 -m pip download -d packaging/wheels 'faster-whisper>=1.0'
python3 packaging/build_offline_app.py --wheels packaging/wheels
```

## How the relocation works, and why it is the whole job

A copied Mach-O binary keeps the absolute paths it was linked against. Copy
Homebrew's `ffmpeg` into a bundle and it still loads `libavcodec` out of
`/opt/homebrew`, so the bundle works on the build machine and nowhere else.

For every binary the build copies in, it walks the dependency closure, copies
each non-system dylib into `vendor/lib`, rewrites the reference to
`@rpath/<name>`, and adds an `LC_RPATH` that resolves `@rpath` relative to the
binary's own location. The interpreter is handled the same way, except its
internal references are rewritten `@loader_path`-relative, since the copy
preserves the framework's internal layout.

Then everything modified is re-signed ad-hoc. **This is not optional.** Editing a
signed arm64 binary invalidates its signature, and macOS kills the loading
process with `SIGKILL` and no diagnostic — an app that dies silently on launch.

Three details cost real debugging time and are worth knowing before touching
`build_offline_app.py`:

- **`otool -L` lists a dylib's own install name first.** Treating it as a
  dependency and rewriting it with `-change` makes the library its own
  dependency. dyld cannot resolve that. The install name is rewritten with
  `-id`, separately, and `_dependencies` filters it out via `otool -D`.
- **A framework's main binary must be signed as the framework.** Every *other*
  Mach-O nested inside one — and the interpreter's extension modules all are —
  must be signed as a plain file. Sealing the enclosing framework does not
  repair a nested `.so` whose own signature was invalidated. The bundle being
  built is itself an `.app`, so `_signing_target` stops its search at the bundle
  root; otherwise every binary resolves to the whole app and nothing nested gets
  signed at all.
- **Adding an `LC_RPATH` invalidates the signature** just as a `-change` does, so
  a dylib that needed no rewrites still has to be re-signed.

## Verifying

`verify_offline_app.py` is the part to trust, not the build log. Every check runs
the bundled interpreter under `env -i`, with `PATH`, `PYTHONPATH` and `HF_HOME`
absent — the closest thing to a machine that has never had Homebrew installed.

| Check | Catches |
|---|---|
| structure | Missing pieces |
| leaks | Any Mach-O still pointing at Homebrew, `$HOME`, or a system Python |
| signatures | Relocated binaries whose signatures were invalidated (the silent-`SIGKILL` cause) |
| interpreter | An interpreter that cannot start without the developer's environment |
| imports | Dependencies loading from outside the bundle; a broken Tk |
| resources | The app resolving system ffmpeg or cached weights instead of its own |
| hub | Model loading that still needs huggingface.co, tested with the endpoint pointed at a dead port |
| run | A real Tier 1 + Tier 2 + transcript pass over a generated WebVTT and `.mov` fixture |

An unsigned binary is *not* a failure — most PyPI wheels ship unsigned `.so`
files and macOS loads them happily. A *modified* signature is fatal, and the two
are reported separately.

The end-to-end check's fixture is a 36-second program whose captions stop well
before the last frame, so `tier1 verdict: FAIL` in that output is expected — the
coverage-ratio check is doing its job on a deliberately stubby fixture. What the
check asserts is that every stage ran, offline, from the bundle.

**`PYTHONDONTWRITEBYTECODE=1` is not optional** when invoking the bundled
interpreter. Without it, Python writes `__pycache__` into its own framework,
which adds files to a sealed bundle and invalidates the seal — so the act of
running the app would break its signature. The launcher sets it, and so does the
verifier; anything else that drives the bundled interpreter must too.

## Distribution

`make dist` verifies the bundle, then writes to `packaging/dist/`:

```
Caption-Inspector-v4-4.0-arm64.dmg    what to send
Caption-Inspector-v4-4.0-arm64.zip    for anything that dislikes DMGs
SHA256SUMS.txt                        so the recipient can check the download
FIRST-LAUNCH.txt                      what the recipient has to do
```

Verification runs *before* packaging, never after: an archive of a broken bundle
is worse than no archive, because it looks finished.

### Two things that silently break a shipped bundle

**Never package with `zip`.** The bundle contains 20 symlinks — the framework
version stubs. Plain `zip` follows them, producing an archive that unpacks into a
broken bundle far larger than the original. `ditto -c -k --sequesterRsrc
--keepParent` preserves symlinks, permissions and the signature, and macOS's
Archive Utility unpacks it correctly. `make dist` uses `ditto`.

**The main executable must be Mach-O.** It is tempting to make it a shell script.
Do not: such a bundle signs as "app bundle with generic", `codesign --verify`
fails on it, and Gatekeeper rejects it on any machine that downloaded it — no
matter how carefully everything inside was signed. `launcher.c` exists for this
reason and is compiled during the build.

A related trap: **a single dangling symlink anywhere inside fails verification of
the whole bundle**, reported against the bundle rather than the link, so the app
reads as unsigned. The build sweeps for them; the verifier checks.

### Signing for other people

Ad-hoc signing is enough to run on the machine that built it. For anyone else:

```
make dist SIGN_IDENTITY="Developer ID Application: NAME (TEAMID)" \
          NOTARY_PROFILE=my-notary-profile
```

This signs inside-out with the hardened runtime, submits the archive to Apple,
waits, staples the ticket, and rebuilds the archives around the stapled bundle.
Set the notary profile up once with:

```
xcrun notarytool store-credentials my-notary-profile \
  --apple-id you@example.com --team-id TEAMID
```

Note that `codesign --deep` is *not* used. Apple deprecated it, and it signs
nested code with the outer options — which is how notarization comes back
rejected for a nested dylib missing the hardened-runtime flag.

### Without a Developer ID

The DMG still works, but macOS quarantines anything that arrives by download,
email or file share, and Gatekeeper will not open an ad-hoc signed app so tagged.
The recipient has to clear it once:

```
xattr -dr com.apple.quarantine "/Applications/Caption Inspector v4.app"
```

Right-click → Open does **not** work here: the flag has to be cleared from the
nested libraries too, which only that command does. `FIRST-LAUNCH.txt` ships this
instruction alongside the DMG.

## Runtime behaviour

`python/offline.py` is what makes the bundle self-sufficient at runtime:

- `find_executable` prefers `vendor/bin` over `PATH`, so the bundled ffmpeg wins
  even where a system one exists.
- `resolve_model` returns a path into `models/`, so faster-whisper never asks the
  hub for a model name.
- `apply_environment` sets `HF_HUB_OFFLINE=1` **before** faster-whisper is
  imported, because `huggingface_hub` reads it at import time.
- `model_is_available` makes a missing model an explicit error instead of a
  silent download attempt.

All of it degrades: with no bundle around it, every lookup falls through to the
system install, which is how a plain checkout keeps working for development.
Set `CAPTION_INSPECTOR_ALLOW_NETWORK=1` to opt back in to hub downloads when
staging a new model.
