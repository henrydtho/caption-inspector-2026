"""Text subtitle formats: SRT, WebVTT, and TTML, parsed to one shape.

Everything upstream of this module wants the same three facts per cue - when it
starts, when it clears, and what it says. What differs between the formats is
only how hard those facts are to get out:

    SRT   two stamps and a blank line. Nothing to it.
    VTT   a spec: headers, NOTE/STYLE/REGION blocks, cue settings, inline
          markup, and a two-field MM:SS.mmm stamp the SRT pattern misses.
    TTML  XML, with the frame rate in an attribute, times expressed five
          different ways, and timing that nests through the element tree.

So the formats are parsed properly here and handed on as `SubtitleCue`s. The
alternative - one loosening regex per format - is what produced a `.vtt` reader
that silently dropped every cue in a file using two-field stamps.

Frame rate matters in TTML and nowhere else: a TTML file can state
`ttp:frameRate="30" ttp:frameRateMultiplier="1000 1001"`, which is 29.97, and
its `HH:MM:SS:FF` stamps are only meaningful against that rate.
"""

import re
import xml.etree.ElementTree as ElementTree
from fractions import Fraction
from pathlib import Path

from timecode import rate_code_for


# Read by the Tier 1 sidecar reader and the cue extractor alike.
SRT_EXTENSIONS = (".srt",)
VTT_EXTENSIONS = (".vtt", ".webvtt")
# .xml is here because half the TTML in circulation is delivered as .xml; the
# parser sniffs the root element rather than trusting the extension.
TTML_EXTENSIONS = (".ttml", ".dfxp", ".xml", ".itt", ".imsc")
SSA_EXTENSIONS = (".ass", ".ssa")
SAMI_EXTENSIONS = (".smi", ".sami")
SBV_EXTENSIONS = (".sbv",)
# `.sub` is two unrelated formats - SubViewer's timestamped blocks and
# MicroDVD's frame ranges - so the reader sniffs rather than assumes.
SUB_EXTENSIONS = (".sub",)
MPL2_EXTENSIONS = (".mpl",)
LRC_EXTENSIONS = (".lrc",)
RT_EXTENSIONS = (".rt",)
# `.stl` is also two formats: EBU-STL is binary, Spruce/DVD Studio Pro STL is
# text. They are told apart by the EBU GSI signature, not by the extension.
STL_EXTENSIONS = (".stl",)

TEXT_SUBTITLE_EXTENSIONS = (
    SRT_EXTENSIONS
    + VTT_EXTENSIONS
    + TTML_EXTENSIONS
    + SSA_EXTENSIONS
    + SAMI_EXTENSIONS
    + SBV_EXTENSIONS
    + SUB_EXTENSIONS
    + MPL2_EXTENSIONS
    + LRC_EXTENSIONS
    + RT_EXTENSIONS
    + STL_EXTENSIONS
)

# Binary formats with no published, stable specification. They are named here
# so the reader can say "this is a Cheetah CAP, which needs a converter" rather
# than "this is not a subtitle file".
UNSUPPORTED_BINARY_EXTENSIONS = {
    ".cap": "Cheetah/CPC CAP",
    ".pac": "Screen Subtitling PAC",
    ".890": "Screen Subtitling 890",
    ".uni": "Unipac",
}


class SubtitleParseError(ValueError):
    """Raised when a text subtitle file cannot be parsed."""


class SubtitleCue:
    """One timed block of subtitle text, in seconds on the file's own timeline."""

    __slots__ = ("start", "end", "text", "identifier", "speaker")

    def __init__(self, start, end, text, identifier=None, speaker=None):
        self.start = float(start)
        self.end = float(end) if end is not None else None
        self.text = text
        self.identifier = identifier
        self.speaker = speaker

    @property
    def duration(self):
        if self.end is None:
            return None
        return max(0.0, self.end - self.start)

    def as_dict(self):
        return {
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "identifier": self.identifier,
            "speaker": self.speaker,
        }

    def __repr__(self):
        return f"<SubtitleCue {self.start:.3f} {self.text[:40]!r}>"


class SubtitleDocument:
    """Parsed text subtitle file."""

    def __init__(self, path, kind, cues, frame_rate=None, drop_frame=False,
                 language=None, header=None):
        self.path = str(path)
        self.kind = kind
        self.cues = cues
        self.frame_rate = frame_rate
        self.drop_frame = drop_frame
        self.language = language
        self.header = header or {}

    def __bool__(self):
        return bool(self.cues)

    def __len__(self):
        return len(self.cues)

    @property
    def declared_rate_code(self):
        """The x100 rate code this file states, if it states one at all.

        Only TTML carries a frame rate. SRT and VTT are wall-clock formats, and
        inventing a rate for them would be inventing a fact.
        """
        if self.frame_rate is None:
            return None
        return rate_code_for(self.frame_rate)

    def span(self):
        if not self.cues:
            return None, None
        starts = [cue.start for cue in self.cues]
        ends = [cue.end if cue.end is not None else cue.start for cue in self.cues]
        return min(starts), max(ends)


# ---------------------------------------------------------------------------
# SRT
# ---------------------------------------------------------------------------

_SRT_ARROW = re.compile(
    r"(?P<start>\d{1,3}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(?P<end>\d{1,3}:\d{2}:\d{2}[,.]\d{1,3})"
)


def _hms_to_seconds(text):
    """HH:MM:SS.mmm or MM:SS.mmm -> seconds."""
    body = text.strip().replace(",", ".")
    parts = body.split(":")
    if len(parts) == 3:
        hours, minutes, rest = parts
    elif len(parts) == 2:
        hours, minutes, rest = "0", parts[0], parts[1]
    else:
        raise SubtitleParseError(f"Not a timestamp: {text!r}")

    try:
        return int(hours) * 3600 + int(minutes) * 60 + float(rest)
    except ValueError as error:
        raise SubtitleParseError(f"Not a timestamp: {text!r}") from error


