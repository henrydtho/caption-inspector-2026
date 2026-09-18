#!/usr/bin/env python3
"""Command line for the subtitle comparison.

    python3 python/compare_cli.py approved.srt delivered.srt
    python3 python/compare_cli.py v1.ttml v2.ttml -o diff.html

Differences are described as the second file relative to the first, so the
approved version goes first. Exits 0 when the two files are equivalent, 2 when
they differ, and 1 when something could not be read - so it drops into a
delivery gate the same way `sync_cli.py` does.
"""

import argparse
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cancellation import CancelToken, OperationCancelled  # noqa: E402
from frame_rate_detect import detect_frame_rate  # noqa: E402
from subtitle_compare import (  # noqa: E402
    DEFAULT_TOLERANCE_MS,
    FAIL,
    WARN,
    compare_subtitles,
    render_text_report,
    write_comparison_report,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Compare two subtitle files for the same programme.",
    )
    parser.add_argument("file_a", help="The reference file (usually the approved version)")
    parser.add_argument("file_b", nargs="?", help="The file to compare against it")
    parser.add_argument("-o", "--output", help="Write a report here; the format follows its extension")
    parser.add_argument(
        "--tolerance-ms",
        type=float,
        default=DEFAULT_TOLERANCE_MS,
        help=f"Treat cues this close as simultaneous (default {DEFAULT_TOLERANCE_MS})",
    )
    parser.add_argument(
        "--max-differences", type=int, help="Print at most this many differences to the terminal"
    )
    parser.add_argument(
        "--frame-rate",
        action="store_true",
        help="Just report what frame rate each file is in, and stop",
    )
    parser.add_argument("--quiet", action="store_true", help="Only print the verdict line")
    return parser.parse_args(argv)


def _report_frame_rates(paths):
    """`--frame-rate`: answer "what is this file in?" without comparing anything."""
    status = 0
    for path in paths:
        detection = detect_frame_rate(path)
        print(f"{Path(path).name}")
        print(f"  {detection.headline()}")
        for note in detection.notes:
            print(f"    - {note}")
        if detection.error or detection.conflict:
            status = 2
    return status


def main(argv=None):
    args = parse_args(argv)

    if args.frame_rate:
        targets = [args.file_a] + ([args.file_b] if args.file_b else [])
        return _report_frame_rates(targets)

    if not args.file_b:
        print("Error: two files are required (or use --frame-rate).", file=sys.stderr)
        return 1

    cancel = CancelToken()

    def on_interrupt(_signum, _frame):
        print("\n  ... stopping", file=sys.stderr)
        cancel.cancel()

    signal.signal(signal.SIGINT, on_interrupt)

    try:
        result = compare_subtitles(
            args.file_a, args.file_b, tolerance_ms=args.tolerance_ms, cancel=cancel
        )
    except OperationCancelled:
        print("Stopped. Nothing was written.", file=sys.stderr)
        return 130
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    if args.output:
        written = write_comparison_report(args.output, result)
        print(f"Wrote {written}", file=sys.stderr)
    elif not args.quiet:
        print(render_text_report(result, max_differences=args.max_differences))

    for error in result.errors:
        print(f"Error: {error}", file=sys.stderr)
    if result.errors:
        return 1

    differences = len(result.differences())
    flagged = result.verdict in (FAIL, WARN)

    if differences:
        print(
            f"{differences} differences between {Path(args.file_a).name} and "
            f"{Path(args.file_b).name}.",
            file=sys.stderr,
        )
    elif flagged:
        # No differences is not the same as nothing to look at: a file can be
        # identical to its counterpart and still fail its own validity checks.
        print(
            "No differences between the two files, but the checks flagged something.",
            file=sys.stderr,
        )
    else:
        print("The two files are equivalent.", file=sys.stderr)

    if differences or flagged:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
