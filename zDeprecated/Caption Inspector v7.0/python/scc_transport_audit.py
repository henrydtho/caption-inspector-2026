"""Per-cue SCC transport audit: raw byte-pair structure, not decoded content.

Independent from the decoded/deduped path on purpose - see the module
docstring in `scc_code_table.py` and the notes in `scc_raw_parser.py`. This
module does not import `caption_cues.py` and must not: that module's cue
boundaries come from `libci`'s decoded, deduped output, and mixing the two
would defeat the point of auditing the raw transmission.
"""

from dataclasses import dataclass, field

from scc_code_table import classify_pair
from sync_check import INFO, PASS, WARN, Check

# Kinds this audit tracks doubling/counts for. TEXT and PADDING are not
# counted; UNKNOWN pairs are collected separately (see CueTransport).
_TRACKED_KINDS = ("RCL", "ENM", "PAC", "TAB_OFFSET", "EDM", "EOC")

# CueTransport attribute prefixes for the doubling/count checks in Task 4,
# with a human label for the message.
_DOUBLING_CHECKED = (
    ("rcl", "RCL"),
    ("enm", "ENM"),
    ("pac", "PAC"),
    ("tab_offset", "Tab Offset"),
    ("edm", "EDM"),
    ("eoc", "EOC"),
)


@dataclass
class CueTransport:
    """Raw transport-level facts about one cue, computed from undeduped pairs."""

    timecode: str
    rcl_count: int = 0
    rcl_doubled: bool = False
    enm_count: int = 0
    enm_doubled: bool = False
    pac_count: int = 0
    pac_doubled: bool = False
    pac_values: list = field(default_factory=list)
    tab_offset_count: int = 0
    tab_offset_doubled: bool = False
    edm_count: int = 0
    edm_doubled: bool = False
    eoc_count: int = 0
    eoc_doubled: bool = False
    # True only if EDM and EOC are both doubled AND both occur within the same
    # raw block (not across separate lines). This is the specific pattern
    # observed in this project's own test data - see
    # test__scc_transport_audit.py - not a documented CEA-608 requirement.
    edm_eoc_combined_and_doubled: bool = False
    total_pairs: int = 0
    unknown_pairs: list = field(default_factory=list)


def _group_block_runs(pairs):
    """Group adjacent identical (kind, raw pair) tokens within one block.

    Doubling is a per-transmission redundancy within one line, so runs are
    grouped per block rather than across the whole cue - the EDM/EOC
    "combined in the same block" check below depends on that.
    """
    classified = [(classify_pair(pair), pair) for pair in pairs]
    runs = []
    index = 0
    count = len(classified)
    while index < count:
        kind, raw = classified[index]
        run_length = 1
        while index + run_length < count and classified[index + run_length] == (kind, raw):
            run_length += 1
        runs.append((kind, raw, run_length))
        index += run_length
    return runs


def _apply_block(cue, block):
    cue.total_pairs += len(block.pairs)

    block_edm_doubled = False
    block_eoc_doubled = False

    for kind, raw, run_length in _group_block_runs(block.pairs):
        doubled = run_length >= 2

        if kind == "RCL":
            cue.rcl_count += 1
            cue.rcl_doubled = cue.rcl_doubled or doubled
        elif kind == "ENM":
            cue.enm_count += 1
            cue.enm_doubled = cue.enm_doubled or doubled
        elif kind == "PAC":
            cue.pac_count += 1
            cue.pac_doubled = cue.pac_doubled or doubled
            cue.pac_values.append(raw)
        elif kind == "TAB_OFFSET":
            cue.tab_offset_count += 1
            cue.tab_offset_doubled = cue.tab_offset_doubled or doubled
        elif kind == "EDM":
            cue.edm_count += 1
            cue.edm_doubled = cue.edm_doubled or doubled
            block_edm_doubled = doubled
        elif kind == "EOC":
            cue.eoc_count += 1
            cue.eoc_doubled = cue.eoc_doubled or doubled
            block_eoc_doubled = doubled
        elif kind == "UNKNOWN":
            cue.unknown_pairs.append(raw)
        # TEXT is not tracked.

    if block_edm_doubled and block_eoc_doubled:
        cue.edm_eoc_combined_and_doubled = True


