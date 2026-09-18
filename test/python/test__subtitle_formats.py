#!/usr/bin/env python3
"""Tests for the subtitle parsers.

Run from this directory:  python3 -m pytest test__subtitle_formats.py -v

The first sections cover SRT, WebVTT and TTML, where each test pins a specific
thing the old SRT-regex reader got wrong or could not see at all. The sections
after them cover the rest of the formats the app accepts - SSA/ASS, SAMI,
SubViewer, MicroDVD, SBV, MPL2, LRC, RealText, Spruce STL and binary EBU-STL -
and concentrate on the two things that break a reader: the ambiguous
extensions, and the formats that count frames instead of seconds.
"""

import sys
from pathlib import Path

import pytest

PYTHON_DIR = Path(__file__).resolve().parents[2] / "python"
sys.path.insert(0, str(PYTHON_DIR))

from subtitle_formats import (  # noqa: E402
    SubtitleParseError,
    parse_lrc,
    parse_microdvd,
    parse_mpl2,
    parse_realtext,
    parse_sami,
    parse_sbv,
    parse_spruce_stl,
    parse_srt,
    parse_ssa,
    parse_subviewer,
    parse_ttml,
    parse_vtt,
    read_subtitle_document,
)


# ---------------------------------------------------------------------------
# WebVTT
# ---------------------------------------------------------------------------


def test_vtt_reads_two_field_timestamps():
    """`MM:SS.mmm` is legal WebVTT, and the old SRT pattern skipped it.

    A file using it parsed to zero cues, which read downstream as "this caption
    file has no dialogue" rather than "this reader cannot see it".
    """
    document = parse_vtt(
        "WEBVTT\n\n00:01.000 --> 00:04.000\nFirst line\n\n"
        "01:02.500 --> 01:05.000\nSecond line\n"
    )
    assert len(document) == 2
    assert document.cues[0].start == pytest.approx(1.0)
    assert document.cues[1].start == pytest.approx(62.5)


def test_vtt_skips_note_style_and_region_blocks():
    document = parse_vtt(
        "WEBVTT\n\n"
        "NOTE this comment\nspans two lines\n\n"
        "STYLE\n::cue { color: yellow }\n\n"
        "REGION\nid:fred width:40%\n\n"
        "00:00:01.000 --> 00:00:02.000\nOnly real dialogue\n"
    )
    assert len(document) == 1
    assert document.cues[0].text == "Only real dialogue"


def test_vtt_keeps_the_voice_speaker_and_drops_the_rest_of_the_markup():
    document = parse_vtt(
        "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n"
        "<v.loud Roger Bingham>We are <i>totally</i> ready\n"
    )
    cue = document.cues[0]
    assert cue.speaker == "Roger Bingham"
    assert cue.text == "We are totally ready"


def test_vtt_decodes_entities_and_strips_inline_timestamps():
    document = parse_vtt(
        "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\n"
        "Salt &amp; pepper<00:00:02.000> and &#8212; a dash\n"
    )
    assert document.cues[0].text == "Salt & pepper and — a dash"


def test_vtt_reads_cue_identifiers_and_ignores_cue_settings():
    document = parse_vtt(
        "WEBVTT\n\nintro\n00:00:01.000 --> 00:00:02.000 align:start position:10%\nHello\n"
    )
    cue = document.cues[0]
    assert cue.identifier == "intro"
    assert cue.text == "Hello"


def test_vtt_records_the_hls_timestamp_map_without_shifting_times():
    """The offset is reported, not silently applied.

    X-TIMESTAMP-MAP relates the cue clock to an MPEG-TS clock. Quietly shifting
    every cue by it would move a whole file's timing on the strength of a header
    the app cannot verify.
    """
    document = parse_vtt(
        "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:900000\n\n"
        "00:00:01.000 --> 00:00:02.000\nHello\n"
    )
    assert "MPEGTS:900000" in document.header["x_timestamp_map"]
    assert document.cues[0].start == pytest.approx(1.0)


def test_vtt_without_a_signature_is_rejected():
    with pytest.raises(SubtitleParseError):
        parse_vtt("00:00:01.000 --> 00:00:02.000\nNo signature line\n")


