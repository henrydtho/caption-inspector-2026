#!/usr/bin/env python3
"""Tests for the ctypes shim over the C decoder.

Run from this directory:  python3 -m pytest test__decoder_shim.py -v

The decode callbacks run inside ctypes callbacks, where Python prints
"Exception ignored" and carries on rather than raising. An error there does not
fail anything - it silently drops the caption event being handled. So these
tests assert on the decoded output *and* on a clean stderr, because the output
alone looked plausible while mid-row codes were vanishing.
"""

import ast
import contextlib
import difflib
import io
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON_DIR = REPO_ROOT / "python"
sys.path.insert(0, str(PYTHON_DIR))

from caption_cues import extract_cues  # noqa: E402
from inspection_support import decode_file  # noqa: E402


def parity(byte):
    """CEA-608 uses odd parity in the high bit."""
    return byte if bin(byte).count("1") % 2 else byte | 0x80


def text_words(text):
    data = [parity(ord(character)) for character in text.upper()]
    if len(data) % 2:
        data.append(0x80)
    return [f"{data[index]:02x}{data[index + 1]:02x}" for index in range(0, len(data), 2)]


def midrow(code):
    """A channel-1 mid-row control code: 0x11 followed by 0x20..0x2F."""
    return f"{parity(0x11):02x}{parity(code):02x}"


MIDROW_ITALICS = midrow(0x2E)
MIDROW_WHITE = midrow(0x20)


@pytest.fixture
def midrow_scc(tmp_path):
    """An SCC whose caption is styled mid-line, as broadcast captions are.

    Three words separated by mid-row style changes. A mid-row code occupies a
    character cell and displays as a space, so losing one welds its neighbours
    together.
    """
    words = ["9420", "9420", "94ae", "94ae", "9470", "9470"]
    words += text_words("HELLO")
    words += [MIDROW_ITALICS, MIDROW_ITALICS]
    words += text_words("WORLD")
    words += [MIDROW_WHITE, MIDROW_WHITE]
    words += text_words("AGAIN")
    words += ["942f", "942f"]

    path = tmp_path / "midrow.scc"
    path.write_text(
        "\n".join([
            "Scenarist_SCC V1.0", "",
            "00:00:01:00\t" + " ".join(words), "",
            "00:00:05:00\t942c 942c", "",
        ]),
        encoding="utf-8",
    )
    return path


def decode_capturing_stderr(path, rate_code=2997):
    """Decode, and hand back whatever the ctypes callbacks printed.

    A swallowed exception in a callback only ever shows up on stderr, so the
    test has to look there; the return value cannot reveal it.
    """
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        tracks, _logs = decode_file(str(path), rate_code, capture_logs=True)
    return tracks, stderr.getvalue()


def test_decoding_a_styled_caption_raises_nothing_in_the_callbacks(midrow_scc):
    _tracks, stderr = decode_capturing_stderr(midrow_scc)
    assert "Exception ignored" not in stderr
    assert "AttributeError" not in stderr


def test_mid_row_control_codes_reach_the_track(midrow_scc):
    """Four mid-row codes go in; four have to come out.

    They were dropped entirely: the callback raised while reading a misspelled
    struct field, before the event was ever added.
    """
    tracks, _stderr = decode_capturing_stderr(midrow_scc)
    rows = tracks["CEA-608"]["Channel 1"]
    midrow_rows = [row for row in rows if row["type"] == "MidRowControlCode"]
    assert len(midrow_rows) == 4


def test_mid_row_codes_carry_their_style(midrow_scc):
    tracks, _stderr = decode_capturing_stderr(midrow_scc)
    rows = tracks["CEA-608"]["Channel 1"]
    styles = [row["event"] for row in rows if row["type"] == "MidRowControlCode"]
    assert any("Italic" in style for style in styles)
    assert any(style == "{FG-White}" for style in styles)


def test_a_dropped_mid_row_code_does_not_weld_words_together(midrow_scc):
    """The damage this actually did, pinned.

    Losing the mid-row code lost the space it occupies, so the cue text read
    "HELLOWORLDAGAIN" - one unmatchable token. That text is what Tier 2 matches
    against the transcript, so the failure reached the sync numbers.
    """
    cues = extract_cues(str(midrow_scc), 2997)
    assert len(cues) == 1
    assert cues[0].text == "HELLO WORLD AGAIN"
    assert cues[0].normalized == "hello world again"
    assert cues[0].words == ["hello", "world", "again"]


def test_plain_captions_still_decode(tmp_path):
    """The fix must not disturb the ordinary path."""
    words = ["9420", "9420", "94ae", "94ae", "9470", "9470"]
    words += text_words("PLAIN TEXT")
    words += ["942f", "942f"]
    path = tmp_path / "plain.scc"
    path.write_text(
        "\n".join([
            "Scenarist_SCC V1.0", "",
            "00:00:01:00\t" + " ".join(words), "",
            "00:00:05:00\t942c 942c", "",
        ]),
        encoding="utf-8",
    )

    cues = extract_cues(str(path), 2997)
    assert len(cues) == 1
    assert cues[0].text == "PLAIN TEXT"


# ---------------------------------------------------------------------------
# The class of bug, not just the instance
# ---------------------------------------------------------------------------


def _struct_fields_and_attributes(source_path):
    tree = ast.parse(Path(source_path).read_text())

    declared = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for statement in node.body:
            if not isinstance(statement, ast.Assign):
                continue
            if not any(getattr(target, "id", "") == "_fields_" for target in statement.targets):
                continue
            for element in getattr(statement.value, "elts", []):
                if isinstance(element, ast.Tuple) and element.elts:
                    name = element.elts[0]
                    if isinstance(name, ast.Constant):
                        declared.add(name.value)

    used = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    return declared, used


def test_no_attribute_near_misses_a_declared_struct_field():
    """Catch the whole class: an attribute that almost matches a struct field.

    `backgroundForegroundData` versus the header's `backgroundForgroundData` cost
    every mid-row event in every 608 track, and nothing failed - the exception
    was swallowed by ctypes. A near-miss is either a typo or a name too close to
    a real field to keep.
    """
    declared, used = _struct_fields_and_attributes(PYTHON_DIR / "cshim.py")

    suspects = []
    for name in sorted(used - declared):
        # The Python wrapper classes use snake_case on purpose; only camelCase
        # names are candidates for shadowing a C field.
        if "_" in name:
            continue
        close = difflib.get_close_matches(name, declared, n=1, cutoff=0.85)
        if close and close[0] != name:
            suspects.append(f"{name!r} looks like the declared field {close[0]!r}")

    assert not suspects, "possible struct field typos:\n  " + "\n  ".join(suspects)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
