#!/usr/bin/env python3
"""Tests for the v5.5 model-availability handling.

Run from this directory:  python3 -m pytest test__model_availability.py -v

v5 let you pick any of the five model sizes whether or not the machine had it,
then failed after probing the video and extracting its audio. These pin the
three things that changed: the app knows what it has, it says so before doing
any work, and what it says is actionable.
"""

import sys
from pathlib import Path

import pytest

PYTHON_DIR = Path(__file__).resolve().parents[2] / "python"
sys.path.insert(0, str(PYTHON_DIR))

import offline  # noqa: E402
from transcribe import MODEL_SIZES, TranscriptionError, ensure_model_available  # noqa: E402


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    """A fake bundle carrying only the `base` model."""
    resources = tmp_path / "Resources"
    (resources / "models" / "faster-whisper-base").mkdir(parents=True)
    (resources / "models" / "faster-whisper-base" / "model.bin").write_bytes(b"weights")
    (resources / offline.BUNDLE_MARKER).write_text("test bundle\n", encoding="utf-8")

    monkeypatch.setenv("CAPTION_INSPECTOR_RESOURCES", str(resources))
    # Point the cache lookup at an empty directory so only the bundle counts.
    monkeypatch.setenv("HF_HOME", str(tmp_path / "empty-cache"))
    return resources


def test_a_bundle_reports_only_the_models_it_carries(bundle):
    assert offline.is_bundled()
    assert offline.bundled_model_names() == ["base"]
    assert offline.model_status("base") == offline.BUNDLED
    assert offline.model_status("medium") == offline.MISSING
    assert offline.available_models(MODEL_SIZES) == ["base"]


def test_a_bundle_never_offers_to_download(bundle):
    """The whole point of a packaged build; a Get button there would be a lie."""
    assert offline.network_allowed() is False


def test_a_working_tree_may_download(tmp_path, monkeypatch):
    monkeypatch.delenv("CAPTION_INSPECTOR_RESOURCES", raising=False)
    monkeypatch.setenv("HF_HOME", str(tmp_path / "empty-cache"))
    assert offline.is_bundled() is False
    assert offline.network_allowed() is True


def test_an_explicit_opt_in_re_enables_downloads_in_a_bundle(bundle, monkeypatch):
    monkeypatch.setenv("CAPTION_INSPECTOR_ALLOW_NETWORK", "1")
    assert offline.network_allowed() is True


def test_a_cached_model_counts_as_available(tmp_path, monkeypatch):
    monkeypatch.delenv("CAPTION_INSPECTOR_RESOURCES", raising=False)
    hub = tmp_path / "hub" / "models--Systran--faster-whisper-small" / "snapshots" / "abc"
    hub.mkdir(parents=True)
    (hub / "model.bin").write_bytes(b"weights")
    monkeypatch.setenv("HF_HOME", str(tmp_path))

    assert offline.model_status("small") == offline.CACHED
    assert offline.model_is_available("small")


def test_the_missing_model_message_names_what_is_available(bundle):
    message = offline.describe_missing_model("medium", MODEL_SIZES)
    assert "'medium'" in message
    assert "base" in message


def test_a_bundle_is_told_how_to_stage_not_how_to_download(bundle):
    """Advice has to match the situation, or it is worse than none."""
    message = offline.describe_missing_model("medium", MODEL_SIZES)
    assert "stage_model.py" in message
    assert "make offline-app MODELS=" in message
    assert "WhisperModel('medium')" not in message


def test_a_working_tree_is_told_how_to_download(tmp_path, monkeypatch):
    monkeypatch.delenv("CAPTION_INSPECTOR_RESOURCES", raising=False)
    monkeypatch.setenv("HF_HOME", str(tmp_path / "empty-cache"))
    message = offline.describe_missing_model("medium", MODEL_SIZES)
    assert "WhisperModel('medium')" in message
    assert "stage_model.py" not in message


def test_ensure_model_available_raises_for_a_missing_model(bundle):
    with pytest.raises(TranscriptionError) as raised:
        ensure_model_available("medium")
    assert "not installed" in str(raised.value)


def test_ensure_model_available_passes_for_a_present_one(bundle):
    ensure_model_available("base")


def test_ensure_model_available_rejects_an_unknown_size(bundle):
    with pytest.raises(TranscriptionError, match="Unknown model size"):
        ensure_model_available("enormous")


def test_a_missing_model_is_refused_before_any_work_is_done(bundle, tmp_path, monkeypatch):
    """The pre-flight has to fire before the video is even probed.

    Without it a missing model costs a probe, a cue read and a full audio
    extraction before anything is reported.
    """
    import sync_check

    probed = []
    monkeypatch.setattr(
        sync_check, "probe_media",
        lambda *args, **kwargs: probed.append(args) or pytest.fail("probe_media was called"),
    )

    result = sync_check.tier2_check("nonexistent.scc", "nonexistent.mov", model_size="medium")

    assert result.errors and "not installed" in result.errors[0]
    assert probed == []


def test_the_transcript_still_gets_built_when_the_model_is_missing(bundle, tmp_path):
    """A missing model costs the audio check, not the transcript."""
    from transcript_check import build_transcript

    captions = tmp_path / "p.vtt"
    captions.write_text(
        "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nHello there\n\n"
        "00:00:05.000 --> 00:00:08.000\nSecond line\n",
        encoding="utf-8",
    )
    video = tmp_path / "p.mov"
    video.write_bytes(b"not really a movie")

    result = build_transcript(str(captions), str(video), model_size="medium")

    assert len(result.lines) == 2
    assert not result.checked
    assert any("not installed" in error for error in result.errors)
    assert "not checked" in result.verdict_sentence()


def test_model_picker_labels_say_what_is_installed(bundle):
    from model_picker import label_for, size_for

    assert label_for("base") == "base (bundled)"
    assert label_for("medium") == "medium (not installed)"
    assert size_for("medium (not installed)") == "medium"
    assert size_for("base (bundled)") == "base"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
