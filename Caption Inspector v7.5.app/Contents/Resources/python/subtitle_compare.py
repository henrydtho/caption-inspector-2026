"""Compare two subtitle files for the same programme.

The question this answers is the one asked when two versions of a delivery turn
up and nobody can say what changed between them: are these the same programme,
is either of them broken, and where exactly do they differ?

Three passes, in that order, because the later ones are meaningless if an
earlier one fails:

    Validity   Each file on its own. Cues that run backwards, overlap, clear
               before they appear, or sit on screen for four frames. None of
               this needs the other file and all of it invalidates a diff.

    Alignment  Match A's cues to B's by their text, not by their index. Index
               matching is what makes a single inserted cue at the head report
               every remaining line in the programme as changed - the classic
               way a one-line diff turns into a nine-hundred-line one.

    Timing     For every pair that matched, how far apart are they? A constant
               gap is an offset; a gap that grows with position is drift, and
               the slope names the frame-rate pair that caused it.

The distinction that matters most in the output is between a cue whose *words*
changed and one where only punctuation or capitalisation moved. Both are
differences, but only one of them is a script change, and burying the four real
edits among two hundred smart-quote conversions is how a diff stops being read.
"""

import difflib
import json
from html import escape
from pathlib import Path

from cancellation import raise_if_cancelled
from caption_cues import normalize_caption_text, read_comparable_document
from caption_timing import is_program_timecode
from frame_rate_detect import detect_frame_rate
from subtitle_formats import SubtitleParseError, format_label
from sync_check import FAIL, INFO, PASS, WARN, Check
from timecode import format_offset_ms, format_seconds, rate_label, rate_ratio_explanations


_SEVERITY = {INFO: 0, PASS: 1, WARN: 2, FAIL: 3}

# Two cues count as simultaneous when their starts are this close. Below it the
# difference is rounding between two formats' storage precisions, not an edit.
DEFAULT_TOLERANCE_MS = 200

# Cues shorter than this are on screen too briefly to read; longer than this and
# something has failed to clear.
MIN_CUE_SECONDS = 0.5
MAX_CUE_SECONDS = 10.0

# Broadcast subtitle convention: two lines, 42 characters, and a reading rate
# an adult can follow. Exceeded occasionally is normal; exceeded throughout is a
# file that was never conformed.
MAX_LINES_PER_CUE = 3
MAX_CHARS_PER_LINE = 42
MAX_CHARS_PER_SECOND = 25.0

# Below this share of matched cues the two files are not the same programme, and
# every number computed from the alignment is noise.
SAME_PROGRAMME_THRESHOLD = 0.20

# A pair of cues found in a `replace` block is the *same* line edited rather
# than two unrelated lines when their words overlap at least this much.
_EDIT_SIMILARITY = 0.5

SAME = "same"
TIMING = "timing"
TEXT = "text"
ONLY_A = "only_a"
ONLY_B = "only_b"

WORDING = "wording"
FORMATTING = "formatting"


class CueDiff:
    """One line of the comparison."""

    def __init__(self, kind, index_a=None, index_b=None, cue_a=None, cue_b=None,
                 change=None, words=None, baseline=0.0):
        self.kind = kind
        # How far apart the two files are overall, so `relative_start` can say
        # how far this cue departs from that rather than from zero.
        self.baseline = baseline
        self.index_a = index_a
        self.index_b = index_b
        self.cue_a = cue_a
        self.cue_b = cue_b
        # 'wording' or 'formatting', for a text change.
        self.change = change
        # [(op, text)] with op in equal/delete/insert, for inline rendering.
        self.words = words or []

    @property
    def delta_start(self):
        if self.cue_a is None or self.cue_b is None:
            return None
        return self.cue_b.start - self.cue_a.start

    @property
    def delta_end(self):
        if self.cue_a is None or self.cue_b is None:
            return None
        if self.cue_a.end is None or self.cue_b.end is None:
            return None
        return self.cue_b.end - self.cue_a.end

    @property
    def relative_start(self):
        """The cue's shift once the file-wide offset is taken out."""
        delta = self.delta_start
        return None if delta is None else delta - self.baseline

    @property
    def relative_end(self):
        delta = self.delta_end
        return None if delta is None else delta - self.baseline

    def as_dict(self):
        return {
            "kind": self.kind,
            "change": self.change,
            "index_a": self.index_a,
            "index_b": self.index_b,
            "start_a": self.cue_a.start if self.cue_a else None,
            "start_b": self.cue_b.start if self.cue_b else None,
            "text_a": self.cue_a.text if self.cue_a else None,
            "text_b": self.cue_b.text if self.cue_b else None,
            "delta_start": self.delta_start,
            "delta_end": self.delta_end,
            "relative_start": self.relative_start,
            "baseline": self.baseline,
            "words": list(self.words),
        }


class FileSummary:
    """What one side of the comparison is."""

    def __init__(self, path, document, detection):
        self.path = str(path)
        self.name = Path(path).name
        self.document = document
        self.detection = detection

    @property
    def kind(self):
        return self.document.kind

    @property
    def cues(self):
        return self.document.cues

    @property
    def is_program_timecode(self):
        """SCC, MCC and both STLs stamp absolute programme timecode.

        A file whose zero is 10:00:00:00 is not ten hours late; it is stamped
        against the tape. Knowing which side is which is what stops that being
        reported as a sync error.
        """
        return is_program_timecode(self.document.kind)

    @property
    def states_out_times(self):
        """608 clears on a control code, not on a stamp. Nothing to check."""
        return any(cue.end is not None for cue in self.document.cues)

    def span(self):
        return self.document.span()

    def as_dict(self):
        start, end = self.span()
        return {
            "path": self.path,
            "name": self.name,
            "format": format_label(self.kind),
            "track": self.document.header.get("track"),
            "program_timecode": self.is_program_timecode,
            "cues": len(self.document.cues),
            "start": start,
            "end": end,
            "frame_rate": self.detection.label() if self.detection else None,
            "frame_rate_confidence": self.detection.confidence if self.detection else None,
        }