def parse_srt(text, path="<srt>"):
    cues = []
    for block in re.split(r"\r?\n\s*\r?\n", text):
        lines = [line.rstrip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue

        arrow_index = next(
            (index for index, line in enumerate(lines) if _SRT_ARROW.search(line)), None
        )
        if arrow_index is None:
            continue

        match = _SRT_ARROW.search(lines[arrow_index])
        identifier = None
        if arrow_index > 0 and lines[arrow_index - 1].strip().isdigit():
            identifier = lines[arrow_index - 1].strip()

        body_lines = lines[arrow_index + 1:]
        body, speaker = _clean_inline_markup("\n".join(body_lines))
        if not body.strip():
            continue

        cues.append(
            SubtitleCue(
                _hms_to_seconds(match.group("start")),
                _hms_to_seconds(match.group("end")),
                body,
                identifier,
                speaker,
            )
        )

    if not cues:
        raise SubtitleParseError(f"No timed cues were found in {Path(path).name}.")

    return SubtitleDocument(path, "srt", cues)


# ---------------------------------------------------------------------------
# WebVTT
# ---------------------------------------------------------------------------

_VTT_ARROW = re.compile(
    r"(?P<start>(?:\d{1,3}:)?\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*"
    r"(?P<end>(?:\d{1,3}:)?\d{1,2}:\d{2}[.,]\d{1,3})(?P<settings>.*)$"
)

# Blocks that carry no dialogue. WebVTT allows all three at file scope.
_VTT_SKIP_BLOCK = re.compile(r"^(NOTE|STYLE|REGION)\b", re.IGNORECASE)

_VTT_TIMESTAMP_TAG = re.compile(r"<\d{1,3}:\d{2}:\d{2}[.,]\d{1,3}>")
_VOICE_TAG = re.compile(r"<v(?:\.[^\s>]+)*\s+([^>]*)>", re.IGNORECASE)
_ANY_TAG = re.compile(r"</?[^>]+>")

_ENTITIES = {
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&apos;": "'",
    "&nbsp;": " ",
    "&lrm;": "",
    "&rlm;": "",
}

# HLS segments carry an offset between the cue timeline and the MPEG-TS clock.
_TIMESTAMP_MAP = re.compile(
    r"X-TIMESTAMP-MAP\s*=\s*(?P<body>.+)$", re.IGNORECASE
)


def _decode_entities(text):
    for entity, replacement in _ENTITIES.items():
        text = text.replace(entity, replacement)
    # Numeric references, decimal and hex.
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
    text = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), text)
    return text


def _clean_inline_markup(text):
    """Strip VTT/SRT inline markup, returning (text, speaker).

    The speaker is worth keeping rather than discarding: it is the one piece of
    markup that carries meaning for a transcript, and `<v Roger>` is how WebVTT
    spells it.
    """
    speaker = None
    voice = _VOICE_TAG.search(text or "")
    if voice:
        # `<v.loud Roger Bingham>` - the classes are before the name.
        speaker = voice.group(1).strip() or None

    cleaned = _VTT_TIMESTAMP_TAG.sub("", text or "")
    cleaned = _ANY_TAG.sub("", cleaned)
    cleaned = _decode_entities(cleaned)

    lines = [" ".join(line.split()) for line in cleaned.splitlines()]
    return "\n".join(line for line in lines if line), speaker


def _parse_cue_settings(text):
    settings = {}
    for token in (text or "").split():
        if ":" in token:
            key, _, value = token.partition(":")
            settings[key.strip().lower()] = value.strip()
    return settings


def parse_vtt(text, path="<vtt>"):
    """Parse a WebVTT file.

    Handles the parts of the spec that show up in real deliveries: the header
    block, NOTE/STYLE/REGION blocks, cue identifiers, cue settings after the
    arrow, two-field `MM:SS.mmm` stamps, and inline markup including voice
    spans.
    """
    body = text.lstrip("﻿")
    lines = body.splitlines()
    if not lines or not lines[0].strip().upper().startswith("WEBVTT"):
        raise SubtitleParseError(
            f"{Path(path).name} does not start with the WEBVTT signature line."
        )

    header = {}
    signature = lines[0].strip()
    if len(signature) > 6:
        header["signature"] = signature[6:].strip(" -\t")

    blocks = re.split(r"\r?\n\s*\r?\n", body)
    cues = []

    for index, block in enumerate(blocks):
        block_lines = [line for line in block.splitlines() if line.strip()]
        if not block_lines:
            continue

        if index == 0:
            # The header block: the signature plus any metadata lines. Stop at
            # the first timing line, which a file with no blank line after the
            # signature will put right here.
            for line in block_lines[1:]:
                if _VTT_ARROW.search(line):
                    break
                mapping = _TIMESTAMP_MAP.search(line)
                if mapping:
                    header["x_timestamp_map"] = mapping.group("body").strip()
                elif ":" in line:
                    key, _, value = line.partition(":")
                    header[key.strip().lower().replace("-", "_")] = value.strip()
            if not any(_VTT_ARROW.search(line) for line in block_lines):
                continue

        if _VTT_SKIP_BLOCK.match(block_lines[0]):
            continue

        arrow_index = next(
            (position for position, line in enumerate(block_lines) if _VTT_ARROW.search(line)),
            None,
        )
        if arrow_index is None:
            continue

        match = _VTT_ARROW.search(block_lines[arrow_index])
        identifier = None
        if arrow_index > 0:
            candidate = block_lines[arrow_index - 1].strip()
            if candidate.upper() != "WEBVTT" and not candidate.upper().startswith("WEBVTT"):
                identifier = candidate

        payload, speaker = _clean_inline_markup("\n".join(block_lines[arrow_index + 1:]))
        if not payload.strip():
            continue

        cues.append(
            SubtitleCue(
                _hms_to_seconds(match.group("start")),
                _hms_to_seconds(match.group("end")),
                payload,
                identifier,
                speaker,
            )
        )

    if not cues:
        raise SubtitleParseError(f"No timed cues were found in {Path(path).name}.")

    return SubtitleDocument(path, "vtt", cues, header=header)


# ---------------------------------------------------------------------------
# TTML
# ---------------------------------------------------------------------------

_OFFSET_TIME = re.compile(r"^(?P<value>\d+(?:\.\d+)?)(?P<metric>h|m|s|ms|f|t)$", re.IGNORECASE)
_CLOCK_TIME = re.compile(
    r"^(?P<hours>\d{1,4}):(?P<minutes>\d{2}):(?P<seconds>\d{2})"
    r"(?:(?P<frac>\.\d+)|:(?P<frames>\d{2,3})(?P<subframes>\.\d+)?)?$"
)


