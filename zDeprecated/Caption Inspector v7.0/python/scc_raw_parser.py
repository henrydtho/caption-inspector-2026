"""Raw parser for the plain-text Scenarist_SCC V1.0 format.

This bypasses `libci` entirely. `.scc` is a hex-pairs-in-a-text-file format, so
reading it does not need the C decoder - and for the transport audit, it must
not go through it: `libci`'s decoded output has already collapsed doubled
control codes into one logical command by the time it reaches the Python
layer (see `caption_cues.py` / `inspection_support.py`), which is exactly the
transmission-level detail this audit needs to see.

This module only reads lines into raw byte-pair blocks. It does not interpret
the pairs (see `scc_code_table.py`) or group them into cues (see
`scc_transport_audit.py`).
"""

from dataclasses import dataclass, field
from pathlib import Path

_HEADER = "Scenarist_SCC V1.0"


@dataclass
class SccBlock:
    """One line of the file: a timecode and the raw hex-pair tokens after it."""

    timecode: str
    pairs: list = field(default_factory=list)


def parse_scc_file(path):
    """Parse a Scenarist_SCC V1.0 file into one `SccBlock` per non-blank line.

    Blocks are returned in file order and are never merged - a cue's "build"
    line and its later "erase" line are separate blocks. Grouping them into
    logical cues is `scc_transport_audit.group_into_cue_blocks`'s job, not
    this parser's.
    """
    # The format uses \r\n line endings; splitlines() handles that (and bare
    # \r or \n) without assuming which one a given file actually used.
    text = Path(path).read_text(encoding="utf-8", errors="replace")

    blocks = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line == _HEADER:
            continue

        timecode, tab, payload = line.partition("\t")
        if not tab:
            # Tolerate a file whose tab got mangled into runs of spaces.
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            timecode, payload = parts

        pairs = payload.split()
        if not pairs:
            continue

        blocks.append(SccBlock(timecode=timecode.strip(), pairs=pairs))

    return blocks