class ComparisonResult:
    def __init__(self, path_a, path_b):
        self.path_a = str(path_a)
        self.path_b = str(path_b)
        self.file_a = None
        self.file_b = None
        self.checks = []
        self.diffs = []
        self.stats = {}
        self.errors = []

    def add(self, check):
        if check is not None:
            self.checks.append(check)
        return check

    @property
    def verdict(self):
        if self.errors:
            return FAIL
        worst = INFO
        for check in self.checks:
            if _SEVERITY[check.status] > _SEVERITY[worst]:
                worst = check.status
        return PASS if worst == INFO else worst

    def differences(self):
        return [diff for diff in self.diffs if diff.kind != SAME]

    def of_kind(self, kind):
        return [diff for diff in self.diffs if diff.kind == kind]

    def as_dict(self):
        return {
            "file_a": self.file_a.as_dict() if self.file_a else None,
            "file_b": self.file_b.as_dict() if self.file_b else None,
            "verdict": self.verdict,
            "stats": dict(self.stats),
            "checks": [check.as_dict() for check in self.checks],
            "differences": [diff.as_dict() for diff in self.differences()],
            "errors": list(self.errors),
        }


# ---------------------------------------------------------------------------
# Validity: each file on its own
# ---------------------------------------------------------------------------


def _examples(items, limit=4):
    shown = "; ".join(items[:limit])
    if len(items) > limit:
        shown += f"; and {len(items) - limit} more"
    return shown


def _cue_reference(index, cue):
    return f"#{index} at {format_seconds(cue.start)}"


def validate_document(document, label):
    """Everything checkable about one subtitle file without the other."""
    cues = document.cues
    checks = []

    if not cues:
        return [Check(f"{label}: content", FAIL, "The file contains no cues.")]

    out_of_order = []
    for index in range(1, len(cues)):
        if cues[index].start < cues[index - 1].start:
            out_of_order.append(
                f"#{index + 1} at {format_seconds(cues[index].start)} follows "
                f"{format_seconds(cues[index - 1].start)}"
            )
    checks.append(
        Check(f"{label}: cue order", PASS, "Cue start times advance throughout.")
        if not out_of_order
        else Check(
            f"{label}: cue order",
            FAIL,
            f"{len(out_of_order)} cues start before the cue in front of them.",
            [
                "A subtitle file is a timeline; cues that run backwards mean the export is broken.",
                "Examples: " + _examples(out_of_order),
            ],
            {"count": len(out_of_order)},
        )
    )

    # CEA-608 clears on a later control code, so a decoded track has in-times
    # only. Checking durations against nothing and reporting PASS would be a
    # clean bill of health for a test that never ran.
    timed = [cue for cue in cues if cue.end is not None]
    if not timed:
        checks.append(
            Check(
                f"{label}: cue duration",
                INFO,
                "This format states no cue out-times, so duration and overlap were not checked.",
                [
                    "CEA-608 text clears on a later control code rather than on a stamp of its "
                    "own, so there is no out-time to check against.",
                ],
            )
        )

    inverted, instant, brief, endless = [], [], [], []
    for index, cue in enumerate(cues, start=1):
        if cue.end is None:
            continue
        duration = cue.end - cue.start
        if duration < 0:
            inverted.append(_cue_reference(index, cue))
        elif duration == 0:
            instant.append(_cue_reference(index, cue))
        elif duration < MIN_CUE_SECONDS:
            brief.append(f"{_cue_reference(index, cue)} for {duration:.2f}s")
        elif duration > MAX_CUE_SECONDS:
            endless.append(f"{_cue_reference(index, cue)} for {duration:.1f}s")

    if inverted or instant:
        broken = inverted + instant
        checks.append(
            Check(
                f"{label}: cue duration",
                FAIL,
                f"{len(broken)} cues clear at or before the moment they appear.",
                [
                    "A cue whose out is not after its in never reaches the screen.",
                    "Examples: " + _examples(broken),
                ],
                {"inverted": len(inverted), "zero_length": len(instant)},
            )
        )
    elif brief or endless:
        detail = []
        if brief:
            detail.append(f"{len(brief)} shorter than {MIN_CUE_SECONDS:g}s: " + _examples(brief))
        if endless:
            detail.append(f"{len(endless)} longer than {MAX_CUE_SECONDS:g}s: " + _examples(endless))
        checks.append(
            Check(
                f"{label}: cue duration",
                WARN,
                f"{len(brief) + len(endless)} cues sit outside the readable range.",
                detail,
                {"too_short": len(brief), "too_long": len(endless)},
            )
        )
    elif timed:
        checks.append(Check(f"{label}: cue duration", PASS, "Every cue is on screen for a readable time."))

    overlaps = []
    for index in range(1, len(cues)):
        previous, current = cues[index - 1], cues[index]
        if previous.end is None:
            continue
        if current.start < previous.end - 0.001:
            overlaps.append(
                f"#{index} ends {format_seconds(previous.end)} but #{index + 1} starts "
                f"{format_seconds(current.start)}"
            )
    # Skipped entirely without out-times; the note under cue duration says so.
    if timed and not overlaps:
        checks.append(Check(f"{label}: overlap", PASS, "No two cues are on screen at once."))
    elif overlaps:
        checks.append(
            Check(
                f"{label}: overlap",
                WARN,
                f"{len(overlaps)} cues overlap the one before them.",
                [
                    "Overlapping cues stack or replace each other depending on the player.",
                    "Examples: " + _examples(overlaps),
                ],
                {"count": len(overlaps)},
            )
        )

    long_lines, tall_cues, fast = [], [], []
    for index, cue in enumerate(cues, start=1):
        lines = cue.text.splitlines() or [cue.text]
        if len(lines) > MAX_LINES_PER_CUE:
            tall_cues.append(f"{_cue_reference(index, cue)} has {len(lines)} lines")
        for line in lines:
            if len(line) > MAX_CHARS_PER_LINE:
                long_lines.append(f"{_cue_reference(index, cue)} runs {len(line)} characters")
                break
        if cue.end is not None:
            duration = cue.end - cue.start
            characters = len(cue.text.replace("\n", " "))
            if duration >= MIN_CUE_SECONDS and characters / duration > MAX_CHARS_PER_SECOND:
                fast.append(f"{_cue_reference(index, cue)} at {characters / duration:.0f} cps")

    conformance = []
    if long_lines:
        conformance.append(f"{len(long_lines)} cues over {MAX_CHARS_PER_LINE} characters on a line: " + _examples(long_lines))
    if tall_cues:
        conformance.append(f"{len(tall_cues)} cues over {MAX_LINES_PER_CUE} lines: " + _examples(tall_cues))
    if fast:
        conformance.append(f"{len(fast)} cues above {MAX_CHARS_PER_SECOND:g} characters per second: " + _examples(fast))

    checks.append(
        Check(f"{label}: readability", PASS, "Line lengths and reading rates are within convention.")
        if not conformance
        else Check(
            f"{label}: readability",
            WARN,
            f"{len(long_lines) + len(tall_cues) + len(fast)} cues exceed the usual broadcast limits.",
            conformance + ["These are conventions, not errors - house style may differ."],
            {"long_lines": len(long_lines), "tall_cues": len(tall_cues), "fast": len(fast)},
        )
    )

    return checks


