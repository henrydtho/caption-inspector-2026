#!/usr/bin/env python3
"""Tests for reading a file's frame rate off the file.

Run from this directory:  python3 -m pytest test__frame_rate_detect.py -v

The app no longer asks which frame rate a delivery is in. That only works if
the detector is right about three separate things: what a file states, what its
timecode frame field rules out, and - for the wall-clock formats, which have no
rate field at all - which frame grid its stamps sit on.

The tests that matter most here are the negative ones. A detector that answers
"25 fps" to a file typed at whole seconds is worse than one that says it does
not know, because the wrong number is acted on.
"""

import random
import sys
from fractions import Fraction
from pathlib import Path

import pytest

PYTHON_DIR = Path(__file__).resolve().parents[2] / "python"
sys.path.insert(0, str(PYTHON_DIR))

from frame_rate_detect import (  # noqa: E402
    AMBIGUOUS,
    DECLARED,
    INFERRED,
    UNKNOWN,
    detect_frame_rate,
    infer_from_frame_field,
    infer_from_quantisation,
)


RATES = {
    2397: Fraction(24000, 1001),
    2400: Fraction(24, 1),
    2500: Fraction(25, 1),
    2997: Fraction(30000, 1001),
    3000: Fraction(30, 1),
    5000: Fraction(50, 1),
    5994: Fraction(60000, 1001),
    6000: Fraction(60, 1),
}


def _stamp(seconds):
    total = int(round(float(seconds) * 1000))
    hours, rest = divmod(total, 3600000)
    minutes, rest = divmod(rest, 60000)
    secs, milliseconds = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def srt_on_grid(rate, count=80, seed=3):
    """An SRT authored on `rate`'s frame grid, at irregular frame numbers.

    Irregular on purpose: cues at round frame counts land on several grids at
    once, which is a property of the fixture rather than of the format.
    """
    rng = random.Random(seed)
    blocks, frame = [], 137
    for index in range(1, count + 1):
        duration = rng.randint(int(rate), int(rate * 4))
        blocks.append(
            f"{index}\n{_stamp(Fraction(frame) / rate)} --> "
            f"{_stamp(Fraction(frame + duration) / rate)}\nLine {index} of dialogue\n"
        )
        frame += duration + rng.randint(3, int(rate * 3))
    return "\n".join(blocks)


def scc_at(counting, drop_frame, rows=120, seed=5):
    rng = random.Random(seed)
    separator = ";" if drop_frame else ":"
    lines = ["Scenarist_SCC V1.0", ""]
    frame = counting * 3600
    for _ in range(rows):
        total = frame
        frames = total % counting
        total //= counting
        seconds = total % 60
        total //= 60
        minutes, hours = total % 60, total // 60
        lines.append(
            f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{frames:02d}\t"
            "9420 9420 94ae 94ae 9452 c8e5 ecec ef80 942f 942f"
        )
        lines.append("")
        frame += rng.randint(counting, counting * 4)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Wall-clock formats: which frame grid do the stamps sit on?
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", sorted(RATES))
def test_every_supported_rate_is_read_back_off_an_srt(tmp_path, code):
    """SRT has no frame-rate field, but a converted master leaves its grid behind."""
    target = tmp_path / f"a{code}.srt"
    target.write_text(srt_on_grid(RATES[code]))

    detection = detect_frame_rate(target)
    assert detection.rate_code == code
    assert detection.confidence in (INFERRED, AMBIGUOUS)


def test_a_lower_rate_wins_over_the_multiple_that_also_fits():
    """Every 25 fps frame is also a 50 fps frame, so both grids fit.

    The lower rate is the one the file was authored at; reporting 50 because it
    also explains the stamps would be true and useless.
    """
    times = [float(Fraction(frame) / 25) for frame in range(1, 200, 7)]
    candidates, _ = infer_from_quantisation(times, precision_us=1_000)
    assert candidates[0] == 2500
    assert 5000 in candidates


def test_whole_second_stamps_are_reported_as_no_evidence(tmp_path):
    """A file typed at round seconds fits 24, 25, 30, 50 and 60 at once.

    Picking one of them would be inventing a fact from rounding.
    """
    blocks = [
        f"{index}\n00:00:{index * 3:02d},000 --> 00:00:{index * 3 + 2:02d},000\nLine {index}\n"
        for index in range(1, 25)
    ]
    target = tmp_path / "round.srt"
    target.write_text("\n".join(blocks))

    detection = detect_frame_rate(target)
    assert detection.rate_code is None
    assert detection.confidence == UNKNOWN
    assert "no frame-rate evidence" in " ".join(detection.notes)


def test_stamps_on_no_grid_at_all_are_reported_as_wall_clock(tmp_path):
    rng = random.Random(11)
    blocks, position = [], 1.0
    for index in range(1, 45):
        blocks.append(
            f"{index}\n{_stamp(position)} --> {_stamp(position + 1.437)}\nLine {index}\n"
        )
        position += rng.uniform(1.71, 2.93)
    target = tmp_path / "arbitrary.srt"
    target.write_text("\n".join(blocks))

    detection = detect_frame_rate(target)
    assert detection.rate_code is None
    assert detection.is_wall_clock