def _local_name(tag):
    """Element/attribute name without its namespace.

    TTML has shipped under at least four namespaces (TTAF1 drafts, TTML1,
    TTML2, IMSC), and matching on the full name means rejecting valid files for
    having the wrong vintage of namespace URI.
    """
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _attribute(element, name, default=None):
    """Look up an attribute by local name, whatever prefix it carries."""
    for key, value in element.attrib.items():
        if _local_name(key) == name:
            return value
    return default


class _TtmlTiming:
    """The frame/tick rates a TTML file declares, and how to read a time with them."""

    def __init__(self, frame_rate, sub_frame_rate, tick_rate, drop_mode):
        self.frame_rate = frame_rate
        self.sub_frame_rate = sub_frame_rate or 1
        self.tick_rate = tick_rate
        self.drop_mode = drop_mode

    def seconds(self, text):
        """Parse a TTML time expression to seconds.

        Both forms in the spec: offset-time (`10s`, `240f`, `50t`) and
        clock-time (`00:00:10.500`, `00:00:10:12` where the last field is
        frames).
        """
        value = (text or "").strip()
        if not value:
            return None

        offset = _OFFSET_TIME.match(value)
        if offset:
            magnitude = float(offset.group("value"))
            metric = offset.group("metric").lower()
            if metric == "h":
                return magnitude * 3600.0
            if metric == "m":
                return magnitude * 60.0
            if metric == "s":
                return magnitude
            if metric == "ms":
                return magnitude / 1000.0
            if metric == "f":
                if not self.frame_rate:
                    raise SubtitleParseError(
                        "This file uses frame offsets but declares no ttp:frameRate."
                    )
                return magnitude / float(self.frame_rate)
            if metric == "t":
                if not self.tick_rate:
                    raise SubtitleParseError(
                        "This file uses tick offsets but declares no ttp:tickRate."
                    )
                return magnitude / float(self.tick_rate)

        clock = _CLOCK_TIME.match(value)
        if not clock:
            raise SubtitleParseError(f"Not a TTML time expression: {text!r}")

        seconds = (
            int(clock.group("hours")) * 3600
            + int(clock.group("minutes")) * 60
            + int(clock.group("seconds"))
        )

        if clock.group("frac"):
            return seconds + float(clock.group("frac"))

        frames_text = clock.group("frames")
        if frames_text is None:
            return float(seconds)

        if not self.frame_rate:
            raise SubtitleParseError(
                f"{text!r} counts frames but the file declares no ttp:frameRate."
            )

        frames = float(frames_text)
        if clock.group("subframes"):
            # `00:00:10:12.2` at subFrameRate 4 is 12 frames plus 2 subframes,
            # i.e. 12.5 frames - the field is a subframe count, not a decimal.
            subframes = int(clock.group("subframes").lstrip("."))
            frames += subframes / float(self.sub_frame_rate or 1)

        return seconds + frames / float(self.frame_rate)


def _ttml_timing(root):
    """Read ttp:frameRate, its multiplier, subFrameRate, tickRate, dropMode."""
    frame_rate = None
    raw_rate = _attribute(root, "frameRate")
    if raw_rate:
        try:
            frame_rate = Fraction(int(float(raw_rate)))
        except (TypeError, ValueError):
            frame_rate = None

    multiplier = _attribute(root, "frameRateMultiplier")
    if frame_rate and multiplier:
        parts = multiplier.replace(",", " ").split()
        if len(parts) == 2:
            try:
                numerator, denominator = int(parts[0]), int(parts[1])
                if denominator:
                    frame_rate = frame_rate * Fraction(numerator, denominator)
            except (TypeError, ValueError, ZeroDivisionError):
                pass

    def _int_attribute(name):
        raw = _attribute(root, name)
        if raw in (None, ""):
            return None
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return None

    sub_frame_rate = _int_attribute("subFrameRate") or 1
    tick_rate = _int_attribute("tickRate")
    if tick_rate is None and frame_rate and sub_frame_rate:
        # Spec default: frameRate x subFrameRate, else 1 tick per second.
        tick_rate = int(float(frame_rate) * sub_frame_rate)

    drop_mode = (_attribute(root, "dropMode") or "nonDrop").strip()
    return _TtmlTiming(frame_rate, sub_frame_rate, tick_rate or 1, drop_mode)


def _ttml_agents(root):
    """xml:id -> agent name, so `ttm:agent` on a `<p>` can name a speaker."""
    agents = {}
    for element in root.iter():
        if _local_name(element.tag) != "agent":
            continue
        agent_id = _attribute(element, "id")
        if not agent_id:
            continue
        name = None
        for child in element.iter():
            if _local_name(child.tag) == "name" and (child.text or "").strip():
                name = child.text.strip()
                break
        if name:
            agents[agent_id] = name
    return agents


def _ttml_text(element):
    """Flatten a `<p>` to text, turning `<br/>` into a line break.

    Spans are transparent - their styling is irrelevant to a transcript, but
    their text is not.
    """
    parts = []

    def walk(node, is_root=False):
        if not is_root and _local_name(node.tag) == "br":
            parts.append("\n")
        elif _local_name(node.tag) == "metadata":
            return
        if node.text:
            parts.append(node.text)
        for child in node:
            walk(child)
            if child.tail:
                parts.append(child.tail)

    walk(element, is_root=True)

    raw = "".join(parts)
    lines = [" ".join(line.split()) for line in raw.split("\n")]
    return "\n".join(line for line in lines if line)


