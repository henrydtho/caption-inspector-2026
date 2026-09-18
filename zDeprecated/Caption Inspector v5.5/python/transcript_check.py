"""Build a transcript from a caption file, and check it against the video.

The driver for the Transcript tab and the `--transcript` CLI flag. It reuses the
Tier 2 machinery - the same cue extraction, the same transcriber, the same
aligner - because the question "does line 412 match the audio" is the same
question Tier 2 answers in aggregate. What differs is the output: every line of
the file, in order, each carrying its own verdict, rather than one median.

Without a video it still produces the transcript and says the match was not
checked. That is a useful thing to run on its own - it converts a TTML delivery
to a readable transcript - and it must never imply a check it did not perform.
"""

from pathlib import Path

from cancellation import OperationCancelled, raise_if_cancelled
from caption_timing import CaptionTimingError, read_caption_timings
from media_probe import ProbeError, probe_media
from transcript_export import build_lines, TranscriptResult


def build_transcript(
    caption_path,
    video_path=None,
    rate_code=None,
    model_size="base",
    language=None,
    tolerance_ms=200.0,
    max_cues=None,
    progress=None,
    cancel=None,
    transcript_cache=True,
):
    """Produce a `TranscriptResult` for `caption_path`.

    When `video_path` is given, the dialogue is transcribed and every cue is
    matched against it. When it is not, the lines come back unchecked.
    """
    from caption_cues import extract_cues
    from sync_check import caption_timeline_offset, resolve_rate_code
    from transcribe import TranscriptionError, ensure_model_available

    def report(message):
        if progress:
            progress(message)

    errors = []
    notes = []

    # A missing model stops the audio check but not the transcript, so this
    # drops the video rather than failing the run: the lines still come back,
    # honestly marked "not checked".
    if video_path:
        try:
            ensure_model_available(model_size)
        except TranscriptionError as error:
            errors.append(str(error))
            notes.append(
                "The transcript below was built from the caption file alone. Its timings "
                "have not been checked against the dialogue."
            )
            video_path = None

    # ------------------------------------------------------------ caption side
    timings = None
    try:
        timings = read_caption_timings(caption_path)
    except CaptionTimingError as error:
        # Media-embedded captions have no sidecar timings, which is fine. A
        # genuinely unreadable sidecar surfaces when the cue extraction fails.
        if Path(caption_path).suffix.lower() not in (".mov", ".mp4", ".ts", ".mpg", ".mpeg", ".m2v"):
            notes.append(str(error))

    media = None
    if video_path:
        raise_if_cancelled(cancel)
        try:
            media = probe_media(video_path, cancel=cancel)
        except ProbeError as error:
            errors.append(str(error))
            video_path = None

    resolved_rate = (
        resolve_rate_code(timings, media, rate_code)
        if timings
        else (rate_code or (media.rate_code if media else None) or 2997)
    )

    report("Reading caption cues...")
    raise_if_cancelled(cancel)
    try:
        cues = extract_cues(caption_path, resolved_rate)
    except Exception as error:
        errors.append(f"Could not read caption cues: {error}")
        return TranscriptResult(
            caption_path,
            [],
            video_path,
            media,
            source_kind=timings.kind if timings else None,
            tolerance_ms=tolerance_ms,
            errors=errors,
            notes=notes,
        )

    if max_cues and len(cues) > max_cues:
        step = len(cues) / float(max_cues)
        cues = [cues[int(index * step)] for index in range(max_cues)]
        notes.append(
            f"Sampled {max_cues} cues evenly across the timeline; the transcript is not complete."
        )

    source_kind = timings.kind if timings else Path(caption_path).suffix.lower().lstrip(".")
    document = getattr(timings, "subtitle_document", None) if timings else None
    detected_language = document.language if document else None

    # ------------------------------------------------------------- video side
    if not cues:
        errors.append("No caption cues with text were found, so there is no transcript to build.")
        return TranscriptResult(
            caption_path, [], video_path, media, source_kind=source_kind,
            tolerance_ms=tolerance_ms, errors=errors, notes=notes,
            language=detected_language,
        )

    if not video_path or media is None:
        notes.append(
            "No video was supplied, so each line's timing was taken from the caption file and "
            "not verified against spoken audio."
        )
        return TranscriptResult(
            caption_path,
            build_lines(cues, None, tolerance_ms, checked=False),
            None,
            media,
            source_kind=source_kind,
            tolerance_ms=tolerance_ms,
            notes=notes,
            errors=errors,
            language=detected_language,
        )

    if not media.has_audio:
        errors.append(
            f"{Path(video_path).name} has no audio stream, so the transcript cannot be "
            "checked against dialogue."
        )
        notes.append("Lines are reported unchecked because the video carries no audio.")
        return TranscriptResult(
            caption_path,
            build_lines(cues, None, tolerance_ms, checked=False),
            video_path,
            media,
            source_kind=source_kind,
            tolerance_ms=tolerance_ms,
            notes=notes,
            errors=errors,
            language=detected_language,
        )

    from alignment import align_cues_to_transcript, summarize_alignment
    from transcribe import TranscriptionError, transcribe_media

    report(f"Transcribing dialogue with faster-whisper ({model_size})...")
    try:
        transcript = transcribe_media(
            video_path,
            model_size=model_size,
            language=language,
            progress=report,
            use_cache=transcript_cache,
            cancel=cancel,
        )
    except OperationCancelled:
        raise
    except TranscriptionError as error:
        errors.append(str(error))
        notes.append("Lines are reported unchecked because the dialogue could not be transcribed.")
        return TranscriptResult(
            caption_path,
            build_lines(cues, None, tolerance_ms, checked=False),
            video_path,
            media,
            source_kind=source_kind,
            tolerance_ms=tolerance_ms,
            notes=notes,
            errors=errors,
            language=detected_language,
        )

    # Only SCC/MCC need rebasing; see `caption_timeline_offset`.
    start_offset, start_timecode = caption_timeline_offset(media, resolved_rate, caption_path)
    if start_offset:
        notes.append(
            f"Caption timecodes were rebased by the video's start timecode "
            f"({start_timecode}, {start_offset:.3f} s) so both timelines start at the head of "
            "the media file."
        )

    report(f"Matching {len(cues)} lines against {len(transcript.words)} transcribed words...")
    matches = align_cues_to_transcript(
        cues, transcript, start_offset=start_offset, cancel=cancel
    )
    summary = summarize_alignment(cues, matches, media, start_offset=start_offset)

    if summary["matched"] and summary["matched"] < max(4, len(cues) // 10):
        notes.append(
            f"Only {summary['matched']} of {len(cues)} lines were found in the dialogue, so the "
            "median offset rests on few points. Heavy music under dialogue, a non-verbatim "
            "caption file, or the wrong language will all do this."
        )

    drift = summary.get("drift") or {}
    if abs(drift.get("slope_ms_per_minute") or 0.0) >= 20.0 and (drift.get("r_squared") or 0.0) >= 0.5:
        notes.append(
            f"The offsets are sloped, not flat ({drift['slope_ms_per_minute']:+.0f} ms per "
            "minute). Individual line offsets will read small at one end of the program and "
            "large at the other; that is drift, and sliding the file will not fix it."
        )

    lines = build_lines(cues, matches, tolerance_ms, checked=True)

    return TranscriptResult(
        caption_path,
        lines,
        video_path,
        media,
        summary=summary,
        tolerance_ms=tolerance_ms,
        start_offset=start_offset,
        start_timecode=start_timecode,
        source_kind=source_kind,
        language=detected_language or transcript.language,
        errors=errors,
        notes=notes,
    )