# ---------------------------------------------------------------------------
# TTML
# ---------------------------------------------------------------------------


TTML_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<tt xmlns="http://www.w3.org/ns/ttml" '
    'xmlns:ttp="http://www.w3.org/ns/ttml#parameter" '
    'xmlns:tts="http://www.w3.org/ns/ttml#styling" '
    'xmlns:ttm="http://www.w3.org/ns/ttml#metadata" '
    '{attributes} xml:lang="en">'
)


def ttml(body, attributes='ttp:frameRate="25"'):
    return TTML_HEADER.format(attributes=attributes) + body + "</tt>"


def test_ttml_applies_the_frame_rate_multiplier():
    """`frameRate=30` with multiplier `1000 1001` is 29.97, not 30.

    Reading it as 30 makes every frame-counted stamp in the file wrong by
    0.1%, which is exactly the drift signature Tier 2 hunts for - so the parser
    would manufacture the bug the app exists to find.
    """
    document = parse_ttml(
        ttml(
            "<body><div><p begin='0s' end='1s'>Hello</p></div></body>",
            attributes='ttp:frameRate="30" ttp:frameRateMultiplier="1000 1001"',
        )
    )
    assert float(document.frame_rate) == pytest.approx(30000 / 1001)
    assert document.declared_rate_code == 2997


def test_ttml_nested_div_timing_shifts_its_children():
    document = parse_ttml(
        ttml(
            "<body><div begin='00:00:10.000'>"
            "<p begin='0s' end='2s'>Shifted</p></div></body>"
        )
    )
    cue = document.cues[0]
    assert cue.start == pytest.approx(10.0)
    assert cue.end == pytest.approx(12.0)


def test_ttml_seq_container_runs_children_back_to_back():
    document = parse_ttml(
        ttml(
            "<body><div timeContainer='seq' begin='00:01:00.000'>"
            "<p dur='3s'>One</p><p dur='3s'>Two</p></div></body>"
        )
    )
    assert [cue.start for cue in document.cues] == pytest.approx([60.0, 63.0])
    assert document.cues[1].end == pytest.approx(66.0)


def test_ttml_reads_every_time_expression_form():
    document = parse_ttml(
        ttml(
            "<body><div>"
            "<p begin='00:00:01.500' end='00:00:02.000'>clock with fraction</p>"
            "<p begin='00:00:10:05' end='00:00:11:00'>clock with frames</p>"
            "<p begin='250f' end='300f'>frame offset</p>"
            "<p begin='1500ms' end='2s'>millisecond offset</p>"
            "<p begin='2m' end='121s'>minute offset</p>"
            "</div></body>"
        )
    )
    starts = [cue.start for cue in document.cues]
    # 25 fps: 5 frames = 0.2 s, 250 frames = 10 s.
    assert sorted(starts) == pytest.approx([1.5, 1.5, 10.0, 10.2, 120.0])


def test_ttml_frames_require_a_declared_rate():
    with pytest.raises(SubtitleParseError):
        parse_ttml(
            ttml(
                "<body><div><p begin='00:00:10:05' end='00:00:11:00'>x</p></div></body>",
                attributes="",
            )
        )


def test_ttml_flattens_spans_and_turns_br_into_a_line_break():
    document = parse_ttml(
        ttml(
            "<body><div><p begin='0s' end='1s'>"
            "Line <span tts:color='red'>one</span><br/>Line two"
            "</p></div></body>"
        )
    )
    assert document.cues[0].text == "Line one\nLine two"


def test_ttml_resolves_the_speaker_from_a_metadata_agent():
    document = parse_ttml(
        ttml(
            "<head><metadata>"
            "<ttm:agent xml:id='ag1'><ttm:name type='full'>Roger Bingham</ttm:name></ttm:agent>"
            "</metadata></head>"
            "<body><div><p begin='0s' end='1s' ttm:agent='ag1'>Hello</p></div></body>"
        )
    )
    assert document.cues[0].speaker == "Roger Bingham"


def test_ttml_ignores_styling_and_layout_elements():
    document = parse_ttml(
        ttml(
            "<head><styling><style xml:id='s1' tts:color='white'/></styling>"
            "<layout><region xml:id='r1'/></layout></head>"
            "<body><div><p begin='0s' end='1s' region='r1'>Only me</p></div></body>"
        )
    )
    assert [cue.text for cue in document.cues] == ["Only me"]


