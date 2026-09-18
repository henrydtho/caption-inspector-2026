"""EBU-STL (EBU Tech 3264), the binary subtitle exchange format.

Everything else this app reads is text. STL is fixed-width binary: a 1024-byte
GSI header describing the file, then 128-byte TTI blocks, one per subtitle -
except when a subtitle is longer than the 112-byte text field, in which case it
continues across several blocks and only the last one is marked.

Three things in here are easy to get wrong and expensive when you do:

    Frame rate   comes from the GSI Disk Format Code, `STL25.01` or `STL30.01`,
                 and nowhere else. The in and out cues are `hh mm ss ff` as four
                 raw bytes, so reading a 30-frame file at 25 moves every cue.

    Timecodes    are program timecode, not media time. A file that starts at
                 10:00:00:00 is not ten hours of black - it is a normal
                 broadcast deliverable, and its cue times need rebasing against
                 the video the same way SCC does.

    Text         is ISO 6937 by default, which is not Latin-1 and which Python
                 has no codec for. Its accented characters are a diacritic byte
                 followed by the letter it sits on, so they are decoded here to
                 a combining mark and composed.
"""

import unicodedata
from fractions import Fraction
from pathlib import Path


GSI_BLOCK_SIZE = 1024
TTI_BLOCK_SIZE = 128
TEXT_FIELD_SIZE = 112

# TTI text-field control bytes.
_NEWLINE = 0x8A
_UNUSED_SPACE = 0x8F
# 0x80-0x85 open/close italic and underline; 0x00-0x1F teletext colour and
# box attributes. All presentation, none of it dialogue.
_ATTRIBUTE_BYTES = set(range(0x00, 0x20)) | set(range(0x80, 0x86))

# TTI Comment Flag: 0x01 marks an authoring note rather than a subtitle.
_COMMENT_FLAG = 0x01

# TTI Extension Block Number: 0xFF is the last block of a subtitle.
_LAST_BLOCK = 0xFF

# Character Code Table -> Python codec, for the tables that have one.
_CCT_CODECS = {
    "01": "iso8859_5",   # Cyrillic
    "02": "iso8859_6",   # Arabic
    "03": "iso8859_7",   # Greek
    "04": "iso8859_8",   # Hebrew
}

# ISO 6937 diacritic bytes -> the Unicode combining mark they apply to the
# character that follows.
_ISO6937_DIACRITICS = {
    0xC1: "̀",  # grave
    0xC2: "́",  # acute
    0xC3: "̂",  # circumflex
    0xC4: "̃",  # tilde
    0xC5: "̄",  # macron
    0xC6: "̆",  # breve
    0xC7: "̇",  # dot above
    0xC8: "̈",  # diaeresis
    0xCA: "̊",  # ring above
    0xCB: "̧",  # cedilla
    0xCD: "̋",  # double acute
    0xCE: "̨",  # ogonek
    0xCF: "̌",  # caron
}

# ISO 6937 supplementary set: the single-byte characters above 0xA0.
_ISO6937_SUPPLEMENTARY = {
    0xA1: "¡", 0xA2: "¢", 0xA3: "£", 0xA4: "$",      0xA5: "¥",
    0xA7: "§", 0xA8: "¤", 0xA9: "‘", 0xAA: "“", 0xAB: "«",
    0xAC: "←", 0xAD: "↑", 0xAE: "→", 0xAF: "↓",
    0xB0: "°", 0xB1: "±", 0xB2: "²", 0xB3: "³", 0xB4: "×",
    0xB5: "µ", 0xB6: "¶", 0xB7: "·", 0xB8: "÷", 0xB9: "’",
    0xBA: "”", 0xBB: "»", 0xBC: "¼", 0xBD: "½", 0xBE: "¾",
    0xBF: "¿",
    0xD0: "―", 0xD1: "¹", 0xD2: "®", 0xD3: "©", 0xD4: "™",
    0xD5: "♪", 0xD6: "¬", 0xD7: "¦",
    0xDC: "⅛", 0xDD: "⅜", 0xDE: "⅝", 0xDF: "⅞",
    0xE0: "Ω", 0xE1: "Æ", 0xE2: "Đ", 0xE3: "ª", 0xE4: "Ħ",
    0xE6: "Ĳ", 0xE7: "Ŀ", 0xE8: "Ł", 0xE9: "Ø", 0xEA: "Œ",
    0xEB: "º", 0xEC: "Þ", 0xED: "Ŧ", 0xEE: "Ŋ", 0xEF: "ŉ",
    0xF0: "ĸ", 0xF1: "æ", 0xF2: "đ", 0xF3: "ð", 0xF4: "ħ",
    0xF5: "ı", 0xF6: "ĳ", 0xF7: "ŀ", 0xF8: "ł", 0xF9: "ø",
    0xFA: "œ", 0xFB: "ß", 0xFC: "þ", 0xFD: "ŧ", 0xFE: "ŋ",
    0xFF: "­",
}


class StlError(ValueError):
    """Raised when a file is not a readable EBU-STL."""


def looks_like_ebu_stl(data):
    """Does this blob carry the GSI Disk Format Code?

    `.stl` is also Spruce/DVD Studio Pro's text format, so the two are told
    apart by the signature rather than the extension.
    """
    if len(data) < 11:
        return False
    return bytes(data[3:6]).upper() == b"STL"


def _ascii(data, start, length):
    return bytes(data[start:start + length]).decode("ascii", errors="replace").strip("\x00 ").strip()


