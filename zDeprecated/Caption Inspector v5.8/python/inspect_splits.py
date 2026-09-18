#!/usr/bin/env python3
"""Show why a caption's text was split, and what got joined back together.

    python3 python/inspect_splits.py program.scc --rate 2997

A 608 caption line arrives as runs of characters with control codes between
them, and the cue text is those runs rejoined. Every rejoin is a guess about
whether the code between them occupied a space on screen. This prints the guess
alongside the evidence, so a suspicious word like "ph one" can be traced to the
exact code that split it.

Written for one question: when a mid-row style code lands inside a word, does
that encoder intend it to occupy a display cell (so the space is real) or is it
a style toggle the previous decoder simply swallowed? The answer decides how the
runs should be joined, and it is a property of the file, not of the format.
"""

import argparse
import io
import contextlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from inspection_support import decode_file, normalized_event_text  # noqa: E402


# A join that fused or separated two alphanumerics is the interesting case.
_WORD_EDGE = re.compile(r"[A-Za-z0-9]$")
_WORD_START = re.compile(r"^[A-Za-z0-9]")


def analyse(caption_path, rate_code, track=None, limit=40):
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        tracks, _logs = decode_file(str(caption_path), rate_code, capture_logs=True)

    swallowed = stderr.getvalue().count("Exception ignored")
    if swallowed:
        print(f"WARNING: {swallowed} exceptions were swallowed inside the decoder "
              "callbacks. Caption events are being dropped.\n")

    family, name, rows = _pick(tracks, track)
    if not rows:
        print("No caption rows were decoded.")
        return 1

    print(f"Track: {family} / {name}   ({len(rows)} events)\n")

    counts = {}
    for row in rows:
        counts[row["type"]] = counts.get(row["type"], 0) + 1
    print("Event types:")
    for kind, count in sorted(counts.items(), key=lambda item: -item[1]):
        print(f"  {count:6d}  {kind}")
    print()

    # Walk the stream, pairing each text run with whatever preceded it.
    findings = []
    previous_text = None
    between = []

    for row in rows:
        kind = row["type"]
        if kind == "Line21TextString":
            text = normalized_event_text(row).strip('"')
            if previous_text is not None and between:
                fused = bool(_WORD_EDGE.search(previous_text)) and bool(_WORD_START.match(text))
                findings.append({
                    "left": previous_text,
                    "right": text,
                    "between": list(between),
                    "mid_word": fused,
                })
            previous_text = text
            between = []
        elif kind in ("Line21ControlCode",):
            command = normalized_event_text(row)
            # A caption boundary; runs either side are different cues.
            if command in ("{EOC}", "{EDM}", "{RCL}", "{CR}"):
                previous_text = None
                between = []
            else:
                between.append((kind, command))
        else:
            between.append((kind, normalized_event_text(row)))

    mid_word = [f for f in findings if f["mid_word"]]
    print(f"Text runs rejoined inside a cue: {len(findings)}")
    print(f"  ...of those, joins that fall inside a word: {len(mid_word)}\n")

    if not mid_word:
        print("No word was split by a control code in this file, so the join rule "
              "cannot be the cause of fragmented words here.")
        return 0

    print("Joins that fall inside a word - these are the ones that produce")
    print('text like "ph one". The codes listed are what sat between the runs:\n')
    for finding in mid_word[:limit]:
        codes = ", ".join(f"{kind}:{event}" for kind, event in finding["between"])
        left = finding["left"][-28:]
        right = finding["right"][:28]
        print(f'  ...{left!r} + {right!r}')
        print(f'      separated by: {codes}')
        print(f'      joined with a space -> ...{left[-12:]} {right[:12]}...')
        print(f'      joined with nothing -> ...{left[-12:]}{right[:12]}...')
        print()

    if len(mid_word) > limit:
        print(f"  ... and {len(mid_word) - limit} more\n")

    kinds = {}
    for finding in mid_word:
        for kind, _event in finding["between"]:
            kinds[kind] = kinds.get(kind, 0) + 1
    print("Codes responsible for the mid-word joins:")
    for kind, count in sorted(kinds.items(), key=lambda item: -item[1]):
        print(f"  {count:6d}  {kind}")
    return 0


def _pick(tracks, track):
    if track:
        family, name = track
        return family, name, tracks.get(family, {}).get(name, [])

    line21 = tracks.get("CEA-608", {})
    if "Channel 1" in line21:
        return "CEA-608", "Channel 1", line21["Channel 1"]

    for family in ("CEA-608", "CEA-708"):
        for name, rows in (tracks.get(family, {}) or {}).items():
            if rows:
                return family, name, rows
    return None, None, []


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("captions", help="Caption file to inspect (.scc .mcc .mov .mp4 .ts)")
    parser.add_argument("--rate", type=int, default=2997, help="Frame rate x100 (default 2997)")
    parser.add_argument("--channel", help='Force a track, e.g. "Channel 2"')
    parser.add_argument("--limit", type=int, default=40, help="Maximum examples to print")
    args = parser.parse_args(argv)

    if not Path(args.captions).exists():
        raise SystemExit(f"No such file: {args.captions}")

    track = ("CEA-608", args.channel) if args.channel else None
    return analyse(args.captions, args.rate, track, args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