def test_ttml_namespace_vintage_does_not_matter():
    """TTML has shipped under several namespaces; matching on local names."""
    document = parse_ttml(
        '<tt xmlns="http://www.w3.org/2006/10/ttaf1">'
        "<body><div><p begin='1s' end='2s'>Old namespace</p></div></body></tt>"
    )
    assert document.cues[0].text == "Old namespace"


def test_ttml_that_is_not_ttml_is_rejected_clearly():
    with pytest.raises(SubtitleParseError, match="root element"):
        parse_ttml("<something><else/></something>")


# ---------------------------------------------------------------------------
# SRT and dispatch
# ---------------------------------------------------------------------------


def test_srt_parses_index_times_and_multiline_text():
    document = parse_srt(
        "1\n00:00:01,000 --> 00:00:04,000\nFirst line\nSecond line\n\n"
        "2\n00:00:05,000 --> 00:00:06,000\nNext cue\n"
    )
    assert len(document) == 2
    assert document.cues[0].identifier == "1"
    assert document.cues[0].text == "First line\nSecond line"
    assert document.cues[0].end == pytest.approx(4.0)


def test_content_decides_the_format_not_the_extension(tmp_path):
    """A .xml holding TTML and a .vtt holding SRT are both real deliveries."""
    disguised_ttml = tmp_path / "captions.xml"
    disguised_ttml.write_text(
        ttml("<body><div><p begin='1s' end='2s'>From xml</p></div></body>"),
        encoding="utf-8",
    )
    assert read_subtitle_document(disguised_ttml).kind == "ttml"

    disguised_srt = tmp_path / "captions.vtt"
    disguised_srt.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nActually SRT\n", encoding="utf-8"
    )
    assert read_subtitle_document(disguised_srt).kind == "srt"


def test_a_byte_order_mark_does_not_break_parsing(tmp_path):
    path = tmp_path / "bom.vtt"
    path.write_text(
        "﻿WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello\n", encoding="utf-8"
    )
    assert len(read_subtitle_document(path)) == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


# ---------------------------------------------------------------------------
# SubStation Alpha
# ---------------------------------------------------------------------------


ASS_SAMPLE = """[Script Info]
Title: Demo
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname
Style: Default,Arial

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:04.00,Default,Roger,0,0,0,,Hello {\\i1}there{\\i0}\\Nsecond line
Comment: 0,0:00:05.00,0:00:06.00,Default,,0,0,0,,an authoring note
Dialogue: 0,0:00:05.50,0:00:08.00,Default,,0,0,0,,Real text, with a comma
"""


def test_ass_reads_the_column_order_from_the_format_line():
    """The Text column is last and is the only one allowed to hold commas.

    Splitting a Dialogue line on every comma truncates any line of dialogue
    containing one, which is most of them.
    """
    document = parse_ssa(ASS_SAMPLE)
    assert [cue.text for cue in document.cues] == [
        "Hello there\nsecond line",
        "Real text, with a comma",
    ]


def test_ass_drops_comment_events_and_keeps_the_name_as_speaker():
    document = parse_ssa(ASS_SAMPLE)
    assert len(document) == 2
    assert document.cues[0].speaker == "Roger"
    assert document.cues[0].start == pytest.approx(1.0)
    assert document.cues[0].end == pytest.approx(4.0)


def test_ass_drops_vector_drawing_blocks():
    """`\\p1` switches the text field to drawing coordinates, not dialogue."""
    document = parse_ssa(
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:04.00,Default,,0,0,0,,"
        "{\\p1}m 0 0 l 100 0 l 100 50{\\p0}Actual dialogue\n"
    )
    assert document.cues[0].text == "Actual dialogue"


def test_ssa_v4_and_ass_v4_plus_are_told_apart():
    plain = parse_ssa(
        "[Script Info]\nScriptType: v4.00\n\n[Events]\n"
        "Format: Marked, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: Marked=0,0:00:01.00,0:00:04.00,Default,,0000,0000,0000,,Line\n"
    )
    assert plain.kind == "ssa"
    assert parse_ssa(ASS_SAMPLE).kind == "ass"


