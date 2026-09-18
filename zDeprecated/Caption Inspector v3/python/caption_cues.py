"""Visible caption cues with the time they hit the screen.

Tier 2 needs "this text became visible at this second". `inspection_support`
already reconstructs pop-on cues for the browser UI; this module reuses that
decode path and extends it to roll-up and paint-on, which are what news and
live-originated deliverables actually use.
"""

import re
from pathlib import Path

from caption_timing import CaptionTimingError, read_caption_timings
from inspection_support import decode_file, normalized_event_text
from timecode import RATE_BY_CODE, DROP_FRAME_CODES, TimecodeError, timecode_to_seconds


# Roll-up and paint-on put text on screen as it arrives; pop-on holds it in a
# back buffer until EOC.
_ROLLUP_COMMANDS = {"RU2", "RU3", "RU4"}
_PAINT_ON_COMMAND = "RDC"
_COMMAND_PATTERN = re.compile(r"^\{([A-Z0-9]+)\}$")

_SPEAKER_PATTERN = re.compile(r"^\s*(?:>>+|-)\s*(?:[A-Z][A-Z .'-]{1,30}:)?\s*")
_BRACKETED = re.compile(r"[\[\(][^\]\)]*[\]\)]")
_MUSIC = re.compile(r"[♪♫♩♬#]")
_NON_WORD = re.compile(r"[^a-z0-9' ]+")


class Cue:
    """One block of caption text and the moment it became visible."""

    def __init__(self, timecode, seconds, text, mode, channel=None):
        self.timecode = timecode
        self.seconds = seconds
        self.text = text
        self.mode = mode
        self.channel = channel
        self.normalized = normalize_caption_text(text)
        self.words = self.normalized.split()

    def __repr__(self):
        return f"<Cue {self.timecode} {self.mode} {self.text[:40]!r}>"

    def as_dict(self):
        return {
            "timecode": self.timecode,
            "seconds": self.seconds,
            "text": self.text,
            "mode": self.mode,
            "channel": self.channel,
        }


def normalize_caption_text(text):
    """Reduce caption text to comparable words.

    Speaker chevrons, sound-effect brackets, and music notes are in the caption
    but never in the transcript, so they only add noise to the match.
    """
    cleaned = _MUSIC.sub(" ", text or "")
    cleaned = _BRACKETED.sub(" ", cleaned)
    cleaned = _SPEAKER_PATTERN.sub("", cleaned)
    cleaned = cleaned.replace(">>", " ").lower()
    cleaned = cleaned.replace("’", "'").replace("‘", "'")
    cleaned = _NON_WORD.sub(" ", cleaned)
    return " ".join(cleaned.split())


def _timecode_text(raw_time):
    return (raw_time or "").strip("[]")


def _seconds_for(timecode, rate_code, drop_frame):
    rate = RATE_BY_CODE[rate_code]
    drop = drop_frame if rate_code in DROP_FRAME_CODES else None
    try:
        return timecode_to_seconds(timecode, rate, drop_frame=drop)
    except TimecodeError:
        return None


def build_cues_from_rows(rows, rate_code, drop_frame=None, channel=None):
    """Reconstruct visible cues from one decoded CEA-608 track.

    Pop-on text is buffered from RCL and committed at EOC. Roll-up and paint-on
    text is visible the moment it arrives, so the cue is stamped at the first
    text string and committed on CR, EDM, or a mode change.
    """
    cues = []
    mode = "pop-on"
    pending_text = []
    pending_time = None

    def commit(visible_timecode, commit_mode):
        if not pending_text:
            return
        text = " ".join(part for part in pending_text if part).strip()
        if not text:
            pending_text.clear()
            return
        timecode = visible_timecode or pending_time
        seconds = _seconds_for(timecode, rate_code, drop_frame) if timecode else None
        if seconds is not None:
            cues.append(Cue(timecode, seconds, text, commit_mode, channel))
        pending_text.clear()

    for row in rows:
        event_text = normalized_event_text(row)
        row_type = row["type"]
        timecode = _timecode_text(row["time"])

        if row_type == "Line21ControlCode":
            match = _COMMAND_PATTERN.match(event_text)
            command = match.group(1) if match else None

            if command == "RCL":
                mode = "pop-on"
                pending_text.clear()
                pending_time = timecode
                continue

            if command in _ROLLUP_COMMANDS:
                if pending_text and mode != "pop-on":
                    commit(pending_time, mode)
                mode = "roll-up"
                pending_text.clear()
                continue

            if command == _PAINT_ON_COMMAND:
                if pending_text and mode != "pop-on":
                    commit(pending_time, mode)
                mode = "paint-on"
                pending_text.clear()
                continue

            if command == "EOC":
                if mode == "pop-on":
                    commit(timecode, "pop-on")
                continue

            if command in ("CR", "EDM"):
                if mode != "pop-on":
                    commit(pending_time, mode)
                continue

            continue

        if row_type == "Line21TextString":
            text = event_text.strip('"')
            if not text.strip():
                continue
            if not pending_text:
                pending_time = timecode
            pending_text.append(text)
            continue

    # A roll-up track that never sees a trailing CR still has a last line.
    if pending_text and mode != "pop-on":
        commit(pending_time, mode)

    return cues


