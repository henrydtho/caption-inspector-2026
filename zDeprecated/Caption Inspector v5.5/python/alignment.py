"""Match caption cues to a transcript and measure drift.

The measurement is the point of the whole tier: for every cue we can find in the
dialogue, record (position on the timeline, caption-to-audio offset), then fit a
line through those points.

    flat line near zero  -> in sync
    flat line off zero   -> constant offset, fixable by sliding the file
    sloped line          -> drift, and the slope is the frame-rate error

Matching is deliberately coarse - captions are not verbatim transcripts, and
they do not need to be. A rare-token vote plus a similarity check finds enough
anchors across a program to fit a line through.

Both timelines have to be measured from the same zero before any of that means
anything. Caption timecodes are absolute program timecode; the transcriber's
timestamps are relative to the start of the media file. On a file whose start
timecode is 00:58:30:00 those two zeros are 3510 seconds apart, and diffing
them raw reports that gap as the sync error. Everything here works in
media-relative seconds - caption timecode minus the video's start timecode,
which is the same value Tier 1 already parsed off the container.
"""

import re
from collections import defaultdict
from difflib import SequenceMatcher

from cancellation import raise_if_cancelled
from timecode import format_offset_ms, format_seconds, rate_ratio_explanations


# Words too common to anchor on - they appear everywhere in the transcript and
# their positions carry no information.
STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "do", "for", "from", "had", "has",
    "have", "he", "her", "his", "i", "if", "in", "is", "it", "its", "me", "my", "no", "not", "of",
    "on", "or", "our", "she", "so", "that", "the", "their", "them", "then", "there", "they",
    "this", "to", "up", "us", "was", "we", "were", "what", "when", "will", "with", "you", "your",
}

MIN_CUE_TOKENS = 3
MAX_ANCHOR_TOKENS = 10
MIN_CONFIDENCE = 0.62

# Cues sampled for the coarse pass, which searches the whole transcript to
# survive a grossly offset file (a one-hour rebase, or 20 minutes of drift).
COARSE_SAMPLE = 40
# Once the coarse line exists, every cue is searched inside this window around
# its predicted position.
FINE_RADIUS_SECONDS = 12.0

_WORD_CLEAN = re.compile(r"[^a-z0-9']+")


class Match:
    """One cue anchored to a point in the transcript."""

    __slots__ = (
        "cue", "audio_seconds", "media_seconds", "offset", "confidence", "matched_text",
    )

    def __init__(self, cue, audio_seconds, confidence, matched_text, start_offset=0.0):
        self.cue = cue
        self.audio_seconds = audio_seconds
        # Caption timecode rebased onto the media file's timeline, which is the
        # timeline the transcriber's timestamps live on.
        self.media_seconds = cue.seconds - start_offset
        # Positive offset: the caption is on screen before the words are spoken.
        self.offset = audio_seconds - self.media_seconds
        self.confidence = confidence
        self.matched_text = matched_text

    def as_dict(self):
        return {
            "timecode": self.cue.timecode,
            "caption_seconds": self.cue.seconds,
            "media_seconds": self.media_seconds,
            "audio_seconds": self.audio_seconds,
            "offset": self.offset,
            "confidence": self.confidence,
            "caption_text": self.cue.text,
            "matched_text": self.matched_text,
        }


class TranscriptIndex:
    """Token positions in the transcript, for rare-token candidate lookup."""

    def __init__(self, transcript):
        self.words = transcript.words
        self.tokens = []
        self.starts = []
        self.positions = defaultdict(list)

        for word in transcript.words:
            token = _WORD_CLEAN.sub("", word.text.lower()).strip("'")
            if not token:
                continue
            index = len(self.tokens)
            self.tokens.append(token)
            self.starts.append(word.start)
            self.positions[token].append(index)

        # Index -> time is monotonic, so a time window maps to an index range by
        # binary search rather than a scan.
        self._count = len(self.tokens)

    def __len__(self):
        return self._count

    def index_range_for_time(self, low_seconds, high_seconds):
        import bisect

        low = bisect.bisect_left(self.starts, low_seconds)
        high = bisect.bisect_right(self.starts, high_seconds)
        return low, high

    def text_at(self, start, length):
        return " ".join(self.tokens[start:start + length])


