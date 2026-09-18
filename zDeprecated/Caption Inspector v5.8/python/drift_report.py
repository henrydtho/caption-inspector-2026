"""Reports for people who did not run the check.

The audience is a vendor who has been told their file is wrong and does not
agree. So the report leads with the number and the sentence that explains it -
"drifts 340 ms per 10 minutes, consistent with a 29.97 reference that was never
rate-converted to 25" - and keeps the raw measurements below it as backup.
"""

import html
import json
from datetime import datetime
from pathlib import Path

from sync_check import FAIL, INFO, PASS, WARN
from timecode import format_offset_ms, format_seconds, rate_label


APP_VERSION = "5.8"

_STATUS_WORDS = {
    PASS: "PASS",
    WARN: "REVIEW",
    FAIL: "FAIL",
    INFO: "NOTE",
}

_STATUS_COLORS = {
    PASS: "#1f7a4d",
    WARN: "#9a6212",
    FAIL: "#b3261e",
    INFO: "#4a5560",
}


def _timestamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def headline_sentence(tier1, tier2=None):
    """The one sentence that goes in the email body."""
    if tier1 and tier1.errors:
        return f"The check could not run: {tier1.errors[0]}"

    if tier2 and tier2.tier2:
        drift = tier2.tier2["drift"]
        ms_per_minute = drift["slope_ms_per_minute"]
        median_offset = tier2.tier2["median_offset"]

        if abs(ms_per_minute) >= 10.0:
            explanations = drift.get("explanations") or []
            sentence = (
                f"Captions drift {abs(ms_per_minute):.0f} ms per minute "
                f"({abs(ms_per_minute) * 10:.0f} ms per 10 minutes) against the dialogue"
            )
            if explanations:
                sentence += f", consistent with a file {explanations[0]['text']}"
            return sentence + "."

        if abs(median_offset) * 1000 >= 200:
            return (
                f"Captions sit a constant {format_offset_ms(median_offset)} from the dialogue "
                "with no drift, so the whole file needs sliding rather than rate-converting."
            )

        return (
            f"Captions track the dialogue within {abs(median_offset) * 1000:.0f} ms end to end, "
            "with no measurable drift."
        )

    failures = tier1.failures() if tier1 else []
    if failures:
        return failures[0].headline

    warnings = tier1.warnings() if tier1 else []
    if warnings:
        return warnings[0].headline

    return "The caption file's timing is consistent with the delivered video."


def _verdict_of(tier1, tier2):
    order = {INFO: 0, PASS: 1, WARN: 2, FAIL: 3}
    verdicts = [result.verdict for result in (tier1, tier2) if result]
    if not verdicts:
        return INFO
    return max(verdicts, key=lambda verdict: order[verdict])


def _media_lines(result):
    if not result or not result.media:
        return []
    return result.media.summary_lines()


def _caption_lines(result):
    if not result or not result.timings:
        return []

    timings = result.timings
    lines = [
        f"File: {Path(timings.path).name}",
        f"Format: {timings.kind.upper()}",
        f"Rows: {len(timings.entries)}",
        f"Counting: {'drop-frame' if timings.drop_frame else 'non-drop'}",
        f"Read at: {rate_label(result.rate_code)} fps",
    ]
    first = timings.first_text_entry() or timings.first_entry()
    last = timings.last_entry()
    if first:
        lines.append(f"First caption: {first.timecode}")
    if last:
        lines.append(f"Last event: {last.timecode}")
    return lines


