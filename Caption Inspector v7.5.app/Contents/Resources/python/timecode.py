"""SMPTE timecode and frame-rate math for the sync QC tier.

Everything here is exact-rational on purpose. Comparing a caption file's last
timecode against a video's duration is only meaningful to the frame if the
29.97 / 23.98 rates stay as 30000/1001 and 24000/1001 rather than decimals.
"""

import re
from fractions import Fraction


# Frame rates keyed by the "x100" code the rest of the app already speaks
# (the -f flag of the C tool, the Frame rate x100 spinbox in the UI).
RATE_BY_CODE = {
    2397: Fraction(24000, 1001),
    2400: Fraction(24, 1),
    2500: Fraction(25, 1),
    2997: Fraction(30000, 1001),
    3000: Fraction(30, 1),
    5000: Fraction(50, 1),
    5994: Fraction(60000, 1001),
    6000: Fraction(60, 1),
}

RATE_LABELS = {
    2397: "23.976",
    2400: "24",
    2500: "25",
    2997: "29.97",
    3000: "30",
    5000: "50",
    5994: "59.94",
    6000: "60",
}

# Rates that carry a drop-frame variant. Drop-frame only exists for the
# 1001-denominator NTSC rates.
DROP_FRAME_CODES = (2997, 5994)

# Shared "Auto (read from the file)" + explicit rate dropdown, so any panel
# that needs a fallback for an SCC too short to auto-detect (see
# frame_rate_detect.py) can offer the same choices without inventing its own.
AUTO_RATE_LABEL = "Auto (read from the file)"


def rate_choice_labels():
    """Frame-rate dropdown values: auto-detect first, then every known rate."""
    return [AUTO_RATE_LABEL] + [
        f"{label} fps" for _, label in sorted(RATE_LABELS.items(), key=lambda item: item[0])
    ]


def rate_code_for_choice(choice):
    """The rate code for a `rate_choice_labels()` value, or None for "Auto"."""
    if not choice or choice == AUTO_RATE_LABEL:
        return None
    label = choice.replace(" fps", "").strip()
    for code, candidate_label in RATE_LABELS.items():
        if candidate_label == label:
            return code
    return None


TIMECODE_PATTERN = re.compile(
    r"^\s*(?P<hour>\d{1,2}):(?P<minute>\d{2}):(?P<second>\d{2})(?P<sep>[:;.,])(?P<frame>\d{1,3})\s*$"
)


class TimecodeError(ValueError):
    """Raised when a timecode string cannot be interpreted."""


def rate_code_for(rate):
    """Map a float/Fraction frame rate onto the nearest supported x100 code."""
    if rate is None:
        return None

    target = Fraction(rate).limit_denominator(100000)
    best_code = None
    best_delta = None
    for code, candidate in RATE_BY_CODE.items():
        delta = abs(float(candidate - target))
        if best_delta is None or delta < best_delta:
            best_code = code
            best_delta = delta

    # 0.05 fps is tight enough to keep 29.97 and 30 apart but loose enough to
    # absorb ffprobe reporting 29.970030 instead of 30000/1001.
    return best_code if best_delta is not None and best_delta <= 0.05 else None


def rate_label(code):
    return RATE_LABELS.get(code, str(code / 100.0 if code else "unknown"))


def nominal_rate(rate):
    """Timecode counting rate: 29.97 counts 30 frames per labelled second."""
    return int(round(float(rate)))


def parse_timecode(text):
    """Parse HH:MM:SS:FF or HH:MM:SS;FF.

    Returns (hour, minute, second, frame, drop_frame). The semicolon (or comma
    or period) separator before the frame field is the SCC / MCC convention for
    flagging drop-frame.
    """
    match = TIMECODE_PATTERN.match(text or "")
    if not match:
        raise TimecodeError(f"Not a timecode: {text!r}")

    return (
        int(match.group("hour")),
        int(match.group("minute")),
        int(match.group("second")),
        int(match.group("frame")),
        match.group("sep") in (";", ","),
    )


