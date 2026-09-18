"""ffprobe/ffmpeg wrappers.

The video is the ground truth in a sync dispute: its frame count and start
timecode are facts, and the caption file either agrees with them or it does not.
"""

import json
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

from timecode import rate_code_for, rate_label


VIDEO_EXTENSIONS = (".mov", ".mp4", ".mxf", ".ts", ".mpg", ".mpeg", ".m2v", ".mkv", ".avi", ".m4v", ".wav", ".mp3")


class ProbeError(RuntimeError):
    """Raised when ffprobe is missing or cannot read the file."""


def ffprobe_path():
    return shutil.which("ffprobe")


def ffmpeg_path():
    return shutil.which("ffmpeg")


def require_ffprobe():
    path = ffprobe_path()
    if not path:
        raise ProbeError(
            "ffprobe was not found on PATH. Install FFmpeg (brew install ffmpeg) and try again."
        )
    return path


def _parse_fraction(text):
    if not text:
        return None
    try:
        value = Fraction(str(text))
    except (ValueError, ZeroDivisionError):
        return None
    return value if value > 0 else None


def _first_float(*values):
    for value in values:
        if value in (None, "", "N/A"):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


class MediaInfo:
    """Flattened view of the ffprobe JSON, with the fields sync QC needs."""

    def __init__(self, path, raw):
        self.path = str(path)
        self.raw = raw

        self.format = raw.get("format", {}) or {}
        streams = raw.get("streams", []) or []
        self.video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
        self.audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
        self.data_streams = [s for s in streams if s.get("codec_type") == "data"]

        self.container_duration = _first_float(self.format.get("duration"))
        self.video_duration = _first_float((self.video_stream or {}).get("duration"))
        self.duration = self.video_duration or self.container_duration

        self.frame_rate = None
        self.declared_frame_rate = None
        self.nb_frames = None
        self.counted_frames = None
        self.width = None
        self.height = None

        if self.video_stream:
            r_rate = _parse_fraction(self.video_stream.get("r_frame_rate"))
            avg_rate = _parse_fraction(self.video_stream.get("avg_frame_rate"))
            self.frame_rate = avg_rate or r_rate
            self.declared_frame_rate = r_rate or avg_rate
            self.width = self.video_stream.get("width")
            self.height = self.video_stream.get("height")
            nb_frames = self.video_stream.get("nb_frames")
            if nb_frames not in (None, "", "N/A"):
                try:
                    self.nb_frames = int(nb_frames)
                except (TypeError, ValueError):
                    self.nb_frames = None

        self.rate_code = rate_code_for(self.frame_rate) if self.frame_rate else None
        self.start_timecode = self._find_start_timecode()

    def _find_start_timecode(self):
        """Media start timecode, if the file carries one.

        A vendor authoring against a 01:00:00:00 head-based master and
        delivering against a zero-based file is one of the most common causes
        of "the captions are an hour off".
        """
        format_tags = self.format.get("tags", {}) or {}
        for key in ("timecode", "time_code", "TIMECODE"):
            if format_tags.get(key):
                return format_tags[key]

        for stream in self.raw.get("streams", []) or []:
            tags = stream.get("tags", {}) or {}
            for key in ("timecode", "time_code", "TIMECODE"):
                if tags.get(key):
                    return tags[key]
        return None

    @property
    def has_video(self):
        return self.video_stream is not None

    @property
    def has_audio(self):
        return bool(self.audio_streams)

    def total_frames(self):
        """Best available frame count, and how confident we are in it.

        Returns (frames, source). `nb_frames` is authoritative when the
        container carries it; otherwise duration x rate, which is exact for
        constant-frame-rate mezzanine files and close enough elsewhere.
        """
        if self.counted_frames is not None:
            return self.counted_frames, "decoded frame count"
        if self.nb_frames:
            return self.nb_frames, "container frame count"
        if self.duration and self.frame_rate:
            return int(round(self.duration * float(self.frame_rate))), "duration x frame rate"
        return None, "unavailable"

    def summary_lines(self):
        lines = [f"File: {Path(self.path).name}"]
        if self.has_video:
            rate_text = f"{float(self.frame_rate):.3f} fps" if self.frame_rate else "unknown fps"
            if self.rate_code:
                rate_text += f" ({rate_label(self.rate_code)})"
            lines.append(f"Video: {self.width}x{self.height} @ {rate_text}")
        else:
            lines.append("Video: none")

        if self.duration:
            lines.append(f"Duration: {self.duration:.3f} s")

        frames, source = self.total_frames()
        if frames is not None:
            lines.append(f"Total frames: {frames} ({source})")

        if self.start_timecode:
            lines.append(f"Start timecode: {self.start_timecode}")

        lines.append(f"Audio streams: {len(self.audio_streams)}")
        return lines


def probe_media(path, count_frames=False):
    """Run ffprobe against `path`.

    `count_frames` decodes the whole file for an exact frame count. It is slow
    and off by default; the container count is right for normal deliveries.
    """
    media_path = Path(path)
    if not media_path.exists():
        raise ProbeError(f"File not found: {media_path}")

    command = [
        require_ffprobe(),
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
    ]
    if count_frames:
        command += ["-count_frames"]
    command.append(str(media_path))

    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as error:
        raise ProbeError(f"Could not run ffprobe: {error}") from error

    if completed.returncode != 0:
        message = (completed.stderr or "").strip() or f"ffprobe exited with {completed.returncode}"
        raise ProbeError(f"ffprobe could not read {media_path.name}: {message}")

    try:
        raw = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as error:
        raise ProbeError(f"ffprobe returned output that could not be parsed: {error}") from error

    info = MediaInfo(media_path, raw)

    if count_frames and info.video_stream:
        counted = info.video_stream.get("nb_read_frames")
        if counted not in (None, "", "N/A"):
            try:
                info.counted_frames = int(counted)
            except (TypeError, ValueError):
                info.counted_frames = None

    if not info.has_video and not info.has_audio:
        raise ProbeError(f"{media_path.name} has no video or audio streams that ffprobe can read.")

    return info


def extract_audio(media_path, output_path, sample_rate=16000, stream_index=None, progress=None):
    """Decode the dialogue track to 16 kHz mono PCM for the transcriber."""
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise ProbeError(
            "ffmpeg was not found on PATH. Install FFmpeg (brew install ffmpeg) and try again."
        )

    command = [ffmpeg, "-v", "error", "-y", "-i", str(media_path)]
    if stream_index is not None:
        command += ["-map", f"0:a:{stream_index}"]
    else:
        command += ["-map", "0:a:0?"]
    command += ["-ac", "1", "-ar", str(sample_rate), "-vn", "-f", "wav", str(output_path)]

    if progress:
        progress("Extracting audio with ffmpeg...")

    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        message = (completed.stderr or "").strip() or f"ffmpeg exited with {completed.returncode}"
        raise ProbeError(f"Audio extraction failed: {message}")

    if not Path(output_path).exists() or Path(output_path).stat().st_size == 0:
        raise ProbeError("Audio extraction produced an empty file. Does this asset have a dialogue track?")

    return Path(output_path)