def parse_ttml(text, path="<ttml>"):
    """Parse TTML / DFXP / IMSC / iTT.

    Timing nests: `begin` on a `<div>` shifts every `<p>` inside it, and a
    `seq` container makes each child start where the previous one ended. Both
    are handled, because an ignored `<div begin=...>` silently shifts a whole
    reel.
    """
    try:
        root = ElementTree.fromstring(text.lstrip("﻿"))
    except ElementTree.ParseError as error:
        raise SubtitleParseError(f"{Path(path).name} is not well-formed XML: {error}") from error

    if _local_name(root.tag) != "tt":
        raise SubtitleParseError(
            f"{Path(path).name} is XML but its root element is <{_local_name(root.tag)}>, not <tt>."
        )

    timing = _ttml_timing(root)
    agents = _ttml_agents(root)
    language = _attribute(root, "lang")

    body = next((child for child in root if _local_name(child.tag) == "body"), None)
    if body is None:
        raise SubtitleParseError(f"{Path(path).name} has no <body> element.")

    cues = []

    def resolve(element, parent_start):
        """Return (start, end) for one element, in absolute seconds."""
        begin = timing.seconds(_attribute(element, "begin"))
        end = timing.seconds(_attribute(element, "end"))
        duration = timing.seconds(_attribute(element, "dur"))

        start = parent_start + (begin or 0.0)
        if end is not None:
            finish = parent_start + end
        elif duration is not None:
            finish = start + duration
        else:
            finish = None
        return start, finish

    def walk(element, parent_start):
        container = (_attribute(element, "timeContainer") or "par").strip().lower()
        cursor = parent_start

        for child in element:
            name = _local_name(child.tag)
            if name in ("metadata", "style", "region", "styling", "layout", "head"):
                continue

            base = cursor if container == "seq" else parent_start
            start, finish = resolve(child, base)

            if name == "p":
                content = _ttml_text(child)
                if content.strip():
                    agent_id = _attribute(child, "agent")
                    cues.append(
                        SubtitleCue(
                            start,
                            finish,
                            content,
                            _attribute(child, "id"),
                            agents.get(agent_id) if agent_id else None,
                        )
                    )
            else:
                walk(child, start)

            if container == "seq":
                cursor = finish if finish is not None else start

    walk(body, 0.0)

    if not cues:
        raise SubtitleParseError(f"No timed <p> cues were found in {Path(path).name}.")

    cues.sort(key=lambda cue: cue.start)

    header = {}
    if timing.frame_rate:
        header["frame_rate"] = str(float(timing.frame_rate))
    if timing.drop_mode:
        header["drop_mode"] = timing.drop_mode
    time_base = _attribute(root, "timeBase")
    if time_base:
        header["time_base"] = time_base

    return SubtitleDocument(
        path,
        "ttml",
        cues,
        frame_rate=timing.frame_rate,
        drop_frame=timing.drop_mode.lower().startswith("drop"),
        language=language,
        header=header,
    )


# ---------------------------------------------------------------------------
# SubStation Alpha (.ssa) and Advanced SubStation Alpha (.ass)
# ---------------------------------------------------------------------------

# `H:MM:SS.cc` - one hour digit, centiseconds, and no leading zero on hours.
_SSA_TIME = re.compile(r"^\s*(\d{1,3}):(\d{1,2}):(\d{1,2})[.:](\d{1,3})\s*$")

# `{\i1}`, `{\pos(320,240)}`, `{\p1}` - override blocks, all of them styling.
_SSA_OVERRIDE = re.compile(r"\{[^{}]*\}")
# `\p1` turns the following text into vector drawing coordinates. Left in, a
# banner or a censor box arrives in the transcript as "m 0 0 l 100 0 l 100 50".
_SSA_DRAWING = re.compile(r"\\p([0-9]+)")


def _ssa_time(text):
    match = _SSA_TIME.match(text or "")
    if not match:
        raise SubtitleParseError(f"Not an SSA timestamp: {text!r}")
    hours, minutes, seconds, fraction = match.groups()
    # The field is centiseconds in every SSA/ASS file in circulation, but it is
    # written as a fraction, so scale by its width rather than assuming two.
    fraction_value = int(fraction) / (10 ** len(fraction))
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + fraction_value


def _ssa_text(raw):
    """Strip SSA override blocks and expand its line breaks.

    Text inside a `\\p1` drawing block is coordinates, not dialogue, so it is
    dropped along with the block that opened it.
    """
    parts = []
    drawing = 0
    cursor = 0

    for override in _SSA_OVERRIDE.finditer(raw or ""):
        if not drawing:
            parts.append(raw[cursor:override.start()])
        cursor = override.end()
        for level in _SSA_DRAWING.findall(override.group(0)):
            drawing = int(level)
    if not drawing:
        parts.append(raw[cursor:])

    body = "".join(parts)
    body = body.replace("\\N", "\n").replace("\\n", "\n")
    body = body.replace("\\h", " ")
    lines = [" ".join(line.split()) for line in body.splitlines()]
    return "\n".join(line for line in lines if line)


def parse_ssa(text, path="<ssa>"):
    """Parse SubStation Alpha and Advanced SubStation Alpha.

    The `[Events]` section declares its own column order on a `Format:` line,
    and it genuinely varies between SSA v4 and ASS v4.00+, so the columns are
    read from that line rather than assumed. `Text` is always last and is the
    only field allowed to contain commas.
    """
    fields = None
    cues = []
    header = {}
    section = None

    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip().lower()
            continue

        key, _, value = stripped.partition(":")
        key = key.strip().lower()

        if section == "script info":
            if value.strip() and not stripped.startswith(";"):
                header[key.replace(" ", "_")] = value.strip()
            continue

        if section != "events":
            continue

        if key == "format":
            fields = [name.strip().lower() for name in value.split(",")]
            continue

        if key != "dialogue":
            # `Comment:` lines are authoring notes and `Picture:`/`Sound:` are
            # cue-sheet rows. None of them are on screen.
            continue

        if fields is None:
            fields = [
                "layer", "start", "end", "style", "name",
                "marginl", "marginr", "marginv", "effect", "text",
            ]

        columns = value.split(",", len(fields) - 1)
        if len(columns) < len(fields):
            continue

        row = dict(zip(fields, columns))
        if "start" not in row or "end" not in row:
            continue

        body = _ssa_text(row.get("text", ""))
        if not body.strip():
            continue

        speaker = (row.get("name") or "").strip() or None
        cues.append(
            SubtitleCue(_ssa_time(row["start"]), _ssa_time(row["end"]), body, None, speaker)
        )

    if not cues:
        raise SubtitleParseError(f"No Dialogue events were found in {Path(path).name}.")

    cues.sort(key=lambda cue: cue.start)
    kind = "ass" if str(header.get("scripttype", "")).lower().endswith("+") else "ssa"
    return SubtitleDocument(path, kind, cues, language=header.get("language"), header=header)


# ---------------------------------------------------------------------------
# SAMI (.smi, .sami)
# ---------------------------------------------------------------------------

_SAMI_SYNC = re.compile(r"<\s*SYNC\b([^>]*)>", re.IGNORECASE)
_SAMI_START = re.compile(r"\bSTART\s*=\s*\"?(-?\d+)\"?", re.IGNORECASE)
_SAMI_END = re.compile(r"\bEND\s*=\s*\"?(-?\d+)\"?", re.IGNORECASE)
_SAMI_CLASS = re.compile(r"<\s*P\b[^>]*\bCLASS\s*=\s*\"?([A-Za-z0-9_.-]+)\"?", re.IGNORECASE)
_SAMI_BREAK = re.compile(r"<\s*BR\s*/?\s*>", re.IGNORECASE)


