"""Turn a caption or subtitle file into a transcript, and say whether it fits the video.

This is the third question the app answers. The decoder answers "what do these
captions say", Sync QC answers "is the file in sync", and this answers "give me
the dialogue as a document, and mark the lines that do not line up with what is
actually spoken".

The match column is the part that matters. A transcript nobody can trust is just
a text file; a transcript where every line carries the offset to the spoken
audio is a document a vendor cannot argue with. When there is no video, the
lines are still produced and the column reads "not checked" - it never reads
"matched" on evidence that does not exist.
"""

import csv
import html
import io
import json
from pathlib import Path

from timecode import format_offset_ms, format_seconds


# Per-line match outcomes.
MATCHED = "matched"      # found in the dialogue, inside tolerance
OFF = "off"              # found, but the offset is outside tolerance
UNMATCHED = "unmatched"  # not found in the transcript at all
UNCHECKED = "unchecked"  # no video was supplied, so nothing was compared

STATUS_WORDS = {
    MATCHED: "in sync",
    OFF: "out of tolerance",
    UNMATCHED: "not found in audio",
    UNCHECKED: "not checked",
}

EXPORT_FORMATS = ("text", "markdown", "csv", "json", "srt", "vtt")


class TranscriptLine:
    """One cue, as a transcript line, with its match to the spoken audio."""

    __slots__ = (
        "index", "timecode", "start", "end", "speaker", "text",
        "status", "audio_seconds", "offset", "confidence", "matched_text",
    )

    def __init__(self, index, timecode, start, end, speaker, text, status=UNCHECKED,
                 audio_seconds=None, offset=None, confidence=None, matched_text=None):
        self.index = index
        self.timecode = timecode
        self.start = start
        self.end = end
        self.speaker = speaker
        self.text = text
        self.status = status
        self.audio_seconds = audio_seconds
        self.offset = offset
        self.confidence = confidence
        self.matched_text = matched_text

    @property
    def offset_ms(self):
        return None if self.offset is None else self.offset * 1000.0

    def as_dict(self):
        return {
            "index": self.index,
            "timecode": self.timecode,
            "start": self.start,
            "end": self.end,
            "speaker": self.speaker,
            "text": self.text,
            "status": self.status,
            "status_text": STATUS_WORDS[self.status],
            "audio_seconds": self.audio_seconds,
            "offset": self.offset,
            "offset_ms": self.offset_ms,
            "confidence": self.confidence,
            "matched_text": self.matched_text,
        }


class TranscriptResult:
    """A transcript plus the evidence for how well it fits the video."""

    def __init__(self, caption_path, lines, video_path=None, media=None, summary=None,
                 tolerance_ms=200.0, start_offset=0.0, start_timecode=None,
                 source_kind=None, language=None, errors=None, notes=None,
                 stopped=False):
        self.caption_path = str(caption_path)
        self.lines = lines
        self.video_path = str(video_path) if video_path else None
        self.media = media
        self.summary = summary
        self.tolerance_ms = tolerance_ms
        self.start_offset = start_offset
        self.start_timecode = start_timecode
        self.source_kind = source_kind
        self.language = language
        self.errors = list(errors or [])
        self.notes = list(notes or [])
        self.stopped = stopped

    # ----------------------------------------------------------------- counts

    @property
    def checked(self):
        """True when the lines were actually compared against audio."""
        return any(line.status != UNCHECKED for line in self.lines)

    def count(self, status):
        return sum(1 for line in self.lines if line.status == status)

    @property
    def matched(self):
        return self.count(MATCHED)

    @property
    def out_of_tolerance(self):
        return self.count(OFF)

    @property
    def unmatched(self):
        return self.count(UNMATCHED)

    @property
    def match_rate(self):
        if not self.lines or not self.checked:
            return None
        found = self.matched + self.out_of_tolerance
        return found / len(self.lines)

    def verdict_sentence(self):
        """One line a non-engineer can act on."""
        if self.stopped:
            return "Stopped before the transcript was finished."
        if not self.lines:
            return "No dialogue lines were found in this file."
        if not self.checked:
            return (
                f"{len(self.lines)} dialogue lines transcribed. No video was supplied, so "
                "whether they line up with spoken audio was not checked."
            )

        median = (self.summary or {}).get("median_offset")
        found = self.matched + self.out_of_tolerance
        if found == 0:
            return (
                "None of the subtitle lines could be found in the video's dialogue. "
                "Either this caption file belongs to a different asset, or the audio has no "
                "usable speech."
            )

        rate = self.match_rate or 0.0
        if self.out_of_tolerance == 0 and rate >= 0.5:
            return (
                f"All {found} matched lines sit within +/-{self.tolerance_ms:.0f} ms of the "
                f"spoken dialogue (median {format_offset_ms(median or 0.0)}). "
                "This transcript matches the video."
            )
        return (
            f"{self.matched} of {found} matched lines are within tolerance; "
            f"{self.out_of_tolerance} are outside +/-{self.tolerance_ms:.0f} ms "
            f"(median {format_offset_ms(median or 0.0)}). "
            f"{self.unmatched} lines were not found in the dialogue."
        )

    def as_dict(self):
        return {
            "caption_file": self.caption_path,
            "video_file": self.video_path,
            "source_kind": self.source_kind,
            "language": self.language,
            "checked_against_audio": self.checked,
            "tolerance_ms": self.tolerance_ms,
            "start_offset": self.start_offset,
            "start_timecode": self.start_timecode,
            "verdict": self.verdict_sentence(),
            "counts": {
                "lines": len(self.lines),
                "matched": self.matched,
                "out_of_tolerance": self.out_of_tolerance,
                "unmatched": self.unmatched,
                "match_rate": self.match_rate,
            },
            "alignment": self.summary,
            "notes": self.notes,
            "errors": self.errors,
            "stopped": self.stopped,
            "lines_detail": [line.as_dict() for line in self.lines],
        }


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


