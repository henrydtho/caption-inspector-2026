"""Two-tier caption sync QC.

Tier 1 is pure math: ffprobe the video, read the caption file's timecodes, and
decide whether the two can possibly describe the same program. It runs in
seconds and catches the common vendor failure - authoring against one frame rate
and delivering the file labelled as another - before anyone watches anything.

Tier 2 is audio-verified: transcribe the dialogue, match caption text to the
transcript, and regress caption-to-audio offset against position on the
timeline. A flat line near zero is in sync. A sloped line is drift, and the
slope names the rate error.
"""

from pathlib import Path

from caption_timing import CaptionTimingError, read_caption_timings
from media_probe import ProbeError, probe_media
from timecode import (
    DROP_FRAME_CODES,
    RATE_BY_CODE,
    TimecodeError,
    format_offset_ms,
    format_seconds,
    frames_to_seconds,
    nominal_rate,
    parse_timecode,
    rate_label,
    explain_timing_ratio,
    rate_ratio_explanations,
    timecode_to_frames,
    timecode_to_seconds,
)


PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
INFO = "INFO"

_SEVERITY = {INFO: 0, PASS: 1, WARN: 2, FAIL: 3}

# A caption file is treated as covering the whole program when its last event
# lands within this fraction of the program end. Below it, the ratio test is
# reported but not used to fail the delivery.
FULL_PROGRAM_COVERAGE = 0.90


class Check:
    """One named pass/fail observation, with the numbers behind it."""

    def __init__(self, name, status, headline, detail=None, data=None):
        self.name = name
        self.status = status
        self.headline = headline
        self.detail = detail or []
        self.data = data or {}

    def as_dict(self):
        return {
            "name": self.name,
            "status": self.status,
            "headline": self.headline,
            "detail": list(self.detail),
            "data": dict(self.data),
        }


class SyncResult:
    """Aggregate result of one or both tiers."""

    def __init__(self, video_path, caption_path):
        self.video_path = str(video_path) if video_path else None
        self.caption_path = str(caption_path)
        self.checks = []
        self.media = None
        self.timings = None
        self.rate_code = None
        self.tier2 = None
        self.errors = []

    def add(self, check):
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

    def failures(self):
        return [check for check in self.checks if check.status == FAIL]

    def warnings(self):
        return [check for check in self.checks if check.status == WARN]

    def as_dict(self):
        return {
            "video": self.video_path,
            "captions": self.caption_path,
            "verdict": self.verdict,
            "rate_code": self.rate_code,
            "checks": [check.as_dict() for check in self.checks],
            "tier2": self.tier2,
            "errors": list(self.errors),
        }


def _video_start_seconds(media, rate_code):
    """Wall-clock offset of the video's start timecode, 0 when it has none."""
    if not media or not media.start_timecode:
        return 0.0, None
    try:
        rate = RATE_BY_CODE[rate_code]
        seconds = timecode_to_seconds(media.start_timecode, rate)
        return seconds, media.start_timecode
    except (TimecodeError, KeyError):
        return 0.0, media.start_timecode


def _check_timecode_ordering(timings, rate_code):
    """Timecodes must climb. Out-of-order rows mean a broken export."""
    out_of_order = []
    previous_frames = None
    previous_timecode = None

    for entry in timings.entries:
        try:
            frames = timings.frames_at(entry, rate_code)
        except (TimecodeError, KeyError):
            continue
        if previous_frames is not None and frames < previous_frames:
            out_of_order.append((previous_timecode, entry.timecode))
        previous_frames = frames
        previous_timecode = entry.timecode

    if not out_of_order:
        return Check(
            "Timecode ordering",
            PASS,
            "Timecodes advance monotonically.",
            data={"out_of_order": 0},
        )

    detail = [f"{before} is followed by {after}" for before, after in out_of_order[:5]]
    if len(out_of_order) > 5:
        detail.append(f"...and {len(out_of_order) - 5} more.")

    return Check(
        "Timecode ordering",
        FAIL,
        f"{len(out_of_order)} caption rows go backwards in time.",
        detail,
        {"out_of_order": len(out_of_order)},
    )


