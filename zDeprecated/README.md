# zDeprecated

Frozen copies of superseded Caption Inspector app versions. Nothing in here is
maintained. Do not point new work at these files.

Each snapshot is self-contained: its `.app` resolves its repo root to its own
folder, which carries its own copies of `cshim.py`, `libci.1.0.0.dylib`, and
that version's Python sources. Each bundle identifier is retagged
(`com.local.captioninspector.vN`) so macOS LaunchServices does not confuse the
snapshots with each other or with the live app.

## Caption Inspector v7.0

Snapshot taken 2026-09-17, superseded by **Caption Inspector v7.5**
(`Caption Inspector v7.5.app` at the repo root).

### What v7.0 did

A new, independent SCC transport compliance audit (raw byte-pair doubling and PAC/EDM/EOC
structure, parsed directly from `.scc` text rather than through `libci`), a **Delivery Compliance** section on `.scc` PDF
exports, and a standalone `transport_compare.py` byte-level diff tool. See *What changed in v7.0* in the top-level README.

### Why it was superseded

New evidence disproved one of v7.0's severity calls: `edm_eoc_combined_and_doubled` was flagged WARN on a 2-for-2
correlation with two failing test files, but a newly submitted fixture (`maccaps_v3_working.scc`, confirmed working in
Switch) has that exact pattern on every cue. v7.5 downgrades it to INFO and, in fixing up the test suite for the new
fixture, found and fixed a real cue-boundary bug in `group_into_cue_blocks` (a vendor encoder that resends RCL right
before its erase sequence was splitting one cue into two). See *What changed in v7.5* in the top-level README.

## Caption Inspector v6.0

Snapshot taken 2026-09-17, superseded by **Caption Inspector v7.0**
(`Caption Inspector v7.0.app` at the repo root).

### What v6.0 did

Export Text and Export PDF buttons on the Inspect Captions tab's Test Results pane, and made the app bundle itself
relocatable - it carries its own copy of `python/` and `libci` under `Contents/Resources/`, so it can be copied to
`/Applications` and run without this working tree present.

### Why it was superseded

Nothing wrong with it - v7.0 adds a new, independent SCC transport compliance audit (raw byte-pair doubling and PAC/EDM/EOC
structure, parsed directly from `.scc` text rather than through `libci`), a **Delivery Compliance** section on `.scc` PDF
exports, and a standalone `transport_compare.py` byte-level diff tool. See *What changed in v7.0* in the top-level README.

## Caption Inspector v5.8

Snapshot taken 2026-09-17, superseded by **Caption Inspector v6.0**
(`Caption Inspector v6.0.app` at the repo root).

### What v5.8 did

The Compare Subtitles tab, frame rate read from the file instead of asked for, and
nine more supported subtitle formats. See *What changed in v5.8* in the top-level
README for the detail.

### Why it was superseded

Nothing wrong with it - v6.0 adds **Export Text** and **Export PDF** buttons to the
Inspect Captions tab's results pane, so a decoded track or subtitle spotting list can
be saved to a file instead of copy-pasted or screenshotted.

## Caption Inspector v5.5

Snapshot taken 2026-08-18, superseded by **Caption Inspector v5.7**
(`Caption Inspector v5.7.app` at the repo root).

### What v5.5 did

Model availability shown in the picker instead of failing late, and the mid-row
control code fix that stopped the decoder silently dropping caption events.

### Why it was superseded

Two reporting faults, both visible on the first real delivery run through it.

The speaker label was printed twice - `LAUREN: LAUREN: What, the Cheshire grapevine?`
- because v5 started extracting the speaker into its own field but left it in the
line text, and every renderer prints the field in front of the text.

And every line the matcher could not attempt was reported as "not found in audio":
sound effects like `[cheering]`, and any cue under three words such as `- Oh.`. The
aligner declines those outright, so the app was accusing the caption file of a fault
it had never tested for. On a real programme they were around 40% of the lines.

## Caption Inspector v5

Snapshot taken 2026-08-18, superseded by **Caption Inspector v5.5**, which is itself
now frozen above.

### What v5 did

Everything v4 did, and fixed v4's speaker-label bug, so caption lines written
`LAUREN: ...` were timed correctly.

### Why it was superseded

The model picker offered all five transcription sizes whether or not the machine had
them. Choosing one it did not have looked fine, ran the whole check, and failed at the
last step - after the video had been probed, the cues read and the audio extracted -
with a message about staging weights into a bundle directory and no indication of how.

A built bundle carries only the models it was built with and deliberately will not
download more, so this was reachable on any bundle for four of the five sizes. v5.5
shows what is installed in the picker itself, refuses before doing any work, and says
what to do about it in terms that match how the app is being run.

## Caption Inspector v4

Snapshot taken 2026-08-18, superseded by **Caption Inspector v5**, which is itself now
frozen above. This snapshot carries `packaging/` as well, since v4 was the first
version that could build a distributable bundle.

### What v4 did

TTML and WebVTT support, the Transcript & Match tab, Stop buttons, and a fully
self-contained offline build.

### Why it was superseded

