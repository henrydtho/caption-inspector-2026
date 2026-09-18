#!/usr/bin/env python3
"""Byte-level transport diff between two .scc files.

    python3 python/transport_compare.py approved.scc delivered.scc

This is the transport-structure diff `subtitle_compare.py` does not do - it
only compares cue text/timing, never how the control codes for a cue were
actually sent. `compare()` runs `scc_transport_audit`'s per-cue audit on both
files and reports every `CueTransport` field where the two differ, cue by
cue. Cues are matched by position (first cue against first cue, and so on) -
there is no text-based alignment here, unlike `subtitle_compare.py`, because
transport structure has no text to align on.

Only applies to `.scc` input; see `scc_transport_audit.py` for why.
"""

import argparse
import sys
from dataclasses import fields
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scc_raw_parser import parse_scc_file  # noqa: E402
from scc_transport_audit import CueTransport, group_into_cue_blocks  # noqa: E402

# Compared in this order; pac_values is a list and always shown alongside a
# pac_count difference rather than compared on its own.
_COMPARED_FIELDS = [
    field.name for field in fields(CueTransport) if field.name not in ("timecode", "pac_values")
]


class CueDiff:
    """The fields that differ for one cue index, plus both cues' raw values."""

    def __init__(self, index, cue_a, cue_b, differing_fields):
        self.index = index
        self.cue_a = cue_a
        self.cue_b = cue_b
        self.differing_fields = differing_fields

    def as_lines(self):
        lines = [f"Cue {self.index}: {self.cue_a.timecode} / {self.cue_b.timecode}"]
        for field_name in self.differing_fields:
            value_a = getattr(self.cue_a, field_name)
            value_b = getattr(self.cue_b, field_name)
            lines.append(f"  {field_name}: {value_a!r} -> {value_b!r}")
            if field_name == "pac_count":
                lines.append(f"    pac_values: {self.cue_a.pac_values!r} -> {self.cue_b.pac_values!r}")
        return lines


class TransportComparisonResult:
    def __init__(self, path_a, path_b, cues_a, cues_b, cue_diffs):
        self.path_a = path_a
        self.path_b = path_b
        self.cues_a = cues_a
        self.cues_b = cues_b
        self.cue_diffs = cue_diffs

    @property
    def cue_count_differs(self):
        return len(self.cues_a) != len(self.cues_b)

    def render_text(self):
        lines = [
            f"Transport comparison: {Path(self.path_a).name} vs {Path(self.path_b).name}",
            f"  {len(self.cues_a)} cue(s) vs {len(self.cues_b)} cue(s)",
        ]

        if self.cue_count_differs:
            lines.append(
                "  Cue counts differ - only the cues present in both files were compared."
            )

        if not self.cue_diffs:
            lines.append("  No transport differences in the cues compared.")
            return "\n".join(lines)

        lines.append(f"  {len(self.cue_diffs)} cue(s) differ:")
        for diff in self.cue_diffs:
            lines.extend(f"  {line}" for line in diff.as_lines())

        return "\n".join(lines)


def _audit(path):
    blocks = parse_scc_file(path)
    return group_into_cue_blocks(blocks)


def compare(path_a, path_b):
    """Run the transport audit on both files and diff them cue by cue."""
    cues_a = _audit(path_a)
    cues_b = _audit(path_b)

    cue_diffs = []
    for index, (cue_a, cue_b) in enumerate(zip(cues_a, cues_b), start=1):
        differing_fields = [
            field_name
            for field_name in _COMPARED_FIELDS
            if getattr(cue_a, field_name) != getattr(cue_b, field_name)
        ]
        if differing_fields:
            cue_diffs.append(CueDiff(index, cue_a, cue_b, differing_fields))

    return TransportComparisonResult(path_a, path_b, cues_a, cues_b, cue_diffs)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Diff the raw SCC transport structure of two .scc files, cue by cue.",
    )
    parser.add_argument("file_a", help="The reference file")
    parser.add_argument("file_b", help="The file to compare against it")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    for path in (args.file_a, args.file_b):
        if Path(path).suffix.lower() != ".scc":
            print(f"Error: {path} is not a .scc file - this only compares raw SCC transport.", file=sys.stderr)
            return 1

    try:
        result = compare(args.file_a, args.file_b)
    except OSError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    print(result.render_text())

    if result.cue_diffs or result.cue_count_differs:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