def _status_for(match, tolerance_ms):
    if match is None:
        return UNMATCHED
    return MATCHED if abs(match.offset) * 1000.0 <= tolerance_ms else OFF


def build_lines(cues, matches=None, tolerance_ms=200.0, checked=False):
    """Pair cues with their matches and produce transcript lines.

    Matches are keyed by identity of the cue they came from, so a cue that the
    aligner skipped is reported as unmatched rather than silently dropped from
    the transcript. The transcript has to contain every line of the file - it is
    a transcript, not a list of successes.
    """
    by_cue = {}
    for match in matches or []:
        by_cue[id(match.cue)] = match

    lines = []
    for index, cue in enumerate(cues, start=1):
        match = by_cue.get(id(cue))
        if not checked:
            status = UNCHECKED
        else:
            status = _status_for(match, tolerance_ms)

        lines.append(
            TranscriptLine(
                index,
                cue.timecode,
                cue.seconds,
                getattr(cue, "end_seconds", None),
                getattr(cue, "speaker", None),
                cue.text,
                status,
                match.audio_seconds if match else None,
                match.offset if match else None,
                match.confidence if match else None,
                match.matched_text if match else None,
            )
        )
    return lines


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _header_lines(result):
    lines = [
        f"Caption file: {Path(result.caption_path).name}",
        f"Format: {(result.source_kind or 'unknown').upper()}",
        f"Lines: {len(result.lines)}",
    ]
    if result.language:
        lines.append(f"Language: {result.language}")
    if result.video_path:
        lines.append(f"Video file: {Path(result.video_path).name}")
    if result.start_timecode:
        lines.append(
            f"Video start timecode: {result.start_timecode} "
            f"({result.start_offset:.3f} s, removed before comparing)"
        )
    if result.checked:
        lines.append(
            f"Matched: {result.matched + result.out_of_tolerance} of {len(result.lines)} "
            f"({result.matched} in tolerance, {result.out_of_tolerance} out, "
            f"{result.unmatched} not found)"
        )
        median = (result.summary or {}).get("median_offset")
        if median is not None:
            lines.append(f"Median offset: {format_offset_ms(median)}")
        drift = (result.summary or {}).get("drift") or {}
        if drift.get("slope_ms_per_minute") is not None:
            lines.append(f"Drift: {drift['slope_ms_per_minute']:+.0f} ms per minute")
    else:
        lines.append("Matched: not checked (no video supplied)")
    return lines


def _annotation(line):
    if line.status == UNCHECKED:
        return ""
    if line.status == UNMATCHED:
        return "  [not found in audio]"
    return f"  [{format_offset_ms(line.offset)}, confidence {line.confidence:.2f}]"


