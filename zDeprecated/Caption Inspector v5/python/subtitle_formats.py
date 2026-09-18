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

TEXT_SUBTITLE_EXTENSIONS = SRT_EXTENSIONS + VTT_EXTENSIONS + TTML_EXTENSIONS


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
# Dispatch
# ---------------------------------------------------------------------------


def _sniff(text):
    """Identify the format from the content, not the extension."""
    head = text.lstrip("﻿").lstrip()[:400].upper()
    if head.startswith("WEBVTT"):
        return "vtt"
    if head.startswith("<?XML") or head.startswith("<TT") or "<TT " in head[:200]:
        return "ttml"
    if _SRT_ARROW.search(text[:4000] or ""):
        return "srt"
    return None


def read_subtitle_document(path):
    """Parse an SRT, VTT, or TTML file into a `SubtitleDocument`."""
    subtitle_path = Path(path)
    if not subtitle_path.exists():
        raise SubtitleParseError(f"File not found: {subtitle_path}")

    try:
        text = subtitle_path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as error:
        raise SubtitleParseError(f"Could not read {subtitle_path.name}: {error}") from error

    suffix = subtitle_path.suffix.lower()
    kind = _sniff(text)

    # The extension is a hint; the content decides. A .xml holding TTML and a
    # .vtt holding SRT are both things vendors ship.
    if kind == "ttml" or (kind is None and suffix in TTML_EXTENSIONS):
        return parse_ttml(text, subtitle_path)
    if kind == "vtt" or (kind is None and suffix in VTT_EXTENSIONS):
        return parse_vtt(text, subtitle_path)
    if kind == "srt" or (kind is None and suffix in SRT_EXTENSIONS):
        return parse_srt(text, subtitle_path)

    raise SubtitleParseError(
        f"{subtitle_path.name} is not a text subtitle file this app can read. "
        f"Supported: {', '.join(TEXT_SUBTITLE_EXTENSIONS)}."
    )


def is_text_subtitle(path):
    return Path(path).suffix.lower() in TEXT_SUBTITLE_EXTENSIONS