def _decode_iso6937(payload):
    """Decode an ISO 6937 byte string.

    A byte in the 0xC1-0xCF range is a diacritic that belongs to the *next*
    character, so it is buffered and composed rather than emitted.
    """
    out = []
    pending = None

    for byte in payload:
        if byte in _ISO6937_DIACRITICS:
            pending = _ISO6937_DIACRITICS[byte]
            continue

        if byte < 0x80:
            character = chr(byte)
        else:
            character = _ISO6937_SUPPLEMENTARY.get(byte)
            if character is None:
                pending = None
                continue

        if pending:
            character = unicodedata.normalize("NFC", character + pending)
            pending = None
        out.append(character)

    return "".join(out)


def _decode_text_field(payload, cct):
    """Turn one TTI text field into lines of dialogue."""
    codec = _CCT_CODECS.get(cct)
    lines = []
    current = bytearray()

    def flush():
        if codec:
            text = bytes(current).decode(codec, errors="replace")
        else:
            text = _decode_iso6937(bytes(current))
        lines.append(" ".join(text.split()))
        current.clear()

    for byte in payload:
        if byte == _UNUSED_SPACE:
            break
        if byte == _NEWLINE:
            flush()
            continue
        if byte in _ATTRIBUTE_BYTES:
            continue
        current.append(byte)

    flush()
    return "\n".join(line for line in lines if line)


def _timecode_seconds(data, offset, frame_rate):
    hours, minutes, seconds, frames = (int(value) for value in data[offset:offset + 4])
    return hours * 3600 + minutes * 60 + seconds + frames / frame_rate


def _frame_rate_from_dfc(dfc):
    code = (dfc or "").upper()
    if code.startswith("STL25"):
        return 25.0
    if code.startswith("STL30"):
        return 30.0
    raise StlError(
        f"Unrecognised STL Disk Format Code {dfc!r}. Expected STL25.01 or STL30.01."
    )


def read_gsi(data):
    """The GSI header fields worth carrying downstream."""
    if len(data) < GSI_BLOCK_SIZE:
        raise StlError("The file is shorter than the 1024-byte GSI header.")

    dfc = _ascii(data, 3, 8)
    cct = _ascii(data, 12, 2) or "00"

    return {
        "code_page": _ascii(data, 0, 3),
        "disk_format_code": dfc,
        "display_standard": _ascii(data, 11, 1),
        "character_code_table": cct,
        "language_code": _ascii(data, 14, 2),
        "original_programme_title": _ascii(data, 16, 32),
        "original_episode_title": _ascii(data, 48, 32),
        "translated_programme_title": _ascii(data, 80, 32),
        "translator": _ascii(data, 144, 32),
        "creation_date": _ascii(data, 224, 6),
        "revision_date": _ascii(data, 230, 6),
        "total_tti_blocks": _ascii(data, 238, 5),
        "total_subtitles": _ascii(data, 243, 5),
        "max_characters_per_row": _ascii(data, 251, 2),
        "max_rows": _ascii(data, 253, 2),
        "timecode_status": _ascii(data, 255, 1),
        "start_of_programme": _ascii(data, 256, 8),
        "first_in_cue": _ascii(data, 264, 8),
        "country_of_origin": _ascii(data, 274, 3),
        "publisher": _ascii(data, 277, 32),
    }


def read_stl(path):
    """Read an EBU-STL file into `(header, frame_rate, [(start, end, text)])`.

    Times are in seconds of program timecode. Comment blocks (`CF = 1`) are
    authoring notes rather than subtitles and are dropped.
    """
    stl_path = Path(path)
    try:
        data = stl_path.read_bytes()
    except OSError as error:
        raise StlError(f"Could not read {stl_path.name}: {error}") from error

    if not looks_like_ebu_stl(data):
        raise StlError(f"{stl_path.name} does not carry an EBU-STL GSI signature.")

    header = read_gsi(data)
    frame_rate = _frame_rate_from_dfc(header["disk_format_code"])
    cct = header["character_code_table"]

    body = data[GSI_BLOCK_SIZE:]
    if len(body) < TTI_BLOCK_SIZE:
        raise StlError(f"{stl_path.name} has a GSI header but no TTI blocks.")

    rows = []
    pending_start = None
    pending_end = None
    pending_lines = []

    for offset in range(0, len(body) - TTI_BLOCK_SIZE + 1, TTI_BLOCK_SIZE):
        block = body[offset:offset + TTI_BLOCK_SIZE]

        extension_block_number = block[3]
        comment_flag = block[15]

        start = _timecode_seconds(block, 5, frame_rate)
        end = _timecode_seconds(block, 9, frame_rate)
        text = _decode_text_field(block[16:16 + TEXT_FIELD_SIZE], cct)

        # The spec enumerates CF as 0x00 or 0x01, so only 0x01 means comment.
        # Reading "anything non-zero" instead would silently drop subtitles from
        # a writer that leaves the byte as padding, and losing dialogue is a far
        # worse failure than carrying the odd authoring note.
        if comment_flag == _COMMENT_FLAG:
            continue

        if pending_start is None:
            pending_start, pending_end = start, end
        if text:
            pending_lines.append(text)

        # 0xFF marks the last block of a subtitle; anything lower is a
        # continuation whose text belongs to the block that opened it.
        if extension_block_number == _LAST_BLOCK:
            body_text = "\n".join(line for line in pending_lines if line)
            if body_text.strip():
                rows.append((pending_start, pending_end, body_text))
            pending_start = pending_end = None
            pending_lines = []

    # A file whose final subtitle was never terminated still has that subtitle.
    if pending_lines:
        body_text = "\n".join(line for line in pending_lines if line)
        if body_text.strip():
            rows.append((pending_start, pending_end, body_text))

    if not rows:
        raise StlError(f"No displayable subtitles were found in {stl_path.name}.")

    return header, Fraction(frame_rate).limit_denominator(1001), rows
