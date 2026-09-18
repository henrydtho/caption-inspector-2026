import io
import re
from contextlib import redirect_stdout
from pathlib import Path

from cshim import get_decoded_captions
from subtitle_formats import (
    TEXT_SUBTITLE_EXTENSIONS,
    UNSUPPORTED_BINARY_EXTENSIONS,
    SubtitleParseError,
    format_label,
    read_subtitle_document,
)


# Containers and broadcast caption files, which go through the C decoder.
DECODED_TYPES = ("ts", "mpg", "mp4", "mcc", "scc", "mov")

# Text subtitle files, which are parsed in Python. Named without the dot to
# match `DECODED_TYPES`, since the two are concatenated for the file pickers.
SUBTITLE_TYPES = tuple(extension.lstrip(".") for extension in TEXT_SUBTITLE_EXTENSIONS)

SUPPORTED_TYPES = DECODED_TYPES + SUBTITLE_TYPES

# The track family subtitle cues are filed under. It is not CEA-608 or CEA-708:
# a WebVTT file has no channels, no services, and no control codes, and saying
# otherwise in the track list would be a lie about the delivery.
SUBTITLE_FAMILY = "Subtitles"

SUBTITLE_ROW_TYPE = "SubtitleCue"

# Row types that carry dialogue, whatever produced them.
TEXT_ROW_TYPES = ("Line21TextString", "DtvccTextString", SUBTITLE_ROW_TYPE)


def validate_input_path(input_path):
    suffix = Path(input_path).suffix.lower()
    if suffix in UNSUPPORTED_BINARY_EXTENSIONS:
        raise ValueError(
            f"{UNSUPPORTED_BINARY_EXTENSIONS[suffix]} files are binary and undocumented. "
            "Convert the file to SRT, STL, or TTML first."
        )
    if suffix.lstrip(".") not in SUPPORTED_TYPES:
        raise ValueError(
            f"Unsupported file type: {suffix or 'no extension'}. "
            f"Supported: {', '.join('.' + name for name in SUPPORTED_TYPES)}."
        )


def _subtitle_timestamp(seconds):
    """`HH:MM:SS.mmm`, bracketed to match the decoder's caption times."""
    if seconds is None:
        return ""
    whole = int(seconds)
    milliseconds = int(round((seconds - whole) * 1000))
    if milliseconds == 1000:
        whole += 1
        milliseconds = 0
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"[{hours:02d}:{minutes:02d}:{secs:02d}.{milliseconds:03d}]"


def subtitle_tracks(input_path):
    """Read a text subtitle file into the same track shape the decoder returns.

    One file is one track. Its rows carry the cue's own end time, which the
    decoded formats have no equivalent of - 608 clears on a later control code,
    not on a stamp of its own - so it is kept on the row rather than discarded
    to make the two shapes identical.
    """
    document = read_subtitle_document(input_path)

    rows = []
    for index, cue in enumerate(document.cues, start=1):
        rows.append(
            {
                "time": _subtitle_timestamp(cue.start),
                "type": SUBTITLE_ROW_TYPE,
                "event": cue.text,
                "end_time": _subtitle_timestamp(cue.end),
                "index": index,
                "speaker": cue.speaker,
                "identifier": cue.identifier,
            }
        )

    track_name = format_label(document.kind)
    if document.language:
        track_name = f"{track_name} ({document.language})"

    return {SUBTITLE_FAMILY: {track_name: rows}}, document


def element_to_row(element):
    caption_time = getattr(element, "caption_time", None)
    return {
        "time": str(caption_time) if caption_time else "",
        "type": type(element).__name__,
        "event": str(element),
    }


def text_preview(rows):
    lines = []
    for row in rows:
        if row["type"] not in TEXT_ROW_TYPES:
            continue
        lines.append(f"{row['time']} {row['event']}".strip())
    return "\n".join(lines)


