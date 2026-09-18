#!/usr/bin/env python3
"""Tests for the v3 caption sync QC tiers.

Run from this directory:  python3 -m pytest test__sync_check.py -v

Tier 1 tests build real SCC fixtures and (when ffmpeg is present) real video, so
the checks run against files rather than mocks. Tier 2 tests drive the alignment
and regression math with a synthetic transcript, which keeps them fast and
removes any dependency on faster-whisper or a model download.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PYTHON_DIR = Path(__file__).resolve().parents[2] / "python"
sys.path.insert(0, str(PYTHON_DIR))

from alignment import (  # noqa: E402
    align_cues_to_transcript,
    describe_drift,
    summarize_alignment,
    theil_sen,
)
from caption_cues import Cue, normalize_caption_text  # noqa: E402
from caption_timing import read_caption_timings  # noqa: E402
from sync_check import FAIL, PASS, WARN, tier1_check  # noqa: E402
from timecode import (  # noqa: E402
    explain_timing_ratio,
    frames_to_timecode,
    rate_code_for,
    timecode_to_frames,
    timecode_to_seconds,
)
from transcribe import Transcript, Word  # noqa: E402


HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

# Deliberately varied dialogue: caption text has to be distinctive enough to
# anchor against a transcript, and repeated boilerplate would not test that.
DIALOGUE = [
    "welcome back to the evening broadcast",
    "our correspondent reports from the harbour tonight",
    "shipping containers stacked along the northern pier",
    "the mayor declined to comment this afternoon",
    "meteorologists expect heavy rainfall by thursday",
    "local farmers describe the harvest as disappointing",
    "engineers finished the bridge inspection yesterday",
    "the museum reopens after eighteen months of renovation",
    "a record number of visitors attended the festival",
    "traffic on the coastal road remains heavily congested",
    "researchers published their findings in a journal",
    "the orchestra performed three encores last night",
]


def parity(byte):
    """608 uses odd parity in the high bit."""
    return byte if bin(byte).count("1") % 2 else byte | 0x80


def encode_caption_text(text):
    data = [parity(ord(character)) for character in text.upper()]
    if len(data) % 2:
        data.append(0x80)
    return [f"{data[index]:02x}{data[index + 1]:02x}" for index in range(0, len(data), 2)]


def caption_payload(text):
    """RCL, ENM, PAC row 15, the text, then EOC - each doubled, as SCC requires."""
    words = ["9420", "9420", "94ae", "94ae", "9470", "9470"]
    for word in encode_caption_text(text):
        words.extend([word, word])
    words.extend(["942f", "942f"])
    return " ".join(words)


def write_scc(path, rate_code, drop_frame, count, first_second, interval, scale=1.0, offset=0.0):
    """Write an SCC whose cues sit at known content times.

    `scale` models the vendor bug: the same content point stamped as if the
    program ran at a different rate. `offset` models a head-based master.
    """
    from timecode import RATE_BY_CODE

    rate = float(RATE_BY_CODE[rate_code])
    lines = ["Scenarist_SCC V1.0", ""]
    content_times = []

    for index in range(count):
        content_second = first_second + index * interval
        content_times.append(content_second)
        stamped = content_second * scale + offset
        timecode = frames_to_timecode(round(stamped * rate), rate, drop_frame)
        lines.append(f"{timecode}\t{caption_payload(DIALOGUE[index % len(DIALOGUE)])}")
        lines.append("")
        clear = round((stamped + interval * scale * 0.6) * rate)
        lines.append(f"{frames_to_timecode(clear, rate, drop_frame)}\t942c 942c")
        lines.append("")

    Path(path).write_text("\n".join(lines), encoding="utf-8")
    return content_times


def make_video(path, duration, rate="25", with_audio=True):
    command = [
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"color=c=black:s=160x90:r={rate}",
    ]
    if with_audio:
        command += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono"]
    command += [
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
    ]
    if with_audio:
        command += ["-c:a", "aac", "-shortest"]
    command.append(str(path))
    subprocess.run(command, check=True, capture_output=True)
    return path


# --------------------------------------------------------------------------
# Timecode math
# --------------------------------------------------------------------------


def test_drop_frame_skips_two_frames_per_minute():
    # Frame 1800 is labelled 00:01:00;02 because ;00 and ;01 are skipped.
    assert timecode_to_frames("00:01:00;02", 30000 / 1001, drop_frame=True) == 1800
    # Every tenth minute keeps its frames, which is what makes drop-frame exact.
    assert timecode_to_frames("00:10:00;00", 30000 / 1001, drop_frame=True) == 17982
    assert timecode_to_seconds("00:10:00;00", 30000 / 1001, drop_frame=True) == pytest.approx(600.0, abs=0.001)


def test_non_drop_at_2997_runs_ahead_of_real_time():
    # The 0.1% error that makes a non-drop file look right at the head and late
    # at the tail: 3.6 seconds per hour.
    seconds = timecode_to_seconds("01:00:00:00", 30000 / 1001, drop_frame=False)
    assert seconds == pytest.approx(3603.6, abs=0.1)


def test_timecode_round_trips_in_both_conventions():
    for drop in (False, True):
        rate = 30000 / 1001
        for frames in (0, 1799, 1800, 17982, 108000):
            timecode = frames_to_timecode(frames, rate, drop)
            assert timecode_to_frames(timecode, rate, drop_frame=drop) == frames


def test_rate_code_matching_separates_2997_from_30():
    assert rate_code_for(30000 / 1001) == 2997
    assert rate_code_for(30.0) == 3000
    assert rate_code_for(29.970030) == 2997
    assert rate_code_for(24000 / 1001) == 2397


def test_timing_ratio_names_the_rate_pair():
    # 29.97 into 25 runs 1.1988x long; a file covering 99% of the program lands
    # a little under that, and the explanation should still find it.
    matches = explain_timing_ratio(1.1988 * 0.99)
    assert matches
    assert matches[0]["authored_code"] == 2997
    assert matches[0]["played_code"] == 2500
    assert matches[0]["implied_coverage"] == pytest.approx(0.99, abs=0.005)


def test_timing_ratio_rejects_implausible_coverage():
    # 0.62 is not near any rate pair ratio, so nothing should be offered.
    # (0.5 deliberately is one - 25 into 50 - and must still be explained.)
    assert explain_timing_ratio(0.62) == []


# --------------------------------------------------------------------------
# Caption file reading
# --------------------------------------------------------------------------


def test_scc_reader_flags_text_and_erase_rows(tmp_path):
    scc = tmp_path / "sample.scc"
    write_scc(scc, 2500, False, count=5, first_second=2.0, interval=4.0)

    timings = read_caption_timings(scc)
    assert timings.kind == "scc"
    assert len(timings.entries) == 10
    assert sum(1 for entry in timings.entries if entry.has_text) == 5
    assert sum(1 for entry in timings.entries if entry.is_erase) == 5
    assert timings.first_text_entry().timecode == "00:00:02:00"


def test_scc_reader_detects_drop_frame_from_separator(tmp_path):
    scc = tmp_path / "df.scc"
    write_scc(scc, 2997, True, count=4, first_second=2.0, interval=4.0)
    assert read_caption_timings(scc).drop_frame is True


# --------------------------------------------------------------------------
# Tier 1
# --------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_tier1_passes_a_matching_delivery(tmp_path):
    video = make_video(tmp_path / "program.mp4", duration=120)
    scc = tmp_path / "good.scc"
    write_scc(scc, 2500, False, count=25, first_second=3.0, interval=4.6)

    result = tier1_check(scc, video)
    assert not result.errors
    assert result.verdict == PASS, [
        (check.name, check.status, check.headline) for check in result.checks
    ]


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_tier1_catches_a_2997_file_delivered_as_25(tmp_path):
    """The headline bug: timed against one rate, delivered labelled as another."""
    video = make_video(tmp_path / "program.mp4", duration=120)
    scc = tmp_path / "mislabelled.scc"
    write_scc(scc, 2500, False, count=25, first_second=3.0, interval=4.6, scale=(30000 / 1001) / 25)

    result = tier1_check(scc, video)
    assert result.verdict == FAIL

    overrun = next(check for check in result.checks if check.name == "Program overrun")
    assert overrun.status == FAIL

    coverage = next(check for check in result.checks if check.name == "Coverage ratio")
    assert coverage.status == FAIL
    assert coverage.data["explanations"], "the rate pair behind the ratio should be named"
    best = coverage.data["explanations"][0]
    assert best["authored_code"] == 2997 and best["played_code"] == 2500


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_tier1_catches_an_hour_rebased_file(tmp_path):
    video = make_video(tmp_path / "program.mp4", duration=120)
    scc = tmp_path / "rebased.scc"
    write_scc(scc, 2500, False, count=25, first_second=3.0, interval=4.6, offset=3600.0)

    result = tier1_check(scc, video)
    start = next(check for check in result.checks if check.name == "Start reference")
    assert start.status == FAIL
    assert "hour" in start.headline.lower()


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_tier1_flags_non_drop_scc_on_an_ntsc_delivery(tmp_path):
    video = make_video(tmp_path / "program.mp4", duration=700, rate="30000/1001")
    scc = tmp_path / "ndf.scc"
    write_scc(scc, 2997, False, count=60, first_second=3.0, interval=11.0)

    result = tier1_check(scc, video)
    convention = next(check for check in result.checks if check.name == "Drop-frame convention")
    assert convention.status == WARN
    assert "non-drop" in convention.headline


def test_tier1_runs_without_a_video(tmp_path):
    scc = tmp_path / "solo.scc"
    write_scc(scc, 2500, False, count=6, first_second=1.0, interval=3.0)

    result = tier1_check(scc)
    assert not result.errors
    assert any(check.name == "Timecode ordering" for check in result.checks)
    assert result.verdict == PASS


def test_tier1_rejects_an_unreadable_caption_file(tmp_path):
    bogus = tmp_path / "notes.txt"
    bogus.write_text("this is not a caption file", encoding="utf-8")

    result = tier1_check(bogus)
    assert result.errors
    assert result.verdict == FAIL


# --------------------------------------------------------------------------
# Tier 2 alignment and drift regression
# --------------------------------------------------------------------------


WORD_POOL = (
    "harbour lantern gravel courtyard mineral ledger tunnel orchard beacon thistle "
    "granite compass mariner willow furnace cobbler pageant almanac quarry saffron "
    "trellis bramble cistern juniper meridian scaffold hollow parapet sorrel vellum "
    "kettle brindle wharf plover cinder marram gable spindle "
).split()


def build_cues(count=40, first_second=5.0, interval=12.0, seed=7):
    """Distinct dialogue per cue, the way a real program reads.

    A deterministic shuffle of an uncommon word pool, so each cue is genuinely
    findable. Repetitive text gets its own test rather than being baked in here.
    """
    import random

    generator = random.Random(seed)
    cues = []
    for index in range(count):
        seconds = first_second + index * interval
        frame = DIALOGUE[index % len(DIALOGUE)].split()[:3]
        picked = generator.sample(WORD_POOL, 5)
        text = " ".join(frame + picked)
        cues.append(Cue(frames_to_timecode(round(seconds * 25), 25.0, False), seconds, text, "pop-on"))
    return cues


def build_transcript(cues, offset=0.0, drift_per_second=0.0):
    """A transcript whose words sit at each cue's time, plus injected error.

    `drift_per_second` is the slope Tier 2 is supposed to recover: the audio
    lands progressively later than the caption claims.
    """
    words = []
    for cue in cues:
        audio_start = cue.seconds + offset + drift_per_second * cue.seconds
        for position, token in enumerate(cue.normalized.split()):
            start = audio_start + position * 0.35
            words.append(Word(token, start, start + 0.3))
    return Transcript(words, language="en", model_size="test")


def test_alignment_anchors_cues_when_in_sync():
    cues = build_cues()
    transcript = build_transcript(cues)

    matches = align_cues_to_transcript(cues, transcript)
    assert len(matches) >= len(cues) - 2

    summary = summarize_alignment(cues, matches)
    assert summary["median_offset"] == pytest.approx(0.0, abs=0.05)
    assert summary["drift"]["slope_ms_per_minute"] == pytest.approx(0.0, abs=2.0)


def test_alignment_measures_a_constant_offset_without_inventing_drift():
    cues = build_cues()
    transcript = build_transcript(cues, offset=1.25)

    summary = summarize_alignment(cues, align_cues_to_transcript(cues, transcript))
    assert summary["median_offset"] == pytest.approx(1.25, abs=0.05)
    # A pure shift must not read as a rate error, or the fix goes out wrong.
    assert abs(summary["drift"]["slope_ms_per_minute"]) < 5.0


def test_alignment_recovers_the_drift_slope():
    # 0.0005 s/s is 30 ms per minute: a small, realistic conform error.
    cues = build_cues(count=60, interval=20.0)
    transcript = build_transcript(cues, drift_per_second=0.0005)

    summary = summarize_alignment(cues, align_cues_to_transcript(cues, transcript))
    drift = summary["drift"]
    assert drift["slope_ms_per_minute"] == pytest.approx(30.0, abs=3.0)
    assert drift["r_squared"] > 0.95


def test_alignment_survives_a_gross_offset():
    """A file an hour out still has to find its anchors.

    Captions timed against a 01:00:00:00 head, dialogue on a zero-based file:
    every cue is a full hour away from its audio, far outside the fine pass's
    search window, so only the global coarse pass can rescue it.
    """
    cues = build_cues(count=40, interval=15.0)
    for cue in cues:
        cue.seconds += 3600.0
    transcript = build_transcript(cues, offset=-3600.0)

    summary = summarize_alignment(cues, align_cues_to_transcript(cues, transcript))
    assert summary["matched"] >= 30
    assert summary["median_offset"] == pytest.approx(-3600.0, abs=0.1)


def test_drift_fit_ignores_a_handful_of_bad_matches():
    cues = build_cues(count=50, interval=15.0)
    transcript = build_transcript(cues, drift_per_second=0.0005)
    matches = align_cues_to_transcript(cues, transcript)

    # Mismatches happen in real audio; a few must not bend the slope.
    for match in matches[::12]:
        match.offset += 9.0

    summary = summarize_alignment(cues, matches)
    assert summary["drift"]["slope_ms_per_minute"] == pytest.approx(30.0, abs=6.0)
    assert summary["drift"]["rejected"] > 0


def test_drift_ratio_names_the_same_rate_pair_as_tier_1():
    """Tier 1 and Tier 2 must not contradict each other on one file.

    A file authored against 29.97 and delivered at 25 runs long by 29.97/25.
    Tier 1 reports that as 1.1988. Tier 2 measures audio-minus-caption against
    caption time, which yields the reciprocal, so it has to invert before
    naming a rate pair - otherwise the report reads "25 into 29.97", the exact
    opposite of the truth, in the sentence the vendor gets shown.
    """
    stretch = 30000 / 1001 / 25

    cues = build_cues(count=40, interval=15.0)
    for cue in cues:
        cue.seconds *= stretch
    # Dialogue sits at the unstretched time the caption was authored from.
    transcript = build_transcript(cues, drift_per_second=(1.0 / stretch) - 1.0)

    drift = summarize_alignment(cues, align_cues_to_transcript(cues, transcript))["drift"]
    assert drift["r_squared"] > 0.99
    assert drift["implied_ratio"] == pytest.approx(stretch, rel=0.01)

    best = drift["explanations"][0]
    assert best["authored_code"] == 2997
    assert best["played_code"] == 2500
    assert "timed against 29.97 fps but delivered at 25 fps" in best["text"]


def test_drift_noise_is_not_reported_as_a_rate_mismatch():
    """A slope fitted to scatter must not be handed over as a diagnosis.

    Transcribed word onsets jitter by ~100 ms. Over a short program that jitter
    fits some nonzero slope, and naming a frame-rate pair off it sends a vendor
    chasing a fault that is not there.
    """
    import random

    generator = random.Random(11)
    cues = build_cues(count=12, interval=14.0)
    transcript = build_transcript(cues)
    matches = align_cues_to_transcript(cues, transcript)
    for match in matches:
        match.offset += generator.uniform(-0.12, 0.12)

    drift = summarize_alignment(cues, matches)["drift"]
    # Enough jitter to clear the flat 10 ms/minute gate, but no real slope.
    assert drift["r_squared"] < 0.5

    status, headline, detail = describe_drift(drift)
    body = " ".join([headline] + detail)
    # Either "inside the noise floor" or "fit is too poor to call" is honest.
    # Naming a frame-rate pair, or failing the file outright, is not.
    assert status in (PASS, WARN)
    assert "fps but delivered at" not in body
    assert "rate conversion" not in body


# ---------------------------------------------------------------------------
# Short cues, matched from their neighbours (v5.7)
# ---------------------------------------------------------------------------


def build_short_cue_script(reaction="yeah", count=6):
    """Long lines with a short reaction after each, the shape of reality TV."""
    longs = [
        "shipping containers stacked along the northern pier",
        "meteorologists expect heavy rainfall arriving by thursday",
        "the museum reopens after eighteen months of renovation",
        "engineers finished the bridge inspection yesterday afternoon",
        "researchers published their findings in a quarterly journal",
        "traffic on the coastal road remains heavily congested",
    ][:count]

    words, cues, clock = [], [], 2.0
    for text in longs:
        cues.append(Cue("00:00:00:00", clock, text, "scc"))
        for position, token in enumerate(text.split()):
            words.append(Word(token, clock + position * 0.32, clock + position * 0.32 + 0.3))
        clock += len(text.split()) * 0.32 + 1.5

        cues.append(Cue("00:00:00:00", clock, reaction, "scc"))
        words.append(Word(reaction, clock, clock + 0.3))
        clock += 3.0

    return cues, Transcript(words, language="en", model_size="test")


def test_short_cues_are_matched_from_their_neighbours():
    """One- and two-word cues used to be skipped outright.

    A programme of short reactions reported most of its lines unmatchable: the
    three-token floor exists so a global search never hunts for "yeah", but once
    the neighbours are anchored the position is boxed in and the search is safe.
    """
    cues, transcript = build_short_cue_script()
    matches = align_cues_to_transcript(cues, transcript)

    short_cues = [cue for cue in cues if len(cue.words) < 3]
    matched_short = [m for m in matches if len(m.cue.words) < 3]

    assert len(short_cues) == 6
    assert len(matched_short) == 6
    assert all(m.short_cue for m in matched_short)


def test_identical_short_cues_each_find_their_own_occurrence():
    """The ambiguity is resolved by context, not by relaxing the threshold.

    Six cues all reading "yeah", six occurrences in the audio. Each has to take
    the one between its own neighbours; taking any other would report a
    fabricated offset with high confidence.
    """
    cues, transcript = build_short_cue_script()
    matches = align_cues_to_transcript(cues, transcript)

    for match in matches:
        if len(match.cue.words) < 3:
            assert abs(match.offset) * 1000 < 200, (
                f"short cue at {match.cue.seconds}s anchored to the wrong occurrence"
            )


def test_a_short_cue_absent_from_the_audio_is_not_invented():
    """A narrow window must not force a match onto whatever is nearest."""
    cues, transcript = build_short_cue_script()
    cues.insert(3, Cue("00:00:00:00", cues[2].seconds + 0.5, "absolutely", "scc"))

    matches = align_cues_to_transcript(cues, transcript)
    assert not any(match.cue.text == "absolutely" for match in matches)


def test_a_short_cue_with_no_anchored_neighbour_is_left_alone():
    """With nothing bounding the search there is no reason to trust a one-word hit."""
    cues = [Cue("00:00:00:00", 5.0, "yeah", "scc")]
    words = [Word("yeah", t, t + 0.3) for t in (1.0, 5.0, 9.0, 13.0)]
    transcript = Transcript(words, language="en", model_size="test")

    assert align_cues_to_transcript(cues, transcript) == []


def test_sound_effect_cues_are_still_never_matched():
    """Nobody speaks "[cheering]"; the short pass must not reach for it."""
    cues, transcript = build_short_cue_script()
    cues.insert(3, Cue("00:00:00:00", cues[2].seconds + 0.5, "[cheering]", "scc"))

    matches = align_cues_to_transcript(cues, transcript)
    assert not any(match.cue.text == "[cheering]" for match in matches)


def test_theil_sen_is_not_dragged_by_an_outlier():
    points = [(float(x), 2.0 * x) for x in range(20)]
    points.append((10.0, 500.0))
    slope, _ = theil_sen(points)
    assert slope == pytest.approx(2.0, abs=0.05)


def test_caption_text_normalisation_strips_non_dialogue():
    assert normalize_caption_text(">> JOHN: Hello there!") == "hello there"
    assert normalize_caption_text("[door slams] We should go.") == "we should go"
    assert normalize_caption_text("♪ music playing ♪") == "music playing"


# ---------------------------------------------------------------------------
# Speaker labels (the v5 fix)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "caption, expected_speaker",
    [
        ("LAUREN: What, the Cheshire grapevine?", "LAUREN"),
        (">> LAUREN: with chevrons", "LAUREN"),
        ("- LAUREN: dash then name", "LAUREN"),
        ("MAN 2: over here", "MAN 2"),
        ("Lauren: title case", "Lauren"),
        ("- More people than you think know.", None),
        ("to the ring, Leanne Brown!", None),
    ],
)
def test_speaker_labels_are_split_off_however_they_are_written(caption, expected_speaker):
    """A bare `NAME:` has to be stripped, not just `>> NAME:`.

    This was the v4 bug: the leading chevron or dash was mandatory in the
    pattern, so a file writing plain `LAUREN:` kept the label in the text handed
    to the matcher.
    """
    from caption_cues import split_speaker

    speaker, _dialogue = split_speaker(caption)
    assert speaker == expected_speaker
    assert "lauren" not in normalize_caption_text(caption).split()


@pytest.mark.parametrize(
    "caption",
    [
        "I'll tell you this: it's freezing",
        "One thing matters: the deadline is Friday",
        "Then he said: go home",
    ],
)
def test_dialogue_containing_a_colon_is_left_alone(caption):
    """Stripping everything before a colon would eat half the line."""
    from caption_cues import split_speaker

    speaker, dialogue = split_speaker(caption)
    assert speaker is None
    assert dialogue == caption


def test_a_speaker_label_does_not_drag_the_anchor_off_the_dialogue():
    """The v4 failure, pinned.

    Nobody says the speaker's name, so the label has no match in the transcript.
    Left in place it occupied the cue's first token slot, the window vote landed
    one word early, and the cue was timed from the word *before* the line - a
    scattered negative offset that varied with whatever preceded it.
    """
    spoken = [
        ("nobody", 8.00), ("really", 8.30),
        ("what", 9.00), ("the", 9.25), ("cheshire", 9.45), ("grapevine", 9.90),
    ]
    transcript = Transcript(
        [Word(token, start, start + 0.25) for token, start in spoken],
        language="en",
        model_size="test",
    )

    cue = Cue("00:00:09:00", 9.0, "LAUREN: What, the Cheshire grapevine?", "scc")
    matches = align_cues_to_transcript([cue], transcript)

    assert len(matches) == 1
    # Anchored on "what" at 9.00, not on "really" at 8.30.
    assert matches[0].audio_seconds == pytest.approx(9.0)
    assert abs(matches[0].offset) * 1000 < 50


def test_labelled_and_unlabelled_cues_measure_identically():
    """The label must change the transcript's text and nothing else."""
    spoken = [
        ("engineers", 5.0), ("finished", 5.4), ("the", 5.8),
        ("bridge", 6.0), ("inspection", 6.4), ("yesterday", 7.0),
    ]
    transcript = Transcript(
        [Word(token, start, start + 0.3) for token, start in spoken],
        language="en",
        model_size="test",
    )

    plain = Cue("00:00:05:00", 5.0, "Engineers finished the bridge inspection yesterday", "scc")
    labelled = Cue("00:00:05:00", 5.0, "DANIEL: Engineers finished the bridge inspection yesterday", "scc")

    plain_match = align_cues_to_transcript([plain], transcript)[0]
    labelled_match = align_cues_to_transcript([labelled], transcript)[0]

    assert labelled_match.offset == pytest.approx(plain_match.offset)
    assert labelled.speaker == "DANIEL"