def _anchor_tokens(cue):
    """Pick the tokens worth searching on, rarest-first bias."""
    tokens = [token for token in cue.words if token]
    if len(tokens) < MIN_CUE_TOKENS:
        return []
    return tokens[:MAX_ANCHOR_TOKENS]


def _find_best_window(index, tokens, low=None, high=None):
    """Vote for the transcript position where `tokens` start.

    Every occurrence of cue token i at transcript position p votes for a window
    starting at p - i. The winning start is scored with a similarity ratio so a
    lucky single-token vote cannot pass as a match.
    """
    if not tokens:
        return None

    low = 0 if low is None else max(0, low)
    high = len(index) if high is None else min(len(index), high)
    if high - low < len(tokens):
        return None

    votes = defaultdict(float)
    for token_position, token in enumerate(tokens):
        occurrences = index.positions.get(token)
        if not occurrences:
            continue

        # A token that appears constantly is noise; weight votes by rarity.
        weight = 1.0 if token not in STOP_WORDS else 0.25
        if len(occurrences) > 200:
            weight *= 0.25

        for occurrence in occurrences:
            if occurrence < low or occurrence >= high:
                continue
            start = occurrence - token_position
            if start < 0:
                continue
            votes[start] += weight
            # Neighbouring starts absorb a dropped or inserted word.
            votes[start - 1] += weight * 0.5
            votes[start + 1] += weight * 0.5

    if not votes:
        return None

    cue_text = " ".join(tokens)
    window_length = len(tokens)
    best = None

    for start, _score in sorted(votes.items(), key=lambda item: -item[1])[:12]:
        if start < 0 or start >= len(index):
            continue
        # Score against a window the same length as the cue. Scoring against a
        # longer window lets a candidate that starts a word early beat the
        # correct one, because the extra trailing text drags the ratio down on
        # whichever candidate is actually right.
        candidate_text = index.text_at(start, window_length)
        confidence = SequenceMatcher(None, cue_text, candidate_text).ratio()
        if best is None or confidence > best[1]:
            best = (start, confidence, candidate_text)

    if not best or best[1] < MIN_CONFIDENCE:
        return None

    start, confidence, candidate_text = best

    # Anchor on the first cue token that actually matches, not on the start of
    # the window. The two are usually the same. When they are not - a caption
    # that opens with something nobody says, or a word the transcriber dropped -
    # the window start is a transcript word that has nothing to do with this cue,
    # and timing the cue from it reports the gap before the line as sync error.
    # That is a silent failure: the window still scores well enough to pass, so
    # the cue is reported as matched, just at the wrong moment.
    anchor = start + _first_matching_position(tokens, index, start, len(tokens))

    return {
        "start": start,
        "anchor": anchor,
        "confidence": confidence,
        "audio_seconds": index.starts[anchor],
        "text": candidate_text,
    }


def _first_matching_position(tokens, index, start, length):
    """Offset into the window of the first token shared with the cue.

    Token-level rather than character-level: the question is which transcribed
    word the cue begins at, and that is a question about words.
    """
    window = index.tokens[start:start + length]
    if not window:
        return 0

    for tag, _cue_start, _cue_end, window_start, _window_end in SequenceMatcher(
        None, tokens, window
    ).get_opcodes():
        if tag == "equal":
            return window_start

    return 0


def _median(values):
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _median_absolute_deviation(values, center):
    if not values:
        return 0.0
    return _median([abs(value - center) for value in values])


