#!/usr/bin/env python3
"""Tests for comparing two subtitle files for the same programme.

Run from this directory:  python3 -m pytest test__subtitle_compare.py -v

Two things decide whether this feature is usable rather than merely correct.

The first is that the alignment is by text, not by index. A single cue inserted
at the head shifts every later cue by one, and a positional diff reports the
whole programme as changed - which is the point at which nobody reads the diff.

The second is that it separates a script change from a smart-quote conversion.
Both are differences; only one of them is news.
"""

import random
import sys
from fractions import Fraction
from pathlib import Path

import pytest

PYTHON_DIR = Path(__file__).resolve().parents[2] / "python"
sys.path.insert(0, str(PYTHON_DIR))

from subtitle_compare import (  # noqa: E402
    FAIL,
    FORMATTING,
    ONLY_A,
    ONLY_B,
    PASS,
    SAME,
    TEXT,
    TIMING,
    WARN,
    WORDING,
    compare_subtitles,
    render_text_report,
    validate_document,
)
from subtitle_formats import read_subtitle_document  # noqa: E402


LINES = [
    "Roger, are you seeing this?",
    "It's coming from the north ridge.",
    "Get everyone back inside.",
    "I said inside, now!",
    "We've got maybe ten minutes.",
    "That's not enough time.",
    "It'll have to be.",
    "Then we do it in the dark.",
    "Watch the treeline.",
    "Go now!",
]


def _stamp(seconds):
    total = int(round(float(seconds) * 1000))
    hours, rest = divmod(total, 3600000)
    minutes, rest = divmod(rest, 60000)
    secs, milliseconds = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def _grid(count, rate=Fraction(25), seed=1):
    """A fixed set of in/out times, so an edit changes text and nothing else."""
    rng = random.Random(seed)
    slots, frame = [], 250
    for _ in range(count):
        duration = rng.randint(30, 70)
        slots.append((float(Fraction(frame) / rate), float(Fraction(frame + duration) / rate)))
        frame += duration + rng.randint(10, 50)
    return slots


GRID = _grid(len(LINES) + 6)


def _srt(entries):
    return "\n".join(
        f"{index}\n{_stamp(start)} --> {_stamp(end)}\n{text}\n"
        for index, (start, end, text) in enumerate(entries, start=1)
    )


def _base():
    return [(GRID[index][0], GRID[index][1], text) for index, text in enumerate(LINES)]


def _write(tmp_path, name, entries):
    target = tmp_path / name
    target.write_text(_srt(entries))
    return target


def _compare(tmp_path, entries_a, entries_b, **kwargs):
    return compare_subtitles(
        _write(tmp_path, "a.srt", entries_a), _write(tmp_path, "b.srt", entries_b), **kwargs
    )


# ---------------------------------------------------------------------------
# Alignment
# ---------------------------------------------------------------------------


def test_identical_files_report_no_differences(tmp_path):
    result = _compare(tmp_path, _base(), _base())
    assert result.differences() == []
    assert result.stats["identical"] == len(LINES)


def test_a_cue_inserted_at_the_head_does_not_shift_every_later_cue(tmp_path):
    """The reason the alignment is by text rather than by index.

    Positionally, inserting one cue makes cue 2 of A line up with cue 3 of B and
    every remaining line reads as changed. It should be one addition.
    """
    changed = _base()
    changed.insert(0, (GRID[len(LINES)][0], GRID[len(LINES)][1], "Previously, on the show..."))
    # Keep the file in time order: the inserted cue takes the first slot and the
    # rest move down, which is what a real re-export would do.
    changed = [
        (GRID[index][0], GRID[index][1], text) for index, (_, _, text) in enumerate(changed)
    ]

    result = _compare(tmp_path, _base(), changed)
    assert result.stats["only_in_b"] == 1
    assert result.stats["only_in_a"] == 0
    assert result.stats["wording_changed"] == 0