def _sami_text(raw):
    body = _SAMI_BREAK.sub("\n", raw or "")
    body = _ANY_TAG.sub("", body)
    body = _decode_entities(body)
    body = body.replace(" ", " ")
    lines = [" ".join(line.split()) for line in body.splitlines()]
    return "\n".join(line for line in lines if line)


def parse_sami(text, path="<sami>"):
    """Parse SAMI.

    SAMI is nominally SGML and almost never well-formed - unclosed `<P>`, bare
    `&`, mismatched case - so it is read as a stream of `<SYNC Start=...>`
    markers rather than through an XML parser.

    Two things the format leaves implicit: a cue runs until the next `SYNC`,
    and a `SYNC` whose only content is `&nbsp;` is a clear, not a blank
    subtitle. A file may also carry several languages as `<P Class=...>`; the
    class with the most dialogue wins and the rest are listed in the header.
    """
    markers = list(_SAMI_SYNC.finditer(text or ""))
    if not markers:
        raise SubtitleParseError(f"No <SYNC> blocks were found in {Path(path).name}.")

    by_class = {}
    for index, marker in enumerate(markers):
        start_match = _SAMI_START.search(marker.group(1))
        if not start_match:
            continue
        start = int(start_match.group(1)) / 1000.0

        end_match = _SAMI_END.search(marker.group(1))
        explicit_end = int(end_match.group(1)) / 1000.0 if end_match else None

        block_end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        block = text[marker.end():block_end]

        class_match = _SAMI_CLASS.search(block)
        class_name = (class_match.group(1) if class_match else "").upper() or "DEFAULT"

        by_class.setdefault(class_name, []).append((start, explicit_end, _sami_text(block)))

    if not by_class:
        raise SubtitleParseError(f"No <SYNC Start=...> markers were found in {Path(path).name}.")

    def dialogue_count(rows):
        return sum(1 for _, _, body in rows if body.strip())

    primary = max(by_class, key=lambda name: dialogue_count(by_class[name]))
    rows = by_class[primary]

    cues = []
    for index, (start, explicit_end, body) in enumerate(rows):
        if not body.strip():
            continue
        end = explicit_end
        if end is None:
            end = rows[index + 1][0] if index + 1 < len(rows) else None
        cues.append(SubtitleCue(start, end, body))

    if not cues:
        raise SubtitleParseError(f"No timed cues were found in {Path(path).name}.")

    header = {"class": primary}
    if len(by_class) > 1:
        header["available_classes"] = ", ".join(sorted(by_class))
    return SubtitleDocument(path, "sami", cues, language=primary, header=header)


# ---------------------------------------------------------------------------
# SubViewer (.sub) and YouTube SBV (.sbv)
# ---------------------------------------------------------------------------

# SubViewer: `00:00:01.00,00:00:04.00`.  SBV: `0:00:01.000,0:00:04.000`.
_COMMA_RANGE = re.compile(
    r"^\s*(?P<start>\d{1,3}:\d{1,2}:\d{1,2}[.,]\d{1,3})\s*,\s*"
    r"(?P<end>\d{1,3}:\d{1,2}:\d{1,2}[.,]\d{1,3})\s*$"
)


def _subviewer_seconds(text):
    """`HH:MM:SS.cc` or `HH:MM:SS.mmm` - the fraction's width sets its scale."""
    body = text.strip().replace(",", ".")
    hours, minutes, rest = body.split(":")
    whole, _, fraction = rest.partition(".")
    value = int(hours) * 3600 + int(minutes) * 60 + int(whole)
    if fraction:
        value += int(fraction) / (10 ** len(fraction))
    return float(value)


# SubViewer's `[INFORMATION]` block and its `[COLF]...` styling row.
_SUBVIEWER_HEADER = re.compile(r"^\[([A-Z][A-Z ]*)\]\s*(.*)$")


def _parse_comma_range_blocks(text, kind, path, line_break):
    """Read a file whose cues are a `start,end` line followed by their text.

    Scanned line by line rather than split on blank lines: SubViewer files that
    separate cues with a blank line and files that do not are both in
    circulation, and splitting on blanks reads the second kind as one cue whose
    text is the rest of the file.
    """
    cues = []
    header = {}
    pending = None
    body_lines = []

    def flush():
        if pending is None:
            return
        body = "\n".join(body_lines)
        if line_break:
            body = re.sub(line_break, "\n", body, flags=re.IGNORECASE)
        body, speaker = _clean_inline_markup(body)
        if body.strip():
            cues.append(
                SubtitleCue(
                    _subviewer_seconds(pending.group("start")),
                    _subviewer_seconds(pending.group("end")),
                    body,
                    None,
                    speaker,
                )
            )

    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        match = _COMMA_RANGE.match(stripped)
        if match:
            flush()
            pending = match
            body_lines = []
            continue

        if pending is None:
            header_match = _SUBVIEWER_HEADER.match(stripped)
            if header_match and header_match.group(2).strip():
                key = header_match.group(1).strip().lower().replace(" ", "_")
                header[key] = header_match.group(2).strip()
            continue

        body_lines.append(line.rstrip())

    flush()

    if not cues:
        raise SubtitleParseError(f"No timed cues were found in {Path(path).name}.")

    cues.sort(key=lambda cue: cue.start)
    return SubtitleDocument(path, kind, cues, header=header)


def parse_subviewer(text, path="<sub>"):
    """Parse SubViewer 2, whose cues are `start,end` followed by `[br]`-joined text."""
    return _parse_comma_range_blocks(text, "subviewer", path, line_break=r"\[br\]")


def parse_sbv(text, path="<sbv>"):
    """Parse YouTube SBV - SRT's block layout with a comma instead of an arrow."""
    return _parse_comma_range_blocks(text, "sbv", path, line_break=None)


# ---------------------------------------------------------------------------
# MicroDVD (.sub) and MPL2 (.mpl)
# ---------------------------------------------------------------------------

_MICRODVD_LINE = re.compile(r"^\s*\{(?P<start>-?\d+)\}\{(?P<end>-?\d+)\}(?P<body>.*)$")
_MPL2_LINE = re.compile(r"^\s*\[(?P<start>-?\d+)\]\[(?P<end>-?\d+)\](?P<body>.*)$")
# `{y:i}`, `{c:$0000ff}`, `{f:Arial}` - per-cue styling, dropped like SSA's.
_MICRODVD_CONTROL = re.compile(r"^\s*(?:\{[a-zA-Z]:[^{}]*\}\s*)+")