def render_text_report(tier1, tier2=None):
    """Plain-text report. Paste-able into a ticket or an email."""
    verdict = _verdict_of(tier1, tier2)
    lines = [
        "CAPTION SYNC QC REPORT",
        f"Caption Inspector v{APP_VERSION}   {_timestamp()}",
        "=" * 72,
        "",
        f"RESULT: {_STATUS_WORDS[verdict]}",
        "",
        headline_sentence(tier1, tier2),
        "",
    ]

    if tier1 and tier1.errors:
        lines.append("ERRORS")
        lines.append("-" * 72)
        lines.extend(f"  {error}" for error in tier1.errors)
        lines.append("")
        return "\n".join(lines)

    media_lines = _media_lines(tier1) or _media_lines(tier2)
    if media_lines:
        lines.append("VIDEO")
        lines.append("-" * 72)
        lines.extend(f"  {line}" for line in media_lines)
        lines.append("")

    caption_lines = _caption_lines(tier1) or _caption_lines(tier2)
    if caption_lines:
        lines.append("CAPTION FILE")
        lines.append("-" * 72)
        lines.extend(f"  {line}" for line in caption_lines)
        lines.append("")

    if tier1 and tier1.checks:
        lines.append("TIER 1 - TIMECODE AND FRAME RATE MATH")
        lines.append("-" * 72)
        for check in tier1.checks:
            lines.append(f"  [{_STATUS_WORDS[check.status]:<6}] {check.name}: {check.headline}")
            for detail in check.detail:
                lines.append(f"           {detail}")
        lines.append("")

    if tier2:
        lines.append("TIER 2 - AUDIO-VERIFIED SYNC")
        lines.append("-" * 72)
        if tier2.errors:
            lines.extend(f"  {error}" for error in tier2.errors)
        for check in tier2.checks:
            lines.append(f"  [{_STATUS_WORDS[check.status]:<6}] {check.name}: {check.headline}")
            for detail in check.detail:
                lines.append(f"           {detail}")
        lines.append("")

        if tier2.tier2:
            summary = tier2.tier2
            drift = summary["drift"]
            lines.append("  Measurements")
            lines.append(f"    Cues matched to dialogue: {summary['matched']} of {summary['cues']}")
            lines.append(f"    Median offset:            {format_offset_ms(summary['median_offset'])}")
            lines.append(f"    Drift rate:               {drift['slope_ms_per_minute']:+.1f} ms/minute")
            lines.append(f"    Drift per 10 minutes:     {drift['slope_ms_per_minute'] * 10:+.0f} ms")
            lines.append(f"    Accumulated across program: {format_offset_ms(drift['total_drift_seconds'])}")
            lines.append(f"    Implied timing ratio:     {drift['implied_ratio']:.5f}")
            lines.append(f"    Fit r-squared:            {drift['r_squared']:.3f}")
            lines.append("")

    lines.append("WHAT THE NUMBERS MEAN")
    lines.append("-" * 72)
    lines.extend(
        f"  {line}"
        for line in [
            "Offset is measured as caption time minus the moment the words are spoken.",
            "A positive offset means captions appear before the dialogue.",
            "A constant offset with no drift is a fixed shift: slide the whole file.",
            "An offset that grows across the program is drift: the caption timings were",
            "built against a different frame rate and need rate conversion, not relabelling.",
        ]
    )
    lines.append("")

    return "\n".join(lines)


def _sample_x(sample):
    """Position of a sample on the media timeline.

    Older summaries only carry `caption_seconds` (absolute program timecode);
    the fit is against `media_seconds`, so the plotted line and the plotted dots
    have to use the same axis.
    """
    return sample.get("media_seconds", sample["caption_seconds"])


