#!/usr/bin/env python3
"""Regression tests for the SCC transport compliance audit.

Run from this directory:  python3 -m pytest test__scc_transport_audit.py -v

IMPORTANT: these are regression tests against the actual observed hex in six
real deliveries (test/media/scc_transport/), not claims about the CEA-608
spec. They pin what this project's own manual player testing found:

- iyuno_working.scc and blu_working.scc played back correctly.
- 2g_original_failing.scc and 2g_doubled_failing.scc did not (no PAC on their
  first 9 cues; the "doubled" variant additionally has EDM+EOC doubled and
  combined in one block).
- 3play_failing.scc did not either, and has a confound: its PAC values are
  also nonstandard, on top of EDM+EOC being doubled and combined.

HISTORY - `edm_eoc_combined_and_doubled` as a candidate failure cause,
investigated and disproven: it was originally flagged WARN on the strength of
a 2-for-2 correlation with the two failing files above. `maccaps_v3_working.scc`
disproved that - it has the exact same pattern (EDM+EOC doubled and combined
in one block) on every single cue, and was confirmed working in Switch. The
trait is downgraded to INFO in `scc_transport_audit.evaluate_cue` and stays
that way: do not re-promote it to WARN without first accounting for this
fixture.

# TODO: 3play_failing.scc's actual root cause is unknown again. It has PAC on
# every cue (unlike the 2G files) and the EDM/EOC pattern is now known not to
# be a failure signal either. Confirmed failing, cause unidentified - do not
# assert an explanation here until one is actually found.

A future reader should not read a correlation in this small a fixture set as
"proven root cause".
"""

import sys
from pathlib import Path

import pytest

PYTHON_DIR = Path(__file__).resolve().parents[2] / "python"
sys.path.insert(0, str(PYTHON_DIR))

from scc_raw_parser import parse_scc_file  # noqa: E402
from scc_code_table import classify_pair  # noqa: E402
from scc_transport_audit import group_into_cue_blocks  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "media" / "scc_transport"


def _cues(filename):
    blocks = parse_scc_file(FIXTURES / filename)
    return group_into_cue_blocks(blocks)


# --- Task 1: raw parser -------------------------------------------------

def test_parse_scc_file_skips_header_and_blank_lines():
    blocks = parse_scc_file(FIXTURES / "blu_working.scc")
    assert all(block.timecode != "Scenarist_SCC V1.0" for block in blocks)
    assert all(block.pairs for block in blocks)


def test_parse_scc_file_keeps_build_and_erase_lines_separate():
    blocks = parse_scc_file(FIXTURES / "iyuno_working.scc")
    # 10 cues, each a build line and a separate erase line: 20 blocks.
    assert len(blocks) == 20
    assert blocks[0].timecode == "01:09:59:03"
    assert blocks[1].timecode == "01:10:00:23"
    assert blocks[1].pairs == ["942c", "942c"]


# --- Task 2: byte-pair classification -----------------------------------

@pytest.mark.parametrize(
    "pair, expected",
    [
        ("9420", "RCL"),
        ("94ae", "ENM"),
        ("942c", "EDM"),
        ("942f", "EOC"),
        ("8080", "PADDING"),
        # Verified real PAC pairs from this project's fixtures.
        ("9470", "PAC"),
        ("94d0", "PAC"),
        ("94f2", "PAC"),
        # Verified real Tab Offset pairs - see scc_code_table.py's module
        # docstring for why these are not PAC despite looking similar.
        ("97a1", "TAB_OFFSET"),
        ("97a2", "TAB_OFFSET"),
        # Ordinary caption text.
        ("61f4", "TEXT"),
        ("2031", "TEXT"),
    ],
)
def test_classify_pair(pair, expected):
    assert classify_pair(pair) == expected


def test_classify_pair_unknown_control_looking_byte():
    # 0x9b -> 0x1b stripped of parity: a control-code-shaped byte with no
    # entry in RCL/ENM/EDM/EOC/PADDING/PAC/TAB_OFFSET.
    assert classify_pair("9b00") == "UNKNOWN"


# --- Task 3: per-cue transport audit -------------------------------------

def test_group_into_cue_blocks_counts_cues():
    for filename in (
        "iyuno_working.scc",
        "blu_working.scc",
        "2g_original_failing.scc",
        "2g_doubled_failing.scc",
        "3play_failing.scc",
        "maccaps_v3_working.scc",
    ):
        cues = _cues(filename)
        assert len(cues) == 10, filename


def test_doubling_and_combined_flags_on_a_known_good_file():
    cues = _cues("blu_working.scc")
    first = cues[0]
    assert first.rcl_count == 1 and not first.rcl_doubled
    assert first.pac_count == 1 and not first.pac_doubled
    assert first.edm_count >= 1 and first.eoc_count == 1
    assert not first.edm_eoc_combined_and_doubled


def test_doubling_flags_on_a_known_doubled_file():
    cues = _cues("2g_doubled_failing.scc")
    first = cues[0]
    assert first.rcl_doubled
    assert first.pac_count == 0
    assert first.edm_eoc_combined_and_doubled


# --- Task 5 regression assertions ----------------------------------------

def test_working_files_have_a_position_code_on_every_cue():
    for filename in ("iyuno_working.scc", "blu_working.scc", "maccaps_v3_working.scc"):
        for cue in _cues(filename):
            assert cue.pac_count > 0, f"{filename} {cue.timecode}"


def test_2g_files_are_missing_pac_on_cues_1_through_9():
    for filename in ("2g_original_failing.scc", "2g_doubled_failing.scc"):
        cues = _cues(filename)
        for cue in cues[:9]:
            assert cue.pac_count == 0, f"{filename} {cue.timecode}"
        assert cues[9].pac_count > 0, filename


def test_edm_eoc_combined_and_doubled_appears_in_both_passing_and_failing_files():
    # Deliberately includes maccaps_v3_working.scc (known good, confirmed in
    # Switch) alongside the two known-bad files, on all 10 real cues: this
    # trait shows up in both passing and failing deliveries, so it must not
    # be treated as a failure signal on its own. See the module docstring's
    # HISTORY note.
    for filename in ("2g_doubled_failing.scc", "3play_failing.scc", "maccaps_v3_working.scc"):
        for cue in _cues(filename):
            assert cue.edm_eoc_combined_and_doubled, f"{filename} {cue.timecode}"


def test_edm_eoc_not_combined_and_doubled_on_working_files():
    for filename in ("iyuno_working.scc", "blu_working.scc"):
        for cue in _cues(filename):
            assert not cue.edm_eoc_combined_and_doubled, f"{filename} {cue.timecode}"
