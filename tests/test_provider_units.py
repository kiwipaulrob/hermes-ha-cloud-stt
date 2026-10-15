"""Behaviour contracts for the pure helpers in provider.py.

No network, no Home Assistant, no ffmpeg: framing, event parsing, pipeline
choice, URL mapping, and config resolution. A failure here means the wire
protocol drifted from the Assist pipeline spec.
"""

from __future__ import annotations

import sys

import pytest

sys.path.insert(0, __import__("pathlib").Path(__file__).resolve().parents[1].as_posix())

from provider import (  # noqa: E402
    HaCloudError,
    _extract_handler_id,
    _extract_stt_transcript,
    _frame_audio_chunk,
    _ha_ws_url,
    _pick_preferred_pipeline,
    _settings,
    _termination_frame,
)


def test_frame_audio_chunk_prefixes_handler_byte():
    assert _frame_audio_chunk(1, b"\xa1\xb2\xc3") == b"\x01\xa1\xb2\xc3"


def test_termination_frame_is_bare_handler_byte():
    assert _termination_frame(7) == b"\x07"


def test_extract_stt_transcript_prefers_new_shape():
    event = {"stt_output": {"text": "hello there"}, "stt_text": "legacy"}
    assert _extract_stt_transcript(event) == "hello there"


def test_extract_stt_transcript_falls_back_to_legacy():
    assert _extract_stt_transcript({"stt_text": "old shape"}) == "old shape"


def test_extract_stt_transcript_empty_when_absent():
    assert _extract_stt_transcript({}) == ""


def test_extract_handler_id_ok():
    assert _extract_handler_id({"runner_data": {"stt_binary_handler_id": 3}}) == 3


def test_extract_handler_id_missing_raises():
    with pytest.raises(HaCloudError):
        _extract_handler_id({"runner_data": {}})


def test_extract_handler_id_out_of_range_raises():
    with pytest.raises(HaCloudError):
        _extract_handler_id({"runner_data": {"stt_binary_handler_id": 999}})


def test_pick_preferred_pipeline_prefers_flagged():
    pipelines = [{"id": "a"}, {"id": "b", "preferred": True}, {"id": "c"}]
    assert _pick_preferred_pipeline(pipelines) == "b"


def test_pick_preferred_pipeline_falls_back_to_first():
    assert _pick_preferred_pipeline([{"id": "x"}, {"id": "y"}]) == "x"


def test_pick_preferred_pipeline_empty_when_none():
    assert _pick_preferred_pipeline([]) == ""
    assert _pick_preferred_pipeline({}) == ""


def test_ha_ws_url_http_to_ws():
    assert _ha_ws_url("http://homeassistant.local:8123") == \
        "ws://homeassistant.local:8123/api/websocket"


def test_ha_ws_url_https_to_wss():
    assert _ha_ws_url("https://ha.example.com") == "wss://ha.example.com/api/websocket"


def test_ha_ws_url_garbage_raises():
    with pytest.raises(HaCloudError):
        _ha_ws_url("not-a-url")


def test_settings_defaults_to_mdns_and_empty_pipeline(monkeypatch):
    monkeypatch.delenv("HASS_URL", raising=False)
    url, pipeline = _settings({})
    assert url == "http://homeassistant.local:8123"
    assert pipeline == ""


def test_settings_config_pipeline_wins(monkeypatch):
    monkeypatch.delenv("HASS_URL", raising=False)
    url, pipeline = _settings({"ha_cloud": {"url": "http://ha:8123/", "pipeline": "abc"}})
    assert url == "http://ha:8123"
    assert pipeline == "abc"


def test_settings_model_alias_accepted(monkeypatch):
    monkeypatch.delenv("HASS_URL", raising=False)
    _, pipeline = _settings({"ha_cloud": {"model": "xyz"}})
    assert pipeline == "xyz"