def test_a_reworded_cue_is_one_change_with_an_inline_word_diff(tmp_path):
    changed = _base()
    changed[3] = (changed[3][0], changed[3][1], "I said get inside, right now!")

    result = _compare(tmp_path, _base(), changed)
    edits = result.of_kind(TEXT)
    assert len(edits) == 1
    assert edits[0].change == WORDING
    assert ("insert", "get") in edits[0].words
    assert ("insert", "right") in edits[0].words


def test_a_removed_cue_is_reported_once(tmp_path):
    changed = _base()
    del changed[6]

    result = _compare(tmp_path, _base(), changed)
    assert result.stats["only_in_a"] == 1
    assert result.of_kind(ONLY_A)[0].cue_a.text == LINES[6]


def test_two_unrelated_lines_are_a_removal_and_an_addition_not_an_edit(tmp_path):
    """A cue replaced by something with no words in common is not "edited".

    Reporting it as one change produces a word diff in which everything is
    deleted and everything is inserted, which is harder to read than saying one
    line went and another arrived.
    """
    changed = _base()
    changed[4] = (changed[4][0], changed[4][1], "Completely different dialogue entirely")

    result = _compare(tmp_path, _base(), changed)
    assert result.stats["only_in_a"] == 1
    assert result.stats["only_in_b"] == 1
    assert result.stats["wording_changed"] == 0


# ---------------------------------------------------------------------------
# Wording versus formatting
# ---------------------------------------------------------------------------


def test_a_punctuation_change_is_separated_from_a_script_change(tmp_path):
    """Both are differences. Only one of them is a script change.

    A file re-exported through another tool converts every apostrophe in the
    programme; burying the real edits among those is how a diff stops being read.
    """
    changed = _base()
    changed[1] = (changed[1][0], changed[1][1], "It’s coming from the north ridge.")
    changed[8] = (changed[8][0], changed[8][1], "Watch the treeline…")
    changed[3] = (changed[3][0], changed[3][1], "I said get inside, right now!")

    result = _compare(tmp_path, _base(), changed)
    assert result.stats["formatting_changed"] == 2
    assert result.stats["wording_changed"] == 1

    formatting = [diff for diff in result.of_kind(TEXT) if diff.change == FORMATTING]
    assert {diff.index_a for diff in formatting} == {2, 9}


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------


def _shifted(entries, by):
    return [(start + by, end + by, text) for start, end, text in entries]


def _scaled(entries, factor):
    return [(start * factor, end * factor, text) for start, end, text in entries]


def test_a_constant_shift_is_reported_as_an_offset_not_drift(tmp_path):
    result = _compare(tmp_path, _base(), _shifted(_base(), 10.0))
    check = next(check for check in result.checks if check.name == "Timing relationship")
    assert check.status == WARN
    assert "offset" in check.headline
    assert check.data["median_offset"] == pytest.approx(10.0, abs=0.01)


def test_a_growing_difference_is_reported_as_drift_and_names_the_rate_pair(tmp_path):
    """A file conformed 25 -> 23.976 without retiming runs long by exactly 25/23.976.

    Naming the pair is the difference between a fixable ticket and an argument.
    """
    result = _compare(tmp_path, _base(), _scaled(_base(), 25 / 23.976))
    check = next(check for check in result.checks if check.name == "Timing relationship")
    assert check.status == FAIL
    assert "drift" in check.headline
    assert any("23.976" in text for text in check.data["rate_explanations"])


def test_scattered_retiming_is_not_reported_as_drift(tmp_path):
    """A straight line fits anything. It only means drift when the cues sit on it.

    Cues nudged individually produce a slope too, and calling that a rate error
    sends someone hunting for a conform problem that does not exist.
    """
    rng = random.Random(4)
    changed = [
        (start + rng.uniform(-1.5, 1.5), end, text) for start, end, text in _base()
    ]

    result = _compare(tmp_path, _base(), changed)
    check = next(check for check in result.checks if check.name == "Timing relationship")
    assert check.status == WARN
    assert "retimed by varying amounts" in check.headline


