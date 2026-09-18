#!/usr/bin/env python3
"""Tests for the v4 transcript tab and the Stop plumbing.

Run from this directory:  python3 -m pytest test__transcript_and_cancel.py -v

Transcription is not exercised here - a model download is not a unit test - so
the alignment is driven with a synthetic transcript, the same way the Tier 2
tests do it.
"""

import sys
import threading
import time
from pathlib import Path

import pytest

PYTHON_DIR = Path(__file__).resolve().parents[2] / "python"
sys.path.insert(0, str(PYTHON_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from alignment import align_cues_to_transcript  # noqa: E402
from cancellation import CancelToken, OperationCancelled, raise_if_cancelled  # noqa: E402
from caption_cues import Cue, cues_from_text_subtitle  # noqa: E402
from caption_timing import read_caption_timings  # noqa: E402
from sync_check import caption_timeline_offset  # noqa: E402
from transcript_export import (  # noqa: E402
    MATCHED,
    NOT_DIALOGUE,
    OFF,
    TOO_SHORT,
    UNCHECKED,
    UNMATCHED,
    build_lines,
    render,
    TranscriptResult,
)

import test__sync_check as fx  # noqa: E402


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


def test_token_starts_clear_and_raises_once_cancelled():
    token = CancelToken()
    raise_if_cancelled(token)  # must not raise
    token.cancel()
    assert token.cancelled
    with pytest.raises(OperationCancelled):
        token.raise_if_cancelled()


def test_a_none_token_is_always_safe():
    """Callers pass None when nothing can be cancelled; that must not crash."""
    raise_if_cancelled(None)


def test_reset_makes_a_token_reusable():
    token = CancelToken()
    token.cancel()
    token.reset()
    raise_if_cancelled(token)
    assert not token.cancelled


class _FakeProcess:
    """Stands in for a subprocess; records how it was asked to die."""

    def __init__(self, already_finished=False):
        self.terminated = False
        self.killed = False
        self._finished = already_finished

    def poll(self):
        return 0 if self._finished else None

    def terminate(self):
        self.terminated = True
        self._finished = True

    def kill(self):
        self.killed = True
        self._finished = True

    def wait(self, timeout=None):
        return 0


def test_cancel_terminates_a_registered_child_process():
    token = CancelToken()
    process = _FakeProcess()
    token.register_process(process)
    token.cancel()
    assert process.terminated


def test_registering_after_cancel_kills_immediately_and_raises():
    """A Stop that lands microseconds before a spawn must still be honoured.

    Otherwise the process starts anyway and the run continues to completion
    after the user has asked it to stop.
    """
    token = CancelToken()
    token.cancel()
    process = _FakeProcess()
    with pytest.raises(OperationCancelled):
        token.register_process(process)
    assert process.terminated


def test_unregistered_process_is_left_alone():
    token = CancelToken()
    process = _FakeProcess()
    token.register_process(process)
    token.unregister_process(process)
    token.cancel()
    assert not process.terminated


def test_alignment_stops_when_the_token_is_cancelled():
    cues = fx.build_cues(count=200)
    transcript = fx.build_transcript(cues)
    token = CancelToken()
    token.cancel()

    with pytest.raises(OperationCancelled):
        align_cues_to_transcript(cues, transcript, cancel=token)


def test_alignment_stops_partway_through_a_long_run():
    """Cancelled from another thread, mid-run, the way the button does it."""
    cues = fx.build_cues(count=400)
    transcript = fx.build_transcript(cues)
    token = CancelToken()
    outcome = {}

    def work():
        try:
            align_cues_to_transcript(cues, transcript, cancel=token)
            outcome["result"] = "completed"
        except OperationCancelled:
            outcome["result"] = "cancelled"

    thread = threading.Thread(target=work)
    thread.start()
    time.sleep(0.01)
    token.cancel()
    thread.join(timeout=30)

    assert not thread.is_alive()
    assert outcome["result"] in ("cancelled", "completed")


# ---------------------------------------------------------------------------
# Which caption formats get rebased
# ---------------------------------------------------------------------------


class _FakeMedia:
    def __init__(self, start_timecode="00:58:30:00", duration=600.0):
        self.start_timecode = start_timecode
        self.duration = duration


def test_scc_is_rebased_by_the_video_start_timecode():
    offset, timecode = caption_timeline_offset(_FakeMedia(), 2500, "program.scc")
    assert offset == pytest.approx(3510.0)
    assert timecode == "00:58:30:00"


@pytest.mark.parametrize("name", ["program.vtt", "program.ttml", "program.srt", "program.dfxp"])
def test_text_subtitles_are_never_rebased(name):
    """Their zero is already the head of the programme.

    Subtracting the start timecode from a WebVTT file would invent the same
    3510-second error the SCC path exists to remove, in the other direction.
    """
    offset, timecode = caption_timeline_offset(_FakeMedia(), 2500, name)
    assert offset == 0.0
    assert timecode is None


def test_mcc_is_rebased_like_scc():
    offset, _ = caption_timeline_offset(_FakeMedia(), 2500, "program.mcc")
    assert offset == pytest.approx(3510.0)


def test_a_zero_based_video_rebases_nothing():
    offset, _ = caption_timeline_offset(_FakeMedia("00:00:00:00"), 2500, "program.scc")
    assert offset == 0.0


# ---------------------------------------------------------------------------
# Timing rows from text subtitles
# ---------------------------------------------------------------------------


def test_text_subtitle_timings_record_both_display_and_erase(tmp_path):
    """Recording only cue starts hides a subtitle that runs past the program end."""
    path = tmp_path / "p.vtt"
    path.write_text(
        "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nOne\n\n"
        "00:00:05.000 --> 00:00:09.000\nTwo\n",
        encoding="utf-8",
    )
    timings = read_caption_timings(path)

    assert sum(1 for entry in timings.entries if entry.has_text) == 2
    assert sum(1 for entry in timings.entries if entry.is_erase) == 2
    # The last row is the final clear, not the final cue's start.
    assert timings.last_entry().is_erase


def test_ttml_timings_carry_the_declared_rate(tmp_path):
    path = tmp_path / "p.ttml"
    path.write_text(
        '<tt xmlns="http://www.w3.org/ns/ttml" '
        'xmlns:ttp="http://www.w3.org/ns/ttml#parameter" '
        'ttp:frameRate="30" ttp:frameRateMultiplier="1000 1001">'
        "<body><div><p begin='1s' end='2s'>Hello</p></div></body></tt>",
        encoding="utf-8",
    )
    timings = read_caption_timings(path)
    assert timings.declared_rate_code == 2997
    assert timings.kind == "ttml"


def test_text_subtitle_cues_keep_their_out_point_and_speaker(tmp_path):
    path = tmp_path / "p.vtt"
    path.write_text(
        "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\n<v Reporter>Hello there\n",
        encoding="utf-8",
    )
    cues = cues_from_text_subtitle(path)
    assert len(cues) == 1
    assert cues[0].seconds == pytest.approx(1.0)
    assert cues[0].end_seconds == pytest.approx(4.0)
    assert cues[0].speaker == "Reporter"


# ---------------------------------------------------------------------------
# Transcript assembly and rendering
# ---------------------------------------------------------------------------


def _cue(seconds, text, end=None, speaker=None):
    return Cue("00:00:00:00", seconds, text, "vtt", end_seconds=end, speaker=speaker)


def test_every_cue_appears_in_the_transcript_even_when_unmatched():
    """A transcript must be the whole file, not the lines that matched."""
    cues = fx.build_cues(count=12)
    transcript = fx.build_transcript(cues)
    matches = align_cues_to_transcript(cues, transcript)
    # Drop some matches to simulate cues the aligner could not find.
    lines = build_lines(cues, matches[:5], tolerance_ms=200.0, checked=True)

    assert len(lines) == len(cues)
    assert sum(1 for line in lines if line.status == UNMATCHED) == len(cues) - 5


def test_lines_are_unchecked_when_no_video_was_supplied():
    cues = [_cue(1.0, "hello"), _cue(2.0, "world")]
    lines = build_lines(cues, None, checked=False)
    assert all(line.status == UNCHECKED for line in lines)

    result = TranscriptResult("p.vtt", lines, source_kind="vtt")
    assert not result.checked
    assert "not checked" in result.verdict_sentence()


def test_tolerance_decides_matched_versus_out_of_tolerance():
    cues = fx.build_cues(count=10)
    transcript = fx.build_transcript(cues, offset=0.5)
    matches = align_cues_to_transcript(cues, transcript)

    tight = build_lines(cues, matches, tolerance_ms=100.0, checked=True)
    loose = build_lines(cues, matches, tolerance_ms=1000.0, checked=True)

    assert all(line.status == OFF for line in tight if line.offset is not None)
    assert all(line.status == MATCHED for line in loose if line.offset is not None)


def test_verdict_never_claims_a_match_it_did_not_measure():
    cues = [_cue(1.0, "hello")]
    result = TranscriptResult("p.vtt", build_lines(cues, None, checked=False))
    sentence = result.verdict_sentence()
    assert "matches the video" not in sentence
    assert "not checked" in sentence


@pytest.mark.parametrize(
    "output_format", ["text", "prose", "markdown", "csv", "json", "srt", "vtt", "html"]
)
def test_every_export_format_renders(output_format):
    cues = [
        _cue(1.0, "First line", end=4.0, speaker="Reporter"),
        _cue(5.0, "Second line\nwith a break", end=9.0),
    ]
    result = TranscriptResult(
        "p.vtt", build_lines(cues, None, checked=False), source_kind="vtt"
    )
    payload = render(result, output_format)
    assert payload.strip()
    assert "First line" in payload


def test_srt_export_gives_every_cue_an_out_point():
    """608 cues have no clear time; an SRT without one is invalid."""
    cues = [_cue(1.0, "no end time"), _cue(5.0, "also none")]
    result = TranscriptResult("p.scc", build_lines(cues, None, checked=False))
    payload = render(result, "srt")
    assert "-->" in payload
    for block in payload.strip().split("\n\n"):
        assert "-->" in block.splitlines()[1]


def test_vtt_export_round_trips_the_speaker():
    cues = [_cue(1.0, "Hello", end=2.0, speaker="Reporter")]
    result = TranscriptResult("p.vtt", build_lines(cues, None, checked=False))
    payload = render(result, "vtt")
    assert payload.startswith("WEBVTT")
    assert "<v Reporter>Hello" in payload


def test_prose_export_groups_consecutive_lines_by_speaker():
    cues = [
        _cue(1.0, "First bit", speaker="Anna"),
        _cue(2.0, "second bit", speaker="Anna"),
        _cue(3.0, "a reply", speaker="Ben"),
    ]
    result = TranscriptResult("p.vtt", build_lines(cues, None, checked=False))
    paragraphs = render(result, "prose").strip().split("\n\n")
    assert paragraphs == ["Anna: First bit second bit", "Ben: a reply"]


# ---------------------------------------------------------------------------
# v5.7: the speaker label is printed once, and unmatchable lines are not faults
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "caption, speaker, dialogue",
    [
        ("TANYA (VOICEOVER): Previously on The Real Housewives",
         "TANYA (VOICEOVER)", "Previously on The Real Housewives"),
        ("NICOLE: All the girls have checked up on me,",
         "NICOLE", "All the girls have checked up on me,"),
        (">> LAUREN: with chevrons", "LAUREN", "with chevrons"),
        ("- MARTIN: Boy or girl?", "MARTIN", "Boy or girl?"),
    ],
)
def test_the_speaker_label_is_removed_from_the_line_text(caption, speaker, dialogue):
    """It travels in its own field, and every renderer prints it in front.

    Leaving it in the text too printed it twice:
    "LAUREN: LAUREN: What, the Cheshire grapevine?".
    """
    line = build_lines([_cue(1.0, caption)], None, checked=False)[0]
    assert line.speaker == speaker
    assert line.text == dialogue


def test_a_line_without_a_speaker_is_untouched():
    line = build_lines([_cue(1.0, "- Erm, yeah, I am.")], None, checked=False)[0]
    assert line.speaker is None
    assert line.text == "- Erm, yeah, I am."


def test_rendering_prints_the_speaker_exactly_once():
    cues = [_cue(1.0, "LYSTRA: Through the grapevine I heard that she's pregnant.")]
    result = TranscriptResult("p.scc", build_lines(cues, None, checked=False))
    for output_format in ("text", "prose", "markdown", "csv", "json", "srt", "vtt", "html"):
        payload = render(result, output_format)
        assert payload.count("LYSTRA") == 1, f"{output_format} repeated the speaker"


@pytest.mark.parametrize(
    "caption, expected",
    [
        ("[theme music]", NOT_DIALOGUE),
        ("[cheering]", NOT_DIALOGUE),
        ("\u266a\u266a", NOT_DIALOGUE),
        ("[rock music, cheering]", NOT_DIALOGUE),
        ("- Oh.", TOO_SHORT),
        ("Wow!", TOO_SHORT),
        ("of Cheshire--", TOO_SHORT),
        ("- More people than you think know.", UNMATCHED),
    ],
)
def test_lines_the_matcher_cannot_attempt_are_not_reported_as_missing(caption, expected):
    """"Not found in audio" accuses the file of a fault never tested for.

    A sound effect is never spoken, and a one-word cue is below the anchor
    threshold, so the aligner declines it outright. On a real delivery these
    were 40% of the lines and every one read as a failure.
    """
    line = build_lines([_cue(1.0, caption)], None, tolerance_ms=200.0, checked=True)[0]
    assert line.status == expected


def test_untestable_lines_are_excluded_from_the_match_rate():
    cues = [
        _cue(1.0, "[theme music]"),
        _cue(2.0, "- Oh."),
        _cue(3.0, "More people than you think know"),
        _cue(4.0, "Shipping containers along the northern pier"),
    ]
    result = TranscriptResult(
        "p.scc", build_lines(cues, None, tolerance_ms=200.0, checked=True),
        video_path="p.mov", summary={"median_offset": 0.0},
    )

    assert result.untestable == 2
    assert result.testable == 2
    assert result.unmatched == 2
    # Two of two testable lines were looked for; the sound effect and the
    # one-worder must not drag the rate down.
    assert result.match_rate == 0.0
    assert "2 further lines carry no dialogue" in result.verdict_sentence()


def test_json_export_is_machine_readable():
    import json

    cues = [_cue(1.0, "Hello", end=2.0)]
    result = TranscriptResult("p.vtt", build_lines(cues, None, checked=False), source_kind="vtt")
    payload = json.loads(render(result, "json"))
    assert payload["counts"]["lines"] == 1
    assert payload["checked_against_audio"] is False
    assert payload["lines_detail"][0]["text"] == "Hello"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