def test_anchor_survives_a_word_the_transcriber_dropped():
    """Defence in depth: any unmatched leading token, not just a speaker label.

    The cue is timed from the first word that actually matches, so a word the
    transcriber missed costs a word of precision rather than anchoring the cue
    onto unrelated audio.
    """
    spoken = [
        ("pause", 7.5), ("nobody", 8.0),
        ("what", 9.0), ("the", 9.25), ("cheshire", 9.45), ("grapevine", 9.90),
    ]
    transcript = Transcript(
        [Word(token, start, start + 0.25) for token, start in spoken],
        language="en",
        model_size="test",
    )

    cue = Cue("00:00:09:00", 9.0, "Erm, what, the Cheshire grapevine?", "scc")
    match = align_cues_to_transcript([cue], transcript)[0]
    assert match.audio_seconds == pytest.approx(9.0)



# A 00:58:30:00 head is 3510 s of program timecode ahead of the file's own zero.
# It is the shape that produced the "constant offset 3,510,345 ms" report: the
# caption side was absolute program timecode, the transcript side was
# file-relative, and the difference was the head, not the sync error.
START_TIMECODE_SECONDS = 3510.0


def rebase_cues_to_program_timecode(cues, start_offset=START_TIMECODE_SECONDS):
    """Restamp cues as absolute program timecode, the way an SCC carries them."""
    rebased = []
    for cue in cues:
        seconds = cue.seconds + start_offset
        rebased.append(
            Cue(
                frames_to_timecode(round(seconds * 25), 25.0, False),
                seconds,
                cue.text,
                cue.mode,
            )
        )
    return rebased