def theil_sen(points):
    """Median-of-pairwise-slopes fit. Immune to the mismatches that will happen.

    Least squares would let a handful of bad anchors invent a slope; drift is
    the headline number here, so the robust estimator drives the first pass.
    """
    if len(points) < 2:
        return 0.0, (points[0][1] if points else 0.0)

    slopes = []
    for i in range(len(points)):
        x_i, y_i = points[i]
        for j in range(i + 1, len(points)):
            x_j, y_j = points[j]
            if x_j == x_i:
                continue
            slopes.append((y_j - y_i) / (x_j - x_i))

    if not slopes:
        return 0.0, _median([y for _, y in points])

    slope = _median(slopes)
    intercept = _median([y - slope * x for x, y in points])
    return slope, intercept


def least_squares(points):
    """Plain OLS fit plus r-squared, reported alongside the robust fit."""
    count = len(points)
    if count < 2:
        return 0.0, (points[0][1] if points else 0.0), 0.0

    sum_x = sum(x for x, _ in points)
    sum_y = sum(y for _, y in points)
    mean_x = sum_x / count
    mean_y = sum_y / count

    variance = sum((x - mean_x) ** 2 for x, _ in points)
    if variance == 0:
        return 0.0, mean_y, 0.0

    covariance = sum((x - mean_x) * (y - mean_y) for x, y in points)
    slope = covariance / variance
    intercept = mean_y - slope * mean_x

    total_sum_squares = sum((y - mean_y) ** 2 for _, y in points)
    residual_sum_squares = sum((y - (slope * x + intercept)) ** 2 for x, y in points)
    r_squared = 1.0 - (residual_sum_squares / total_sum_squares) if total_sum_squares else 0.0

    return slope, intercept, r_squared