def test_too_few_stamps_is_not_evidence():
    candidates, notes = infer_from_quantisation([1.0, 2.0, 3.0], precision_us=1_000)
    assert candidates == []
    assert "too few" in " ".join(notes)


# ---------------------------------------------------------------------------
# Timecode formats: what does the frame field rule out?
# ---------------------------------------------------------------------------


def test_scc_drop_frame_marker_settles_ntsc(tmp_path):
    """A `;` separator exists only for the 1001-denominator rates."""
    target = tmp_path / "a.scc"
    target.write_text(scc_at(30, drop_frame=True))

    detection = detect_frame_rate(target)
    assert detection.rate_code == 2997
    assert detection.drop_frame is True
    assert detection.confidence == INFERRED


def test_scc_frame_numbers_rule_out_lower_rates(tmp_path):
    """A file using frame 24 is not 24 fps, whatever anyone says it is."""
    target = tmp_path / "b.scc"
    target.write_text(scc_at(25, drop_frame=False))

    detection = detect_frame_rate(target)
    assert detection.rate_code == 2500
    assert detection.confidence == INFERRED


def test_non_drop_ntsc_is_reported_as_ambiguous(tmp_path):
    """29.97 and 30 count frames identically; the timecodes cannot separate them."""
    target = tmp_path / "c.scc"
    target.write_text(scc_at(30, drop_frame=False))

    detection = detect_frame_rate(target)
    assert detection.confidence == AMBIGUOUS
    assert set(detection.candidates) == {2997, 3000}


def test_a_short_file_does_not_trust_its_highest_frame_number():
    """With eight rows, never reaching frame 29 says nothing about the rate."""
    candidates, notes = infer_from_frame_field([0, 4, 11, 19, 22, 27, 8, 15], drop_frame=False)
    assert candidates == []
    assert "too few" in " ".join(notes)


# ---------------------------------------------------------------------------
# Declarations, and declarations that are wrong
# ---------------------------------------------------------------------------


def _ttml(declared, grid_rate, count=60):
    rng = random.Random(9)
    cues, frame = [], 240

    def clock(seconds):
        total = int(round(seconds * 1000))
        hours, rest = divmod(total, 3600000)
        minutes, rest = divmod(rest, 60000)
        secs, milliseconds = divmod(rest, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{milliseconds:03d}"

    for index in range(count):
        duration = rng.randint(int(grid_rate), int(grid_rate * 3))
        begin = float(Fraction(frame) / grid_rate)
        end = float(Fraction(frame + duration) / grid_rate)
        cues.append(f'<p begin="{clock(begin)}" end="{clock(end)}">Line {index}</p>')
        frame += duration + rng.randint(4, int(grid_rate * 2))

    return (
        '<?xml version="1.0"?><tt xmlns="http://www.w3.org/ns/ttml" '
        'xmlns:ttp="http://www.w3.org/ns/ttml#parameter" '
        f'ttp:frameRate="{declared}"><body><div>' + "".join(cues) + "</div></body></tt>"
    )


def test_a_declared_rate_is_reported_as_declared(tmp_path):
    target = tmp_path / "a.ttml"
    target.write_text(_ttml(25, Fraction(25)))

    detection = detect_frame_rate(target)
    assert detection.rate_code == 2500
    assert detection.confidence == DECLARED
    assert detection.conflict is False


def test_a_declared_rate_the_stamps_contradict_is_a_conflict(tmp_path):
    """The vendor error the whole Sync QC tab exists to catch, found in one file.

    A TTML labelled 25 whose cues sit on a 29.97 grid is mislabelled, and it
    will drift against picture no matter which of the two rates is used.
    """
    target = tmp_path / "b.ttml"
    target.write_text(_ttml(25, Fraction(30000, 1001)))

    detection = detect_frame_rate(target)
    assert detection.conflict is True
    assert detection.declared_code == 2500
    assert detection.inferred_code == 2997
    assert "do not agree" in " ".join(detection.notes)


def test_an_assumed_rate_is_not_reported_as_declared(tmp_path):
    """Spruce STL states no rate, and the reader's fallback is not a declaration.

    `caption_timing` fills one in so the file can be read at all. Reporting that
    back as "the file says 30" puts a guess in front of someone as a fact.
    """
    target = tmp_path / "c.stl"
    target.write_text(
        "$TapeOffset = FALSE\n"
        + "\n".join(
            f"00:00:{index:02d}:{index % 24:02d} , 00:00:{index + 2:02d}:00 , Line {index}"
            for index in range(1, 40)
        )
    )

    detection = detect_frame_rate(target)
    assert detection.confidence != DECLARED
    assert detection.declared_code is None


def test_an_unreadable_file_reports_an_error_rather_than_a_rate(tmp_path):
    target = tmp_path / "d.srt"
    target.write_text("this is not a subtitle file\n")

    detection = detect_frame_rate(target)
    assert detection.rate_code is None
    assert detection.error