def test_start_timecode_is_reported_as_offset_when_it_is_not_removed():
    """The bug, pinned: diffing the two timelines raw returns the head."""
    file_relative = build_cues()
    transcript = build_transcript(file_relative)
    program_cues = rebase_cues_to_program_timecode(file_relative)

    summary = summarize_alignment(
        program_cues, align_cues_to_transcript(program_cues, transcript)
    )

    assert summary["matched"] >= len(program_cues) - 2
    assert summary["median_offset"] == pytest.approx(-START_TIMECODE_SECONDS, abs=1.0)


def test_start_timecode_is_removed_before_measuring_offset():
    file_relative = build_cues()
    transcript = build_transcript(file_relative, offset=0.08)
    program_cues = rebase_cues_to_program_timecode(file_relative)

    matches = align_cues_to_transcript(
        program_cues, transcript, start_offset=START_TIMECODE_SECONDS
    )
    summary = summarize_alignment(
        program_cues, matches, start_offset=START_TIMECODE_SECONDS
    )

    assert summary["matched"] >= len(program_cues) - 2
    # The head is gone; what is left is the real 80 ms sync error.
    assert summary["median_offset"] == pytest.approx(0.08, abs=0.05)
    assert abs(summary["median_offset"]) * 1000 < 200.0
    assert summary["drift"]["slope_ms_per_minute"] == pytest.approx(0.0, abs=2.0)