def align_cues_to_transcript(cues, transcript, start_offset=0.0, progress=None, cancel=None):
    """Anchor cues to the transcript in two passes.

    `start_offset` is the video's start timecode in seconds, from Tier 1. Cue
    timecodes are absolute program timecode and the transcript is stamped from
    the head of the file, so the cue side is rebased by that amount before
    anything is compared. It is 0.0 for a zero-based file, which leaves the
    behaviour on those files exactly as it was.

    The coarse pass searches the entire transcript for a sample of cues, so a
    file that is an hour out or drifting by minutes still finds its footing. The
    fine pass then searches every cue in a narrow window around the position the
    coarse line predicts.
    """
    index = TranscriptIndex(transcript)
    if not len(index):
        return []

    usable = [(cue, _anchor_tokens(cue)) for cue in cues]
    usable = [(cue, tokens) for cue, tokens in usable if tokens]
    if not usable:
        return []

    # Coarse pass: sample evenly so the estimate spans the whole timeline.
    sample_step = max(1, len(usable) // COARSE_SAMPLE)
    coarse_points = []
    for cue, tokens in usable[::sample_step]:
        raise_if_cancelled(cancel)
        found = _find_best_window(index, tokens)
        if found:
            cue_seconds = cue.seconds - start_offset
            coarse_points.append((cue_seconds, found["audio_seconds"] - cue_seconds))

    if len(coarse_points) >= 4:
        # Throw out coarse anchors that disagree wildly before fitting.
        offsets = [offset for _, offset in coarse_points]
        center = _median(offsets)
        spread = _median_absolute_deviation(offsets, center) or 1.0
        coarse_points = [
            point for point in coarse_points if abs(point[1] - center) <= max(5.0, 6.0 * spread)
        ]

    slope, intercept = theil_sen(coarse_points) if len(coarse_points) >= 2 else (0.0, _median(
        [offset for _, offset in coarse_points]
    ) if coarse_points else 0.0)

    if progress:
        progress(f"Coarse pass anchored {len(coarse_points)} cues; refining...")

    matches = []
    for position, (cue, tokens) in enumerate(usable):
        # The fine pass is the long one on a feature - thousands of cues, each a
        # windowed search. Checking every 25 keeps the cost negligible.
        if position % 25 == 0:
            raise_if_cancelled(cancel)
        cue_seconds = cue.seconds - start_offset
        predicted = intercept + slope * cue_seconds
        centre = cue_seconds + predicted
        low, high = index.index_range_for_time(centre - FINE_RADIUS_SECONDS, centre + FINE_RADIUS_SECONDS)
        found = _find_best_window(index, tokens, low, high)
        if not found:
            continue
        matches.append(
            Match(cue, found["audio_seconds"], found["confidence"], found["text"], start_offset)
        )

    return matches


def _fit_drift(matches, duration=None):
    # x is media-relative seconds, so the fit's intercept is the offset at the
    # head of the file rather than at 00:00:00:00 of an unrelated timecode run.
    points = [(match.media_seconds, match.offset) for match in matches]
    if len(points) < 2:
        return {
            "slope": 0.0,
            "intercept": points[0][1] if points else 0.0,
            "slope_ms_per_minute": 0.0,
            "r_squared": 0.0,
            "points": len(points),
            "rejected": 0,
            "total_drift_seconds": 0.0,
            "residual_ms": 0.0,
            "implied_ratio": 1.0,
            "explanations": [],
            "first_third_offset": 0.0,
            "last_third_offset": 0.0,
        }

    # Robust fit first, then reject residual outliers, then refit by OLS so the
    # reported r-squared describes the surviving points.
    robust_slope, robust_intercept = theil_sen(points)
    residuals = [y - (robust_slope * x + robust_intercept) for x, y in points]
    spread = _median_absolute_deviation(residuals, _median(residuals)) or 0.0
    limit = max(0.75, 4.0 * spread)

    kept = [
        point
        for point, residual in zip(points, residuals)
        if abs(residual - _median(residuals)) <= limit
    ]
    rejected = len(points) - len(kept)
    if len(kept) < 4:
        kept = points
        rejected = 0

    slope, intercept, r_squared = least_squares(kept)
    fitted_residuals = [y - (slope * x + intercept) for x, y in kept]
    residual_ms = _median_absolute_deviation(fitted_residuals, 0.0) * 1000.0

    ordered = sorted(kept, key=lambda point: point[0])
    third = max(1, len(ordered) // 3)
    first_third_offset = _median([y for _, y in ordered[:third]])
    last_third_offset = _median([y for _, y in ordered[-third:]])

    span = duration or (ordered[-1][0] - ordered[0][0]) or 0.0
    total_drift = slope * span

    # Offsets are audio-minus-caption regressed against caption time, so
    # (1 + slope) is dialogue-over-caption. Tier 1 and rate_ratio_explanations
    # both speak in authored-over-played - caption over dialogue - so invert
    # here. Reporting the reciprocal names the frame-rate pair backwards and
    # contradicts Tier 1 on the same file.
    implied_ratio = 1.0 / (1.0 + slope) if (1.0 + slope) > 0.0 else 1.0

    return {
        "slope": slope,
        "intercept": intercept,
        "slope_ms_per_minute": slope * 60000.0,
        "r_squared": r_squared,
        "points": len(kept),
        "rejected": rejected,
        "span_seconds": span,
        "total_drift_seconds": total_drift,
        "residual_ms": residual_ms,
        "robust_slope_ms_per_minute": robust_slope * 60000.0,
        "implied_ratio": implied_ratio,
        "explanations": rate_ratio_explanations(implied_ratio, tolerance=0.0008),
        "first_third_offset": first_third_offset,
        "last_third_offset": last_third_offset,
    }


def summarize_alignment(cues, matches, media=None, start_offset=0.0):
    """Roll matches up into the numbers the report and the UI both use."""
    duration = media.duration if media else None
    offsets = [match.offset for match in matches]
    confidences = [match.confidence for match in matches]

    drift = _fit_drift(matches, duration)

    return {
        "cues": len(cues),
        "matched": len(matches),
        "match_rate": (len(matches) / len(cues)) if cues else 0.0,
        "median_offset": _median(offsets),
        "median_confidence": _median(confidences),
        "min_offset": min(offsets) if offsets else 0.0,
        "max_offset": max(offsets) if offsets else 0.0,
        "drift": drift,
        "samples": [match.as_dict() for match in matches],
        "duration": duration,
        "start_offset": start_offset,
        "start_timecode": media.start_timecode if media else None,
    }


def describe_drift(drift, media=None):
    """Turn the fit into a status and vendor-readable sentences."""
    from sync_check import FAIL, PASS, WARN

    ms_per_minute = drift["slope_ms_per_minute"]
    total_drift = drift["total_drift_seconds"]
    per_ten_minutes = ms_per_minute * 10

    detail = [
        f"Measured across {drift['points']} matched cues"
        + (f" ({drift['rejected']} outliers rejected)." if drift["rejected"] else "."),
        f"Drift rate: {ms_per_minute:+.0f} ms per minute ({per_ten_minutes:+.0f} ms per 10 minutes).",
        f"Fit quality: r-squared {drift['r_squared']:.3f}, "
        f"typical scatter {drift['residual_ms']:.0f} ms.",
        f"Start of program offset {format_offset_ms(drift['first_third_offset'])}, "
        f"end of program offset {format_offset_ms(drift['last_third_offset'])}.",
    ]

    if abs(ms_per_minute) < 10.0:
        return (
            PASS,
            f"No meaningful drift: {ms_per_minute:+.0f} ms per minute.",
            detail
            + ["Caption timing holds steady across the program, which rules out a frame-rate mismatch."],
        )

    # A slope is only worth reporting if the points actually sit on a line.
    # Transcribed word onsets scatter by around 100 ms, so a handful of cues
    # will always fit some nonzero slope out of pure noise. Naming a frame-rate
    # pair off that noise puts a wrong diagnosis in a vendor's inbox, which is
    # worse than reporting nothing.
    scatter_seconds = drift["residual_ms"] / 1000.0
    if abs(total_drift) <= scatter_seconds:
        return (
            PASS,
            f"No meaningful drift: {ms_per_minute:+.0f} ms per minute is within measurement noise.",
            detail
            + [
                f"Across the whole program the slope accumulates to "
                f"{format_offset_ms(total_drift)}, smaller than the "
                f"{drift['residual_ms']:.0f} ms scatter of the individual matches.",
                "That is a flat line inside the noise floor, not a frame-rate mismatch.",
            ],
        )

    if drift["r_squared"] < 0.5:
        return (
            WARN,
            f"Drift cannot be measured reliably: {ms_per_minute:+.0f} ms per minute on a poor fit.",
            detail
            + [
                f"The points do not sit on a line (r-squared {drift['r_squared']:.3f}), so the "
                "slope is not trustworthy and no frame-rate pair can be named from it.",
                "Re-run with a larger model, or check for music and effects under the dialogue.",
            ],
        )

    if media and media.duration:
        detail.append(
            f"Over this {format_seconds(media.duration)} program that accumulates to "
            f"{format_offset_ms(total_drift)} between the head and the tail."
        )

    explanations = drift.get("explanations") or []
    if explanations:
        best = explanations[0]
        detail.append(
            f"The drift rate corresponds to a file {best['text']} "
            f"(ratio {best['ratio']:.4f})."
        )
        detail.append(
            "The fix is a rate conversion of the caption timings, not a relabel and not a "
            "constant nudge - a nudge leaves the head or the tail wrong."
        )
    else:
        detail.append(
            "The slope does not match a standard frame-rate pair, so this looks like an edit "
            "or conform difference rather than a clean rate mislabel."
        )

    status = FAIL if abs(ms_per_minute) >= 50.0 or abs(total_drift) >= 1.0 else WARN
    headline = (
        f"Captions drift {abs(ms_per_minute):.0f} ms per minute "
        f"({'later' if ms_per_minute < 0 else 'earlier'} than the dialogue as the program runs)."
    )
    return status, headline, detail