# ---------------------------------------------------------------------------
# Alignment
# ---------------------------------------------------------------------------


def _word_diff(text_a, text_b):
    """Inline word-level diff, as [(op, text)] with op in equal/delete/insert."""
    words_a = text_a.replace("\n", " ").split()
    words_b = text_b.replace("\n", " ").split()
    matcher = difflib.SequenceMatcher(None, words_a, words_b, autojunk=False)

    parts = []
    for op, a_start, a_end, b_start, b_end in matcher.get_opcodes():
        if op == "equal":
            parts.append(("equal", " ".join(words_a[a_start:a_end])))
        else:
            if a_start != a_end:
                parts.append(("delete", " ".join(words_a[a_start:a_end])))
            if b_start != b_end:
                parts.append(("insert", " ".join(words_b[b_start:b_end])))
    return parts


def _classify_pair(index_a, cue_a, index_b, cue_b, tolerance_s, baseline=0.0):
    """One matched pair: unchanged, retimed, or edited.

    `baseline` is how far apart the two files are overall. A cue is "retimed"
    when it departs from *that*, not from zero - otherwise a file delivered
    with a ten-second pre-roll, or an SCC stamped from 01:00:00:00, reports
    every single cue as retimed and the one cue that genuinely moved is lost
    among them.
    """
    if cue_a.text == cue_b.text:
        delta = abs(cue_b.start - cue_a.start - baseline)
        if delta <= tolerance_s:
            return CueDiff(SAME, index_a, index_b, cue_a, cue_b)
        return CueDiff(TIMING, index_a, index_b, cue_a, cue_b)

    change = (
        FORMATTING
        if normalize_caption_text(cue_a.text) == normalize_caption_text(cue_b.text)
        else WORDING
    )
    return CueDiff(
        TEXT, index_a, index_b, cue_a, cue_b, change, _word_diff(cue_a.text, cue_b.text)
    )


def align_cues(cues_a, cues_b, tolerance_s, cancel=None):
    """Pair A's cues with B's by text, and classify every pair.

    Matched on normalised text so that a punctuation change does not break the
    alignment and shunt every later cue out of position - the reason this is not
    a positional diff.
    """
    keys_a = [normalize_caption_text(cue.text) for cue in cues_a]
    keys_b = [normalize_caption_text(cue.text) for cue in cues_b]

    matcher = difflib.SequenceMatcher(None, keys_a, keys_b, autojunk=False)
    diffs = []

    for op, a_start, a_end, b_start, b_end in matcher.get_opcodes():
        raise_if_cancelled(cancel)

        if op == "equal":
            for offset in range(a_end - a_start):
                index_a, index_b = a_start + offset, b_start + offset
                diffs.append(
                    _classify_pair(
                        index_a + 1, cues_a[index_a], index_b + 1, cues_b[index_b], tolerance_s
                    )
                )
            continue

        if op == "delete":
            for index in range(a_start, a_end):
                diffs.append(CueDiff(ONLY_A, index + 1, None, cues_a[index], None))
            continue

        if op == "insert":
            for index in range(b_start, b_end):
                diffs.append(CueDiff(ONLY_B, None, index + 1, None, cues_b[index]))
            continue

        # `replace`: a run of cues that changed. Pair them off in order, but
        # only where the words still overlap - two genuinely different lines
        # reported as one edit is less readable than a removal and an addition.
        length_a, length_b = a_end - a_start, b_end - b_start
        paired = min(length_a, length_b)
        for offset in range(paired):
            index_a, index_b = a_start + offset, b_start + offset
            similarity = difflib.SequenceMatcher(
                None, keys_a[index_a], keys_b[index_b], autojunk=False
            ).ratio()
            if similarity >= _EDIT_SIMILARITY:
                diffs.append(
                    _classify_pair(
                        index_a + 1, cues_a[index_a], index_b + 1, cues_b[index_b], tolerance_s
                    )
                )
            else:
                diffs.append(CueDiff(ONLY_A, index_a + 1, None, cues_a[index_a], None))
                diffs.append(CueDiff(ONLY_B, None, index_b + 1, None, cues_b[index_b]))

        for offset in range(paired, length_a):
            index = a_start + offset
            diffs.append(CueDiff(ONLY_A, index + 1, None, cues_a[index], None))
        for offset in range(paired, length_b):
            index = b_start + offset
            diffs.append(CueDiff(ONLY_B, None, index + 1, None, cues_b[index]))

    return diffs