# ---------------------------------------------------------------------------
# SAMI
# ---------------------------------------------------------------------------


def test_sami_ends_a_cue_at_the_next_sync_and_treats_nbsp_as_a_clear():
    """SAMI has no end time. A `&nbsp;`-only SYNC is how it clears the screen.

    Read literally, that block is a subtitle whose text is a space, and it
    lands in the transcript as a blank cue.
    """
    document = parse_sami(
        "<SAMI><BODY>\n"
        "<SYNC Start=1000><P Class=ENUSCC>Hello<br>world\n"
        "<SYNC Start=4000><P Class=ENUSCC>&nbsp;\n"
        "<SYNC Start=5000><P Class=ENUSCC>Second cue\n"
        "</BODY></SAMI>\n"
    )
    assert len(document) == 2
    assert document.cues[0].text == "Hello\nworld"
    assert document.cues[0].end == pytest.approx(4.0)


def test_sami_picks_the_class_carrying_the_most_dialogue():
    document = parse_sami(
        "<SAMI><BODY>\n"
        "<SYNC Start=1000><P Class=FRFRCC>Bonjour\n"
        "<SYNC Start=2000><P Class=ENUSCC>Hello\n"
        "<SYNC Start=3000><P Class=ENUSCC>there\n"
        "<SYNC Start=4000><P Class=ENUSCC>again\n"
        "</BODY></SAMI>\n"
    )
    assert document.language == "ENUSCC"
    assert len(document) == 3
    assert "FRFRCC" in document.header["available_classes"]


# ---------------------------------------------------------------------------
# SubViewer, SBV, MPL2, LRC, RealText
# ---------------------------------------------------------------------------


def test_subviewer_expands_br_markers_and_reads_centisecond_stamps():
    document = parse_subviewer(
        "[INFORMATION]\n[TITLE]Demo\n[END INFORMATION]\n[SUBTITLE]\n"
        "00:00:01.50,00:00:04.00\nHello[br]world\n"
    )
    assert document.cues[0].start == pytest.approx(1.5)
    assert document.cues[0].text == "Hello\nworld"
    assert document.header["title"] == "Demo"


def test_subviewer_cues_need_no_blank_line_between_them():
    """Both layouts ship. Splitting on blank lines reads the packed one as one cue."""
    document = parse_subviewer(
        "00:00:01.00,00:00:04.00\nFirst\n00:00:05.00,00:00:08.00\nSecond\n"
    )
    assert [cue.text for cue in document.cues] == ["First", "Second"]


def test_sbv_reads_millisecond_stamps():
    """SBV's fraction is milliseconds where SubViewer's is centiseconds.

    Both are `HH:MM:SS.f`, so the fraction's width has to set its scale - fixing
    it at two digits reads `0:00:01.500` as 1.5 seconds only by luck and
    `0:00:01.050` as 1.5 seconds wrongly.
    """
    document = parse_sbv("0:00:01.050,0:00:04.000\nHello\n")
    assert document.cues[0].start == pytest.approx(1.05)


def test_mpl2_stamps_are_tenths_of_a_second():
    document = parse_mpl2("[15][40]Hello|world\n")
    assert document.cues[0].start == pytest.approx(1.5)
    assert document.cues[0].end == pytest.approx(4.0)
    assert document.cues[0].text == "Hello\nworld"


def test_lrc_repeats_a_line_carrying_several_stamps():
    document = parse_lrc("[ar:Someone]\n[00:01.00]First\n[00:05.00][01:00.00]Chorus\n")
    assert [round(cue.start, 2) for cue in document.cues] == [1.0, 5.0, 60.0]
    assert document.header["ar"] == "Someone"


def test_lrc_strips_enhanced_word_stamps():
    document = parse_lrc("[00:01.00]<00:01.00>Hello <00:01.50>world\n")
    assert document.cues[0].text == "Hello world"


def test_realtext_runs_a_cue_until_the_next_time_marker():
    document = parse_realtext(
        '<window><time begin="00:00:01"/>Hello<br/>world\n'
        '<time begin="00:00:04"/><clear/>\n'
        '<time begin="00:00:05"/>Second cue\n</window>\n'
    )
    assert len(document) == 2
    assert document.cues[0].end == pytest.approx(4.0)
    assert document.cues[0].text == "Hello\nworld"