def test_rebased_samples_carry_media_relative_positions():
    file_relative = build_cues()
    transcript = build_transcript(file_relative)
    program_cues = rebase_cues_to_program_timecode(file_relative)

    summary = summarize_alignment(
        program_cues,
        align_cues_to_transcript(
            program_cues, transcript, start_offset=START_TIMECODE_SECONDS
        ),
        start_offset=START_TIMECODE_SECONDS,
    )

    sample = summary["samples"][0]
    # Absolute timecode is preserved for the vendor-facing report; the plotted
    # and regressed axis is the media-relative one.
    assert sample["caption_seconds"] >= START_TIMECODE_SECONDS
    assert sample["media_seconds"] == pytest.approx(
        sample["caption_seconds"] - START_TIMECODE_SECONDS
    )
    assert summary["start_offset"] == pytest.approx(START_TIMECODE_SECONDS)


def test_drift_is_still_recovered_on_a_program_timecode_file():
    """Rebasing must not eat the slope it is supposed to measure."""
    file_relative = build_cues()
    transcript = build_transcript(file_relative, drift_per_second=0.001)
    program_cues = rebase_cues_to_program_timecode(file_relative)

    summary = summarize_alignment(
        program_cues,
        align_cues_to_transcript(
            program_cues, transcript, start_offset=START_TIMECODE_SECONDS
        ),
        start_offset=START_TIMECODE_SECONDS,
    )

    assert summary["drift"]["slope_ms_per_minute"] == pytest.approx(60.0, abs=6.0)


def test_zero_based_file_is_unchanged_by_the_rebase():
    cues = build_cues()
    transcript = build_transcript(cues, offset=0.5)

    without = summarize_alignment(cues, align_cues_to_transcript(cues, transcript))
    with_zero = summarize_alignment(
        cues,
        align_cues_to_transcript(cues, transcript, start_offset=0.0),
        start_offset=0.0,
    )

    assert with_zero["median_offset"] == pytest.approx(without["median_offset"])
    assert with_zero["matched"] == without["matched"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