def timecode_to_frames(text, rate, drop_frame=None):
    """Convert a timecode string to an absolute frame count at `rate`.

    `drop_frame` overrides the separator in the string; pass None to trust the
    string. Drop-frame counting skips frame numbers 00/01 (or 00-03 at 59.94)
    at the top of every minute except every tenth minute.
    """
    hour, minute, second, frame, tc_is_drop = parse_timecode(text)
    is_drop = tc_is_drop if drop_frame is None else drop_frame

    counting_rate = nominal_rate(rate)
    frames = ((hour * 60 + minute) * 60 + second) * counting_rate + frame

    if not is_drop:
        return frames

    # Only 29.97/59.94 have a meaningful drop-frame cadence. Two dropped frames
    # per minute at 30, scaled by the multiple of 30 for higher rates.
    dropped_per_minute = 2 * (counting_rate // 30)
    if dropped_per_minute == 0:
        return frames

    total_minutes = hour * 60 + minute
    return frames - dropped_per_minute * (total_minutes - total_minutes // 10)


def frames_to_timecode(frames, rate, drop_frame=False):
    """Inverse of `timecode_to_frames`, for report rendering."""
    frames = int(round(frames))
    counting_rate = nominal_rate(rate)
    negative = frames < 0
    frames = abs(frames)

    if drop_frame:
        dropped_per_minute = 2 * (counting_rate // 30)
        frames_per_10min = counting_rate * 600 - dropped_per_minute * 9
        frames_per_min = counting_rate * 60 - dropped_per_minute

        ten_minute_blocks = frames // frames_per_10min
        remainder = frames % frames_per_10min
        if remainder >= dropped_per_minute:
            remainder_minutes = (remainder - dropped_per_minute) // frames_per_min
        else:
            remainder_minutes = 0
        frames += dropped_per_minute * (9 * ten_minute_blocks + remainder_minutes)
        separator = ";"
    else:
        separator = ":"

    frame = frames % counting_rate
    total_seconds = frames // counting_rate
    second = total_seconds % 60
    minute = (total_seconds // 60) % 60
    hour = total_seconds // 3600

    sign = "-" if negative else ""
    return f"{sign}{hour:02d}:{minute:02d}:{second:02d}{separator}{frame:02d}"


def frames_to_seconds(frames, rate):
    """Wall-clock seconds for a frame count played at `rate`."""
    return float(Fraction(int(round(frames))) / Fraction(rate))


def seconds_to_frames(seconds, rate):
    return float(Fraction(seconds).limit_denominator(1000000) * Fraction(rate))


def timecode_to_seconds(text, rate, drop_frame=None):
    return frames_to_seconds(timecode_to_frames(text, rate, drop_frame), rate)


def format_seconds(seconds):
    """HH:MM:SS.mmm, for humans reading a report."""
    negative = seconds < 0
    seconds = abs(float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    whole = seconds % 60
    return f"{'-' if negative else ''}{hours:02d}:{minutes:02d}:{whole:06.3f}"


def format_offset_ms(seconds):
    """Signed millisecond string, the unit vendors actually argue about."""
    return f"{seconds * 1000:+.0f} ms"


def explain_timing_ratio(ratio, min_coverage=0.90, max_coverage=1.005):
    """Name the rate pair behind an observed caption-span-to-program ratio.

    An observed ratio is never exactly the rate ratio, because a caption file
    stops at the last line of dialogue rather than the last frame of picture.
    So the test is not "does this equal 1.1988" but "is there a rate pair whose
    ratio, multiplied by a believable coverage of the program, lands here".
    The implied coverage comes back with the match and is worth quoting: a rate
    pair that only fits if the file covers 62% of the program is not a match.
    """
    if ratio <= 0:
        return []

    matches = []
    for authored_code, authored_rate in RATE_BY_CODE.items():
        for played_code, played_rate in RATE_BY_CODE.items():
            if authored_code == played_code:
                continue
            pair_ratio = float(authored_rate / played_rate)
            implied_coverage = ratio / pair_ratio
            if not (min_coverage <= implied_coverage <= max_coverage):
                continue
            matches.append(
                {
                    "authored_code": authored_code,
                    "played_code": played_code,
                    "ratio": pair_ratio,
                    "implied_coverage": implied_coverage,
                    "text": (
                        f"timed against {rate_label(authored_code)} fps "
                        f"but delivered at {rate_label(played_code)} fps"
                    ),
                }
            )

    # Closest to full coverage wins: that is the pair that explains the file
    # with the least left over.
    matches.sort(key=lambda item: abs(item["implied_coverage"] - 1.0))
    return matches


def rate_ratio_explanations(ratio, tolerance=0.0015):
    """Name the frame-rate pair that would produce an observed timing ratio.

    A caption file authored against rate A but played at rate B runs long or
    short by exactly A/B. Handing the vendor "1.1988, i.e. 29.97 into 25" is the
    difference between a fixable ticket and an argument.
    """
    matches = []
    for authored_code, authored_rate in RATE_BY_CODE.items():
        for played_code, played_rate in RATE_BY_CODE.items():
            if authored_code == played_code:
                continue
            candidate = float(authored_rate / played_rate)
            if abs(candidate - ratio) <= tolerance * max(1.0, candidate):
                matches.append(
                    {
                        "authored_code": authored_code,
                        "played_code": played_code,
                        "ratio": candidate,
                        "text": (
                            f"timed against {rate_label(authored_code)} fps "
                            f"but delivered at {rate_label(played_code)} fps"
                        ),
                    }
                )

    matches.sort(key=lambda item: abs(item["ratio"] - ratio))
    return matches