DEFAULT_MICRODVD_FRAME_RATE = 25.0


def _microdvd_body(raw):
    body = _MICRODVD_CONTROL.sub("", raw or "")
    lines = []
    for part in body.split("|"):
        part = _MICRODVD_CONTROL.sub("", part)
        lines.append(" ".join(part.split()))
    return "\n".join(line for line in lines if line)


def parse_microdvd(text, path="<sub>", frame_rate=None):
    """Parse MicroDVD, whose cue times are frame numbers.

    Frames only mean seconds against a rate, and MicroDVD has nowhere to put
    one. The convention the players follow is that a first cue of `{1}{1}25.000`
    is the rate rather than a subtitle, so that is read when present; otherwise
    the file is parsed at 25 and the assumption is recorded in the header where
    the UI can show it, because guessing silently is how a whole reel ends up
    4% out.
    """
    rows = []
    declared_rate = None

    for line in (text or "").splitlines():
        match = _MICRODVD_LINE.match(line)
        if not match:
            continue
        start = int(match.group("start"))
        end = int(match.group("end"))
        body = match.group("body")

        if not rows and start == end and re.fullmatch(r"\s*\d{1,3}(?:[.,]\d+)?\s*", body or ""):
            declared_rate = float(body.strip().replace(",", "."))
            continue

        rows.append((start, end, body))

    if not rows:
        raise SubtitleParseError(f"No {{start}}{{end}} cues were found in {Path(path).name}.")

    rate = frame_rate or declared_rate or DEFAULT_MICRODVD_FRAME_RATE
    if rate <= 0:
        rate = DEFAULT_MICRODVD_FRAME_RATE

    cues = []
    for start, end, body in rows:
        content = _microdvd_body(body)
        if not content.strip():
            continue
        cues.append(SubtitleCue(start / rate, end / rate if end >= start else None, content))

    if not cues:
        raise SubtitleParseError(f"No timed cues were found in {Path(path).name}.")

    header = {"frame_rate": str(rate)}
    if declared_rate is None and frame_rate is None:
        header["frame_rate_source"] = "assumed - the file declares no rate"
    return SubtitleDocument(
        path, "microdvd", cues, frame_rate=Fraction(rate).limit_denominator(1001), header=header
    )


def parse_mpl2(text, path="<mpl>"):
    """Parse MPL2, which is MicroDVD's layout stamped in tenths of a second."""
    cues = []
    for line in (text or "").splitlines():
        match = _MPL2_LINE.match(line)
        if not match:
            continue
        body = "\n".join(
            " ".join(part.lstrip("/").split()) for part in match.group("body").split("|")
        )
        body = "\n".join(part for part in body.splitlines() if part)
        if not body.strip():
            continue
        start = int(match.group("start")) / 10.0
        end = int(match.group("end")) / 10.0
        cues.append(SubtitleCue(start, end if end >= start else None, body))

    if not cues:
        raise SubtitleParseError(f"No [start][end] cues were found in {Path(path).name}.")

    cues.sort(key=lambda cue: cue.start)
    return SubtitleDocument(path, "mpl2", cues)


# ---------------------------------------------------------------------------
# LRC (.lrc)
# ---------------------------------------------------------------------------

_LRC_STAMP = re.compile(r"\[(?P<minutes>\d{1,3}):(?P<seconds>\d{1,2}(?:[.:]\d{1,3})?)\]")
_LRC_TAG = re.compile(r"^\s*\[(?P<key>[a-zA-Z#]+)\s*:(?P<value>[^\]]*)\]\s*$")
_LRC_WORD_STAMP = re.compile(r"<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>")


def parse_lrc(text, path="<lrc>"):
    """Parse LRC, including the enhanced per-word variant.

    A line may carry several stamps (`[00:12.00][01:30.00]chorus`), which is the
    format's way of repeating a line, so each stamp becomes its own cue. Nothing
    in LRC states when a line clears, so a cue runs until the next one starts.
    """
    header = {}
    stamped = []

    for line in (text or "").splitlines():
        tag = _LRC_TAG.match(line)
        if tag and not _LRC_STAMP.match(line.strip()):
            header[tag.group("key").strip().lower()] = tag.group("value").strip()
            continue

        stamps = list(_LRC_STAMP.finditer(line))
        if not stamps:
            continue

        body = _LRC_WORD_STAMP.sub("", line[stamps[-1].end():])
        body = " ".join(body.split())
        if not body:
            continue

        for stamp in stamps:
            seconds_text = stamp.group("seconds").replace(":", ".")
            stamped.append(
                (int(stamp.group("minutes")) * 60 + float(seconds_text), body)
            )

    if not stamped:
        raise SubtitleParseError(f"No [mm:ss] stamps were found in {Path(path).name}.")

    stamped.sort(key=lambda item: item[0])
    cues = [
        SubtitleCue(start, stamped[index + 1][0] if index + 1 < len(stamped) else None, body)
        for index, (start, body) in enumerate(stamped)
    ]
    return SubtitleDocument(path, "lrc", cues, language=header.get("la"), header=header)


# ---------------------------------------------------------------------------
# RealText (.rt)
# ---------------------------------------------------------------------------

_RT_TIME_TAG = re.compile(r"<\s*time\b([^>]*)>", re.IGNORECASE)
_RT_BEGIN = re.compile(r"\b(?:begin|start)\s*=\s*\"?([^\"\s>]+)\"?", re.IGNORECASE)
_RT_END = re.compile(r"\bend\s*=\s*\"?([^\"\s>]+)\"?", re.IGNORECASE)
_RT_CLEAR = re.compile(r"<\s*clear\s*/?\s*>", re.IGNORECASE)


def _realtext_seconds(value):
    """RealText times are `hh:mm:ss.d`, `mm:ss.d`, `ss.d`, or a bare number."""
    body = (value or "").strip()
    if not body:
        raise SubtitleParseError("Empty RealText time value.")
    parts = body.split(":")
    try:
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + float(part.replace(",", "."))
    except ValueError as error:
        raise SubtitleParseError(f"Not a RealText time: {value!r}") from error
    return seconds