def build_cues_from_dtvcc_rows(rows, rate_code, drop_frame=None, service=None):
    """CEA-708 fallback: stamp each decoded text string where it lands.

    708 window semantics are richer than 608's, but for drift measurement the
    arrival time of the text is the signal that matters.
    """
    cues = []
    for row in rows:
        if row["type"] != "DtvccTextString":
            continue
        text = normalized_event_text(row).strip('"')
        if not text.strip():
            continue
        timecode = _timecode_text(row["time"])
        seconds = _seconds_for(timecode, rate_code, drop_frame)
        if seconds is None:
            continue
        cues.append(Cue(timecode, seconds, text, "708", service))
    return cues


def _pick_track(tracks):
    """Choose the primary caption track.

    CC1 / Service 1 is the primary English track by convention; otherwise take
    whichever track carries the most text.
    """
    line21 = tracks.get("CEA-608", {})
    dtvcc = tracks.get("CEA-708", {})

    if "Channel 1" in line21:
        return "CEA-608", "Channel 1", line21["Channel 1"]

    def text_count(rows):
        return sum(1 for row in rows if row["type"] in ("Line21TextString", "DtvccTextString"))

    candidates = [("CEA-608", name, rows) for name, rows in line21.items()]
    candidates += [("CEA-708", name, rows) for name, rows in dtvcc.items()]
    if not candidates:
        return None, None, []

    return max(candidates, key=lambda item: text_count(item[2]))


def extract_cues(caption_path, rate_code, track=None):
    """Decode `caption_path` and return visible cues for the primary track.

    `track` may be ("CEA-608", "Channel 2") to force a specific track.
    """
    caption_source = Path(caption_path)
    suffix = caption_source.suffix.lower()

    if suffix in (".srt", ".vtt"):
        return _cues_from_subtitle_file(caption_source, rate_code)

    frame_rate_arg = rate_code if suffix in (".scc",) else 0
    tracks, _ = decode_file(str(caption_source), frame_rate_arg, capture_logs=True)

    drop_frame = None
    try:
        timings = read_caption_timings(caption_source)
        drop_frame = timings.drop_frame
    except CaptionTimingError:
        drop_frame = None

    if track:
        family, name = track
        rows = tracks.get(family, {}).get(name, [])
    else:
        family, name, rows = _pick_track(tracks)

    if not rows:
        return []

    if family == "CEA-608":
        return build_cues_from_rows(rows, rate_code, drop_frame, channel=name)
    return build_cues_from_dtvcc_rows(rows, rate_code, drop_frame, service=name)


def _cues_from_subtitle_file(path, rate_code):
    """SRT/VTT cues, read straight from the file's wall-clock stamps."""
    import re as _re

    text = path.read_text(encoding="utf-8", errors="replace")
    time_pattern = _re.compile(
        r"(?P<start>\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(?P<end>\d{2}:\d{2}:\d{2}[,.]\d{3})"
    )

    cues = []
    blocks = _re.split(r"\n\s*\n", text)
    for block in blocks:
        match = time_pattern.search(block)
        if not match:
            continue
        lines = [line for line in block.splitlines() if not time_pattern.search(line)]
        body = " ".join(line.strip() for line in lines if line.strip() and not line.strip().isdigit())
        if not body:
            continue
        stamp = match.group("start").replace(",", ".")
        hours, minutes, rest = stamp.split(":")
        seconds = int(hours) * 3600 + int(minutes) * 60 + float(rest)
        cues.append(Cue(match.group("start"), seconds, body, "subtitle"))

    return cues