def test_a_shift_inside_tolerance_is_not_a_difference(tmp_path):
    result = _compare(tmp_path, _base(), _shifted(_base(), 0.05), tolerance_ms=200)
    assert result.of_kind(TIMING) == []
    assert result.stats["identical"] == len(LINES)


def test_a_uniform_shift_is_the_baseline_not_a_list_of_retimed_cues(tmp_path):
    """The whole file moving is one fact, not one fact per cue.

    Reported per cue, a ten-second pre-roll difference marks every line in the
    programme as retimed, and the one cue that genuinely moved is lost in it.
    """
    result = _compare(tmp_path, _base(), _shifted(_base(), 10.0), tolerance_ms=10)
    assert result.of_kind(TIMING) == []
    assert result.stats["baseline_offset"] == pytest.approx(10.0, abs=0.01)

    check = next(check for check in result.checks if check.name == "Timing relationship")
    assert "offset" in check.headline


def test_a_cue_that_departs_from_the_file_wide_offset_is_retimed(tmp_path):
    """One cue out of step with the rest is exactly what should surface."""
    changed = _shifted(_base(), 10.0)
    changed[4] = (changed[4][0] + 0.8, changed[4][1] + 0.8, changed[4][2])

    result = _compare(tmp_path, _base(), changed, tolerance_ms=200)
    retimed = result.of_kind(TIMING)
    assert len(retimed) == 1
    assert retimed[0].index_a == 5
    # Reported as its departure from the baseline, not as the raw 10.8s gap.
    assert retimed[0].relative_start == pytest.approx(0.8, abs=0.01)
    assert retimed[0].delta_start == pytest.approx(10.8, abs=0.01)


# ---------------------------------------------------------------------------
# Validity, and the sanity check on the whole comparison
# ---------------------------------------------------------------------------


def test_cues_that_run_backwards_fail_validation(tmp_path):
    entries = _base()
    entries[4], entries[5] = entries[5], entries[4]
    target = _write(tmp_path, "broken.srt", entries)

    checks = validate_document(read_subtitle_document(target), "A")
    order = next(check for check in checks if check.name == "A: cue order")
    assert order.status == FAIL


def test_a_cue_that_clears_before_it_appears_fails_validation(tmp_path):
    entries = _base()
    entries[2] = (entries[2][0], entries[2][0] - 1.0, entries[2][2])
    target = _write(tmp_path, "inverted.srt", entries)

    checks = validate_document(read_subtitle_document(target), "A")
    duration = next(check for check in checks if check.name == "A: cue duration")
    assert duration.status == FAIL


def test_overlapping_cues_are_flagged(tmp_path):
    entries = _base()
    entries[3] = (entries[3][0], entries[4][0] + 2.0, entries[3][2])
    target = _write(tmp_path, "overlap.srt", entries)

    checks = validate_document(read_subtitle_document(target), "A")
    overlap = next(check for check in checks if check.name == "A: overlap")
    assert overlap.status == WARN


def test_two_different_programmes_are_called_out_before_anything_else(tmp_path):
    """Every number downstream of the alignment is noise if the files are unrelated."""
    other = [
        (GRID[index][0], GRID[index][1], f"Unrelated dialogue number {index} from another show")
        for index in range(len(LINES))
    ]

    result = _compare(tmp_path, _base(), other)
    check = next(check for check in result.checks if check.name == "Same programme")
    assert check.status == FAIL
    assert result.verdict == FAIL


def test_an_unreadable_file_is_an_error_not_a_comparison(tmp_path):
    broken = tmp_path / "broken.srt"
    broken.write_text("this is not a subtitle file\n")

    result = compare_subtitles(_write(tmp_path, "a.srt", _base()), broken)
    assert result.errors
    assert result.verdict == FAIL
    assert result.diffs == []


# ---------------------------------------------------------------------------
# Formats and reports
# ---------------------------------------------------------------------------