def baseline_offset(diffs):
    """How far apart the two files are overall - the median of matched pairs.

    The median rather than the mean: a handful of genuinely retimed cues should
    not drag the baseline they are being measured against.
    """
    deltas = [
        diff.delta_start
        for diff in diffs
        if diff.kind in (SAME, TIMING, TEXT) and diff.delta_start is not None
    ]
    return _median(deltas) or 0.0


def apply_baseline(diffs, baseline, tolerance_s):
    """Re-judge every matched pair against the file-wide offset."""
    for diff in diffs:
        diff.baseline = baseline
        if diff.kind not in (SAME, TIMING):
            continue
        departure = abs(diff.delta_start - baseline) if diff.delta_start is not None else 0.0
        diff.kind = SAME if departure <= tolerance_s else TIMING
    return diffs


# ---------------------------------------------------------------------------
# Timing relationship
# ---------------------------------------------------------------------------


def _median(values):
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _least_squares(points):
    """(slope, intercept) of delta against position, or (0, median) if flat."""
    count = len(points)
    if count < 3:
        return 0.0, _median([delta for _, delta in points]) or 0.0

    mean_t = sum(t for t, _ in points) / count
    mean_d = sum(d for _, d in points) / count
    variance = sum((t - mean_t) ** 2 for t, _ in points)
    if variance == 0:
        return 0.0, mean_d

    slope = sum((t - mean_t) * (d - mean_d) for t, d in points) / variance
    return slope, mean_d - slope * mean_t


def _timebase_note(file_a, file_b, median_delta):
    """Is the gap between these two files just their timecode origin?

    An SCC is stamped against the tape and an SRT against the head of the
    programme, so the two are an hour apart before anything is wrong. Reporting
    that as a sync error is how a correct pair of files fails a check.
    """
    if file_a is None or file_b is None:
        return None
    if file_a.is_program_timecode == file_b.is_program_timecode:
        return None

    stamped, counted = (
        (file_a, file_b) if file_a.is_program_timecode else (file_b, file_a)
    )
    sign = -1 if file_a.is_program_timecode else 1
    origin = sign * median_delta

    note = (
        f"{stamped.name} is {format_label(stamped.kind)}, which stamps absolute programme "
        f"timecode, while {counted.name} counts from the head of the programme. The two are "
        f"{format_seconds(abs(origin))} apart before anything is wrong."
    )
    # A whole number of hours is the giveaway: 01:00:00:00 and 10:00:00:00 are
    # the tape origins in circulation.
    if abs(origin) > 1.0 and abs(round(abs(origin) / 3600.0) * 3600.0 - abs(origin)) < 2.0:
        hours = round(abs(origin) / 3600.0)
        note += (
            f" That is {hours} hour{'s' if hours != 1 else ''} to the second, so it is the "
            "tape origin rather than a sync error."
        )
    return note


def analyse_timing(diffs, tolerance_s, file_a=None, file_b=None):
    """How B's timeline relates to A's, over every cue that matched."""
    points = [
        (diff.cue_a.start, diff.delta_start)
        for diff in diffs
        if diff.kind in (SAME, TIMING, TEXT) and diff.delta_start is not None
    ]
    if not points:
        return None, {}

    deltas = [delta for _, delta in points]
    median_delta = _median(deltas)
    slope, intercept = _least_squares(points)

    first_t = min(t for t, _ in points)
    last_t = max(t for t, _ in points)
    span = last_t - first_t
    drift_total = slope * span

    residuals = [abs(delta - (intercept + slope * t)) for t, delta in points]
    spread = _median(residuals) or 0.0

    stats = {
        "matched_cues": len(points),
        "median_offset": median_delta,
        "min_offset": min(deltas),
        "max_offset": max(deltas),
        "slope": slope,
        "drift_across_span": drift_total,
        "span": span,
        "residual_spread": spread,
    }

    drifting = abs(drift_total) > max(2 * tolerance_s, 0.5)
    offset = abs(median_delta) > tolerance_s

    # A straight line can be fitted through anything. It only *means* drift when
    # the cues actually sit on it - otherwise the slope is an artefact of cues
    # retimed individually, and calling that a rate error sends someone hunting
    # for a conform problem that does not exist.
    explained = drifting and spread <= abs(drift_total) / 4.0

    if drifting and not explained:
        return Check(
            "Timing relationship",
            WARN,
            f"Matched cues have been retimed by varying amounts, up to "
            f"{format_offset_ms(max(abs(min(deltas)), abs(max(deltas))))}.",
            [
                f"Differences run from {format_offset_ms(min(deltas))} to "
                f"{format_offset_ms(max(deltas))}, and no single offset or frame-rate ratio "
                "accounts for them.",
                f"Half the cues are more than {format_offset_ms(spread)} off the best-fit line, "
                "so this is per-cue retiming rather than drift.",
            ],
            stats,
        ), stats

    if drifting:
        ratio = 1.0 + slope
        explanations = rate_ratio_explanations(ratio)
        detail = [
            f"At the head the two files are {format_offset_ms(intercept + slope * first_t)} apart; "
            f"by the tail {format_offset_ms(intercept + slope * last_t)}.",
            f"That is {format_offset_ms(slope * 3600)} per hour, a timing ratio of {ratio:.6f}.",
        ]
        if explanations:
            detail.append(
                "Consistent with the second file being " + explanations[0]["text"] + "."
            )
        else:
            detail.append("No standard frame-rate pair produces this ratio, so it is not a rate error.")
        stats["ratio"] = ratio
        stats["rate_explanations"] = [item["text"] for item in explanations[:3]]
        return Check(
            "Timing relationship",
            FAIL,
            f"The two files drift apart by {format_offset_ms(drift_total)} across the programme.",
            detail,
            stats,
        ), stats

    if offset:
        timebase = _timebase_note(file_a, file_b, median_delta)
        detail = [
            f"The offset is steady - between {format_offset_ms(min(deltas))} and "
            f"{format_offset_ms(max(deltas))} - so this is a shift, not drift.",
        ]
        if timebase:
            detail.append(timebase)
            detail.append(
                "Every cue below is measured against this offset, so only cues that depart "
                "from it are listed as retimed."
            )
            return Check(
                "Timing relationship",
                INFO,
                f"The two files are stamped against different origins, {format_offset_ms(median_delta)} apart.",
                detail,
                stats,
            ), stats

        detail.append(
            "A constant shift is usually a different start timecode or a missing 10-second pre-roll."
        )
        return Check(
            "Timing relationship",
            WARN,
            f"The second file is offset from the first by {format_offset_ms(median_delta)}.",
            detail,
            stats,
        ), stats

    return Check(
        "Timing relationship",
        PASS,
        f"Matched cues sit within {format_offset_ms(tolerance_s)} of each other.",
        [f"Median difference {format_offset_ms(median_delta)} across {len(points)} matched cues."],
        stats,
    ), stats


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _read_side(path, result, label, rate_code=None):
    """Read one side, whatever format it is.

    SCC, MCC and media containers go through the C decoder; the text formats
    parse. Both arrive as the same document, so nothing downstream has to know
    which it got. `rate_code` is only used for SCC input, and only when given -
    otherwise the rate is read off the file, same as everywhere else.
    """
    try:
        document = read_comparable_document(path, rate_code=rate_code)
    except SubtitleParseError as error:
        result.errors.append(f"{label} ({Path(path).name}): {error}")
        return None
    except (OSError, RuntimeError, ValueError) as error:
        result.errors.append(f"{label} ({Path(path).name}): {error}")
        return None
    return FileSummary(path, document, detect_frame_rate(path))