# ---------------------------------------------------------------------------
# The frame-counting formats
# ---------------------------------------------------------------------------


def test_microdvd_reads_the_rate_from_its_first_pseudo_cue():
    """`{1}{1}23.976` is the rate, not a subtitle. Read as one it becomes a cue.

    Frames only mean seconds against a rate, so getting this wrong moves every
    cue in the file by 4%.
    """
    document = parse_microdvd("{1}{1}23.976\n{24}{96}Hello|world\n")
    assert len(document) == 1
    assert document.cues[0].start == pytest.approx(24 / 23.976)
    assert document.header["frame_rate"] == "23.976"


def test_microdvd_records_that_a_missing_rate_was_assumed():
    document = parse_microdvd("{25}{100}Hello\n")
    assert document.cues[0].start == pytest.approx(1.0)
    assert "assumed" in document.header["frame_rate_source"]


def test_microdvd_drops_per_cue_style_blocks():
    document = parse_microdvd("{25}{100}{y:i}Hello|{c:$0000ff}world\n")
    assert document.cues[0].text == "Hello\nworld"


def test_spruce_stl_counts_frames_and_flags_the_rate_it_assumed():
    document = parse_spruce_stl(
        "$FontName = Arial\n00:00:01:15 , 00:00:04:00 , Hello|world\n"
    )
    assert document.cues[0].start == pytest.approx(1.5)
    assert document.cues[0].text == "Hello\nworld"
    assert "assumed" in document.header["frame_rate_source"]


def test_spruce_stl_uses_a_declared_frame_rate_when_there_is_one():
    document = parse_spruce_stl(
        "$FrameRate = 25\n00:00:01:15 , 00:00:04:00 , Hello\n"
    )
    assert document.cues[0].start == pytest.approx(1.6)
    assert document.header["frame_rate"] == "25.0"
    assert "frame_rate_source" not in document.header


# ---------------------------------------------------------------------------
# Ambiguous extensions - the reader has to sniff, not trust the suffix
# ---------------------------------------------------------------------------


def test_a_sub_file_is_microdvd_or_subviewer_depending_on_its_contents(tmp_path):
    """`.sub` is two unrelated formats, and neither says so in its name."""
    microdvd = tmp_path / "a.sub"
    microdvd.write_text("{25}{100}Hello\n")
    assert read_subtitle_document(microdvd).kind == "microdvd"

    subviewer = tmp_path / "b.sub"
    subviewer.write_text("[SUBTITLE]\n00:00:01.00,00:00:04.00\nHello[br]world\n")
    assert read_subtitle_document(subviewer).kind == "subviewer"


def test_an_srt_delivered_as_vtt_is_still_read_as_srt(tmp_path):
    mislabelled = tmp_path / "c.vtt"
    mislabelled.write_text("1\n00:00:01,000 --> 00:00:04,000\nHello\n")
    assert read_subtitle_document(mislabelled).kind == "srt"


def test_a_cheetah_cap_says_what_it_is_rather_than_failing_to_parse(tmp_path):
    """A useless error here sends someone hunting for a corrupt file."""
    cap = tmp_path / "d.cap"
    cap.write_bytes(b"\x00\x01\x02binary")
    with pytest.raises(SubtitleParseError) as error:
        read_subtitle_document(cap)
    assert "Cheetah" in str(error.value)


# ---------------------------------------------------------------------------
# EBU-STL (binary)
# ---------------------------------------------------------------------------


def _gsi(disk_format_code=b"STL25.01", character_code_table=b"00"):
    block = bytearray(b" " * 1024)
    block[0:3] = b"850"
    block[3:11] = disk_format_code
    block[12:14] = character_code_table
    block[14:16] = b"09"
    block[16:48] = b"DEMO PROGRAMME".ljust(32)
    block[256:264] = b"10000000"
    return bytes(block)


def _tti(number, extension_block, time_in, time_out, text, comment=0):
    block = bytearray(b"\x8f" * 128)
    block[1:3] = number.to_bytes(2, "little")
    block[3] = extension_block
    block[5:9] = bytes(time_in)
    block[9:13] = bytes(time_out)
    block[15] = comment
    block[16:16 + len(text)] = text
    return bytes(block)


