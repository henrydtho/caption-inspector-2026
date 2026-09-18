"""Minimal, dependency-free writer for turning plain text into a PDF.

Good enough for saving what is already on screen in a monospace results pane -
not a layout engine, and not a replacement for reportlab if this ever needs
more than fixed-width text on paginated Letter pages.
"""

from pathlib import Path

from sync_check import INFO, WARN

_PAGE_WIDTH = 612  # US Letter, in points.
_PAGE_HEIGHT = 792
_MARGIN = 36
_FONT_SIZE = 9
_LINE_HEIGHT = _FONT_SIZE * 1.25
_CHAR_WIDTH = _FONT_SIZE * 0.6  # Courier's fixed per-glyph advance.


def _wrap_line(line, max_chars):
    if not line:
        return [""]
    wrapped = []
    while len(line) > max_chars:
        wrapped.append(line[:max_chars])
        line = line[max_chars:]
    wrapped.append(line)
    return wrapped


def _escape(text):
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _paginate(text):
    max_chars = max(1, int((_PAGE_WIDTH - 2 * _MARGIN) / _CHAR_WIDTH))
    lines_per_page = max(1, int((_PAGE_HEIGHT - 2 * _MARGIN) / _LINE_HEIGHT))

    all_lines = []
    for raw_line in text.splitlines() or [""]:
        all_lines.extend(_wrap_line(raw_line.rstrip("\r"), max_chars))
    if not all_lines:
        all_lines = [""]

    return [all_lines[i : i + lines_per_page] for i in range(0, len(all_lines), lines_per_page)]


def _page_content_stream(lines):
    top = _PAGE_HEIGHT - _MARGIN
    parts = [f"BT /F1 {_FONT_SIZE} Tf {_LINE_HEIGHT} TL {_MARGIN} {top} Td"]
    for index, line in enumerate(lines):
        prefix = "" if index == 0 else "T* "
        parts.append(f"{prefix}({_escape(line)}) Tj")
    parts.append("ET")
    return "\n".join(parts)


def _write_pdf(objects, destination):
    """`objects[i]` is the body of PDF object number `i + 1`."""
    buffer = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(buffer))
        buffer += f"{index} 0 obj\n{body}\nendobj\n".encode("latin-1", "replace")

    xref_offset = len(buffer)
    buffer += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    buffer += b"0000000000 65535 f \n"
    for offset in offsets:
        buffer += f"{offset:010d} 00000 n \n".encode("ascii")
    buffer += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF"
    ).encode("ascii")

    Path(destination).write_bytes(bytes(buffer))


def build_delivery_compliance_section(source_path):
    """The "Delivery Compliance" section for a PDF export, for `.scc` sources only.

    Runs the raw transport audit (`scc_transport_audit.py`) and summarizes
    WARN/INFO counts per cue. Returns "" for anything that is not a `.scc`
    file - `.mcc` and container-embedded 608/708 have no transport-audit
    capability (see `scc_transport_audit.py`).
    """
    if not source_path or Path(source_path).suffix.lower() != ".scc":
        return ""

    from scc_transport_audit import audit_scc_file

    try:
        audited_cues = audit_scc_file(source_path)
    except OSError:
        return ""

    if not audited_cues:
        return ""

    lines = ["", "Delivery Compliance (SCC transport audit)", "-" * 41, ""]
    total_warn = 0
    total_info = 0

    for index, (cue, checks) in enumerate(audited_cues, start=1):
        warn_checks = [check for check in checks if check.status == WARN]
        info_checks = [check for check in checks if check.status == INFO]
        total_warn += len(warn_checks)
        total_info += len(info_checks)

        lines.append(
            f"Cue {index} ({cue.timecode}): {len(warn_checks)} warning(s), {len(info_checks)} note(s)"
        )
        for check in warn_checks + info_checks:
            lines.append(f"    [{check.status}] {check.headline}")

    lines.insert(4, f"{len(audited_cues)} cue(s) audited - {total_warn} warning(s), {total_info} note(s) total.")
    lines.insert(5, "")

    return "\n".join(lines)


def write_text_as_pdf(text, destination, source_path=None):
    """Render `text` as a simple monospace PDF at `destination`.

    Long lines are wrapped and the text paginated onto US Letter pages. When
    `source_path` is a `.scc` file, a "Delivery Compliance" section is
    appended after `text` - see `build_delivery_compliance_section`.
    """
    compliance_section = build_delivery_compliance_section(source_path)
    if compliance_section:
        text = f"{text}\n{compliance_section}"

    pages = _paginate(text)

    objects = ["", "", ""]  # Reserved for the catalog, page tree, and font.
    page_ids = []
    for lines in pages:
        page_id = len(objects) + 1
        content_id = page_id + 1
        stream = _page_content_stream(lines)
        objects.append(
            "<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 3 0 R >> >> "
            f"/MediaBox [0 0 {_PAGE_WIDTH} {_PAGE_HEIGHT}] /Contents {content_id} 0 R >>"
        )
        objects.append(
            f"<< /Length {len(stream.encode('latin-1', 'replace'))} >>\nstream\n{stream}\nendstream"
        )
        page_ids.append(page_id)

    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects[0] = "<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>"
    objects[2] = "<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>"

    _write_pdf(objects, destination)
