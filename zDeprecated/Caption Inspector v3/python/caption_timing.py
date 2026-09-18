"""Fast, dependency-free timing reads of caption sidecar files.

Tier 1 has to answer "where does this file start and end, and at what rate does
it think it is running" in milliseconds, without decoding anything. That means
reading the timecode column directly rather than going through the C decoder.
"""

import re
from pathlib import Path

from timecode import (
    DROP_FRAME_CODES,
    RATE_BY_CODE,
    TimecodeError,
    parse_timecode,
    timecode_to_frames,
    timecode_to_seconds,
)


SIDECAR_EXTENSIONS = (".scc", ".mcc", ".srt", ".vtt")

# SCC and MCC both use "<timecode><whitespace><payload>".
_TIMED_LINE = re.compile(r"^\s*(\d{1,2}:\d{2}:\d{2}[:;.,]\d{1,3})\s+(.*\S)\s*$")

# MCC headers carry the authored rate: "Time Code Rate=30DF".
_MCC_RATE = re.compile(r"^\s*Time\s*Code\s*Rate\s*=\s*(\d+)\s*(DF)?\s*$", re.IGNORECASE)

_SRT_TIME = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(?P<end>\d{2}:\d{2}:\d{2}[,.]\d{3})"
)

# CEA-608 control codes, parity bit set as they appear in an SCC payload.
_ERASE_CODES = {"942c", "9c2c", "152c", "1d2c"}          # EDM - erase displayed memory
_ERASE_NON_DISPLAYED = {"942e", "9c2e", "152e", "1d2e"}  # ENM
_DISPLAY_CODES = {"942f", "9c2f", "152f", "1d2f"}        # EOC - caption becomes visible
_RESUME_CODES = {"9420", "9c20", "1520", "1d20"}         # RCL
_ROLLUP_CODES = {
    "9425", "9426", "9427", "9c25", "9c26", "9c27",
    "1525", "1526", "1527", "1d25", "1d26", "1d27",
}
_NULL_CODES = {"8080", "0000"}


class CaptionTimingError(ValueError):
    """Raised when a caption sidecar cannot be read for timing."""


class TimedEntry:
    """One timed row of a caption file."""

    def __init__(self, timecode, payload, has_text=False, is_display=False, is_erase=False):
        self.timecode = timecode
        self.payload = payload
        self.has_text = has_text
        self.is_display = is_display
        self.is_erase = is_erase


class CaptionTimings:
    """Timing-only view of a caption sidecar."""

    def __init__(self, path, kind, entries, drop_frame, declared_rate_code=None, header=None):
        self.path = str(path)
        self.kind = kind
        self.entries = entries
        self.drop_frame = drop_frame
        self.declared_rate_code = declared_rate_code
        self.header = header or {}

    def __bool__(self):
        return bool(self.entries)

    @property
    def is_timecode_based(self):
        """SCC/MCC carry SMPTE timecode; SRT/VTT carry wall clock."""
        return self.kind in ("scc", "mcc")

    def first_entry(self):
        return self.entries[0] if self.entries else None

    def last_entry(self):
        return self.entries[-1] if self.entries else None

    def first_text_entry(self):
        return next((entry for entry in self.entries if entry.has_text), None)

    def last_text_entry(self):
        return next((entry for entry in reversed(self.entries) if entry.has_text), None)

    def last_display_entry(self):
        """Last moment something is put on screen, ignoring trailing erases."""
        for entry in reversed(self.entries):
            if entry.is_display or entry.has_text:
                return entry
        return None

    def frames_at(self, entry, rate_code):
        rate = RATE_BY_CODE[rate_code]
        drop = self.drop_frame if rate_code in DROP_FRAME_CODES else False
        return timecode_to_frames(entry.timecode, rate, drop_frame=drop)

    def seconds_at(self, entry, rate_code):
        rate = RATE_BY_CODE[rate_code]
        drop = self.drop_frame if rate_code in DROP_FRAME_CODES else False
        return timecode_to_seconds(entry.timecode, rate, drop_frame=drop)


def _classify_scc_payload(payload):
    """Split an SCC hex payload into text / display / erase flags."""
    words = [word.lower() for word in payload.split() if len(word) == 4]
    has_text = False
    is_display = False
    is_erase = False

    for word in words:
        if word in _NULL_CODES:
            continue
        if word in _DISPLAY_CODES:
            is_display = True
            continue
        if word in _ERASE_CODES:
            is_erase = True
            continue
        if word in _ERASE_NON_DISPLAYED or word in _RESUME_CODES or word in _ROLLUP_CODES:
            continue

        try:
            first = int(word[:2], 16) & 0x7F
            second = int(word[2:], 16) & 0x7F
        except ValueError:
            continue

        # 0x20..0x7F is the printable range; 0x10..0x1F are control/PAC prefixes.
        if first >= 0x20 and second >= 0x20:
            has_text = True

    return has_text, is_display, is_erase