def _write_stl(tmp_path, name, blocks, **gsi_kwargs):
    target = tmp_path / name
    target.write_bytes(_gsi(**gsi_kwargs) + b"".join(blocks))
    return target


def test_ebu_stl_reads_program_timecode_at_the_rate_the_gsi_declares(tmp_path):
    """The Disk Format Code is the only place the frame rate appears.

    The cue times are four raw bytes of `hh mm ss ff`, so reading a 30-frame
    file at 25 shifts every subtitle.
    """
    path = _write_stl(
        tmp_path,
        "a.stl",
        [_tti(1, 0xFF, (10, 0, 1, 0), (10, 0, 4, 0), b"Hello\x8aworld")],
    )
    document = read_subtitle_document(path)
    assert document.kind == "ebu-stl"
    assert document.frame_rate == 25
    # 10:00:01:00 is program timecode, not ten hours into the media.
    assert document.cues[0].start == pytest.approx(36001.0)
    assert document.cues[0].text == "Hello\nworld"
    assert document.header["original_programme_title"] == "DEMO PROGRAMME"


def test_ebu_stl_reads_a_thirty_frame_file_at_thirty(tmp_path):
    path = _write_stl(
        tmp_path,
        "b.stl",
        [_tti(1, 0xFF, (0, 0, 1, 15), (0, 0, 4, 0), b"Hello")],
        disk_format_code=b"STL30.01",
    )
    assert read_subtitle_document(path).cues[0].start == pytest.approx(1.5)


def test_ebu_stl_joins_a_subtitle_split_across_extension_blocks(tmp_path):
    """Only `EBN = 0xFF` ends a subtitle; lower numbers continue the one before.

    Treating every block as its own subtitle turns one long caption into
    several stacked at the same timecode.
    """
    path = _write_stl(
        tmp_path,
        "c.stl",
        [
            _tti(1, 0x00, (10, 0, 1, 0), (10, 0, 4, 0), b"first block"),
            _tti(1, 0xFF, (10, 0, 1, 0), (10, 0, 4, 0), b"second block"),
        ],
    )
    document = read_subtitle_document(path)
    assert len(document) == 1
    assert document.cues[0].text == "first block\nsecond block"


def test_ebu_stl_drops_comment_blocks(tmp_path):
    path = _write_stl(
        tmp_path,
        "d.stl",
        [
            _tti(1, 0xFF, (10, 0, 1, 0), (10, 0, 4, 0), b"Real subtitle"),
            _tti(2, 0xFF, (10, 0, 5, 0), (10, 0, 6, 0), b"authoring note", comment=1),
        ],
    )
    assert [cue.text for cue in read_subtitle_document(path).cues] == ["Real subtitle"]


def test_ebu_stl_composes_iso6937_accents(tmp_path):
    """ISO 6937 puts the accent *before* the letter, and it is not Latin-1.

    Decoded as Latin-1 an accented word arrives as mojibake; decoded byte for
    byte the accent lands on the wrong character.
    """
    path = _write_stl(
        tmp_path, "e.stl", [_tti(1, 0xFF, (10, 0, 1, 0), (10, 0, 4, 0), b"caf\xc2e")]
    )
    assert read_subtitle_document(path).cues[0].text == "café"


def test_ebu_stl_uses_the_character_code_table_for_non_latin_text(tmp_path):
    path = _write_stl(
        tmp_path,
        "f.stl",
        [_tti(1, 0xFF, (10, 0, 1, 0), (10, 0, 4, 0), "Привет".encode("iso8859_5"))],
        character_code_table=b"01",
    )
    assert read_subtitle_document(path).cues[0].text == "Привет"


def test_a_text_stl_is_not_mistaken_for_a_binary_one(tmp_path):
    """`.stl` is EBU's binary format and Spruce's text format both."""
    spruce = tmp_path / "g.stl"
    spruce.write_text("$TapeOffset = FALSE\n00:00:01:00 , 00:00:04:00 , Hello\n")
    assert read_subtitle_document(spruce).kind == "spruce-stl"