def render_text(result):
    """Timecoded transcript with a match annotation per line."""
    out = ["CAPTION TRANSCRIPT", "=" * 72, ""]
    out.extend(_header_lines(result))
    out.append("")
    out.append(result.verdict_sentence())
    out.append("")
    for note in result.notes:
        out.append(f"Note: {note}")
    for error in result.errors:
        out.append(f"Error: {error}")
    if result.notes or result.errors:
        out.append("")

    out.append("-" * 72)
    for line in result.lines:
        speaker = f"{line.speaker}: " if line.speaker else ""
        out.append(f"{line.index:04d}  {line.timecode}{_annotation(line)}")
        for text_line in line.text.splitlines() or [""]:
            out.append(f"      {speaker}{text_line}")
            speaker = ""
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def render_prose(result):
    """Dialogue only, no timecodes - the transcript someone actually reads.

    Consecutive lines from the same speaker join into a paragraph, which is what
    makes it read as a transcript rather than a subtitle dump.
    """
    paragraphs = []
    current_speaker = None
    buffer = []

    def flush():
        if not buffer:
            return
        body = " ".join(buffer)
        if current_speaker:
            paragraphs.append(f"{current_speaker}: {body}")
        else:
            paragraphs.append(body)
        buffer.clear()

    for line in result.lines:
        text = " ".join(line.text.split())
        if not text:
            continue
        if line.speaker != current_speaker:
            flush()
            current_speaker = line.speaker
        buffer.append(text)

    flush()
    return "\n\n".join(paragraphs) + "\n"


def render_markdown(result):
    out = ["# Caption transcript", ""]
    for item in _header_lines(result):
        out.append(f"- {item}")
    out.append("")
    out.append(f"**{result.verdict_sentence()}**")
    out.append("")
    for note in result.notes:
        out.append(f"> Note: {note}")
    if result.notes:
        out.append("")

    if result.checked:
        out.append("| # | Timecode | Speaker | Text | Offset | Confidence | Match |")
        out.append("|---|---|---|---|---|---|---|")
        for line in result.lines:
            text = " ".join(line.text.split()).replace("|", "\\|")
            offset = "-" if line.offset is None else format_offset_ms(line.offset)
            confidence = "-" if line.confidence is None else f"{line.confidence:.2f}"
            out.append(
                f"| {line.index} | {line.timecode} | {line.speaker or ''} | {text} | "
                f"{offset} | {confidence} | {STATUS_WORDS[line.status]} |"
            )
    else:
        out.append("| # | Timecode | Speaker | Text |")
        out.append("|---|---|---|---|")
        for line in result.lines:
            text = " ".join(line.text.split()).replace("|", "\\|")
            out.append(f"| {line.index} | {line.timecode} | {line.speaker or ''} | {text} |")

    out.append("")
    return "\n".join(out)


def render_csv(result):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "index", "timecode", "start_seconds", "end_seconds", "speaker", "text",
        "match", "offset_ms", "confidence", "audio_seconds", "matched_text",
    ])
    for line in result.lines:
        writer.writerow([
            line.index,
            line.timecode,
            f"{line.start:.3f}",
            "" if line.end is None else f"{line.end:.3f}",
            line.speaker or "",
            " ".join(line.text.split()),
            STATUS_WORDS[line.status],
            "" if line.offset_ms is None else f"{line.offset_ms:.0f}",
            "" if line.confidence is None else f"{line.confidence:.3f}",
            "" if line.audio_seconds is None else f"{line.audio_seconds:.3f}",
            line.matched_text or "",
        ])
    return buffer.getvalue()


def render_json(result):
    return json.dumps(result.as_dict(), indent=2) + "\n"


def _srt_stamp(seconds):
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    whole = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis == 1000:
        millis = 999
    return f"{hours:02d}:{minutes:02d}:{whole:02d},{millis:03d}"


def _vtt_stamp(seconds):
    return _srt_stamp(seconds).replace(",", ".")


def _end_for(line, next_line, minimum=1.2):
    """A cue needs an out point; text formats have one, decoded tracks do not."""
    if line.end is not None and line.end > line.start:
        return line.end
    if next_line is not None:
        return max(line.start + 0.4, min(next_line.start - 0.04, line.start + 7.0))
    return line.start + minimum


def render_srt(result):
    out = []
    for position, line in enumerate(result.lines):
        following = result.lines[position + 1] if position + 1 < len(result.lines) else None
        end = _end_for(line, following)
        body = line.text if line.text.strip() else "..."
        if line.speaker:
            body = f"{line.speaker}: {body}"
        out.append(str(position + 1))
        out.append(f"{_srt_stamp(line.start)} --> {_srt_stamp(end)}")
        out.append(body)
        out.append("")
    return "\n".join(out)


def render_vtt(result):
    out = ["WEBVTT", ""]
    for position, line in enumerate(result.lines):
        following = result.lines[position + 1] if position + 1 < len(result.lines) else None
        end = _end_for(line, following)
        body = line.text if line.text.strip() else "..."
        out.append(str(position + 1))
        out.append(f"{_vtt_stamp(line.start)} --> {_vtt_stamp(end)}")
        if line.speaker:
            out.append(f"<v {line.speaker}>{body}")
        else:
            out.append(body)
        out.append("")
    return "\n".join(out)


