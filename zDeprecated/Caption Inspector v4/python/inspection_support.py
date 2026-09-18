import io
import re
from contextlib import redirect_stdout
from pathlib import Path

from cshim import get_decoded_captions


SUPPORTED_TYPES = ("ts", "mpg", "mp4", "mcc", "scc", "mov")


def validate_input_path(input_path):
    suffix = Path(input_path).suffix.lower().lstrip(".")
    if suffix not in SUPPORTED_TYPES:
        raise ValueError(f"Unsupported file type: .{suffix}")


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
        if row["type"] not in ("Line21TextString", "DtvccTextString"):
            continue
        lines.append(f"{row['time']} {row['event']}".strip())
    return "\n".join(lines)


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
            text_events = sum(1 for row in track_rows if row["type"] in ("Line21TextString", "DtvccTextString"))
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
    validate_input_path(input_path)

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