def format_subtitle_cues(rows):
    """The cue-by-cue view of a text subtitle track.

    The decoded formats need `format_visible_caption_cues` to reconstruct when
    text reaches the screen from the control codes around it. A text subtitle
    file states both ends outright, so this reports what the file says rather
    than inferring anything.
    """
    if not rows:
        return "This subtitle file contains no cues.", 0

    lines = []
    for row in rows:
        lines.append(f"Cue {row['index']:03d}")
        lines.append(f"In:  {format_caption_time(row['time'])}")
        if row.get("end_time"):
            lines.append(f"Out: {format_caption_time(row['end_time'])}")
        else:
            lines.append("Out: not stated by the file")
        if row.get("identifier"):
            lines.append(f"Identifier: {row['identifier']}")
        if row.get("speaker"):
            lines.append(f"Speaker: {row['speaker']}")
        lines.append("Text:")
        for text_line in str(row["event"]).splitlines() or [""]:
            lines.append(f"  {text_line}")
        lines.append("")

    return "\n".join(lines).rstrip(), len(rows)


def normalized_event_text(row):
    event_text = row["event"].replace("\n", " ").strip()
    time_prefix = f"{row['time']} - "
    if event_text.startswith(time_prefix):
        event_text = event_text[len(time_prefix):].strip()
    return event_text


def format_caption_time(caption_time):
    return caption_time.strip("[]")


def track_channel_label(track_name):
    if track_name.startswith("Channel "):
        suffix = track_name.split()[-1]
        return f"CC{suffix}"
    return track_name


def parse_pac_position(event_text):
    match = re.fullmatch(r"\{R(?P<row>\d+):C(?P<column>\d+)(?::UL)?\}", event_text)
    if not match:
        return None

    return {
        "row": int(match.group("row")),
        "column": int(match.group("column")),
        "tab": None,
    }


def parse_tab_offset(event_text):
    match = re.fullmatch(r"\{TO(?P<tab>\d+)\}", event_text)
    if not match:
        return None
    return int(match.group("tab"))


def format_position_summary(position):
    line = f"Row {position['row']}, Column {position['column']}"
    if position.get("tab") is not None:
        line += f", Tab {position['tab']}"
    return line


def _dedupe_consecutive_rows(rows):
    deduped_rows = []
    previous_signature = None

    for row in rows:
        signature = (row["type"], normalized_event_text(row))
        if signature == previous_signature:
            continue
        deduped_rows.append(row)
        previous_signature = signature

    return deduped_rows


def build_visible_caption_cues(rows):
    cues = []
    current_cue = None

    for row in _dedupe_consecutive_rows(rows):
        event_text = normalized_event_text(row)
        row_type = row["type"]

        if row_type == "Line21ControlCode" and event_text == "{RCL}":
            current_cue = {
                "start_time": row["time"],
                "visible_in_time": None,
                "commands": [],
                "positioning": [],
                "text_lines": [],
            }
            current_cue["commands"].append("RCL")
            continue

        if current_cue is None:
            continue

        if row_type == "Line21ControlCode" and event_text == "{ENM}":
            if "ENM" not in current_cue["commands"]:
                current_cue["commands"].append("ENM")
            continue

        if row_type == "PreambleAccessCode":
            position = parse_pac_position(event_text)
            if position and position not in current_cue["positioning"]:
                current_cue["positioning"].append(position)
            continue

        if row_type == "TabControlCode":
            tab_offset = parse_tab_offset(event_text)
            if tab_offset is not None:
                if current_cue["positioning"]:
                    current_cue["positioning"][-1]["tab"] = tab_offset
                else:
                    current_cue["positioning"].append({"row": None, "column": None, "tab": tab_offset})
            continue

        if row_type == "Line21TextString":
            current_cue["text_lines"].append(event_text.strip('"'))
            continue

        if row_type == "Line21ControlCode" and event_text == "{EOC}":
            if "EOC" not in current_cue["commands"]:
                current_cue["commands"].append("EOC")
            current_cue["visible_in_time"] = row["time"]
            if current_cue["text_lines"]:
                cues.append(current_cue)
            current_cue = None

    return cues