def _read_scc(path, lines):
    entries = []
    drop_frame = False
    saw_drop_flag = False

    for line in lines:
        match = _TIMED_LINE.match(line)
        if not match:
            continue
        timecode, payload = match.group(1), match.group(2)
        try:
            _, _, _, _, tc_drop = parse_timecode(timecode)
        except TimecodeError:
            continue
        if tc_drop:
            drop_frame = True
        saw_drop_flag = True

        has_text, is_display, is_erase = _classify_scc_payload(payload)
        entries.append(TimedEntry(timecode, payload, has_text, is_display, is_erase))

    if not entries:
        raise CaptionTimingError(f"No timed caption rows were found in {Path(path).name}.")

    # SCC has no rate header. The format is defined against 29.97 drop-frame,
    # but vendors ship 24/25 fps SCCs routinely, so this stays a default the
    # caller can override rather than an assumption.
    declared = 2997 if (drop_frame or not saw_drop_flag) else None
    return CaptionTimings(path, "scc", entries, drop_frame, declared)


def _read_mcc(path, lines):
    entries = []
    header = {}
    drop_frame = False
    declared_rate_code = None

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue

        rate_match = _MCC_RATE.match(stripped)
        if rate_match:
            nominal = int(rate_match.group(1))
            drop_frame = bool(rate_match.group(2)) or drop_frame
            header["time_code_rate"] = stripped.split("=", 1)[1].strip()
            # MCC states the counting rate (30DF means 29.97 drop-frame).
            if nominal == 30 and drop_frame:
                declared_rate_code = 2997
            elif nominal == 60 and drop_frame:
                declared_rate_code = 5994
            elif nominal == 24:
                declared_rate_code = 2400
            elif nominal == 25:
                declared_rate_code = 2500
            elif nominal == 30:
                declared_rate_code = 3000
            elif nominal == 50:
                declared_rate_code = 5000
            elif nominal == 60:
                declared_rate_code = 6000
            continue

        if "=" in stripped and not _TIMED_LINE.match(stripped):
            key, value = stripped.split("=", 1)
            header[key.strip().lower().replace(" ", "_")] = value.strip()
            continue

        match = _TIMED_LINE.match(line)
        if not match:
            continue
        timecode, payload = match.group(1), match.group(2)
        try:
            _, _, _, _, tc_drop = parse_timecode(timecode)
        except TimecodeError:
            continue
        if tc_drop:
            drop_frame = True

        # MCC payloads are compressed ANC packets; treating every timed row as a
        # caption event is the right granularity for a timing-only read.
        entries.append(TimedEntry(timecode, payload, has_text=True, is_display=True))

    if not entries:
        raise CaptionTimingError(f"No timed caption rows were found in {Path(path).name}.")

    return CaptionTimings(path, "mcc", entries, drop_frame, declared_rate_code, header)


def _srt_time_to_timecode(text, rate_code=3000):
    """Render a wall-clock SRT stamp as a timecode string at a nominal rate."""
    body, _, millis = text.replace(",", ".").partition(".")
    hours, minutes, seconds = (int(part) for part in body.split(":"))
    rate = int(round(float(RATE_BY_CODE[rate_code])))
    frame = min(rate - 1, int(round(int(millis) / 1000.0 * rate)))
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frame:02d}"


def _read_subtitle_text(path, lines, kind):
    entries = []
    pending_start = None

    for line in lines:
        match = _SRT_TIME.search(line)
        if match:
            pending_start = match.group("start")
            continue
        if pending_start and line.strip():
            entries.append(
                TimedEntry(_srt_time_to_timecode(pending_start), line.strip(), has_text=True, is_display=True)
            )
            pending_start = None

    if not entries:
        raise CaptionTimingError(f"No timed cues were found in {Path(path).name}.")

    # SRT/VTT are wall-clock formats; 30 non-drop just gives the shared timecode
    # plumbing a consistent counting rate to work in.
    return CaptionTimings(path, kind, entries, drop_frame=False, declared_rate_code=3000)


def read_caption_timings(path):
    """Read timing rows from an SCC, MCC, SRT, or VTT file."""
    caption_path = Path(path)
    if not caption_path.exists():
        raise CaptionTimingError(f"File not found: {caption_path}")

    suffix = caption_path.suffix.lower()
    if suffix not in SIDECAR_EXTENSIONS:
        raise CaptionTimingError(
            f"{suffix or 'This file'} is not a caption sidecar this check can read. "
            f"Supported: {', '.join(SIDECAR_EXTENSIONS)}."
        )

    try:
        text = caption_path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise CaptionTimingError(f"Could not read {caption_path.name}: {error}") from error

    lines = text.splitlines()
    if suffix == ".scc":
        return _read_scc(caption_path, lines)
    if suffix == ".mcc":
        return _read_mcc(caption_path, lines)
    return _read_subtitle_text(caption_path, lines, suffix.lstrip("."))
