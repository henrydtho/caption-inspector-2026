import json
import tempfile
from pathlib import Path

import streamlit as st

from cshim import resolve_caption_converter_library
from inspection_support import SUPPORTED_TYPES, decode_file, text_preview, track_summary_rows


def inject_styles():
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap');

        :root {
            --ci-ink: #12212f;
            --ci-muted: #52616f;
            --ci-surface: rgba(255, 255, 255, 0.86);
            --ci-accent: #e46f2f;
            --ci-accent-soft: #ffd8b8;
            --ci-border: rgba(18, 33, 47, 0.08);
        }

        .stApp {
            background:
                radial-gradient(circle at top left, rgba(253, 191, 143, 0.45), transparent 32%),
                radial-gradient(circle at top right, rgba(89, 166, 196, 0.18), transparent 28%),
                linear-gradient(180deg, #fffaf4 0%, #f6f1eb 52%, #eef3f6 100%);
            color: var(--ci-ink);
        }

        html, body, [class*="css"]  {
            font-family: 'IBM Plex Sans', sans-serif;
        }

        .ci-hero {
            padding: 1.4rem 1.6rem;
            margin-bottom: 1rem;
            border: 1px solid var(--ci-border);
            border-radius: 22px;
            background: linear-gradient(135deg, rgba(255,255,255,0.92), rgba(255,245,236,0.84));
            box-shadow: 0 22px 50px rgba(18, 33, 47, 0.08);
        }

        .ci-kicker {
            font-size: 0.82rem;
            letter-spacing: 0.12em;
            text-transform: uppercase;
            color: var(--ci-accent);
            font-weight: 700;
            margin-bottom: 0.35rem;
        }

        .ci-title {
            font-size: 2.2rem;
            line-height: 1.05;
            font-weight: 700;
            margin: 0;
            color: var(--ci-ink);
        }

        .ci-copy {
            margin-top: 0.7rem;
            max-width: 56rem;
            color: var(--ci-muted);
            font-size: 1rem;
        }

        .ci-card {
            border: 1px solid var(--ci-border);
            border-radius: 18px;
            padding: 1rem 1.1rem;
            background: var(--ci-surface);
            box-shadow: 0 12px 30px rgba(18, 33, 47, 0.05);
        }

        .ci-card-label {
            font-size: 0.8rem;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: var(--ci-muted);
            margin-bottom: 0.35rem;
        }

        .ci-card-value {
            font-size: 1.8rem;
            line-height: 1;
            font-weight: 700;
            color: var(--ci-ink);
        }

        .ci-card-detail {
            margin-top: 0.45rem;
            color: var(--ci-muted);
            font-size: 0.92rem;
        }

        .ci-code {
            font-family: 'IBM Plex Mono', monospace;
            font-size: 0.88rem;
            color: var(--ci-ink);
            word-break: break-word;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_stat_card(label, value, detail):
    st.markdown(
        f"""
        <div class="ci-card">
            <div class="ci-card-label">{label}</div>
            <div class="ci-card-value">{value}</div>
            <div class="ci-card-detail">{detail}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def library_status():
    library_path = Path(resolve_caption_converter_library())
    return library_path, library_path.exists()


def decode_upload(uploaded_file, framerate):
    with tempfile.TemporaryDirectory(prefix="caption-inspector-") as temp_dir:
        input_path = Path(temp_dir) / uploaded_file.name
        input_path.write_bytes(uploaded_file.getbuffer())
        tracks, _ = decode_file(str(input_path), framerate, capture_logs=False)
        return tracks


st.set_page_config(page_title="Caption Inspector App", page_icon="CC", layout="wide")
inject_styles()

st.markdown(
    """
    <section class="ci-hero">
        <div class="ci-kicker">Caption Inspector</div>
        <h1 class="ci-title">Decode, inspect, and compare caption tracks from one screen.</h1>
        <p class="ci-copy">Upload a supported asset, run the existing shared-library decoder, then inspect CEA-608 and CEA-708 events by track without dropping back to the command line.</p>
    </section>
    """,
    unsafe_allow_html=True,
)

library_path, library_ready = library_status()

with st.sidebar:
    st.subheader("Runtime")
    if library_ready:
        st.success("Shared library detected")
    else:
        st.error("Shared library missing")
    st.markdown(f"<div class='ci-code'>{library_path}</div>", unsafe_allow_html=True)
    st.caption("Build it with `make sharedlib` from the repository root if it is not present.")

    st.subheader("Input")
    uploaded_file = st.file_uploader(
        "Asset",
        type=list(SUPPORTED_TYPES),
        help="Supported inputs: ts, mpg, mp4, mcc, scc, mov",
    )
    framerate = st.number_input(
        "Frame rate x100",
        min_value=0,
        max_value=6000,
        value=0,
        step=100,
        help="Use a value such as 2400 when the source needs an explicit frame rate, especially for SCC workflows.",
    )
    decode_clicked = st.button("Decode captions", type="primary", use_container_width=True, disabled=not library_ready)

if decode_clicked:
    if uploaded_file is None:
        st.warning("Choose a supported asset before decoding.")
    else:
        try:
            with st.spinner("Running caption decode pipeline..."):
                st.session_state["decoded_tracks"] = decode_upload(uploaded_file, int(framerate))
                st.session_state["decoded_filename"] = uploaded_file.name
        except OSError as error:
            st.error(f"Unable to load the shared library or one of its native dependencies: {error}")
        except ValueError as error:
            st.error(str(error))
        except Exception as error:
            st.error(f"Decode failed: {error}")

decoded_tracks = st.session_state.get("decoded_tracks")
decoded_filename = st.session_state.get("decoded_filename")

if decoded_tracks:
    summary_rows = track_summary_rows(decoded_tracks)
    total_events = sum(row["events"] for row in summary_rows)
    total_tracks = len(summary_rows)
    total_text_events = sum(row["text_events"] for row in summary_rows)

    stat_cols = st.columns(3)
    with stat_cols[0]:
        render_stat_card("Asset", decoded_filename, "Current in-memory decode result")
    with stat_cols[1]:
        render_stat_card("Tracks", total_tracks, "Non-empty caption tracks discovered")
    with stat_cols[2]:
        render_stat_card("Events", total_events, f"{total_text_events} text-bearing events")

    st.subheader("Track summary")
    st.dataframe(summary_rows, use_container_width=True, hide_index=True)

    family = st.radio("Caption family", ["CEA-608", "CEA-708"], horizontal=True)
    family_tracks = decoded_tracks[family]

    if family_tracks:
        track_name = st.selectbox("Track", list(family_tracks.keys()))
        rows = family_tracks[track_name]
        transcript = text_preview(rows)

        timeline_tab, transcript_tab, json_tab = st.tabs(["Timeline", "Transcript", "JSON"])

        with timeline_tab:
            st.dataframe(rows, use_container_width=True, hide_index=True, height=520)

        with transcript_tab:
            if transcript:
                st.text_area("Text-bearing caption events", transcript, height=520)
            else:
                st.info("This track contains control or styling events but no text strings.")

        with json_tab:
            st.json(rows)

        download_cols = st.columns(2)
        with download_cols[0]:
            st.download_button(
                "Download track JSON",
                data=json.dumps(rows, indent=2),
                file_name=f"{Path(decoded_filename).stem}-{track_name.replace(' ', '-').lower()}.json",
                mime="application/json",
                use_container_width=True,
            )
        with download_cols[1]:
            st.download_button(
                "Download transcript",
                data=transcript,
                file_name=f"{Path(decoded_filename).stem}-{track_name.replace(' ', '-').lower()}.txt",
                mime="text/plain",
                use_container_width=True,
            )
    else:
        st.info(f"No {family} tracks were produced for this asset.")
elif decode_clicked and library_ready:
    st.info("The decode completed, but no caption tracks were returned for this asset.")
else:
    st.info("Upload an asset and run a decode to inspect its caption tracks here.")