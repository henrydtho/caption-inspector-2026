"""Work out what frame rate a caption file is in, by reading the file.

The app used to ask. That is the wrong question to put to a person holding a
delivery they did not author - the file either states its rate or it does not,
and when it does not, the stamps themselves usually give it away.

Three situations, and they are genuinely different:

    Declared     TTML says `ttp:frameRate`, EBU-STL says it in the GSI Disk
                 Format Code, MCC says `Time Code Rate=30DF`. Read it and say
                 so - then check it against the evidence, because a file
                 labelled 25 whose stamps are 29.97 frames is exactly the
                 vendor error the Sync QC tab exists to catch.

    Counted      SCC, MCC and Spruce STL stamp `HH:MM:SS:FF`. The frame field
                 cannot reach the counting rate, so the highest frame number in
                 the file is a floor under it: a file using frame 29 is not
                 25 fps. A `;` separator settles NTSC drop-frame outright.

    Quantised    SRT, WebVTT, ASS and the rest are wall-clock - seconds, no
                 frame rate anywhere in the format. But a subtitle file
                 converted from a frame-based master still has every stamp
                 sitting on a frame boundary, and which boundary tells you the
                 rate. A 25 fps master leaves every cue on a multiple of 40 ms;
                 a 29.97 master leaves them 33.367 ms apart, which no other
                 supported rate explains once the program is long enough.

The last one is the useful trick, and it is also the one that has to be honest
about failing: a file typed by hand at whole seconds, or generated at arbitrary
millisecond times, carries no frame-rate evidence at all. Saying "25" about
those would be inventing a fact, so this module says it does not know.
"""

from fractions import Fraction
from math import gcd
from pathlib import Path

from caption_timing import CaptionTimingError, read_caption_timings
from timecode import RATE_BY_CODE, RATE_LABELS, TimecodeError, parse_timecode, rate_label


# Counting rates a timecode's frame field can be running at, and the x100 codes
# each one covers. 29.97 and 30 count identically; so do 23.976 and 24.
COUNTING_RATES = {
    24: (2397, 2400),
    25: (2500,),
    30: (2997, 3000),
    50: (5000,),
    60: (5994, 6000),
}

# Below this many timed rows, "the highest frame number seen" is not evidence:
# a ten-cue file can easily never use frame 29 at 30 fps.
MIN_ROWS_FOR_FRAME_FIELD = 30

# Below this many stamps the quantisation test cannot separate neighbouring
# rates - early stamps fit almost anything, and it is the late ones that decide.
MIN_STAMPS_FOR_QUANTISATION = 12

# A stamp counts as landing on a frame boundary if it is within half the
# format's own storage precision of one. The µs of slack absorbs the float
# conversion on the way in.
_ROUNDING_SLACK_US = 1

# How finely each format can store a time. This is the rounding a stamp is
# allowed before it stops counting as sitting on a frame boundary, and it is a
# property of the format, not of the data: an SRT stores milliseconds whether or
# not the file in hand happens to use them.
_STORAGE_PRECISION_US = {
    "srt": 1_000,
    "vtt": 1_000,
    "sbv": 1_000,
    "sami": 1_000,
    "ttml": 1_000,
    "ass": 10_000,
    "ssa": 10_000,
    "subviewer": 10_000,
    "lrc": 10_000,
    "mpl2": 100_000,
    "realtext": 100_000,
}
_DEFAULT_PRECISION_US = 1_000

# Stamps all landing on a multiple this coarse are rounded, not authored. At
# 100 ms a stamp sits on a 24, 25, 30, 50 and 60 fps boundary at once, so a rate
# that "fits" is a coincidence of the rounding rather than evidence of anything.
_DEGENERATE_QUANTUM_US = 100_000

# Fraction of stamps that must land on frame boundaries before a rate is said to
# explain the file. Not 100%: hand-edited files routinely carry a few stamps
# nudged off the grid, and rejecting the whole rate over three of them would
# throw away the answer.
_FIT_THRESHOLD = 0.98