def _svg_drift_plot(summary, width=760, height=300):
    """Scatter of offset against timeline position, with the fitted line.

    Hand-rolled SVG rather than matplotlib: the report has to open anywhere,
    and the app must not gain a plotting dependency for one chart.
    """
    samples = summary.get("samples") or []
    if len(samples) < 2:
        return ""

    drift = summary["drift"]
    pad_left, pad_right, pad_top, pad_bottom = 70, 20, 24, 44
    plot_width = width - pad_left - pad_right
    plot_height = height - pad_top - pad_bottom

    xs = [_sample_x(sample) for sample in samples]
    ys = [sample["offset"] * 1000.0 for sample in samples]

    x_min, x_max = min(xs), max(xs)
    if x_max <= x_min:
        return ""

    y_span = max(abs(min(ys)), abs(max(ys)), 120.0) * 1.15
    y_min, y_max = -y_span, y_span

    def to_x(value):
        return pad_left + (value - x_min) / (x_max - x_min) * plot_width

    def to_y(value):
        return pad_top + (y_max - value) / (y_max - y_min) * plot_height

    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
        f'aria-label="Caption offset against position in the program" '
        f'style="max-width:{width}px;font-family:ui-sans-serif,system-ui,sans-serif">',
        f'<rect x="{pad_left}" y="{pad_top}" width="{plot_width}" height="{plot_height}" '
        'fill="var(--plot-bg)" stroke="var(--rule)"/>',
    ]

    # Horizontal gridlines every quarter of the range, zero line emphasised.
    for step in range(5):
        value = y_max - step * (y_max - y_min) / 4.0
        y = to_y(value)
        emphasis = abs(value) < 1e-6
        parts.append(
            f'<line x1="{pad_left}" y1="{y:.1f}" x2="{pad_left + plot_width}" y2="{y:.1f}" '
            f'stroke="{"var(--ink)" if emphasis else "var(--rule)"}" '
            f'stroke-width="{1.4 if emphasis else 1}" stroke-dasharray="{"" if emphasis else "3 3"}"/>'
        )
        parts.append(
            f'<text x="{pad_left - 10}" y="{y + 4:.1f}" text-anchor="end" font-size="11" '
            f'fill="var(--muted)">{value:+.0f}</text>'
        )

    for step in range(5):
        value = x_min + step * (x_max - x_min) / 4.0
        x = to_x(value)
        parts.append(
            f'<text x="{x:.1f}" y="{pad_top + plot_height + 18}" text-anchor="middle" '
            f'font-size="11" fill="var(--muted)">{format_seconds(value)[:8]}</text>'
        )

    slope = drift["slope"]
    intercept = drift["intercept"]
    line_start = (to_x(x_min), to_y((intercept + slope * x_min) * 1000.0))
    line_end = (to_x(x_max), to_y((intercept + slope * x_max) * 1000.0))

    for sample in samples:
        x = to_x(_sample_x(sample))
        y = to_y(max(y_min, min(y_max, sample["offset"] * 1000.0)))
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.6" fill="var(--dot)" opacity="0.65"/>')

    parts.append(
        f'<line x1="{line_start[0]:.1f}" y1="{line_start[1]:.1f}" '
        f'x2="{line_end[0]:.1f}" y2="{line_end[1]:.1f}" stroke="var(--accent)" stroke-width="2.4"/>'
    )

    parts.append(
        f'<text x="{pad_left}" y="{height - 8}" font-size="11" fill="var(--muted)">'
        "Position in program &#8594;</text>"
    )
    parts.append(
        f'<text x="16" y="{pad_top + plot_height / 2:.1f}" font-size="11" fill="var(--muted)" '
        f'transform="rotate(-90 16 {pad_top + plot_height / 2:.1f})" text-anchor="middle">'
        "Offset (ms)</text>"
    )
    parts.append("</svg>")
    return "".join(parts)


def _html_check_rows(checks):
    rows = []
    for check in checks:
        details = "".join(f"<li>{html.escape(detail)}</li>" for detail in check.detail)
        rows.append(
            f'<tr class="status-{check.status.lower()}">'
            f'<td class="status"><span class="pill">{_STATUS_WORDS[check.status]}</span></td>'
            f"<td><strong>{html.escape(check.name)}</strong><div class=\"headline\">"
            f"{html.escape(check.headline)}</div>"
            + (f"<ul>{details}</ul>" if details else "")
            + "</td></tr>"
        )
    return "".join(rows)