def compare_subtitles(
    path_a,
    path_b,
    tolerance_ms=DEFAULT_TOLERANCE_MS,
    cancel=None,
    rate_code_a=None,
    rate_code_b=None,
):
    """Compare two subtitle files and report every way they differ.

    `rate_code_a`/`rate_code_b` are an explicit fallback for an SCC whose own
    timecodes are too few to infer a rate from and that declares none - the
    same edge case the Inspect tab's frame rate dropdown exists for. Leave
    them as None to read the rate off the file, which is enough for anything
    else.
    """
    result = ComparisonResult(path_a, path_b)
    tolerance_s = float(tolerance_ms) / 1000.0

    raise_if_cancelled(cancel)
    result.file_a = _read_side(path_a, result, "File A", rate_code=rate_code_a)
    raise_if_cancelled(cancel)
    result.file_b = _read_side(path_b, result, "File B", rate_code=rate_code_b)
    if result.errors:
        return result

    file_a, file_b = result.file_a, result.file_b

    for check in validate_document(file_a.document, "A"):
        result.add(check)
    raise_if_cancelled(cancel)
    for check in validate_document(file_b.document, "B"):
        result.add(check)

    result.add(_check_frame_rates(file_a, file_b))

    raise_if_cancelled(cancel)
    result.diffs = align_cues(file_a.cues, file_b.cues, tolerance_s, cancel=cancel)

    # Two passes: the first pairs cues by text, the second judges their timing
    # against how far apart the files turned out to be. An SCC stamped from
    # 01:00:00:00 against an SRT starting at zero is 3600 seconds apart on
    # every cue, and calling all of them retimed would say nothing.
    baseline = baseline_offset(result.diffs)
    apply_baseline(result.diffs, baseline, tolerance_s)
    result.stats["baseline_offset"] = baseline

    same = len(result.of_kind(SAME))
    retimed = len(result.of_kind(TIMING))
    edited = result.of_kind(TEXT)
    only_a = len(result.of_kind(ONLY_A))
    only_b = len(result.of_kind(ONLY_B))
    wording = len([diff for diff in edited if diff.change == WORDING])
    formatting = len(edited) - wording

    matched = same + retimed + len(edited)
    larger = max(len(file_a.cues), len(file_b.cues)) or 1

    result.stats.update(
        {
            "cues_a": len(file_a.cues),
            "cues_b": len(file_b.cues),
            "identical": same,
            "retimed": retimed,
            "text_changed": len(edited),
            "wording_changed": wording,
            "formatting_changed": formatting,
            "only_in_a": only_a,
            "only_in_b": only_b,
            "matched": matched,
            "match_rate": matched / larger,
            "tolerance_ms": tolerance_ms,
        }
    )

    result.add(_check_same_programme(result.stats))
    result.add(_check_cue_inventory(result.stats))
    result.add(_check_text_changes(result.stats))

    timing_check, timing_stats = analyse_timing(
        result.diffs, tolerance_s, file_a=file_a, file_b=file_b
    )
    if timing_check and result.stats["match_rate"] >= SAME_PROGRAMME_THRESHOLD:
        result.add(timing_check)
        result.stats.update(timing_stats)

    return result


