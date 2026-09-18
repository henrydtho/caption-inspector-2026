"""Byte-pair classification for raw Scenarist SCC control codes.

This only classifies *whether* a pair is a preamble address code, a tab
offset, or one of the small set of unambiguous control codes - not what a PAC
resolves to (row/column). Resolving PAC to row/column would need a full
CEA-608 PAC table, and this project has no verified source for one beyond the
handful of pairs cross-checked below, so `CueTransport.pac_values` (see
`scc_transport_audit.py`) stays as raw hex.

## Where the PAC / Tab Offset rule below came from, and the conflict in it

The classification rule (mask off the parity bit, first byte in 0x10-0x17 or
0x18-0x1f, second byte in a control-dependent range) is ccextractor's
`disCommand()` in `ccx_decoders_608.c` - open source, same lineage as `libci`.
It was cross-checked against five real byte pairs already confirmed in this
project's test fixtures (`test/media/scc_transport/`, see
`test__scc_transport_audit.py`): `9470`, `94d0`, `94f2`, `97a1`, `97a2`.

Three of those (`9470`, `94d0`, `94f2`) land in ccextractor's PAC range
(second byte 0x40-0x7f). The other two (`97a1`, `97a2`) do not - by
ccextractor's own table they are Tab Offset (TO1/TO2), a distinct CEA-608
control code from PAC, sent immediately after a PAC to fine-tune its column.
That is a real disagreement between "cross-check these five pairs" and "PAC
covers all of them", not a rounding error, so this module keeps Tab Offset as
its own `TAB_OFFSET` classification rather than folding it into `PAC` -
resolved this way deliberately rather than picked silently. Task 4's "no
position code sent" check treats `PAC` and `TAB_OFFSET` as equally counting as
positioning, since a cue in this project's own fixtures carries both together.
"""

RCL = "9420"
ENM = "94ae"
EDM = "942c"
EOC = "942f"
PADDING = "8080"

_EXACT_KINDS = {
    RCL: "RCL",
    ENM: "ENM",
    EDM: "EDM",
    EOC: "EOC",
    PADDING: "PADDING",
}


def _looks_like_control_byte(byte):
    """First byte bit pattern 1001xxxx or 1000xxxx - CEA-608's control-code ranges."""
    return (byte >> 4) in (0x8, 0x9)


def classify_pair(pair_hex):
    """Classify one 4-hex-character SCC byte pair.

    Returns one of "RCL", "ENM", "EDM", "EOC", "PADDING", "PAC", "TAB_OFFSET",
    "TEXT" (the default bucket - SCC text is itself byte-pair encoded), or
    "UNKNOWN" (looks like a control code but is not in any known table here).
    """
    normalized = pair_hex.strip().lower()

    exact = _EXACT_KINDS.get(normalized)
    if exact is not None:
        return exact

    if len(normalized) != 4:
        return "UNKNOWN"

    try:
        first = int(normalized[:2], 16)
        second = int(normalized[2:], 16)
    except ValueError:
        return "UNKNOWN"

    if not _looks_like_control_byte(first):
        return "TEXT"

    # Strip parity (bit 7) to get the underlying 7-bit command bytes, per
    # ccextractor's disCommand()/handle_pac().
    hi = first & 0x7F
    lo = second & 0x7F

    # Channel 2 uses hi 0x18-0x1f; disCommand() normalizes it before dispatch.
    if 0x18 <= hi <= 0x1F:
        hi -= 8

    if hi == 0x17 and 0x21 <= lo <= 0x23:
        return "TAB_OFFSET"

    if 0x10 <= hi <= 0x17 and 0x40 <= lo <= 0x7F:
        return "PAC"

    return "UNKNOWN"