def format_visible_caption_cues(rows, track_name):
    cues = build_visible_caption_cues(rows)
    if not cues:
        return "No visible caption cues were found for this track.", 0

    channel_label = track_channel_label(track_name)
    lines = []
    for cue_index, cue in enumerate(cues, start=1):
        lines.append(f"Cue {cue_index:03d}")
        lines.append(f"Build start: {format_caption_time(cue['start_time'])}")
        lines.append(f"Visible in: {format_caption_time(cue['visible_in_time'])}")
        lines.append("Mode: pop-on")
        lines.append(f"Channel: {channel_label}")
        lines.append(f"Commands: {', '.join(cue['commands'])}")
        if cue["positioning"]:
            lines.append("Positioning:")
            for position in cue["positioning"]:
                if position["row"] is None:
                    lines.append(f"  Tab {position['tab']}")
                else:
                    lines.append(f"  {format_position_summary(position)}")
        lines.append("Visible text:")
        for text_line in cue["text_lines"]:
            lines.append(f"  {text_line}")
        lines.append("")

    return "\n".join(lines).rstrip(), len(cues)


def collect_tracks(engine):
    tracks = {"CEA-608": {}, "CEA-708": {}}

    for channel in range(1, 5):
        elements = engine.get_l21_cc_elements(channel)
        if elements:
            tracks["CEA-608"][f"Channel {channel}"] = [element_to_row(element) for element in elements]

    for service in range(1, 17):
        elements = engine.get_dtvcc_cc_elements(service)
        if elements:
            tracks["CEA-708"][f"Service {service}"] = [element_to_row(element) for element in elements]

    return tracks


def track_summary_rows(tracks):
    rows = []
    for family, family_tracks in tracks.items():
        for track_name, track_rows in family_tracks.items():
            text_events = sum(1 for row in track_rows if row["type"] in TEXT_ROW_TYPES)
            rows.append(
                {
                    "family": family,
                    "track": track_name,
                    "events": len(track_rows),
                    "text_events": text_events,
                }
            )
    return rows


def decode_file(input_path, framerate, capture_logs=False):
    """Decode or parse `input_path` into `{family: {track: rows}}`.

    Text subtitle files never reach the C decoder - it reads caption data out of
    a transport stream or an SCC/MCC payload, and an SRT file is neither.
    """
    validate_input_path(input_path)

    if Path(input_path).suffix.lower() in TEXT_SUBTITLE_EXTENSIONS:
        try:
            tracks, document = subtitle_tracks(input_path)
        except SubtitleParseError as error:
            raise ValueError(str(error)) from error
        log = _subtitle_log(document)
        return (tracks, log) if capture_logs else (tracks, "")

    def decode_tracks():
        engine = get_decoded_captions(str(input_path), framerate)
        if not hasattr(engine, "get_l21_cc_elements") or not hasattr(engine, "get_dtvcc_cc_elements"):
            raise RuntimeError("Caption decoding failed. For SCC inputs, verify that a valid frame rate such as 2400 is set.")
        return collect_tracks(engine)

    if capture_logs:
        log_stream = io.StringIO()
        with redirect_stdout(log_stream):
            tracks = decode_tracks()
        return tracks, log_stream.getvalue()

    return decode_tracks(), ""


def _subtitle_log(document):
    """What the decoder pane shows for a parsed subtitle file.

    Whatever the file declared about itself goes here: the header fields are
    where a wrong frame rate or an assumed one becomes visible.
    """
    lines = [f"Parsed as {format_label(document.kind)}.", f"Cues: {len(document.cues)}"]

    start, end = document.span()
    if start is not None:
        lines.append(f"Span: {_subtitle_timestamp(start)} to {_subtitle_timestamp(end)}")
    if document.frame_rate is not None:
        lines.append(f"Declared frame rate: {float(document.frame_rate):g}")
    if document.language:
        lines.append(f"Language: {document.language}")

    if document.header:
        lines.append("")
        lines.append("File header:")
        for key, value in document.header.items():
            lines.append(f"  {key}: {value}")

    return "\n".join(lines)