# Formats whose stamps are wall-clock seconds, so the quantisation test applies.
_WALL_CLOCK_KINDS = (
    "srt", "vtt", "ttml", "ass", "ssa", "sami", "subviewer", "sbv", "mpl2",
    "lrc", "realtext",
)

# Formats stamped `HH:MM:SS:FF`, where the frame field bounds the rate.
_FRAME_FIELD_KINDS = ("scc", "mcc", "spruce-stl")

# header key -> what to call it, per format, for the rates a file states outright.
_DECLARATION_SOURCES = {
    "ttml": ("frame_rate", "ttp:frameRate"),
    "ebu-stl": ("disk_format_code", "the GSI Disk Format Code"),
    "mcc": ("time_code_rate", "the Time Code Rate header"),
    "microdvd": ("frame_rate", "the file's rate pseudo-cue"),
    "spruce-stl": ("frame_rate", "$FrameRate"),
}

DECLARED = "declared"
INFERRED = "inferred"
AMBIGUOUS = "ambiguous"
UNKNOWN = "unknown"


class RateDetection:
    """What a file says about its frame rate, and what its stamps say."""

    def __init__(self, path, kind):
        self.path = str(path)
        self.kind = kind
        self.declared_code = None
        self.declared_source = None
        self.inferred_code = None
        self.candidates = []
        self.drop_frame = None
        self.confidence = UNKNOWN
        self.timebase = None
        self.notes = []
        self.conflict = False
        self.error = None

    @property
    def rate_code(self):
        """The rate to actually read this file at, or None if nothing says."""
        return self.declared_code or self.inferred_code

    @property
    def is_wall_clock(self):
        return self.timebase == "wall-clock"

    def label(self):
        code = self.rate_code
        return rate_label(code) if code else "unknown"

    def headline(self):
        """One line, in the words someone checking a delivery would use."""
        if self.error:
            return self.error

        code = self.rate_code
        if code is None:
            if self.is_wall_clock:
                return "No frame rate: this format is wall-clock and its stamps sit on no frame grid."
            return "Frame rate could not be determined from the file."

        rate = f"{rate_label(code)} fps"
        if self.drop_frame:
            rate += " drop-frame"
        elif self.drop_frame is False and code in (2997, 5994):
            rate += " non-drop"

        if self.conflict:
            return f"{rate} declared, but the stamps do not agree - see the notes."
        if self.confidence == DECLARED:
            return f"{rate}, declared by the file."
        if self.confidence == INFERRED:
            return f"{rate}, inferred from the file's own stamps."
        if self.confidence == AMBIGUOUS:
            others = ", ".join(rate_label(other) for other in self.candidates if other != code)
            return f"{rate} is the most likely, but {others} fit the file equally well."
        return rate

    def as_dict(self):
        return {
            "path": self.path,
            "kind": self.kind,
            "rate_code": self.rate_code,
            "rate_label": self.label(),
            "declared_code": self.declared_code,
            "declared_source": self.declared_source,
            "inferred_code": self.inferred_code,
            "candidates": list(self.candidates),
            "drop_frame": self.drop_frame,
            "confidence": self.confidence,
            "timebase": self.timebase,
            "conflict": self.conflict,
            "notes": list(self.notes),
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Quantisation: which frame grid do these wall-clock stamps sit on?
# ---------------------------------------------------------------------------


def _common_quantum_us(times_us):
    """The coarsest unit every stamp is a whole multiple of.

    This measures how rounded the data is, which is a different question from
    how finely the format can store it. A file of whole seconds has a 1 s
    quantum however many decimal places the format offers, and that is what
    makes it useless as frame-rate evidence.
    """
    if not times_us:
        return 1
    unit = 0
    for value in times_us:
        unit = gcd(unit, int(value))
    return unit or 1


def _fits_grid(times_us, rate, tolerance_us):
    """Fraction of stamps landing on a frame boundary at `rate`."""
    frame_us = Fraction(1_000_000) / Fraction(rate)
    hits = 0
    for value in times_us:
        index = int(Fraction(value) / frame_us + Fraction(1, 2))
        ideal = index * frame_us
        if abs(Fraction(value) - ideal) <= tolerance_us:
            hits += 1
    return hits / len(times_us)


def infer_from_quantisation(times_seconds, precision_us=_DEFAULT_PRECISION_US):
    """Which supported rates explain these wall-clock stamps?

    Returns (candidates, notes). Candidates are ordered lowest rate first: a
    file on a 25 fps grid is also on a 50 fps grid, and the lower rate is the
    one that was authored.
    """
    notes = []
    times_us = sorted({int(round(value * 1_000_000)) for value in times_seconds if value is not None})
    times_us = [value for value in times_us if value > 0]

    if len(times_us) < MIN_STAMPS_FOR_QUANTISATION:
        notes.append(
            f"Only {len(times_us)} distinct stamps: too few to tell one frame grid from another."
        )
        return [], notes

    quantum_us = _common_quantum_us(times_us)
    if quantum_us >= _DEGENERATE_QUANTUM_US:
        notes.append(
            f"Every stamp is a whole multiple of {quantum_us / 1000:g} ms. Times that round land on "
            "several frame grids at once, so this file carries no frame-rate evidence."
        )
        return [], notes

    tolerance = Fraction(precision_us, 2) + _ROUNDING_SLACK_US
    fits = []
    for code in sorted(RATE_BY_CODE):
        share = _fits_grid(times_us, RATE_BY_CODE[code], tolerance)
        if share >= _FIT_THRESHOLD:
            fits.append((code, share))

    if not fits:
        notes.append(
            "No supported frame rate puts these stamps on frame boundaries. The file is "
            "wall-clock throughout - it was not converted from a frame-based master."
        )
        return [], notes

    notes.append(
        f"All {len(times_us)} stamps land on a {rate_label(fits[0][0])} fps frame boundary, within "
        f"the {precision_us / 1000:g} ms this format stores times to."
    )
    if len(fits) > 1:
        higher = ", ".join(rate_label(code) for code, _ in fits[1:])
        notes.append(
            f"{higher} also fit, as every frame at the lower rate is also a frame at the higher "
            "one. The lowest rate that explains the file is the one it was authored at."
        )
    return [code for code, _ in fits], notes


# ---------------------------------------------------------------------------
# Frame field: what does HH:MM:SS:FF rule out?
# ---------------------------------------------------------------------------


def infer_from_frame_field(frame_numbers, drop_frame):
    """Which counting rates can a timecode using these frame numbers be at?"""
    notes = []
    if not frame_numbers:
        return [], notes

    highest = max(frame_numbers)
    possible = sorted(counting for counting in COUNTING_RATES if counting > highest)
    if not possible:
        notes.append(
            f"The highest frame number in the file is {highest}, which is beyond every supported "
            "counting rate. The timecodes are malformed."
        )
        return [], notes

    counting = possible[0]
    notes.append(
        f"The highest frame number used is {highest}, so the file counts at least "
        f"{highest + 1} frames per second."
    )

    if len(frame_numbers) < MIN_ROWS_FOR_FRAME_FIELD:
        notes.append(
            f"Only {len(frame_numbers)} timed rows, which is too few for the highest frame number "
            "to be trusted as the top of the count."
        )
        return [], notes

    if highest + 1 != counting:
        notes.append(
            f"No row reaches frame {counting - 1}, so {counting} fps is a floor rather than a "
            "reading. The file may be running faster than that."
        )

    codes = list(COUNTING_RATES[counting])
    if drop_frame:
        drop_capable = [code for code in codes if code in (2997, 5994)]
        if drop_capable:
            notes.append(
                "The timecodes use a `;` separator, which only exists for the NTSC rates, so "
                "this is drop-frame."
            )
            return drop_capable, notes

    if len(codes) > 1:
        notes.append(
            f"{' and '.join(rate_label(code) for code in codes)} count frames identically, so the "
            "timecodes alone cannot separate them."
        )
    return codes, notes


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------


def _declaration(kind, header, declared_code):
    """What the file states outright, if it states anything.

    `caption_timing` fills in a rate for SCC and for the frame-counting text
    formats that declare none, which is right for reading the file but is not a
    declaration, and reporting it as one would put a guess in front of someone
    as a fact.
    """
    entry = _DECLARATION_SOURCES.get(kind)
    if not entry or declared_code is None:
        return None, None

    key, source = entry
    if key not in header:
        return None, None
    if header.get("frame_rate_source", "").startswith("assumed"):
        return None, None
    return declared_code, source


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def detect_frame_rate(path):
    """Read `path` and report what frame rate it is in."""
    subtitle_path = Path(path)
    detection = RateDetection(subtitle_path, None)

    try:
        timings = read_caption_timings(subtitle_path)
    except CaptionTimingError as error:
        detection.error = str(error)
        return detection

    detection.kind = timings.kind
    detection.drop_frame = timings.drop_frame if timings.is_timecode_based else None

    header = dict(timings.header or {})
    document = getattr(timings, "subtitle_document", None)
    if document is not None:
        header.update({key: str(value) for key, value in (document.header or {}).items()})

    declared_code, declared_source = _declaration(
        timings.kind, header, timings.declared_rate_code
    )
    detection.declared_code = declared_code
    detection.declared_source = declared_source

    if timings.kind in _FRAME_FIELD_KINDS:
        detection.timebase = "timecode"
        frames = []
        for entry in timings.entries:
            try:
                _, _, _, frame, _ = parse_timecode(entry.timecode)
            except TimecodeError:
                continue
            frames.append(frame)
        candidates, notes = infer_from_frame_field(frames, timings.drop_frame)
    elif timings.kind in _WALL_CLOCK_KINDS:
        detection.timebase = "wall-clock"
        stamps = []
        if document is not None:
            for cue in document.cues:
                stamps.append(cue.start)
                if cue.end is not None:
                    stamps.append(cue.end)
        candidates, notes = infer_from_quantisation(
            stamps, _STORAGE_PRECISION_US.get(timings.kind, _DEFAULT_PRECISION_US)
        )
        # TTML has a `ttp:frameRate` field; the rest of the wall-clock formats
        # have nowhere to put a rate at all, which is worth saying once.
        if candidates and timings.kind not in _DECLARATION_SOURCES:
            notes.insert(
                0,
                f"{_format_name(timings.kind)} has no frame-rate field - its stamps are "
                "wall-clock - so this is read off the grid they sit on.",
            )
    else:
        # MicroDVD counts frames but names no rate anywhere; EBU-STL always
        # declares one. Neither has stamps to infer from.
        detection.timebase = "frames" if timings.kind == "microdvd" else "timecode"
        candidates, notes = [], []

    detection.candidates = candidates
    detection.notes.extend(notes)

    if candidates:
        detection.inferred_code = candidates[0]

    if declared_code is not None:
        detection.confidence = DECLARED
        detection.notes.insert(
            0, f"The file states {rate_label(declared_code)} fps in {declared_source}."
        )
        if candidates and declared_code not in candidates:
            detection.conflict = True
            detection.notes.append(
                f"The stamps do not agree: they fit {', '.join(rate_label(code) for code in candidates)}, "
                f"not the {rate_label(declared_code)} the file claims. One of the two is wrong, and a "
                "mislabelled rate is the usual cause of a delivery that drifts."
            )
        if declared_code == 3000 and timings.kind == "ebu-stl":
            detection.notes.append(
                "EBU-STL spells NTSC as `STL30.01`, so a file delivered for 29.97 states 30 here. "
                "Check the rate against the video before trusting it."
            )
    elif len(candidates) == 1:
        detection.confidence = INFERRED
    elif len(candidates) > 1:
        detection.confidence = AMBIGUOUS
    else:
        detection.confidence = UNKNOWN

    return detection


def _format_name(kind):
    from subtitle_formats import format_label

    return format_label(kind)


def detected_rate_code(path, default=None):
    """Just the rate code, for callers that only want the number."""
    try:
        detection = detect_frame_rate(path)
    except Exception:
        return default
    return detection.rate_code or default