def _check_frame_rates(file_a, file_b):
    rate_a, rate_b = file_a.detection, file_b.detection
    code_a, code_b = rate_a.rate_code, rate_b.rate_code

    conflicts = [side for side in (rate_a, rate_b) if side.conflict]
    if conflicts:
        return Check(
            "Frame rate",
            FAIL,
            "A file states a frame rate its own stamps contradict.",
            [f"{Path(side.path).name}: {note}" for side in conflicts for note in side.notes[-1:]],
            {"a": rate_a.as_dict(), "b": rate_b.as_dict()},
        )

    if code_a is None or code_b is None:
        unknown = [
            Path(side.path).name for side, code in ((rate_a, code_a), (rate_b, code_b)) if code is None
        ]
        return Check(
            "Frame rate",
            INFO,
            f"No frame rate could be read from {' and '.join(unknown)}.",
            [rate_a.headline(), rate_b.headline()],
            {"a": rate_a.as_dict(), "b": rate_b.as_dict()},
        )

    if code_a == code_b:
        return Check(
            "Frame rate",
            PASS,
            f"Both files are {rate_label(code_a)} fps.",
            [rate_a.headline(), rate_b.headline()],
            {"a": rate_a.as_dict(), "b": rate_b.as_dict()},
        )

    return Check(
        "Frame rate",
        WARN,
        f"The files are at different frame rates: {rate_label(code_a)} and {rate_label(code_b)}.",
        [
            rate_a.headline(),
            rate_b.headline(),
            "Different rates are legitimate between a PAL and an NTSC version, but the timings "
            "below will not line up unless one of them was properly conformed.",
        ],
        {"a": rate_a.as_dict(), "b": rate_b.as_dict()},
    )


def _check_same_programme(stats):
    rate = stats["match_rate"]
    if rate >= SAME_PROGRAMME_THRESHOLD:
        return None
    return Check(
        "Same programme",
        FAIL,
        f"Only {rate * 100:.0f}% of cues match between the two files.",
        [
            f"{stats['matched']} of {max(stats['cues_a'], stats['cues_b'])} cues could be paired by text.",
            "These do not look like two versions of the same programme. Check the files before "
            "reading anything else in this report.",
        ],
        dict(stats),
    )


def _check_cue_inventory(stats):
    only_a, only_b = stats["only_in_a"], stats["only_in_b"]
    if not only_a and not only_b:
        return Check(
            "Cue inventory",
            PASS,
            f"Both files carry the same {stats['cues_a']} cues.",
        )
    return Check(
        "Cue inventory",
        WARN,
        f"{only_a} cues only in A, {only_b} only in B.",
        [
            f"A has {stats['cues_a']} cues, B has {stats['cues_b']}.",
            "Listed individually under Differences.",
        ],
        {"only_in_a": only_a, "only_in_b": only_b},
    )


def _check_text_changes(stats):
    wording, formatting = stats["wording_changed"], stats["formatting_changed"]
    if not wording and not formatting:
        return Check("Text", PASS, "Every matched cue reads identically.")

    detail = []
    if wording:
        detail.append(f"{wording} cues have different words - these are script changes.")
    if formatting:
        detail.append(
            f"{formatting} cues differ only in punctuation, capitalisation or line breaks."
        )
    return Check(
        "Text",
        WARN if wording else INFO,
        f"{wording + formatting} matched cues read differently.",
        detail,
        {"wording": wording, "formatting": formatting},
    )


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


_STATUS_WORDS = {PASS: "PASS", WARN: "REVIEW", FAIL: "FAIL", INFO: "NOTE"}

_KIND_WORDS = {
    TIMING: "RETIMED",
    TEXT: "CHANGED",
    ONLY_A: "ONLY IN A",
    ONLY_B: "ONLY IN B",
}

# How an inline word diff reads in a plain-text report.
_MARKERS = {"equal": "{}", "delete": "[-{}-]", "insert": "{{+{}+}}"}


def format_baseline(seconds):
    """The file-wide offset, in whichever unit reads as a quantity.

    Milliseconds are the unit vendors argue about, but "-3599996 ms" is not a
    number anyone recognises as an hour.
    """
    if abs(seconds) >= 10.0:
        return f"{'-' if seconds < 0 else '+'}{format_seconds(abs(seconds))}"
    return format_offset_ms(seconds)


def render_word_diff(words):
    return " ".join(_MARKERS[op].format(text) for op, text in words if text)


def _summary_rows(stats):
    return [
        ("Identical", stats.get("identical", 0)),
        ("Retimed only", stats.get("retimed", 0)),
        ("Wording changed", stats.get("wording_changed", 0)),
        ("Formatting only", stats.get("formatting_changed", 0)),
        ("Only in A", stats.get("only_in_a", 0)),
        ("Only in B", stats.get("only_in_b", 0)),
    ]


def _file_line(summary):
    if summary is None:
        return "unreadable"
    start, end = summary.span()
    span = f"{format_seconds(start)} to {format_seconds(end)}" if start is not None else "empty"
    rate = summary.detection.label() if summary.detection else "unknown"
    lines = [
        f"{summary.name}",
        f"    Format      {format_label(summary.kind)}",
        f"    Cues        {len(summary.cues)}",
        f"    Span        {span}",
        f"    Frame rate  {rate} ({summary.detection.confidence})",
    ]
    track = summary.document.header.get("track")
    if track:
        lines.append(f"    Track       {track}")
    return "\n".join(lines)


def render_text_report(result, max_differences=None):
    """The comparison as plain text."""
    lines = [
        "SUBTITLE COMPARISON",
        "=" * 72,
        "",
        "File A: " + _file_line(result.file_a),
        "",
        "File B: " + _file_line(result.file_b),
        "",
        f"Verdict: {_STATUS_WORDS[result.verdict]}",
        "",
    ]

    if result.errors:
        lines.append("Errors")
        lines.append("-" * 72)
        lines.extend(f"  {error}" for error in result.errors)
        return "\n".join(lines)

    lines.append("Summary")
    lines.append("-" * 72)
    for label, value in _summary_rows(result.stats):
        lines.append(f"  {label:<20s} {value}")
    lines.append("")

    lines.append("Checks")
    lines.append("-" * 72)
    for check in result.checks:
        lines.append(f"  [{_STATUS_WORDS[check.status]}] {check.name}: {check.headline}")
        for detail in check.detail:
            lines.append(f"      {detail}")
    lines.append("")

    differences = result.differences()
    lines.append(f"Differences ({len(differences)})")
    lines.append("-" * 72)
    if not differences:
        lines.append("  None. The two files are identical.")
        return "\n".join(lines)

    shown = differences if max_differences is None else differences[:max_differences]
    for diff in shown:
        lines.extend(f"  {line}" for line in _difference_lines(diff))
        lines.append("")

    if len(shown) < len(differences):
        lines.append(f"  ... and {len(differences) - len(shown)} more.")

    return "\n".join(lines)