def render_html_report(tier1, tier2=None):
    """Self-contained HTML report. No external assets, opens anywhere."""
    verdict = _verdict_of(tier1, tier2)
    headline = headline_sentence(tier1, tier2)

    media_lines = _media_lines(tier1) or _media_lines(tier2)
    caption_lines = _caption_lines(tier1) or _caption_lines(tier2)

    stat_cards = []
    if tier2 and tier2.tier2:
        summary = tier2.tier2
        drift = summary["drift"]
        stat_cards = [
            ("Drift rate", f"{drift['slope_ms_per_minute']:+.0f} ms/min", "per minute of program"),
            ("Per 10 minutes", f"{drift['slope_ms_per_minute'] * 10:+.0f} ms", "accumulated"),
            ("Median offset", format_offset_ms(summary["median_offset"]), "caption vs dialogue"),
            ("Cues matched", f"{summary['matched']}/{summary['cues']}", "anchored to transcript"),
        ]
    elif tier1 and tier1.timings:
        coverage = next((check for check in tier1.checks if check.name == "Coverage ratio"), None)
        if coverage and "ratio" in coverage.data:
            stat_cards.append(("Coverage ratio", f"{coverage.data['ratio']:.4f}", "caption span / program"))
        stat_cards.append(("Caption rows", str(len(tier1.timings.entries)), tier1.timings.kind.upper()))
        if tier1.rate_code:
            stat_cards.append(("Read at", f"{rate_label(tier1.rate_code)} fps", "frame rate used"))

    cards_html = "".join(
        f'<div class="card"><div class="card-label">{html.escape(label)}</div>'
        f'<div class="card-value">{html.escape(value)}</div>'
        f'<div class="card-note">{html.escape(note)}</div></div>'
        for label, value, note in stat_cards
    )

    plot_html = ""
    if tier2 and tier2.tier2 and tier2.tier2.get("samples"):
        plot_html = (
            '<section><h2>Offset across the program</h2>'
            '<p class="lede">Each dot is one caption cue matched to the moment its words are '
            'spoken. A flat line on zero is in sync. A sloped line is drift.</p>'
            f'<div class="plot">{_svg_drift_plot(tier2.tier2)}</div></section>'
        )

    tier1_html = ""
    if tier1 and tier1.checks:
        tier1_html = (
            "<section><h2>Tier 1 &mdash; timecode and frame rate math</h2>"
            f'<table class="checks">{_html_check_rows(tier1.checks)}</table></section>'
        )

    tier2_html = ""
    if tier2:
        error_html = "".join(f"<p class=\"error\">{html.escape(error)}</p>" for error in tier2.errors)
        tier2_html = (
            "<section><h2>Tier 2 &mdash; audio-verified sync</h2>"
            + error_html
            + (f'<table class="checks">{_html_check_rows(tier2.checks)}</table>' if tier2.checks else "")
            + "</section>"
        )

    facts_html = ""
    if media_lines or caption_lines:
        facts_html = (
            '<section class="facts"><div><h3>Video</h3><ul>'
            + "".join(f"<li>{html.escape(line)}</li>" for line in media_lines)
            + "</ul></div><div><h3>Caption file</h3><ul>"
            + "".join(f"<li>{html.escape(line)}</li>" for line in caption_lines)
            + "</ul></div></section>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Caption Sync QC Report</title>
<style>
  :root {{
    --bg: #f6f4f1; --surface: #ffffff; --ink: #17222c; --muted: #5c6873;
    --rule: #dfd9d2; --accent: #c25a1e; --plot-bg: #fbfaf8; --dot: #17222c;
    --pass: #1f7a4d; --warn: #9a6212; --fail: #b3261e; --info: #4a5560;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #14181c; --surface: #1c2228; --ink: #e8eaec; --muted: #9aa5b0;
      --rule: #2e3740; --accent: #e8853f; --plot-bg: #171c21; --dot: #cfd6dc;
      --pass: #5fd39a; --warn: #e0b062; --fail: #f2887f; --info: #9aa5b0;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 32px 20px 64px; background: var(--bg); color: var(--ink);
         font-family: ui-sans-serif, -apple-system, "Segoe UI", sans-serif; line-height: 1.5; }}
  .wrap {{ max-width: 900px; margin: 0 auto; }}
  header {{ border-bottom: 2px solid var(--ink); padding-bottom: 20px; margin-bottom: 28px; }}
  .kicker {{ text-transform: uppercase; letter-spacing: 0.14em; font-size: 0.72rem;
             color: var(--muted); font-weight: 700; }}
  h1 {{ font-size: 1.9rem; margin: 8px 0 12px; line-height: 1.2; }}
  .verdict {{ display: inline-block; padding: 6px 14px; border-radius: 999px; font-weight: 700;
              font-size: 0.85rem; letter-spacing: 0.06em; color: #fff;
              background: var(--{verdict.lower()}); }}
  .headline {{ font-size: 1.05rem; margin-top: 16px; }}
  h2 {{ font-size: 1.15rem; margin: 36px 0 12px; }}
  h3 {{ font-size: 0.9rem; text-transform: uppercase; letter-spacing: 0.08em;
        color: var(--muted); margin: 0 0 8px; }}
  .lede {{ color: var(--muted); margin: 0 0 14px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px;
            margin: 24px 0; }}
  .card {{ background: var(--surface); border: 1px solid var(--rule); border-radius: 12px; padding: 14px; }}
  .card-label {{ font-size: 0.74rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); }}
  .card-value {{ font-size: 1.5rem; font-weight: 700; margin: 6px 0 2px;
                 font-variant-numeric: tabular-nums; }}
  .card-note {{ font-size: 0.78rem; color: var(--muted); }}
  .plot {{ background: var(--surface); border: 1px solid var(--rule); border-radius: 12px;
           padding: 12px; overflow-x: auto; }}
  table.checks {{ width: 100%; border-collapse: collapse; background: var(--surface);
                  border: 1px solid var(--rule); border-radius: 12px; overflow: hidden; }}
  table.checks td {{ padding: 12px 14px; border-top: 1px solid var(--rule); vertical-align: top; }}
  table.checks tr:first-child td {{ border-top: none; }}
  td.status {{ width: 96px; }}
  .pill {{ display: inline-block; padding: 3px 9px; border-radius: 999px; font-size: 0.7rem;
           font-weight: 700; letter-spacing: 0.05em; color: #fff; }}
  .status-pass .pill {{ background: var(--pass); }}
  .status-warn .pill {{ background: var(--warn); }}
  .status-fail .pill {{ background: var(--fail); }}
  .status-info .pill {{ background: var(--info); }}
  table.checks .headline {{ font-size: 0.95rem; margin-top: 3px; }}
  table.checks ul {{ margin: 8px 0 0; padding-left: 18px; color: var(--muted); font-size: 0.88rem; }}
  .facts {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 20px;
            background: var(--surface); border: 1px solid var(--rule); border-radius: 12px;
            padding: 18px; margin-top: 28px; }}
  .facts ul {{ margin: 0; padding-left: 18px; font-size: 0.88rem; color: var(--muted); }}
  .error {{ color: var(--fail); font-weight: 600; }}
  footer {{ margin-top: 40px; padding-top: 16px; border-top: 1px solid var(--rule);
            color: var(--muted); font-size: 0.82rem; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="kicker">Caption sync QC</div>
    <h1>{html.escape(Path(tier1.caption_path).name if tier1 else "Caption report")}</h1>
    <span class="verdict">{_STATUS_WORDS[verdict]}</span>
    <p class="headline">{html.escape(headline)}</p>
  </header>
  <div class="cards">{cards_html}</div>
  {plot_html}
  {tier1_html}
  {tier2_html}
  {facts_html}
  <footer>
    Generated by Caption Inspector v{APP_VERSION} on {_timestamp()}.<br>
    Offset is caption time minus spoken time; positive means the caption leads the dialogue.
    A constant offset is a fixed shift. An offset that grows across the program is drift and
    needs rate conversion, not a relabel.
  </footer>
</div>
</body>
</html>"""


def render_json_report(tier1, tier2=None):
    payload = {
        "app_version": APP_VERSION,
        "generated": _timestamp(),
        "verdict": _verdict_of(tier1, tier2),
        "headline": headline_sentence(tier1, tier2),
        "tier1": tier1.as_dict() if tier1 else None,
        "tier2": tier2.as_dict() if tier2 else None,
    }
    return json.dumps(payload, indent=2, default=str)


def write_report(path, tier1, tier2=None):
    """Write a report, picking the format from the file extension."""
    output_path = Path(path)
    suffix = output_path.suffix.lower()

    if suffix in (".html", ".htm"):
        content = render_html_report(tier1, tier2)
    elif suffix == ".json":
        content = render_json_report(tier1, tier2)
    else:
        content = render_text_report(tier1, tier2)

    output_path.write_text(content, encoding="utf-8")
    return output_path