def _check_drop_frame_legality(timings, rate_code):
    """Drop-frame skips frames 00/01 at the top of every non-tenth minute.

    A file that uses them is not drop-frame no matter what its separators say,
    and every downstream timecode conversion will be wrong.
    """
    if not timings.drop_frame or rate_code not in DROP_FRAME_CODES:
        return None

    rate = RATE_BY_CODE[rate_code]
    dropped_per_minute = 2 * (nominal_rate(rate) // 30)
    illegal = []

    for entry in timings.entries:
        try:
            _, minute, second, frame, _ = parse_timecode(entry.timecode)
        except TimecodeError:
            continue
        if second == 0 and minute % 10 != 0 and frame < dropped_per_minute:
            illegal.append(entry.timecode)

    if not illegal:
        return Check(
            "Drop-frame legality",
            PASS,
            "Drop-frame timecodes are legal.",
            data={"illegal": 0},
        )

    return Check(
        "Drop-frame legality",
        FAIL,
        f"{len(illegal)} timecodes use frame numbers that drop-frame skips.",
        [
            "Drop-frame counting never uses frames "
            f"00-{dropped_per_minute - 1:02d} at the top of a minute unless the minute is a multiple of 10.",
            "Examples: " + ", ".join(illegal[:5]),
            "The file is labelled drop-frame but was almost certainly counted as non-drop.",
        ],
        {"illegal": len(illegal), "examples": illegal[:5]},
    )


def _check_drop_frame_convention(timings, media, rate_code):
    """Drop-frame versus non-drop on an NTSC deliverable.

    At 29.97 a non-drop timecode gains on real time by 0.1%: 3.6 seconds an
    hour. It is invisible at the head, obvious at the tail, and it is the single
    most common reason a file "looks fine at the start and wrong by the end".
    """
    if rate_code not in DROP_FRAME_CODES or not timings.is_timecode_based:
        return None

    video_start_is_drop = None
    if media and media.start_timecode:
        try:
            _, _, _, _, video_start_is_drop = parse_timecode(media.start_timecode)
        except TimecodeError:
            video_start_is_drop = None

    data = {
        "caption_drop_frame": timings.drop_frame,
        "video_drop_frame": video_start_is_drop,
        "rate_code": rate_code,
    }

    duration = media.duration if media else None
    drift_per_hour = 3.6 if rate_code == 2997 else 3.6  # 0.1% either way

    if video_start_is_drop is not None and video_start_is_drop != timings.drop_frame:
        accumulated = (duration / 3600.0 * drift_per_hour) if duration else None
        detail = [
            f"Caption file: {'drop-frame' if timings.drop_frame else 'non-drop'}.",
            f"Video start timecode: {'drop-frame' if video_start_is_drop else 'non-drop'} "
            f"({media.start_timecode}).",
            f"The two conventions diverge by about {drift_per_hour:.1f} seconds per hour.",
        ]
        if accumulated:
            detail.append(f"Over this program that is roughly {accumulated:.1f} seconds by the end.")
        return Check(
            "Drop-frame convention",
            FAIL,
            "Caption file and video disagree on drop-frame.",
            detail,
            data,
        )

    if not timings.drop_frame and duration and duration > 600:
        return Check(
            "Drop-frame convention",
            WARN,
            f"Caption file is non-drop at {rate_label(rate_code)} fps.",
            [
                "Non-drop timecode runs about 3.6 seconds per hour ahead of real time at this rate.",
                "That is fine if the deliverable is also non-drop. If it is drop-frame, "
                "the captions will look correct at the head and late by the end.",
                "The video carries no start timecode to check against."
                if not (media and media.start_timecode)
                else f"Video start timecode: {media.start_timecode}.",
            ],
            data,
        )

    return Check(
        "Drop-frame convention",
        PASS,
        f"Caption file is {'drop-frame' if timings.drop_frame else 'non-drop'}, "
        f"consistent with the deliverable.",
        data=data,
    )


def _check_start_reference(timings, media, rate_code):
    """Catch a caption file authored against a 01:00:00:00 head.

    Timing against a head-based master and delivering against a zero-based file
    is the other classic vendor error, and it is unmistakable in the numbers.
    """
    first_entry = timings.first_text_entry() or timings.first_entry()
    if not first_entry:
        return None

    first_seconds = timings.seconds_at(first_entry, rate_code)
    video_start_seconds, video_start_tc = _video_start_seconds(media, rate_code)

    data = {
        "first_caption_timecode": first_entry.timecode,
        "first_caption_seconds": first_seconds,
        "video_start_timecode": video_start_tc,
        "video_start_seconds": video_start_seconds,
    }

    offset = first_seconds - video_start_seconds

    # Within half a minute of an exact hour is a start-reference mismatch, not a
    # long slate.
    hours_off = round(offset / 3600.0)
    if hours_off >= 1 and abs(offset - hours_off * 3600.0) <= 30.0:
        return Check(
            "Start reference",
            FAIL,
            f"Captions start {hours_off} hour(s) after the video starts.",
            [
                f"First caption: {first_entry.timecode}",
                f"Video start timecode: {video_start_tc or 'none (zero-based file)'}",
                "The caption file was timed against a head-based master "
                f"({hours_off:02d}:00:00:00 start) but the video is not.",
                "Ask the vendor to re-export against the delivered file's timecode, "
                f"or rebase the caption file by -{hours_off:02d}:00:00:00.",
            ],
            data,
        )

    if offset < -0.5:
        return Check(
            "Start reference",
            FAIL,
            "Captions start before the video does.",
            [
                f"First caption: {first_entry.timecode}",
                f"Video start timecode: {video_start_tc or '00:00:00:00'}",
                f"The first caption lands {format_seconds(abs(offset))} before the first frame.",
            ],
            data,
        )

    if media and media.duration and offset > max(120.0, media.duration * 0.25):
        return Check(
            "Start reference",
            WARN,
            f"First caption does not arrive until {format_seconds(offset)} into the program.",
            [
                "That is a long silent head. Confirm it is intentional (slate, "
                "montage, or a partially captioned deliverable).",
            ],
            data,
        )

    return Check(
        "Start reference",
        PASS,
        f"First caption at {first_entry.timecode} sits inside the program.",
        [f"Video start timecode: {video_start_tc or '00:00:00:00 (zero-based)'}"],
        data,
    )


def _check_overrun(timings, media, rate_code, tolerance_frames):
    """Captions that run past the last frame are impossible, not debatable."""
    last_entry = timings.last_entry()
    if not last_entry or not media or not media.duration:
        return None

    rate = RATE_BY_CODE[rate_code]
    video_start_seconds, _ = _video_start_seconds(media, rate_code)
    video_end_seconds = video_start_seconds + media.duration
    last_seconds = timings.seconds_at(last_entry, rate_code)
    tolerance_seconds = frames_to_seconds(tolerance_frames, rate)

    overrun = last_seconds - video_end_seconds
    data = {
        "last_caption_timecode": last_entry.timecode,
        "last_caption_seconds": last_seconds,
        "video_end_seconds": video_end_seconds,
        "overrun_seconds": overrun,
        "tolerance_frames": tolerance_frames,
    }

    if overrun > tolerance_seconds:
        overrun_frames = overrun * float(rate)
        return Check(
            "Program overrun",
            FAIL,
            f"Captions run {format_seconds(overrun)} past the end of the video.",
            [
                f"Last caption event: {last_entry.timecode} "
                f"({format_seconds(last_seconds)} at {rate_label(rate_code)} fps)",
                f"Video ends at {format_seconds(video_end_seconds)} "
                f"({media.total_frames()[0]} frames).",
                f"That is {overrun_frames:.0f} frames of caption data with no picture under it.",
                "A caption file cannot be longer than its program. Either the frame rate "
                "on this file is mislabelled or it was cut against a different master.",
            ],
            data,
        )

    return Check(
        "Program overrun",
        PASS,
        "All caption events land inside the program duration.",
        [
            f"Last caption event: {last_entry.timecode}",
            f"Video ends at {format_seconds(video_end_seconds)}.",
        ],
        data,
    )


def _check_tail_alignment(timings, media, rate_code, tolerance_frames):
    """The ±N frame landing check on the final caption event."""
    last_entry = timings.last_entry()
    if not last_entry or not media or not media.duration:
        return None

    rate = RATE_BY_CODE[rate_code]
    video_start_seconds, _ = _video_start_seconds(media, rate_code)
    video_end_seconds = video_start_seconds + media.duration
    last_seconds = timings.seconds_at(last_entry, rate_code)
    delta = last_seconds - video_end_seconds
    delta_frames = delta * float(rate)

    data = {
        "delta_seconds": delta,
        "delta_frames": delta_frames,
        "tolerance_frames": tolerance_frames,
    }

    if abs(delta_frames) <= tolerance_frames:
        return Check(
            "Tail landing",
            PASS,
            f"Final caption event lands {delta_frames:+.1f} frames from the last frame.",
            data=data,
        )

    return Check(
        "Tail landing",
        INFO,
        f"Final caption event lands {format_seconds(abs(delta))} "
        f"{'after' if delta > 0 else 'before'} the last frame.",
        [
            f"Tolerance is +/-{tolerance_frames} frames.",
            "A gap here is normal when the program ends on music, credits, or silence. "
            "Read it alongside the coverage ratio below rather than on its own.",
        ],
        data,
    )


def rate_fit_table(timings, media, tolerance_frames=2):
    """Re-read the caption timecodes at every supported rate.

    The rate whose reading lands the last caption closest to (but not past) the
    end of the program is the rate these timecodes were actually written at.
    This is the check that names the vendor's mistake.
    """
    if not media or not media.duration:
        return []

    last_entry = timings.last_entry()
    if not last_entry:
        return []

    video_end = media.duration
    rows = []

    for code in sorted(RATE_BY_CODE):
        rate = RATE_BY_CODE[code]
        drop = timings.drop_frame if code in DROP_FRAME_CODES else False
        try:
            frames = timecode_to_frames(last_entry.timecode, rate, drop_frame=drop)
        except TimecodeError:
            continue
        seconds = frames_to_seconds(frames, rate)
        video_start_seconds, _ = _video_start_seconds(media, code)
        rows.append(
            {
                "rate_code": code,
                "label": rate_label(code),
                "last_caption_seconds": seconds,
                "delta_seconds": seconds - (video_start_seconds + video_end),
                "coverage": seconds / (video_start_seconds + video_end) if video_end else None,
                "fits": seconds <= video_start_seconds + video_end + frames_to_seconds(tolerance_frames, rate),
            }
        )

    rows.sort(key=lambda row: (not row["fits"], abs(row["delta_seconds"])))
    return rows


def _check_coverage_ratio(timings, media, rate_code, tolerance_frames):
    """The ratio test: how long the caption file runs versus the program.

    A file that covers the whole program should land at a ratio near 1.00.
    29.97-into-25 lands at 1.199. 25-into-29.97 lands at 0.834. Those numbers
    are what goes back to the vendor.
    """
    last_entry = timings.last_entry()
    if not last_entry or not media or not media.duration:
        return None

    video_start_seconds, _ = _video_start_seconds(media, rate_code)
    video_end_seconds = video_start_seconds + media.duration
    last_seconds = timings.seconds_at(last_entry, rate_code)
    if video_end_seconds <= 0:
        return None

    ratio = last_seconds / video_end_seconds
    fit_rows = rate_fit_table(timings, media, tolerance_frames)
    best_fit = fit_rows[0] if fit_rows else None
    explanations = explain_timing_ratio(ratio)
    delta_seconds = last_seconds - video_end_seconds

    data = {
        "ratio": ratio,
        "delta_seconds": delta_seconds,
        "declared_rate_code": rate_code,
        "rate_fit": fit_rows,
        "explanations": explanations,
    }

    # A ratio is a percentage of the program, so on a 30-second clip a couple of
    # frames looks like a real deviation. Pass on either measure being tight.
    absolute_tolerance = max(1.0, min(5.0, 0.005 * media.duration))
    if abs(ratio - 1.0) <= 0.002 or abs(delta_seconds) <= absolute_tolerance:
        return Check(
            "Coverage ratio",
            PASS,
            f"Caption file spans {ratio * 100:.1f}% of the program.",
            [f"Read at {rate_label(rate_code)} fps, the caption timeline matches the video timeline."],
            data,
        )

    detail = [
        f"Read at {rate_label(rate_code)} fps, the caption file runs to "
        f"{format_seconds(last_seconds)} against a {format_seconds(video_end_seconds)} program.",
        f"Ratio: {ratio:.4f}",
    ]

    status = WARN
    headline = f"Caption file spans {ratio * 100:.1f}% of the program."
    deviation = abs(ratio - 1.0)

    # Below ~1% the ratio alone cannot separate a rate error from a program that
    # simply ends on silence, because SMPTE timecode is already near wall clock.
    # Anything that small is Tier 2's job to confirm.
    decisive = deviation >= 0.01

    # For a file that runs short, "it just ends early" already explains a
    # coverage of `ratio`, and a rate pair near 1.0 can absorb any shortfall by
    # quietly assuming less coverage. A rate hypothesis only earns a failure if
    # it explains substantially more of the program than that null hypothesis -
    # or if the file overruns, where the null hypothesis is impossible.
    overruns = ratio > 1.0
    improvement = (explanations[0]["implied_coverage"] - ratio) if explanations else 0.0
    rate_error_proven = bool(explanations) and decisive and (overruns or improvement >= 0.05)

    if rate_error_proven:
        best = explanations[0]
        status = FAIL
        headline = f"Timing ratio {ratio:.4f} matches a frame-rate mismatch: {best['text']}."
        detail.append(
            f"A file {best['text']} runs {best['ratio']:.4f}x long. Against that, this file's "
            f"timings imply it covers {best['implied_coverage'] * 100:.1f}% of the program, "
            "which is what a normal full-program caption file looks like."
        )
        detail.append(
            f"Re-export the caption file against a {rate_label(best['played_code'])} fps reference, "
            "or rate-convert the existing timecodes rather than relabelling them."
        )
    elif overruns:
        status = FAIL
        detail.append("The caption file describes more program than the video contains.")
    elif ratio < FULL_PROGRAM_COVERAGE:
        detail.append(
            "The caption file stops well before the end of the program. That is expected for a "
            "partial deliverable, and a red flag for a full-program one."
        )
    else:
        status = INFO
        detail.append(
            f"A {deviation * 100:.2f}% shortfall is within what a program ending on music, "
            "credits, or silence produces, so the math cannot call it either way."
        )
        # "The file is simply short" already explains a coverage of `ratio`. Only
        # raise a rate hypothesis when it explains meaningfully more than that.
        if explanations and explanations[0]["implied_coverage"] - ratio > 0.02:
            detail.append(
                f"It is also consistent with a file {explanations[0]['text']}. "
                "Run Tier 2 to settle it: a rate error drifts across the program, a silent tail does not."
            )

    # Only name an alternative rate when reading at it is decisively better; a
    # marginal improvement is noise and undermines the rest of the report.
    if (
        best_fit
        and best_fit["rate_code"] != rate_code
        and best_fit["fits"]
        and abs(delta_seconds) > max(2.0, 0.01 * media.duration)
        and abs(best_fit["delta_seconds"]) < abs(delta_seconds) / 2.0
    ):
        detail.append(
            f"These timecodes land closest to the program end when read at "
            f"{best_fit['label']} fps ({best_fit['delta_seconds']:+.2f} s), not {rate_label(rate_code)} fps."
        )

    return Check("Coverage ratio", status, headline, detail, data)


def _check_rate_declaration(timings, media, rate_code):
    """Does the caption file's own idea of frame rate match the video's?"""
    declared = timings.declared_rate_code
    video_code = media.rate_code if media else None

    data = {
        "caption_declared_rate_code": declared,
        "video_rate_code": video_code,
        "resolved_rate_code": rate_code,
    }

    if not video_code:
        return Check(
            "Frame rate declaration",
            INFO,
            "The video's frame rate could not be matched to a standard rate.",
            [f"Reading captions at {rate_label(rate_code)} fps."],
            data,
        )

    if declared and declared != video_code:
        return Check(
            "Frame rate declaration",
            WARN,
            f"Caption file implies {rate_label(declared)} fps; video is {rate_label(video_code)} fps.",
            [
                "For SCC this is a soft signal - the format has no rate header and the "
                "drop-frame flag is the only hint - but paired with a coverage ratio "
                "away from 1.00 it is the whole story.",
                f"Checks below are run at {rate_label(rate_code)} fps.",
            ],
            data,
        )

    return Check(
        "Frame rate declaration",
        PASS,
        f"Caption file and video agree on {rate_label(video_code)} fps.",
        data=data,
    )


def _check_event_density(timings, media, rate_code):
    """Sanity check on caption volume, to catch truncated exports."""
    text_entries = [entry for entry in timings.entries if entry.has_text]
    data = {"rows": len(timings.entries), "text_rows": len(text_entries)}

    if not text_entries:
        return Check(
            "Caption content",
            FAIL,
            "The caption file contains no displayable text.",
            ["Every row is control data. This file will render blank."],
            data,
        )

    if media and media.duration and media.duration > 60:
        per_minute = len(text_entries) / (media.duration / 60.0)
        data["text_rows_per_minute"] = per_minute
        if per_minute < 1.0:
            return Check(
                "Caption content",
                WARN,
                f"Only {per_minute:.1f} caption rows per minute of program.",
                ["That is sparse for dialogue. Check for a truncated or partial export."],
                data,
            )

    return Check(
        "Caption content",
        PASS,
        f"{len(text_entries)} caption rows carry text.",
        data=data,
    )


def resolve_rate_code(timings, media, override=None):
    """Pick the rate to read the caption timecodes at.

    The video's rate wins, because the deliverable is the thing being QC'd
    against. An explicit override wins over everything.
    """
    if override:
        return override
    if media and media.rate_code:
        return media.rate_code
    if timings.declared_rate_code:
        return timings.declared_rate_code
    return 2997


def tier1_check(caption_path, video_path=None, rate_code=None, tolerance_frames=2, count_frames=False):
    """Fast math-only sync check. Seconds to run, no audio analysis.

    `video_path` is optional: without it the caption file is still checked for
    internal consistency (ordering, drop-frame legality, content).
    """
    result = SyncResult(video_path, caption_path)

    try:
        timings = read_caption_timings(caption_path)
    except CaptionTimingError as error:
        result.errors.append(str(error))
        return result

    result.timings = timings

    media = None
    if video_path:
        try:
            media = probe_media(video_path, count_frames=count_frames)
        except ProbeError as error:
            result.errors.append(str(error))
            return result
        if not media.has_video:
            result.errors.append(
                f"{Path(video_path).name} has no video stream, so there is no frame count to check against."
            )
            return result

    result.media = media
    resolved_rate = resolve_rate_code(timings, media, rate_code)
    result.rate_code = resolved_rate

    if media:
        result.add(_check_rate_declaration(timings, media, resolved_rate))

    result.add(_check_timecode_ordering(timings, resolved_rate))

    drop_check = _check_drop_frame_legality(timings, resolved_rate)
    if drop_check:
        result.add(drop_check)

    result.add(_check_event_density(timings, media, resolved_rate))

    convention_check = _check_drop_frame_convention(timings, media, resolved_rate)
    if convention_check:
        result.add(convention_check)

    start_check = _check_start_reference(timings, media, resolved_rate)
    if start_check:
        result.add(start_check)

    for builder in (_check_overrun, _check_coverage_ratio, _check_tail_alignment):
        check = builder(timings, media, resolved_rate, tolerance_frames)
        if check:
            result.add(check)

    return result


def tier2_check(
    caption_path,
    video_path,
    rate_code=None,
    model_size="base",
    language=None,
    tolerance_ms=200.0,
    max_cues=None,
    progress=None,
    transcript_cache=True,
):
    """Audio-verified drift measurement.

    Transcribes the dialogue, matches caption text against it, and regresses
    offset against timeline position. Slow (transcription dominates); run it
    when Tier 1 passes but the file still looks wrong, or as the full QC pass.
    """
    from alignment import align_cues_to_transcript, describe_drift, summarize_alignment
    from caption_cues import extract_cues
    from transcribe import TranscriptionError, transcribe_media

    result = SyncResult(video_path, caption_path)

    def report(message):
        if progress:
            progress(message)

    try:
        media = probe_media(video_path)
    except ProbeError as error:
        result.errors.append(str(error))
        return result

    result.media = media
    if not media.has_audio:
        result.errors.append(
            f"{Path(video_path).name} has no audio stream, so caption timing cannot be verified against dialogue."
        )
        return result

    try:
        timings = read_caption_timings(caption_path)
        result.timings = timings
    except CaptionTimingError:
        # Media-embedded captions have no sidecar timings; that is fine here.
        timings = None

    resolved_rate = resolve_rate_code(timings, media, rate_code) if timings else (rate_code or media.rate_code or 2997)
    result.rate_code = resolved_rate

    report("Reading caption cues...")
    try:
        cues = extract_cues(caption_path, resolved_rate)
    except Exception as error:  # decoder failures surface as plain messages
        result.errors.append(f"Could not read caption cues: {error}")
        return result

    if not cues:
        result.errors.append("No visible caption cues with text were found, so there is nothing to align.")
        return result

    if max_cues and len(cues) > max_cues:
        # Even sampling across the whole timeline - drift is a slope, and a
        # slope needs both ends.
        step = len(cues) / float(max_cues)
        cues = [cues[int(index * step)] for index in range(max_cues)]

    report(f"Transcribing dialogue with faster-whisper ({model_size})...")
    try:
        transcript = transcribe_media(
            video_path,
            model_size=model_size,
            language=language,
            progress=report,
            use_cache=transcript_cache,
        )
    except TranscriptionError as error:
        result.errors.append(str(error))
        return result

    # Caption cues are stamped in absolute program timecode; the transcript is
    # stamped from the head of the media file. Reuse Tier 1's parse of the
    # container start timecode to put both on the media file's timeline before
    # anything is subtracted, or the start timecode is reported as the offset.
    start_offset, start_timecode = _video_start_seconds(media, resolved_rate)
    if start_offset:
        report(
            f"Rebasing caption timecodes onto the media timeline "
            f"(video start timecode {start_timecode} = {start_offset:.3f} s)."
        )

    report(f"Aligning {len(cues)} caption cues against {len(transcript.words)} transcribed words...")
    matches = align_cues_to_transcript(cues, transcript, start_offset=start_offset)
    summary = summarize_alignment(cues, matches, media, start_offset=start_offset)

    if summary["matched"] < 8:
        result.add(
            Check(
                "Alignment coverage",
                WARN,
                f"Only {summary['matched']} of {len(cues)} cues could be matched to the transcript.",
                [
                    "Too few matches to measure drift reliably.",
                    "Common causes: heavy music or effects under the dialogue, a non-English "
                    "track without --language set, or captions that do not transcribe the "
                    "dialogue verbatim.",
                    "Try a larger model (small or medium) before trusting this result.",
                ],
                summary,
            )
        )
        result.tier2 = summary
        return result

    result.add(
        Check(
            "Alignment coverage",
            PASS,
            f"Matched {summary['matched']} of {len(cues)} cues ({summary['match_rate'] * 100:.0f}%).",
            [f"Median match confidence {summary['median_confidence']:.2f}."],
            summary,
        )
    )

    drift = summary["drift"]
    result.tier2 = summary

    offset_status = PASS
    median_offset = summary["median_offset"]
    if abs(median_offset) * 1000 > tolerance_ms:
        offset_status = FAIL if abs(median_offset) * 1000 > tolerance_ms * 3 else WARN

    result.add(
        Check(
            "Constant offset",
            offset_status,
            f"Median caption-to-dialogue offset is {format_offset_ms(median_offset)}.",
            [
                "Positive means the caption appears before the words are spoken.",
                f"Tolerance is +/-{tolerance_ms:.0f} ms.",
            ]
            + (
                [
                    f"Caption timecodes were rebased by the video's start timecode "
                    f"({start_timecode}, {start_offset:.3f} s) so both timelines are "
                    "measured from the head of the media file."
                ]
                if start_offset
                else []
            )
            + [
                (
                    "A constant offset with no slope is a fixed timing shift, not a rate "
                    "problem - it can be fixed by sliding the whole file."
                )
                if abs(drift["slope_ms_per_minute"]) < 20
                else "This sits on top of a measured drift; fix the drift first.",
            ],
            summary,
        )
    )

    drift_status, drift_headline, drift_detail = describe_drift(drift, media)
    result.add(Check("Drift over time", drift_status, drift_headline, drift_detail, drift))

    return result


def run_full_check(
    caption_path,
    video_path,
    rate_code=None,
    tolerance_frames=2,
    tolerance_ms=200.0,
    model_size="base",
    language=None,
    max_cues=None,
    progress=None,
    count_frames=False,
    force_tier2=False,
):
    """Tier 1, then Tier 2 unless Tier 1 already failed on hard math.

    A file that overruns the program by 20 minutes does not need a transcript to
    prove it is broken; `force_tier2` overrides that when you want the drift
    number anyway.
    """
    tier1 = tier1_check(
        caption_path,
        video_path,
        rate_code=rate_code,
        tolerance_frames=tolerance_frames,
        count_frames=count_frames,
    )

    if tier1.errors:
        return tier1, None

    if tier1.verdict == FAIL and not force_tier2:
        return tier1, None

    if not video_path:
        return tier1, None

    tier2 = tier2_check(
        caption_path,
        video_path,
        rate_code=rate_code or tier1.rate_code,
        model_size=model_size,
        language=language,
        tolerance_ms=tolerance_ms,
        max_cues=max_cues,
        progress=progress,
    )
    return tier1, tier2