def _difference_lines(diff):
    """One difference, as the lines a person reads in a report."""
    word = _KIND_WORDS.get(diff.kind, diff.kind.upper())

    if diff.kind == ONLY_A:
        return [
            f"{word}  A#{diff.index_a} at {format_seconds(diff.cue_a.start)}",
            f"  - {diff.cue_a.text.replace(chr(10), ' / ')}",
        ]

    if diff.kind == ONLY_B:
        return [
            f"{word}  B#{diff.index_b} at {format_seconds(diff.cue_b.start)}",
            f"  + {diff.cue_b.text.replace(chr(10), ' / ')}",
        ]

    header = (
        f"{word}  A#{diff.index_a} / B#{diff.index_b} at {format_seconds(diff.cue_a.start)}"
    )
    if diff.kind == TIMING:
        shift = f"  in  {format_offset_ms(diff.relative_start)}"
        if diff.relative_end is not None:
            shift += f", out {format_offset_ms(diff.relative_end)}"
        if abs(diff.baseline) > 0.001:
            shift += f" (relative to the file-wide {format_baseline(diff.baseline)})"
        return [header, shift, f"  = {diff.cue_a.text.replace(chr(10), ' / ')}"]

    label = "wording" if diff.change == WORDING else "formatting only"
    lines = [f"{header}  ({label})"]
    if diff.relative_start is not None and abs(diff.relative_start) > 0.001:
        lines.append(f"  in  {format_offset_ms(diff.relative_start)}")
    lines.append(f"  - {diff.cue_a.text.replace(chr(10), ' / ')}")
    lines.append(f"  + {diff.cue_b.text.replace(chr(10), ' / ')}")
    lines.append(f"  ~ {render_word_diff(diff.words)}")
    return lines


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Subtitle comparison - {title}</title>
<style>
  :root {{
    --bg: #f6f4f1; --surface: #ffffff; --ink: #17222c; --muted: #5c6873;
    --rule: #dfd9d2; --accent: #c25a1e;
    --pass: #1f7a4d; --warn: #9a6212; --fail: #b3261e; --info: #4a5560;
    --del-bg: #fbe4e2; --del-ink: #8a2019; --ins-bg: #dff2e6; --ins-ink: #12603a;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #14181c; --surface: #1c2228; --ink: #e8eaec; --muted: #9aa5b0;
      --rule: #2e3740; --accent: #e8853f;
      --pass: #5fd39a; --warn: #e0b062; --fail: #f2887f; --info: #9aa5b0;
      --del-bg: #3a1f1d; --del-ink: #f2887f; --ins-bg: #17352a; --ins-ink: #5fd39a;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 32px 20px 64px; background: var(--bg); color: var(--ink);
         font-family: ui-sans-serif, -apple-system, "Segoe UI", sans-serif; line-height: 1.5; }}
  .wrap {{ max-width: 960px; margin: 0 auto; }}
  header {{ border-bottom: 2px solid var(--ink); padding-bottom: 20px; margin-bottom: 28px; }}
  .kicker {{ text-transform: uppercase; letter-spacing: 0.14em; font-size: 0.72rem;
             color: var(--muted); font-weight: 700; }}
  h1 {{ font-size: 1.9rem; margin: 8px 0 12px; line-height: 1.2; }}
  .verdict {{ display: inline-block; padding: 6px 14px; border-radius: 999px; font-weight: 700;
              font-size: 0.85rem; letter-spacing: 0.06em; color: #fff;
              background: var(--{verdict_class}); }}
  h2 {{ font-size: 1.15rem; margin: 36px 0 12px; }}
  .files {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px;
            margin: 20px 0; }}
  .card {{ background: var(--surface); border: 1px solid var(--rule); border-radius: 12px; padding: 14px; }}
  .card-label {{ font-size: 0.74rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); }}
  .card-value {{ font-size: 1.5rem; font-weight: 700; margin: 6px 0 2px; font-variant-numeric: tabular-nums; }}
  dl {{ margin: 0; display: grid; grid-template-columns: auto 1fr; gap: 4px 14px; font-size: 0.9rem; }}
  dt {{ color: var(--muted); }}
  dd {{ margin: 0; font-variant-numeric: tabular-nums; }}
  table {{ width: 100%; border-collapse: collapse; background: var(--surface);
           border: 1px solid var(--rule); border-radius: 12px; overflow: hidden; }}
  th, td {{ text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--rule);
            vertical-align: top; font-size: 0.9rem; }}
  tr:last-child td {{ border-bottom: none; }}
  .pill {{ display: inline-block; padding: 2px 10px; border-radius: 999px; color: #fff;
           font-size: 0.72rem; font-weight: 700; letter-spacing: 0.05em; white-space: nowrap; }}
  .status-pass .pill {{ background: var(--pass); }}
  .status-warn .pill {{ background: var(--warn); }}
  .status-fail .pill {{ background: var(--fail); }}
  .status-info .pill {{ background: var(--info); }}
  ul.detail {{ margin: 6px 0 0; padding-left: 18px; color: var(--muted); font-size: 0.85rem; }}
  .diff {{ font-family: ui-monospace, Menlo, monospace; font-size: 0.85rem; white-space: pre-wrap; }}
  del {{ background: var(--del-bg); color: var(--del-ink); text-decoration: none;
         padding: 0 3px; border-radius: 3px; }}
  ins {{ background: var(--ins-bg); color: var(--ins-ink); text-decoration: none;
         padding: 0 3px; border-radius: 3px; }}
  .when {{ color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap; }}
  footer {{ margin-top: 40px; color: var(--muted); font-size: 0.8rem; }}