def test_the_two_files_need_not_be_the_same_format(tmp_path):
    """Comparing an SRT delivery against the approved TTML is the normal case."""
    source = tmp_path / "a.srt"
    source.write_text(_srt(_base()))

    cues = "".join(
        f'<p begin="{start:.3f}s" end="{end:.3f}s">{text}</p>'
        for start, end, text in _base()
    )
    target = tmp_path / "b.ttml"
    target.write_text(
        '<?xml version="1.0"?><tt xmlns="http://www.w3.org/ns/ttml"><body><div>'
        + cues
        + "</div></body></tt>"
    )

    result = compare_subtitles(source, target)
    assert result.stats["identical"] == len(LINES)
    assert result.file_b.kind == "ttml"


def test_the_text_report_names_every_kind_of_difference(tmp_path):
    changed = _base()
    changed[3] = (changed[3][0], changed[3][1], "I said get inside, right now!")
    changed[5] = (changed[5][0] + 0.6, changed[5][1] + 0.6, changed[5][2])
    del changed[6]

    report = render_text_report(_compare(tmp_path, _base(), changed))
    assert "SUBTITLE COMPARISON" in report
    assert "CHANGED" in report
    assert "RETIMED" in report
    assert "ONLY IN A" in report


# ---------------------------------------------------------------------------
# The decoded formats: SCC, MCC and containers
# ---------------------------------------------------------------------------


MEDIA = Path(__file__).resolve().parents[1] / "media"
SCC_FIXTURE = MEDIA / "Plan9fromOuterSpace.scc"
MCC_FIXTURE = MEDIA / "Plan9fromOuterSpace.mcc"

# The decoded formats need the C shared library, which is not built everywhere.
decoded = pytest.mark.skipif(
    not SCC_FIXTURE.exists(), reason="SCC fixture not present"
)


def _restamp_scc(source, target, add_hours):
    """The same SCC, stamped against a tape that starts `add_hours` in."""
    import re

    def bump(match):
        return f"{int(match.group(1)) + add_hours:02d}:{match.group(2)}:{match.group(3)}{match.group(4)}{match.group(5)}"

    body = re.sub(
        r"^(\d{2}):(\d{2}):(\d{2})([:;])(\d{2})",
        bump,
        source.read_text(),
        flags=re.MULTILINE,
    )
    target.write_text(body)
    return target


@decoded
def test_an_scc_can_be_read_for_comparison():
    """The point of the exercise: an SCC is a subtitle file like any other here."""
    from caption_cues import read_comparable_document

    document = read_comparable_document(SCC_FIXTURE)
    assert document.kind == "scc"
    assert len(document.cues) > 100
    assert document.header["track"].startswith("CEA-608")
    # No rate was supplied; it came off the file's own timecodes.
    assert "29.97" in document.header["read_at"]


@decoded
def test_an_scc_carries_no_out_times_and_the_checks_say_so():
    """608 clears on a control code. A duration check that never ran must not PASS.

    Reporting "every cue is on screen for a readable time" about a file with no
    out-times is a clean bill of health for a test that was skipped.
    """
    from caption_cues import read_comparable_document

    checks = validate_document(read_comparable_document(SCC_FIXTURE), "A")
    duration = next(check for check in checks if check.name == "A: cue duration")
    assert duration.status not in (PASS, FAIL)
    assert "no cue out-times" in duration.headline
    assert not any(check.name == "A: overlap" for check in checks)


@decoded
def test_an_scc_compares_against_an_scc(tmp_path):
    """Two deliveries of the same episode, one stamped an hour into the tape.

    The text is identical, so the only finding should be the origin - and it
    should be named as the origin rather than reported as an hour of drift.
    """
    tape = _restamp_scc(SCC_FIXTURE, tmp_path / "tape.scc", add_hours=1)
    result = compare_subtitles(SCC_FIXTURE, tape)

    assert result.stats["wording_changed"] == 0
    assert result.stats["only_in_a"] == 0
    assert result.stats["only_in_b"] == 0
    assert result.of_kind(TIMING) == []
    assert result.stats["baseline_offset"] == pytest.approx(3600, abs=2)


