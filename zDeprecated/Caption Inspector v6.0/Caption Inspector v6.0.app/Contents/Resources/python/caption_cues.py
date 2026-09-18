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
from subtitle_formats import TEXT_SUBTITLE_EXTENSIONS
from timecode import (
    RATE_BY_CODE,
    DROP_FRAME_CODES,
    TimecodeError,
    frames_to_timecode,
    rate_label,
    timecode_to_seconds,
)


# Roll-up and paint-on put text on screen as it arrives; pop-on holds it in a
# back buffer until EOC.
_ROLLUP_COMMANDS = {"RU2", "RU3", "RU4"}
_PAINT_ON_COMMAND = "RDC"
_COMMAND_PATTERN = re.compile(r"^\{([A-Z0-9]+)\}$")

# Speaker attribution, in the forms caption files actually use:
#
#     >> LAUREN: text        >> text        - LAUREN: text
#     LAUREN: text           - text         MAN 2: text
#
# The leading chevron or dash is optional. It was not, and that was the bug:
# a bare "LAUREN:" survived into the text handed to the matcher, Whisper never
# transcribes a speaker label because nobody says it, and the extra leading
# token dragged every anchor one word early. See `normalize_caption_text`.
_SPEAKER_PREFIX = re.compile(r"^\s*(?:>>+|-{1,2}|\u2013|\u2014)?\s*")
_SPEAKER_LABEL = re.compile(r"^([^:]{1,25}):\s*")
_BRACKETED = re.compile(r"[\[\(][^\]\)]*[\]\)]")
_MUSIC = re.compile(r"[♪♫♩♬#]")
_NON_WORD = re.compile(r"[^a-z0-9' ]+")


class Cue:
    """One block of caption text and the moment it became visible."""

    def __init__(self, timecode, seconds, text, mode, channel=None, end_seconds=None,
                 speaker=None):
        self.timecode = timecode
        self.seconds = seconds
        self.text = text
        self.mode = mode
        self.channel = channel
        # Text formats carry an explicit clear time; 608/708 do not, so this is
        # None for decoded tracks.
        self.end_seconds = end_seconds
        # A text format may name the speaker out of band (WebVTT `<v>`, TTML
        # `ttm:agent`); otherwise it is whatever the caption line spells out.
        self.speaker = speaker or split_speaker(text)[0]
        self.normalized = normalize_caption_text(text)
        self.words = self.normalized.split()

    def __repr__(self):
        return f"<Cue {self.timecode} {self.mode} {self.text[:40]!r}>"

    def as_dict(self):
        return {
            "timecode": self.timecode,
            "seconds": self.seconds,
            "end_seconds": self.end_seconds,
            "text": self.text,
            "mode": self.mode,
            "channel": self.channel,
            "speaker": self.speaker,
        }


def _looks_like_speaker_label(candidate):
    """Is the text before a colon a speaker name, or just dialogue?

    "LAUREN" and "Lauren" are names. "I'll tell you this" is not, and stripping
    it would throw away half the line. The test is deliberately conservative:
    a name is short, and it is either shouted in caps the way caption files
    write attribution, or one or two capitalised words.
    """
    candidate = candidate.strip()
    if not candidate or not any(character.isalpha() for character in candidate):
        return False

    letters = [character for character in candidate if character.isalpha()]
    if letters and all(character.isupper() for character in letters):
        # ALL CAPS: "LAUREN", "MAN 2", "ANNOUNCER", "P.C. SMITH".
        return len(candidate) <= 25

    words = candidate.split()
    if len(words) <= 2 and all(word[:1].isupper() for word in words if word):
        # Title case: "Lauren", "Mrs Smith".
        return len(candidate) <= 25

    return False


def split_speaker(text):
    """Split caption text into (speaker, dialogue).

    The speaker is worth keeping rather than discarding - it is what makes the
    transcript readable - but it must not reach the matcher.
    """
    body = _SPEAKER_PREFIX.sub("", text or "", count=1)

    label = _SPEAKER_LABEL.match(body)
    if label and _looks_like_speaker_label(label.group(1)):
        return label.group(1).strip(), body[label.end():]

    return None, body


def normalize_caption_text(text):
    """Reduce caption text to comparable words.

    Speaker labels, chevrons, sound-effect brackets, and music notes are in the
    caption but never in the transcript, so they only add noise to the match -
    and a leading one does worse than add noise, because the matcher anchors on
    the first token.
    """
    cleaned = _MUSIC.sub(" ", text or "")
    cleaned = _BRACKETED.sub(" ", cleaned)
    _, cleaned = split_speaker(cleaned)
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

    if suffix in TEXT_SUBTITLE_EXTENSIONS:
        return cues_from_text_subtitle(caption_source)

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


def cues_from_text_subtitle(path, document=None):
    """Cues from an SRT, WebVTT, or TTML file.

    The parsing lives in `subtitle_formats`; this only maps its cues onto the
    Cue shape the aligner wants. `document` lets a caller that has already
    parsed the file - the transcript tab does - avoid reading it twice.
    """
    from subtitle_formats import read_subtitle_document

    if document is None:
        document = read_subtitle_document(path)

    rate = RATE_BY_CODE[document.declared_rate_code or 3000]
    drop = document.drop_frame and (document.declared_rate_code in DROP_FRAME_CODES)

    cues = []
    for cue in document.cues:
        if not cue.text.strip():
            continue
        timecode = frames_to_timecode(int(round(cue.start * float(rate))), rate, drop)
        cues.append(
            Cue(
                timecode,
                cue.start,
                cue.text.replace("\n", " "),
                document.kind,
                end_seconds=cue.end,
                speaker=cue.speaker,
            )
        )

    return cues