</style>
</head>
<body>
<div class="wrap">
<header>
  <div class="kicker">Caption Inspector</div>
  <h1>Subtitle comparison</h1>
  <span class="verdict">{verdict_word}</span>
</header>

<div class="files">{file_cards}</div>

<h2>Summary</h2>
<div class="cards">{summary_cards}</div>

<h2>Checks</h2>
<table>{check_rows}</table>

<h2>Differences ({difference_count})</h2>
<table>{difference_rows}</table>

<footer>Generated by Caption Inspector v{app_version}.</footer>
</div>
</body>
</html>
"""


def _html_file_card(summary, side):
    if summary is None:
        return f'<div class="card"><div class="card-label">File {side}</div><div>unreadable</div></div>'
    start, end = summary.span()
    span = f"{format_seconds(start)} to {format_seconds(end)}" if start is not None else "empty"
    detection = summary.detection
    return (
        f'<div class="card"><div class="card-label">File {side}</div>'
        f"<div class=\"card-value\" style=\"font-size:1rem\">{escape(summary.name)}</div>"
        "<dl>"
        f"<dt>Format</dt><dd>{escape(format_label(summary.kind))}</dd>"
        f"<dt>Cues</dt><dd>{len(summary.cues)}</dd>"
        f"<dt>Span</dt><dd>{escape(span)}</dd>"
        f"<dt>Frame rate</dt><dd>{escape(detection.label())} "
        f"<span class=\"when\">({escape(detection.confidence)})</span></dd>"
        + (
            f"<dt>Track</dt><dd>{escape(str(summary.document.header['track']))}</dd>"
            if summary.document.header.get("track")
            else ""
        )
        + "</dl></div>"
    )


def _html_word_diff(words):
    parts = []
    for op, text in words:
        if not text:
            continue
        if op == "equal":
            parts.append(escape(text))
        elif op == "delete":
            parts.append(f"<del>{escape(text)}</del>")
        else:
            parts.append(f"<ins>{escape(text)}</ins>")
    return " ".join(parts)


def _html_difference_row(diff):
    word = _KIND_WORDS.get(diff.kind, diff.kind.upper())
    status = {
        TIMING: "status-info",
        TEXT: "status-warn",
        ONLY_A: "status-fail",
        ONLY_B: "status-pass",
    }.get(diff.kind, "status-info")

    cue = diff.cue_a or diff.cue_b
    when = format_seconds(cue.start)
    reference = " / ".join(
        part
        for part in (
            f"A#{diff.index_a}" if diff.index_a else None,
            f"B#{diff.index_b}" if diff.index_b else None,
        )
        if part
    )

    if diff.kind == ONLY_A:
        body = f"<del>{escape(diff.cue_a.text)}</del>"
    elif diff.kind == ONLY_B:
        body = f"<ins>{escape(diff.cue_b.text)}</ins>"
    elif diff.kind == TIMING:
        shift = format_offset_ms(diff.relative_start)
        if diff.relative_end is not None:
            shift += f", out {format_offset_ms(diff.relative_end)}"
        body = f"<div>{escape(diff.cue_a.text)}</div><ul class=\"detail\"><li>in {escape(shift)}</li></ul>"
    else:
        label = "wording" if diff.change == WORDING else "formatting only"
        body = f'<div class="diff">{_html_word_diff(diff.words)}</div>'
        body += f'<ul class="detail"><li>{escape(label)}</li></ul>'

    return (
        f'<tr class="{status}"><td><span class="pill">{escape(word)}</span></td>'
        f'<td class="when">{escape(when)}<br><span>{escape(reference)}</span></td>'
        f"<td>{body}</td></tr>"
    )


def render_html_report(result, app_version="7.5", max_differences=2000):
    if result.errors:
        rows = "".join(
            f'<tr class="status-fail"><td><span class="pill">FAIL</span></td>'
            f"<td colspan=\"2\">{escape(error)}</td></tr>"
            for error in result.errors
        )
        check_rows = rows
        difference_rows = ""
        summary_cards = ""
    else:
        check_rows = "".join(
            f'<tr class="status-{check.status.lower()}">'
            f'<td><span class="pill">{_STATUS_WORDS[check.status]}</span></td>'
            f"<td>{escape(check.name)}</td>"
            f"<td>{escape(check.headline)}"
            + (
                "<ul class=\"detail\">"
                + "".join(f"<li>{escape(detail)}</li>" for detail in check.detail)
                + "</ul>"
                if check.detail
                else ""
            )
            + "</td></tr>"
            for check in result.checks
        )
        summary_cards = "".join(
            f'<div class="card"><div class="card-label">{escape(label)}</div>'
            f'<div class="card-value">{value}</div></div>'
            for label, value in _summary_rows(result.stats)
        )
        difference_rows = "".join(
            _html_difference_row(diff) for diff in result.differences()[:max_differences]
        )

    title = f"{Path(result.path_a).name} vs {Path(result.path_b).name}"
    return _HTML_TEMPLATE.format(
        title=escape(title),
        verdict_class=result.verdict.lower(),
        verdict_word=_STATUS_WORDS[result.verdict],
        file_cards=_html_file_card(result.file_a, "A") + _html_file_card(result.file_b, "B"),
        summary_cards=summary_cards,
        check_rows=check_rows,
        difference_rows=difference_rows or '<tr><td colspan="3">No differences.</td></tr>',
        difference_count=len(result.differences()),
        app_version=app_version,
    )


def write_comparison_report(path, result, app_version="7.5"):
    """Write a report, picking the format from the file extension."""
    output_path = Path(path)
    suffix = output_path.suffix.lower()

    if suffix in (".html", ".htm"):
        content = render_html_report(result, app_version=app_version)
    elif suffix == ".json":
        content = json.dumps(result.as_dict(), indent=2)
    else:
        content = render_text_report(result)

    output_path.write_text(content, encoding="utf-8")
    return output_path