_STATUS_CLASS = {MATCHED: "ok", OFF: "off", UNMATCHED: "missing", UNCHECKED: "none"}


def render_html(result):
    """Self-contained HTML. No external assets, so it opens on an offline box."""
    rows = []
    for line in result.lines:
        text = html.escape(line.text).replace("\n", "<br>")
        speaker = f'<span class="spk">{html.escape(line.speaker)}</span> ' if line.speaker else ""
        offset = "-" if line.offset is None else html.escape(format_offset_ms(line.offset))
        confidence = "-" if line.confidence is None else f"{line.confidence:.2f}"
        rows.append(
            f'<tr class="s-{_STATUS_CLASS[line.status]}">'
            f"<td>{line.index}</td><td class=\"tc\">{html.escape(line.timecode)}</td>"
            f"<td>{speaker}{text}</td><td class=\"num\">{offset}</td>"
            f"<td class=\"num\">{confidence}</td>"
            f"<td>{html.escape(STATUS_WORDS[line.status])}</td></tr>"
        )

    header = "".join(f"<li>{html.escape(item)}</li>" for item in _header_lines(result))
    notes = "".join(f"<p class=\"note\">{html.escape(note)}</p>" for note in result.notes)

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Caption transcript - {html.escape(Path(result.caption_path).name)}</title>
<style>
  :root {{ --ink:#1c2024; --muted:#5c6670; --rule:#dcd6cd; --bg:#fbf9f6; --card:#ffffff; }}
  body {{ margin:0; padding:32px; background:var(--bg); color:var(--ink);
         font-family: ui-sans-serif, -apple-system, "Segoe UI", sans-serif; line-height:1.5; }}
  .wrap {{ max-width:1080px; margin:0 auto; }}
  h1 {{ font-size:24px; margin:0 0 6px; }}
  .verdict {{ background:var(--card); border:1px solid var(--rule); border-left:4px solid #4a5560;
              padding:14px 16px; margin:18px 0; font-weight:600; }}
  ul.meta {{ list-style:none; padding:0; color:var(--muted); font-size:14px; columns:2; }}
  table {{ width:100%; border-collapse:collapse; background:var(--card); font-size:14px; }}
  th, td {{ text-align:left; padding:8px 10px; border-bottom:1px solid var(--rule); vertical-align:top; }}
  th {{ background:#f1ece5; font-size:12px; text-transform:uppercase; letter-spacing:.04em; }}
  td.tc {{ font-variant-numeric:tabular-nums; white-space:nowrap; color:var(--muted); }}
  td.num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
  .spk {{ font-weight:600; }}
  .note {{ color:var(--muted); font-size:14px; }}
  tr.s-off td {{ background:#fdf6e6; }}
  tr.s-missing td {{ background:#fdecea; }}
  .scroll {{ overflow-x:auto; }}
</style></head><body><div class="wrap">
<h1>Caption transcript</h1>
<ul class="meta">{header}</ul>
<div class="verdict">{html.escape(result.verdict_sentence())}</div>
{notes}
<div class="scroll"><table>
<thead><tr><th>#</th><th>Timecode</th><th>Text</th><th>Offset</th><th>Conf.</th><th>Match</th></tr></thead>
<tbody>{''.join(rows)}</tbody>
</table></div>
</div></body></html>
"""


RENDERERS = {
    "text": render_text,
    "prose": render_prose,
    "markdown": render_markdown,
    "csv": render_csv,
    "json": render_json,
    "srt": render_srt,
    "vtt": render_vtt,
    "html": render_html,
}

_SUFFIX_FORMATS = {
    ".txt": "text",
    ".md": "markdown",
    ".markdown": "markdown",
    ".csv": "csv",
    ".json": "json",
    ".srt": "srt",
    ".vtt": "vtt",
    ".html": "html",
    ".htm": "html",
}


def render(result, output_format="text"):
    renderer = RENDERERS.get(output_format)
    if renderer is None:
        raise ValueError(
            f"Unknown transcript format {output_format!r}. "
            f"Choose one of: {', '.join(sorted(RENDERERS))}."
        )
    return renderer(result)


def format_for_path(path, default="text"):
    return _SUFFIX_FORMATS.get(Path(path).suffix.lower(), default)


def write_transcript(path, result, output_format=None):
    """Write the transcript, picking the format from the extension by default."""
    target = Path(path)
    chosen = output_format or format_for_path(target)
    target.write_text(render(result, chosen), encoding="utf-8")
    return target