def parse_realtext(text, path="<rt>"):
    """Parse RealText, where `<time begin=...>` marks a point in a text stream."""
    markers = list(_RT_TIME_TAG.finditer(text or ""))
    if not markers:
        raise SubtitleParseError(f"No <time> tags were found in {Path(path).name}.")

    rows = []
    for index, marker in enumerate(markers):
        begin = _RT_BEGIN.search(marker.group(1))
        if not begin:
            continue
        end_attribute = _RT_END.search(marker.group(1))

        block_end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        block = text[marker.end():block_end]
        cleared = bool(_RT_CLEAR.search(block))

        body = _SAMI_BREAK.sub("\n", _RT_CLEAR.sub("", block))
        body = _decode_entities(_ANY_TAG.sub("", body))
        body = "\n".join(" ".join(line.split()) for line in body.splitlines())
        body = "\n".join(line for line in body.splitlines() if line)

        rows.append(
            (
                _realtext_seconds(begin.group(1)),
                _realtext_seconds(end_attribute.group(1)) if end_attribute else None,
                body,
                cleared,
            )
        )

    cues = []
    for index, (start, end, body, _cleared) in enumerate(rows):
        if not body.strip():
            continue
        if end is None:
            end = rows[index + 1][0] if index + 1 < len(rows) else None
        cues.append(SubtitleCue(start, end, body))

    if not cues:
        raise SubtitleParseError(f"No timed cues were found in {Path(path).name}.")

    return SubtitleDocument(path, "realtext", cues)


# ---------------------------------------------------------------------------
# Spruce / DVD Studio Pro STL (.stl, text)
# ---------------------------------------------------------------------------

_SPRUCE_LINE = re.compile(
    r"^\s*(?P<start>\d{1,3}:\d{2}:\d{2}[:;.]\d{1,3})\s*,\s*"
    r"(?P<end>\d{1,3}:\d{2}:\d{2}[:;.]\d{1,3})\s*,\s*(?P<body>.*\S)\s*$"
)
_SPRUCE_HEADER = re.compile(r"^\s*\$(?P<key>[A-Za-z_]+)\s*=\s*(?P<value>.*\S)\s*$")

DEFAULT_SPRUCE_FRAME_RATE = 30.0


def parse_spruce_stl(text, path="<stl>", frame_rate=None):
    """Parse Spruce / DVD Studio Pro STL, whose stamps count frames.

    The header may carry `$FrameRate`; when it does not - which is most of the
    time - the file is read at 30 and the assumption is put in the header rather
    than buried, since the last field is frames and reading it at the wrong rate
    moves every cue.
    """
    header = {}
    rows = []

    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue

        header_match = _SPRUCE_HEADER.match(stripped)
        if header_match:
            header[header_match.group("key").strip().lower()] = header_match.group("value").strip()
            continue

        match = _SPRUCE_LINE.match(stripped)
        if match:
            rows.append(match)

    if not rows:
        raise SubtitleParseError(
            f"No `start , end , text` rows were found in {Path(path).name}."
        )

    declared = header.get("framerate") or header.get("frame_rate")
    rate = frame_rate
    if rate is None and declared:
        try:
            rate = float(str(declared).strip().rstrip("iIpP"))
        except ValueError:
            rate = None
    assumed = rate is None
    rate = rate or DEFAULT_SPRUCE_FRAME_RATE

    def seconds(stamp):
        body = stamp.replace(";", ":").replace(".", ":")
        hours, minutes, secs, frames = body.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + int(secs) + int(frames) / rate

    cues = []
    for match in rows:
        body = "\n".join(
            " ".join(part.split()) for part in match.group("body").split("|")
        )
        body, speaker = _clean_inline_markup(body)
        if not body.strip():
            continue
        cues.append(
            SubtitleCue(seconds(match.group("start")), seconds(match.group("end")), body, None, speaker)
        )

    if not cues:
        raise SubtitleParseError(f"No timed cues were found in {Path(path).name}.")

    header["frame_rate"] = str(rate)
    if assumed:
        header["frame_rate_source"] = "assumed - the file declares no $FrameRate"
    drop_frame = any(";" in match.group("start") for match in rows)

    return SubtitleDocument(
        path,
        "spruce-stl",
        cues,
        frame_rate=Fraction(rate).limit_denominator(1001),
        drop_frame=drop_frame,
        header=header,
    )


# ---------------------------------------------------------------------------
# EBU-STL (binary)
# ---------------------------------------------------------------------------


