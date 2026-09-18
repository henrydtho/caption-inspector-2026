#!/usr/bin/env python3
"""Command line for the transcript builder.

    python3 python/transcript_cli.py program.scc --video program.mov -o out.html

Converts a caption or subtitle file to a transcript, and - with `--video` -
marks every line with its offset from the spoken dialogue. Exits 0 when nothing
needs attention, 2 when lines are out of tolerance or missing from the audio, so
it drops into a delivery gate the same way `sync_cli.py` does.
"""

import argparse
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cancellation import CancelToken, OperationCancelled  # noqa: E402
from transcribe import MODEL_SIZES  # noqa: E402
from transcript_check import build_transcript  # noqa: E402
from transcript_export import (  # noqa: E402
    RENDERERS,
    format_for_path,
    render,
    write_transcript,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Turn a caption or subtitle file into a transcript, checked against a video.",
    )
    parser.add_argument("captions", help="Caption or subtitle file (.scc .mcc .srt .vtt .ttml .dfxp .itt)")
    parser.add_argument("--video", help="Video to check the transcript against (optional)")
    parser.add_argument("-o", "--output", help="Write to this file; the format follows its extension")
    parser.add_argument(
        "--format",
        choices=sorted(RENDERERS),
        help="Override the output format (default: from --output's extension, else text)",
    )
    parser.add_argument("--model", choices=MODEL_SIZES, default="base", help="Transcription model size")
    parser.add_argument("--language", help="Force a language instead of auto-detecting")
    parser.add_argument("--rate", type=int, help="Read caption timecodes at this rate x100 (e.g. 2997)")
    parser.add_argument(
        "--tolerance-ms", type=float, default=200.0, help="Per-line match tolerance (default 200)"
    )
    parser.add_argument("--max-lines", type=int, help="Sample at most this many cues")
    parser.add_argument("--no-cache", action="store_true", help="Ignore cached transcripts")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    def progress(message):
        if not args.quiet:
            print(f"  ... {message}", file=sys.stderr)

    cancel = CancelToken()

    # Ctrl-C should stop the run the same way the Stop button does: kill the
    # ffmpeg child and unwind, rather than leave an orphan behind.
    def on_interrupt(_signum, _frame):
        print("\n  ... stopping", file=sys.stderr)
        cancel.cancel()

    signal.signal(signal.SIGINT, on_interrupt)

    try:
        result = build_transcript(
            args.captions,
            args.video,
            rate_code=args.rate,
            model_size=args.model,
            language=args.language,
            tolerance_ms=args.tolerance_ms,
            max_cues=args.max_lines,
            progress=progress,
            cancel=cancel,
            transcript_cache=not args.no_cache,
        )
    except OperationCancelled:
        print("Stopped. Nothing was written.", file=sys.stderr)
        return 130
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    chosen = args.format or (format_for_path(args.output) if args.output else "text")

    if args.output:
        written = write_transcript(args.output, result, chosen)
        print(f"Wrote {written}", file=sys.stderr)
        print(result.verdict_sentence())
    else:
        print(render(result, chosen))

    for error in result.errors:
        print(f"Error: {error}", file=sys.stderr)

    if not result.lines or result.errors:
        return 1
    if result.checked and (result.out_of_tolerance or result.unmatched):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