@decoded
def test_an_scc_compares_against_a_text_subtitle(tmp_path):
    """The normal case: check the SRT of an episode against its SCC."""
    from caption_cues import read_comparable_document

    document = read_comparable_document(SCC_FIXTURE)
    entries = [(cue.start, cue.start + 2.0, cue.text) for cue in document.cues]
    sidecar = tmp_path / "episode.srt"
    sidecar.write_text(_srt(entries))

    result = compare_subtitles(SCC_FIXTURE, sidecar)
    assert result.stats["wording_changed"] == 0
    assert result.stats["identical"] == len(document.cues)
    assert result.file_a.is_program_timecode is True
    assert result.file_b.is_program_timecode is False


@decoded
def test_a_different_timecode_origin_is_not_reported_as_a_sync_error(tmp_path):
    """An SCC stamped from 01:00:00:00 is not an hour out of sync with an SRT.

    Every cue is an hour from its counterpart, and saying so 664 times - or
    calling it a sync failure once - is equally useless.
    """
    from caption_cues import read_comparable_document

    document = read_comparable_document(SCC_FIXTURE)
    entries = [(cue.start, cue.start + 2.0, cue.text) for cue in document.cues]
    sidecar = tmp_path / "episode.srt"
    sidecar.write_text(_srt(entries))

    tape = _restamp_scc(SCC_FIXTURE, tmp_path / "tape.scc", add_hours=1)
    result = compare_subtitles(tape, sidecar)

    check = next(check for check in result.checks if check.name == "Timing relationship")
    assert check.status not in (FAIL, WARN)
    assert "different origins" in check.headline
    assert "tape origin rather than a sync error" in " ".join(check.detail)
    assert result.of_kind(TIMING) == []


@decoded
@pytest.mark.skipif(not MCC_FIXTURE.exists(), reason="MCC fixture not present")
def test_an_scc_and_an_mcc_of_the_same_film_carry_the_same_words():
    """Same film, authored at 29.97 and at 24. The words should not differ."""
    result = compare_subtitles(SCC_FIXTURE, MCC_FIXTURE)
    assert result.stats["wording_changed"] == 0
    assert result.stats["only_in_a"] == 0
    assert result.stats["only_in_b"] == 0

    rates = next(check for check in result.checks if check.name == "Frame rate")
    assert rates.status == WARN
    assert "29.97" in rates.headline and "24" in rates.headline


SCC_TRANSPORT_MEDIA = MEDIA / "scc_transport"
SHORT_SCC_A = SCC_TRANSPORT_MEDIA / "blu_working.scc"
SHORT_SCC_B = SCC_TRANSPORT_MEDIA / "2g_original_failing.scc"


@decoded
@pytest.mark.skipif(not SHORT_SCC_A.exists(), reason="scc_transport fixtures not present")
def test_a_short_scc_needs_an_explicit_rate_to_compare():
    """These fixtures' cues are minutes apart - too sparse to infer a rate from,
    and SCC declares none. Without an override, compare_subtitles reports it as
    an error rather than guessing a rate to decode with.
    """
    result = compare_subtitles(SHORT_SCC_A, SHORT_SCC_B)
    assert result.errors
    assert "could not be decoded" in result.errors[0]


@decoded
@pytest.mark.skipif(not SHORT_SCC_A.exists(), reason="scc_transport fixtures not present")
def test_rate_override_lets_a_short_scc_compare():
    """The same pair, given the rate explicitly - the fallback the Inspect
    tab's frame rate dropdown already offers, now available here too.
    """
    result = compare_subtitles(SHORT_SCC_A, SHORT_SCC_B, rate_code_a=2400, rate_code_b=2400)
    assert not result.errors
    assert result.file_a.document.kind == "scc"
    assert result.file_b.document.kind == "scc"
