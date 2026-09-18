"""Dialogue transcription for Tier 2, via faster-whisper.

faster-whisper rather than the reference openai-whisper package: it is a
CTranslate2 reimplementation that runs several times faster on CPU, which
matters because this is expected to run on a workstation, not a GPU box.

The import is deliberately lazy. Tier 1 must stay runnable - and the app must
stay launchable - on a machine that has never installed it.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from media_probe import ProbeError, extract_audio


MODEL_SIZES = ("tiny", "base", "small", "medium", "large-v3")
DEFAULT_MODEL = "base"

CACHE_VERSION = 2
CACHE_DIR = Path.home() / "Library" / "Caches" / "CaptionInspector" / "transcripts"


class TranscriptionError(RuntimeError):
    """Raised when transcription cannot run or produces nothing usable."""


class Word:
    __slots__ = ("text", "start", "end")

    def __init__(self, text, start, end):
        self.text = text
        self.start = float(start)
        self.end = float(end)

    def as_dict(self):
        return {"text": self.text, "start": self.start, "end": self.end}


class Transcript:
    """Word-level transcript of the dialogue track."""

    def __init__(self, words, language=None, model_size=None, duration=None, from_cache=False):
        self.words = words
        self.language = language
        self.model_size = model_size
        self.duration = duration
        self.from_cache = from_cache
        self.normalized = [word.text for word in words]

    def __len__(self):
        return len(self.words)

    def as_dict(self):
        return {
            "version": CACHE_VERSION,
            "language": self.language,
            "model_size": self.model_size,
            "duration": self.duration,
            "words": [word.as_dict() for word in self.words],
        }

    @classmethod
    def from_dict(cls, payload, from_cache=False):
        words = [Word(item["text"], item["start"], item["end"]) for item in payload.get("words", [])]
        return cls(
            words,
            language=payload.get("language"),
            model_size=payload.get("model_size"),
            duration=payload.get("duration"),
            from_cache=from_cache,
        )


def faster_whisper_available():
    import importlib.util

    return importlib.util.find_spec("faster_whisper") is not None


def install_command():
    return [sys.executable, "-m", "pip", "install", "--user", "faster-whisper>=1.0"]


def install_faster_whisper(progress=None):
    """Install faster-whisper into the running interpreter's user site.

    Wired to an explicit button in the UI; nothing installs on its own.
    """
    if progress:
        progress("Installing faster-whisper (this downloads ~100 MB of wheels)...")

    completed = subprocess.run(install_command(), capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "").strip()
        raise TranscriptionError(f"Installing faster-whisper failed:\n{message[-2000:]}")

    if progress:
        progress("faster-whisper installed.")
    return True


def _cache_key(media_path, model_size, language):
    stat = Path(media_path).stat()
    seed = "|".join(
        [
            str(CACHE_VERSION),
            str(Path(media_path).resolve()),
            str(stat.st_size),
            str(int(stat.st_mtime)),
            model_size,
            language or "auto",
        ]
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def _load_cached(cache_path):
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("version") != CACHE_VERSION:
        return None
    transcript = Transcript.from_dict(payload, from_cache=True)
    return transcript if transcript.words else None


def _store_cached(cache_path, transcript):
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(transcript.as_dict()), encoding="utf-8")
    except OSError:
        # A cache miss is not worth failing a QC run over.
        pass


def _clean_word(text):
    return (text or "").strip()


def transcribe_media(
    media_path,
    model_size=DEFAULT_MODEL,
    language=None,
    device="auto",
    compute_type="int8",
    beam_size=1,
    progress=None,
    use_cache=True,
):
    """Transcribe the dialogue track with word-level timestamps.

    Results are cached per (file, size, mtime, model, language) so re-running a
    check against the same asset is instant.
    """
    media_path = Path(media_path)
    if not media_path.exists():
        raise TranscriptionError(f"File not found: {media_path}")

    if model_size not in MODEL_SIZES:
        raise TranscriptionError(f"Unknown model size {model_size!r}. Choose one of: {', '.join(MODEL_SIZES)}.")

    cache_path = CACHE_DIR / f"{_cache_key(media_path, model_size, language)}.json"
    if use_cache:
        cached = _load_cached(cache_path)
        if cached:
            if progress:
                progress(f"Reusing cached transcript ({len(cached.words)} words).")
            return cached

    if not faster_whisper_available():
        raise TranscriptionError(
            "faster-whisper is not installed, so audio-verified sync (Tier 2) cannot run.\n"
            f"Install it with: {' '.join(install_command())}\n"
            "Tier 1 (the frame-rate math check) works without it."
        )

    from faster_whisper import WhisperModel

    with tempfile.TemporaryDirectory(prefix="caption-inspector-") as work_dir:
        audio_path = Path(work_dir) / "dialogue.wav"
        try:
            extract_audio(media_path, audio_path, progress=progress)
        except ProbeError as error:
            raise TranscriptionError(str(error)) from error

        if progress:
            progress(f"Loading the {model_size} model...")

        try:
            model = WhisperModel(model_size, device=device, compute_type=compute_type)
        except Exception as error:
            # Common on machines where int8 is unsupported; float32 always works.
            if compute_type != "float32":
                if progress:
                    progress(f"{compute_type} unavailable ({error}); retrying in float32...")
                model = WhisperModel(model_size, device=device, compute_type="float32")
            else:
                raise TranscriptionError(f"Could not load the {model_size} model: {error}") from error

        if progress:
            progress("Transcribing...")

        try:
            segments, info = model.transcribe(
                str(audio_path),
                language=language,
                beam_size=beam_size,
                word_timestamps=True,
                vad_filter=True,
            )
        except Exception as error:
            raise TranscriptionError(f"Transcription failed: {error}") from error

        words = []
        total_duration = getattr(info, "duration", None)
        last_report = 0.0

        for segment in segments:
            for word in getattr(segment, "words", None) or []:
                text = _clean_word(getattr(word, "word", ""))
                if not text:
                    continue
                words.append(Word(text, word.start, word.end))

            if progress and total_duration and segment.end - last_report >= 30:
                last_report = segment.end
                percent = min(100.0, segment.end / total_duration * 100.0)
                progress(f"Transcribing... {percent:.0f}% ({len(words)} words)")

    if not words:
        raise TranscriptionError(
            "The transcriber found no speech in this asset's audio. "
            "Check that the dialogue track is the first audio stream."
        )

    transcript = Transcript(
        words,
        language=getattr(info, "language", language),
        model_size=model_size,
        duration=total_duration,
    )

    if use_cache:
        _store_cached(cache_path, transcript)

    if progress:
        progress(f"Transcribed {len(words)} words.")

    return transcript


def clear_transcript_cache():
    """Drop cached transcripts. Returns the number of files removed."""
    if not CACHE_DIR.exists():
        return 0
    removed = 0
    for path in CACHE_DIR.glob("*.json"):
        try:
            os.remove(path)
            removed += 1
        except OSError:
            continue
    return removed