# ---------------------------------------------------------------------------
# Decoded captions as a subtitle document
# ---------------------------------------------------------------------------


def _decoded_origin_seconds(timings, rate_code):
    """Where the file's own timeline starts, in seconds.

    Zero for a container, whose captions are stamped from a PTS that starts
    there anyway, and for an SCC that begins at 00:00:00:00.
    """
    if timings is None or not timings.entries:
        return 0.0
    try:
        return timings.seconds_at(timings.entries[0], rate_code)
    except (TimecodeError, KeyError):
        return 0.0


def document_from_decoded(caption_path, rate_code=None, track=None):
    """Read SCC, MCC or a media container into a `SubtitleDocument`.

    Everything that compares two caption files wants one shape, and the text
    formats already produce it. This puts the decoded formats on the same
    footing so an SCC can be compared against the SRT of the same episode.

    Two things do not survive the trip, and pretending otherwise would be
    worse than losing them:

        Out-times   608 has none. Text clears on a later control code, not on
                    a stamp of its own, so every cue here has `end=None` and
                    the checks that need a duration say they were not run
                    rather than passing vacuously.

        Zero        SCC and MCC stamp absolute programme timecode, so a cue at
                    01:00:02:00 is 3602 seconds. That is the file's own
                    timeline and it is kept; the comparison works out the
                    relationship rather than guessing an origin here.
    """
    from subtitle_formats import SubtitleCue, SubtitleDocument, SubtitleParseError

    caption_source = Path(caption_path)
    suffix = caption_source.suffix.lower()

    if rate_code is None:
        from frame_rate_detect import detect_frame_rate

        rate_code = detect_frame_rate(caption_source).rate_code

    # Only SCC needs the rate handed to the decoder; the rest carry their own.
    frame_rate_arg = rate_code if suffix == ".scc" else 0
    try:
        tracks, _ = decode_file(str(caption_source), frame_rate_arg or 0, capture_logs=True)
    except Exception as error:
        raise SubtitleParseError(f"{caption_source.name} could not be decoded: {error}") from error

    drop_frame = None
    declared_rate_code = None
    timings = None
    try:
        timings = read_caption_timings(caption_source)
        drop_frame = timings.drop_frame
        declared_rate_code = timings.declared_rate_code
    except CaptionTimingError:
        pass

    if track:
        family, name = track
        rows = tracks.get(family, {}).get(name, [])
    else:
        family, name, rows = _pick_track(tracks)

    if not rows:
        raise SubtitleParseError(
            f"No caption tracks with text were found in {caption_source.name}."
        )

    # `rate_code` can still be None for a file that states nothing and is too
    # short to infer from. The decode itself does not need it, but turning
    # timecodes into seconds does, and 29.97 is what the 608 formats are.
    resolved_rate = rate_code or declared_rate_code or 2997

    if family == "CEA-608":
        cues = build_cues_from_rows(rows, resolved_rate, drop_frame, channel=name)
    else:
        cues = build_cues_from_dtvcc_rows(rows, resolved_rate, drop_frame, service=name)

    if not cues:
        raise SubtitleParseError(
            f"{caption_source.name} decoded to {family} / {name}, but no visible cues came out of it."
        )

    # The decoder's clock starts at the file's first row, so a cue in an SCC
    # stamped from 01:00:00:00 comes back as elapsed time. Putting the origin
    # back makes these cues absolute programme timecode, which is what the
    # format actually carries and what a second SCC has to be compared against:
    # two deliveries of the same episode with different tape origins are not the
    # same file, and zeroing both would hide that.
    origin = _decoded_origin_seconds(timings, resolved_rate)
    if origin:
        for cue in cues:
            cue.seconds += origin
            if cue.end_seconds is not None:
                cue.end_seconds += origin

    modes = {}
    for cue in cues:
        modes[cue.mode] = modes.get(cue.mode, 0) + 1

    header = {
        "track": f"{family} / {name}",
        "tracks_found": ", ".join(
            f"{group} / {track_name}"
            for group, group_tracks in tracks.items()
            for track_name in group_tracks
        ),
        "caption_modes": ", ".join(f"{mode} x{count}" for mode, count in sorted(modes.items())),
        "read_at": f"{rate_label(resolved_rate)} fps"
        + (" drop-frame" if drop_frame else ""),
    }
    if timings is not None and timings.entries:
        header["start_timecode"] = timings.entries[0].timecode

    kind = suffix.lstrip(".") if suffix in (".scc", ".mcc") else "decoded"
    return SubtitleDocument(
        caption_source,
        kind,
        [
            SubtitleCue(cue.seconds, cue.end_seconds, cue.text, None, cue.speaker)
            for cue in cues
        ],
        frame_rate=RATE_BY_CODE[resolved_rate],
        drop_frame=bool(drop_frame),
        header=header,
    )


def read_comparable_document(caption_path, rate_code=None, track=None):
    """Read any supported caption or subtitle file into a `SubtitleDocument`.

    The text formats parse; SCC, MCC and media containers decode. Callers that
    only want "the cues in this file, whatever it is" ask here.
    """
    from subtitle_formats import TEXT_SUBTITLE_EXTENSIONS, read_subtitle_document

    if Path(caption_path).suffix.lower() in TEXT_SUBTITLE_EXTENSIONS:
        return read_subtitle_document(caption_path)
    return document_from_decoded(caption_path, rate_code=rate_code, track=track)