def parse_ebu_stl(path):
    """Read an EBU-STL file into a `SubtitleDocument`.

    The decoding lives in `ebu_stl`; this maps its rows onto the shared cue
    shape. Unlike every other format here it takes a path rather than text,
    because the file is binary and decoding it needs the GSI header first.
    """
    from ebu_stl import StlError, read_stl

    try:
        header, frame_rate, rows = read_stl(path)
    except StlError as error:
        raise SubtitleParseError(str(error)) from error

    cues = [SubtitleCue(start, end, body) for start, end, body in rows if body.strip()]
    if not cues:
        raise SubtitleParseError(f"No timed cues were found in {Path(path).name}.")

    return SubtitleDocument(
        path,
        "ebu-stl",
        cues,
        frame_rate=frame_rate,
        drop_frame=False,
        language=header.get("language_code") or None,
        header={key: value for key, value in header.items() if value},
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

# Extension -> parser, for the formats an extension identifies on its own. The
# ambiguous ones (.sub, .stl) are resolved by sniffing instead.
_PARSER_BY_KIND = {
    "srt": parse_srt,
    "vtt": parse_vtt,
    "ttml": parse_ttml,
    "ssa": parse_ssa,
    "ass": parse_ssa,
    "sami": parse_sami,
    "subviewer": parse_subviewer,
    "sbv": parse_sbv,
    "microdvd": parse_microdvd,
    "mpl2": parse_mpl2,
    "lrc": parse_lrc,
    "realtext": parse_realtext,
    "spruce-stl": parse_spruce_stl,
}

_KIND_BY_EXTENSION = {}
for _extensions, _kind in (
    (SRT_EXTENSIONS, "srt"),
    (VTT_EXTENSIONS, "vtt"),
    (TTML_EXTENSIONS, "ttml"),
    (SAMI_EXTENSIONS, "sami"),
    (SBV_EXTENSIONS, "sbv"),
    (MPL2_EXTENSIONS, "mpl2"),
    (LRC_EXTENSIONS, "lrc"),
    (RT_EXTENSIONS, "realtext"),
    (SUB_EXTENSIONS, "subviewer"),
    (STL_EXTENSIONS, "spruce-stl"),
):
    for _extension in _extensions:
        _KIND_BY_EXTENSION[_extension] = _kind
_KIND_BY_EXTENSION[".ass"] = "ass"
_KIND_BY_EXTENSION[".ssa"] = "ssa"

# Human-readable names, for error messages and for the UI's format column.
FORMAT_LABELS = {
    "srt": "SubRip (SRT)",
    "vtt": "WebVTT",
    "ttml": "TTML / DFXP / IMSC",
    "ssa": "SubStation Alpha",
    "ass": "Advanced SubStation Alpha",
    "sami": "SAMI",
    "subviewer": "SubViewer",
    "sbv": "YouTube SBV",
    "microdvd": "MicroDVD",
    "mpl2": "MPL2",
    "lrc": "LRC",
    "realtext": "RealText",
    "spruce-stl": "Spruce / DVD Studio Pro STL",
    "ebu-stl": "EBU-STL",
    "scc": "Scenarist SCC",
    "mcc": "MacCaption MCC",
}

_SAMI_ROOT = re.compile(r"<\s*SAMI\b", re.IGNORECASE)
_RT_ROOT = re.compile(r"<\s*(?:window|time)\b", re.IGNORECASE)
_SCRIPT_INFO = re.compile(r"^\s*\[Script Info\]", re.IGNORECASE | re.MULTILINE)
_MICRODVD_ANY = re.compile(r"^\s*\{-?\d+\}\{-?\d+\}", re.MULTILINE)
_MPL2_ANY = re.compile(r"^\s*\[-?\d+\]\[-?\d+\]", re.MULTILINE)
_SUBVIEWER_MARKER = re.compile(r"\[INFORMATION\]|\[SUBTITLE\]|\[br\]", re.IGNORECASE)
_COMMA_RANGE_ANY = re.compile(
    r"^\s*\d{1,3}:\d{1,2}:\d{1,2}[.,]\d{1,3}\s*,\s*\d{1,3}:\d{1,2}:\d{1,2}[.,]\d{1,3}\s*$",
    re.MULTILINE,
)
_LRC_ANY = re.compile(r"^\s*\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\]", re.MULTILINE)
_SPRUCE_ANY = re.compile(
    r"^\s*\d{1,3}:\d{2}:\d{2}[:;.]\d{1,3}\s*,\s*\d{1,3}:\d{2}:\d{2}[:;.]\d{1,3}\s*,",
    re.MULTILINE,
)


def _sniff(text):
    """Identify the format from the content, not the extension.

    Order is deliberate: the signatures that can only mean one format are
    tested first, and the line-shape tests - which are the only thing telling
    a MicroDVD `.sub` from a SubViewer `.sub` - come last.
    """
    body = text.lstrip("\ufeff")
    head = body.lstrip()[:400].upper()

    if head.startswith("WEBVTT"):
        return "vtt"
    if head.startswith("<?XML") or head.startswith("<TT") or "<TT " in head[:200]:
        return "ttml"

    sample = body[:8000]
    if _SAMI_ROOT.search(sample):
        return "sami"
    if _SCRIPT_INFO.search(sample):
        # ScriptType is `v4.00+` for ASS and `v4.00` for the original SSA.
        return "ass" if re.search(r"ScriptType\s*:\s*v4\.00\+", sample, re.IGNORECASE) else "ssa"
    if _RT_ROOT.search(sample) and "<TIME" in sample.upper():
        return "realtext"
    if _SRT_ARROW.search(body[:4000] or ""):
        return "srt"
    if _MICRODVD_ANY.search(sample):
        return "microdvd"
    if _MPL2_ANY.search(sample):
        return "mpl2"
    if _SPRUCE_ANY.search(sample):
        return "spruce-stl"
    if _COMMA_RANGE_ANY.search(sample):
        return "subviewer" if _SUBVIEWER_MARKER.search(sample) else "sbv"
    if _LRC_ANY.search(sample):
        return "lrc"
    return None


def _supported_summary():
    return ", ".join(sorted(set(TEXT_SUBTITLE_EXTENSIONS)))


def read_subtitle_document(path):
    """Parse any supported subtitle file into a `SubtitleDocument`.

    The extension is a hint; the content decides. A `.xml` holding TTML, a
    `.vtt` holding SRT, a `.sub` that is MicroDVD rather than SubViewer, and a
    `.stl` that is binary EBU rather than Spruce text are all things vendors
    ship, and each of them is identified by what is inside the file.
    """
    subtitle_path = Path(path)
    if not subtitle_path.exists():
        raise SubtitleParseError(f"File not found: {subtitle_path}")

    suffix = subtitle_path.suffix.lower()
    if suffix in UNSUPPORTED_BINARY_EXTENSIONS:
        raise SubtitleParseError(
            f"{subtitle_path.name} is a {UNSUPPORTED_BINARY_EXTENSIONS[suffix]} file. "
            "That format is binary and undocumented, so it has to be converted to "
            "SRT, STL, or TTML before this app can read it."
        )

    try:
        raw = subtitle_path.read_bytes()
    except OSError as error:
        raise SubtitleParseError(f"Could not read {subtitle_path.name}: {error}") from error

    # EBU-STL is binary and is identified by its GSI signature, so it is tested
    # before anything tries to decode the file as text.
    from ebu_stl import looks_like_ebu_stl

    if looks_like_ebu_stl(raw):
        return parse_ebu_stl(subtitle_path)

    text = raw.decode("utf-8-sig", errors="replace")
    kind = _sniff(text)

    if kind is None:
        kind = _KIND_BY_EXTENSION.get(suffix)
        if kind == "subviewer" and _MICRODVD_ANY.search(text[:8000]):
            kind = "microdvd"

    parser = _PARSER_BY_KIND.get(kind)
    if parser is None:
        raise SubtitleParseError(
            f"{subtitle_path.name} is not a subtitle file this app can read. "
            f"Supported: {_supported_summary()}."
        )

    return parser(text, subtitle_path)


def is_text_subtitle(path):
    return Path(path).suffix.lower() in TEXT_SUBTITLE_EXTENSIONS


def format_label(kind):
    """The name a person would use for this format."""
    return FORMAT_LABELS.get(kind, (kind or "unknown").upper())
