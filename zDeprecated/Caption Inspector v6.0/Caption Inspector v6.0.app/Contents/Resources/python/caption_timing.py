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
    frames_to_timecode,
    parse_timecode,
    timecode_to_frames,
    timecode_to_seconds,
)


# The broadcast pair this app decodes itself, plus every text subtitle format
# `subtitle_formats` can read. Kept as one tuple because it is what the file
# pickers and the Tier 1 reader both ask for.
BROADCAST_SIDECAR_EXTENSIONS = (".scc", ".mcc")

SIDECAR_EXTENSIONS = BROADCAST_SIDECAR_EXTENSIONS + (
    ".srt", ".vtt", ".webvtt", ".ttml", ".dfxp", ".itt", ".imsc", ".xml",
    ".ass", ".ssa", ".smi", ".sami", ".sbv", ".sub", ".mpl", ".lrc", ".rt", ".stl",
)

# SCC and MCC both use "<timecode><whitespace><payload>".
_TIMED_LINE = re.compile(r"^\s*(\d{1,2}:\d{2}:\d{2}[:;.,]\d{1,3})\s+(.*\S)\s*$")

# MCC headers carry the authored rate: "Time Code Rate=30DF".
_MCC_RATE = re.compile(r"^\s*Time\s*Code\s*Rate\s*=\s*(\d+)\s*(DF)?\s*$", re.IGNORECASE)

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


# Kinds whose stamps are absolute program timecode rather than media time.
TIMECODE_BASED_KINDS = ("scc", "mcc", "ebu-stl", "spruce-stl")
_TIMECODE_BASED_KINDS = TIMECODE_BASED_KINDS


def is_program_timecode(kind):
    """Are this format's stamps absolute programme timecode?

    The distinction decides whether a constant difference between two files is
    a sync error or just the tape origin one of them was authored against.
    """
    return kind in TIMECODE_BASED_KINDS


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
        # Set for the text formats, whose parsed cues are worth keeping rather
        # than re-parsing downstream. None for SCC/MCC.
        self.subtitle_document = None

    def __bool__(self):
        return bool(self.entries)

    @property
    def is_timecode_based(self):
        """SCC/MCC carry SMPTE timecode; SRT/VTT/TTML carry media time.

        TTML can express times in frames, but its zero is the start of the
        program rather than a timecode run, so it is not timecode-based in the
        sense the 1-hour-start reference check means. Both STL flavours are:
        EBU-STL and Spruce both stamp cues in program timecode, which is why a
        conforming EBU file starts its first subtitle after 10:00:00:00.
        """
        return self.kind in _TIMECODE_BASED_KINDS

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


def _seconds_to_timecode(seconds, rate_code, drop_frame=False):
    """Render wall-clock seconds as a timecode string at a counting rate."""
    rate = RATE_BY_CODE[rate_code]
    frames = int(round(float(seconds) * float(rate)))
    return frames_to_timecode(frames, rate, drop_frame)


def _read_text_subtitle(path):
    """Timing rows from an SRT, WebVTT, or TTML file.

    Delegates the parsing to `subtitle_formats`, which knows the three formats
    properly, and converts the result into the same TimedEntry rows the SCC and
    MCC readers produce so every Tier 1 check works unchanged.

    Each cue becomes two rows - the display at `begin` and the erase at `end`.
    Only recording the start would make `last_display_entry` the start of the
    final cue, and the overrun check would then pass a file whose last subtitle
    runs off the end of the program.
    """
    from subtitle_formats import SubtitleParseError, read_subtitle_document

    try:
        document = read_subtitle_document(path)
    except SubtitleParseError as error:
        raise CaptionTimingError(str(error)) from error

    # TTML states its own frame rate. SRT and VTT are wall-clock formats, so 30
    # non-drop is just a counting rate for the shared timecode plumbing - it is
    # not a claim about the file, and Tier 1 does not treat it as one.
    declared_rate_code = document.declared_rate_code
    render_rate_code = declared_rate_code or 3000
    drop_frame = document.drop_frame and render_rate_code in DROP_FRAME_CODES

    entries = []
    for cue in document.cues:
        entries.append(
            TimedEntry(
                _seconds_to_timecode(cue.start, render_rate_code, drop_frame),
                cue.text,
                has_text=True,
                is_display=True,
            )
        )
        if cue.end is not None and cue.end > cue.start:
            entries.append(
                TimedEntry(
                    _seconds_to_timecode(cue.end, render_rate_code, drop_frame),
                    "",
                    has_text=False,
                    is_erase=True,
                )
            )

    if not entries:
        raise CaptionTimingError(f"No timed cues were found in {Path(path).name}.")

    entries.sort(key=lambda entry: timecode_to_frames(
        entry.timecode, RATE_BY_CODE[render_rate_code], drop_frame=drop_frame
    ))

    timings = CaptionTimings(
        path,
        document.kind,
        entries,
        drop_frame,
        declared_rate_code,
        dict(document.header),
    )
    timings.subtitle_document = document
    return timings


def read_caption_timings(path):
    """Read timing rows from any supported caption sidecar."""
    caption_path = Path(path)
    if not caption_path.exists():
        raise CaptionTimingError(f"File not found: {caption_path}")

    suffix = caption_path.suffix.lower()
    if suffix not in SIDECAR_EXTENSIONS:
        raise CaptionTimingError(
            f"{suffix or 'This file'} is not a caption sidecar this check can read. "
            f"Supported: {', '.join(SIDECAR_EXTENSIONS)}."
        )

    if suffix not in BROADCAST_SIDECAR_EXTENSIONS:
        # `subtitle_formats` opens the file itself - it has to, since EBU-STL is
        # binary and decoding it as text would corrupt it before it is read.
        return _read_text_subtitle(caption_path)

    try:
        text = caption_path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise CaptionTimingError(f"Could not read {caption_path.name}: {error}") from error

    lines = text.splitlines()
    if suffix == ".scc":
        return _read_scc(caption_path, lines)
    return _read_mcc(caption_path, lines)