Tier 2 and the transcript tab mis-timed every caption line that named its speaker
inline. The speaker-stripping pattern required a leading `>>` or `-`, so `>> LAUREN:`
was handled but a bare `LAUREN:` was not - and a bare label is what most files use.

Nobody speaks a speaker's name, so the label never appears in the transcribed audio.
Left in the text, it occupied the cue's first token slot, and the matcher - which votes
for a window start of `position - token_index` - landed one word early. Each cue was then
timed from the word *before* its dialogue. The result was a scatter of negative offsets
whose size depended on the length of the preceding word and the pause before the line,
so it looked like erratic sync error rather than a bug. Cues without a label, or with the
`- ` form, measured correctly, which made the file look partly in sync and partly not.

Measured on identical audio, with two caption files differing only by `NAME:` prefixes:

| caption file | v4 | v5 |
|---|---|---|
| no speaker labels | +60 ms, 7 of 8 in tolerance | +60 ms, 7 of 8 |
| the same lines, labelled | **-8740 ms, 1 of 8** | **+60 ms, 7 of 8** |

v5 makes the leading chevron or dash optional, recognises the label conservatively so
dialogue containing a colon survives intact, keeps the speaker for the transcript, and -
as defence in depth - anchors each cue on the first token that actually matches rather
than on the start of the matched window.

## Caption Inspector v3

Snapshot taken 2026-08-18, superseded by **Caption Inspector v4**, which is itself now
frozen above.

> **This tree is a reconstruction, not a capture.** The v3 sources were edited in
> place while v4 was being built, before the snapshot was taken, and this repo is
> not under version control - so the original files no longer existed to copy.
> The tree here was rebuilt from the frozen v2 snapshot with v3's changes
> reapplied, and checked against the same fixture that produced v3's numbers: it
> reports the same `+25 ms` on the SCC case v3 was written to fix, and reproduces
> v3's WebVTT bug described below. The `.app` bundle is the original.

### What v3 did

Everything v2 did, and fixed v2's Tier 2 offset bug: it subtracted the video's start
timecode from caption timecodes before measuring caption-to-dialogue offset.

### Why it was superseded

v4 adds TTML and proper WebVTT support, a Transcript & Match tab, Stop buttons, and a
fully self-contained offline build.

It also narrows v3's start-timecode correction, which was applied too broadly. Subtracting
the video's start timecode is right for SCC and MCC, whose stamps are absolute program
timecode. It is wrong for WebVTT, TTML and SRT, whose zero is already the head of the
program, and wrong for captions decoded out of a container, which the decoder stamps from
PTS starting at zero. Against a video starting at `00:58:30:00`, v3 would have reported a
perfectly good WebVTT file as 3510 seconds out - the same bug it fixed for SCC, in the
opposite direction. It went unnoticed because the file that motivated the v3 fix was an
SCC. v4 decides the correction per caption source.

Measured on the same fixture - a WebVTT file that is correctly timed against a video
starting at `00:58:30:00`:

| | reported offset |
|---|---|
| v3 | `+3,499,810 ms` |
| v4 | `+65 ms` |

## Caption Inspector v2

Snapshot taken 2026-08-17, superseded by **Caption Inspector v3**, which is itself now
frozen above.

### What v2 did

Everything v1 did, plus the two-tier A/V Sync QC described below.

### Why it was superseded

Tier 2 measured caption-to-dialogue offset by subtracting two timelines that were
never put on the same reference frame. Caption timecodes are parsed as absolute
program timecode; faster-whisper stamps its words from the head of the media
file. On a delivery whose start timecode was `00:58:30:00`, the two zeros sat
3510 seconds apart, and Tier 2 reported that head as the sync error - a
"Constant offset" of roughly `-3,510,000 ms`, with the real offset buried in it.

v3 subtracts the video's start timecode (Tier 1's existing parse of the
container) from each caption timecode before comparing it to a Whisper
timestamp. The same fixture that read `-3,509,975 ms` under v2 reads `+25 ms`
under v3. Files starting at `00:00:00:00` are unaffected, which is why the bug
survived to a release.

## Caption Inspector v1

Snapshot taken 2026-08-17, superseded by **Caption Inspector v2**, which is itself now
frozen above.

### What v1 did

Decode a caption or media file (`.ts .mpg .mp4 .mov .mcc .scc`) through the C library and
browse the resulting CEA-608 / CEA-708 track events, either as a raw event timeline or as
reconstructed visible pop-on cues.

### What changed in v2

v2 keeps all of that and adds an **A/V Sync QC** tier that answers "does this caption file
actually line up with this video?":

- **Tier 1 (math)** — ffprobe the video, parse the caption file's timecodes, and fail fast on
  frame-rate mislabels, 1-hour-start reference mismatches, and captions that run past the end
  of the program. Seconds to run, no audio analysis.
- **Tier 2 (audio)** — transcribe the dialogue with faster-whisper, fuzzy-match caption text to
  the transcript, and regress caption-to-audio offset against timeline position. A sloped line
  is the drift signature of an uncorrected frame-rate mismatch; the slope gives the actual rate
  error.
- Vendor-facing plain-language text/HTML reports, plus a `sync_cli.py` command line for batch
  checking deliveries.
