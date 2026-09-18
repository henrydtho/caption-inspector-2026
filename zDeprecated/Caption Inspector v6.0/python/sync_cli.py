#!/usr/bin/env python3
"""Command line for caption sync QC.

    # fast fail on every delivery - seconds, no audio analysis
    python3 python/sync_cli.py episode.mov episode.scc

    # full pass with audio verification and a report for the vendor
    python3 python/sync_cli.py episode.mov episode.scc --tier2 --report drift.html

    # caption file on its own (ordering, drop-frame legality, content)
    python3 python/sync_cli.py --captions-only episode.scc

Exit codes: 0 pass, 1 review (warnings), 2 fail, 3 the check could not run.
"""

import argparse
import sys
from pathlib import Path

from drift_report import headline_sentence, render_html_report, render_json_report, render_text_report, write_report
from sync_check import FAIL, PASS, WARN, tier1_check, tier2_check
from timecode import RATE_LABELS
from transcribe import MODEL_SIZES


EXIT_CODES = {PASS: 0, WARN: 1, FAIL: 2}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="sync_cli.py",
        description="Check that a caption file is in sync with its video.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("video", nargs="?", help="The delivered video file.")
    parser.add_argument("captions", help="The caption sidecar (.scc, .mcc, .srt, .vtt).")
    parser.add_argument(
        "--captions-only",
        action="store_true",
        help="Skip the video and check the caption file's internal consistency only.",
    )
    parser.add_argument(
        "--tier2",
        action="store_true",
        help="Run audio-verified sync after Tier 1 (transcribes the dialogue; slow).",
    )
    parser.add_argument(
        "--tier2-always",
        action="store_true",
        help="Run Tier 2 even when Tier 1 already failed on hard math.",
    )
    parser.add_argument(
        "--rate",
        type=int,
        choices=sorted(RATE_LABELS),
        help="Frame rate x100 to read caption timecodes at (default: the video's rate).",
    )
    parser.add_argument(
        "--tolerance-frames",
        type=int,
        default=2,
        help="Frames of slack on the tail landing check (default: 2).",
    )
    parser.add_argument(
        "--tolerance-ms",
        type=float,
        default=200.0,
        help="Milliseconds of constant offset tolerated in Tier 2 (default: 200).",
    )
    parser.add_argument(
        "--model",
        default="base",
        choices=MODEL_SIZES,
        help="faster-whisper model size for Tier 2 (default: base).",
    )
    parser.add_argument("--language", help="Force the dialogue language, e.g. en. Default: auto-detect.")
    parser.add_argument(
        "--max-cues",
        type=int,
        default=600,
        help="Cap on cues aligned in Tier 2, sampled evenly across the program (default: 600).",
    )
    parser.add_argument(
        "--count-frames",
        action="store_true",
        help="Decode the video for an exact frame count instead of trusting the container.",
    )
    parser.add_argument("--report", help="Write a report to this path (.html, .json, or .txt).")
    parser.add_argument(
        "--format",
        choices=("text", "html", "json"),
        default="text",
        help="Format for stdout (default: text).",
    )
    parser.add_argument("--quiet", action="store_true", help="Print only the headline sentence.")
    parser.add_argument("--no-cache", action="store_true", help="Ignore cached transcripts.")

    args = parser.parse_args(argv)

    # `video captions` is the normal form; `--captions-only file` takes one path.
    if args.captions_only:
        if args.video and args.captions:
            parser.error("--captions-only takes a single caption file.")
        if args.video and not args.captions:
            args.captions, args.video = args.video, None
    elif not args.video:
        parser.error("Provide both a video and a caption file, or use --captions-only.")

    return args


def main(argv=None):
    args = parse_args(argv)

    caption_path = Path(args.captions)
    if not caption_path.exists():
        print(f"Caption file not found: {caption_path}", file=sys.stderr)
        return 3

    video_path = None
    if not args.captions_only:
        video_path = Path(args.video)
        if not video_path.exists():
            print(f"Video file not found: {video_path}", file=sys.stderr)
            return 3

    def progress(message):
        if not args.quiet:
            print(f"  ... {message}", file=sys.stderr)

    tier1 = tier1_check(
        caption_path,
        video_path,
        rate_code=args.rate,
        tolerance_frames=args.tolerance_frames,
        count_frames=args.count_frames,
    )

    if tier1.errors:
        for error in tier1.errors:
            print(f"error: {error}", file=sys.stderr)
        return 3

    tier2 = None
    if args.tier2 or args.tier2_always:
        if not video_path:
            print("error: Tier 2 needs a video with an audio track.", file=sys.stderr)
            return 3
        if tier1.verdict == FAIL and not args.tier2_always:
            progress("Tier 1 failed on hard math; skipping Tier 2 (use --tier2-always to force it).")
        else:
            tier2 = tier2_check(
                caption_path,
                video_path,
                rate_code=args.rate or tier1.rate_code,
                model_size=args.model,
                language=args.language,
                tolerance_ms=args.tolerance_ms,
                max_cues=args.max_cues,
                progress=progress,
                transcript_cache=not args.no_cache,
            )
            if tier2.errors:
                for error in tier2.errors:
                    print(f"tier 2: {error}", file=sys.stderr)

    if args.quiet:
        print(headline_sentence(tier1, tier2))
    elif args.format == "html":
        print(render_html_report(tier1, tier2))
    elif args.format == "json":
        print(render_json_report(tier1, tier2))
    else:
        print(render_text_report(tier1, tier2))

    if args.report:
        written = write_report(args.report, tier1, tier2)
        print(f"Report written to {written}", file=sys.stderr)

    verdicts = [result.verdict for result in (tier1, tier2) if result]
    if FAIL in verdicts:
        return EXIT_CODES[FAIL]
    if WARN in verdicts:
        return EXIT_CODES[WARN]
    return EXIT_CODES[PASS]


if __name__ == "__main__":
    raise SystemExit(main())