def group_into_cue_blocks(blocks):
    """Group raw `SccBlock`s into one `CueTransport` per cue.

    A cue starts at a block containing RCL and ends at the next block
    containing RCL, or end of file - mirroring `caption_cues.py`'s cue
    boundary conceptually, without importing it (see module docstring).
    Blocks before the first RCL (there should not be any) are skipped.
    """
    cues = []
    current = None

    for block in blocks:
        block_kinds = {classify_pair(pair) for pair in block.pairs}
        if "RCL" in block_kinds:
            if current is not None:
                cues.append(current)
            current = CueTransport(timecode=block.timecode)

        if current is None:
            continue

        _apply_block(current, block)

    if current is not None:
        cues.append(current)

    return cues


def evaluate_cue(cue):
    """Task 4 severity checks for one cue, using the existing PASS/WARN/FAIL/INFO model.

    Only two traits are WARN: no position code, and EDM+EOC doubled and
    combined in one block - the only two this project's own testing (five
    fixtures, see test__scc_transport_audit.py) associates with actual
    playback failure. Doubling in general and a missing ENM are informational
    only, matching what was actually established rather than the original
    (wrong) hypothesis that doubling itself was the cause. `unknown_pairs` are
    reported so nothing silently disappears, without implying they matter.
    """
    checks = []

    if cue.pac_count == 0 and cue.tab_offset_count == 0:
        checks.append(
            Check(
                "position_code",
                WARN,
                "No position code sent for this cue - decoder falls back to default placement",
                data={"timecode": cue.timecode},
            )
        )

    if cue.edm_eoc_combined_and_doubled:
        checks.append(
            Check(
                "edm_eoc_combined_and_doubled",
                WARN,
                "EDM+EOC doubled and combined in one block - matches a pattern seen "
                "alongside playback failure in this project's own testing, not a "
                "confirmed CEA-608 violation",
                data={"timecode": cue.timecode},
            )
        )

    for attribute, label in _DOUBLING_CHECKED:
        count = getattr(cue, f"{attribute}_count")
        doubled = getattr(cue, f"{attribute}_doubled")
        if count > 0 and not doubled:
            checks.append(
                Check(
                    "not_doubled",
                    INFO,
                    f"{label} sent once, not doubled - deviates from CEA-608 "
                    "error-correction convention; empirically did not affect "
                    "playback in this project's testing",
                    data={"timecode": cue.timecode, "code": label},
                )
            )

    if cue.enm_count == 0:
        checks.append(
            Check(
                "no_enm",
                INFO,
                "No ENM sent before caption text - empirically did not affect "
                "playback in this project's testing",
                data={"timecode": cue.timecode},
            )
        )

    if cue.unknown_pairs:
        checks.append(
            Check(
                "unknown_pairs",
                INFO,
                f"Unclassified byte pair(s) in cue: {', '.join(cue.unknown_pairs)}",
                data={"timecode": cue.timecode, "pairs": list(cue.unknown_pairs)},
            )
        )

    if not checks:
        checks.append(
            Check("compliant", PASS, "No transport issues found for this cue.", data={"timecode": cue.timecode})
        )

    return checks


def audit_scc_file(path):
    """Parse, group, and evaluate every cue in an .scc file.

    Returns a list of `(CueTransport, list[Check])` pairs, one per cue.
    """
    # Imported here, not at module level, to keep this module's only hard
    # dependency for callers who already have parsed blocks optional.
    from scc_raw_parser import parse_scc_file

    blocks = parse_scc_file(path)
    cues = group_into_cue_blocks(blocks)
    return [(cue, evaluate_cue(cue)) for cue in cues]
