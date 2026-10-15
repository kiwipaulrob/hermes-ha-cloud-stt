"""register(ctx) wiring: the ha_cloud transcription provider goes through the
public plugin API with the exact name voice dispatch depends on."""

from __future__ import annotations

import sys

import pytest

import ha_cloud_stt_plugin
from ha_cloud_stt_plugin import provider as provider_mod


class RecordingCtx:
    def __init__(self):
        self.transcription_providers = []

    def register_transcription_provider(self, provider):
        self.transcription_providers.append(provider)


@pytest.fixture
def ctx():
    c = RecordingCtx()
    ha_cloud_stt_plugin.register(c)
    return c


def test_registers_exactly_one_transcription_provider(ctx):
    (provider,) = ctx.transcription_providers
    assert provider is not None


def test_provider_name_is_ha_cloud(ctx):
    (provider,) = ctx.transcription_providers
    assert provider.name == "ha_cloud"


def test_provider_class_matches_module(ctx):
    (provider,) = ctx.transcription_providers
    assert isinstance(provider, provider_mod.HaCloudSttProvider)


def test_provider_unavailable_without_token(ctx, monkeypatch):
    (provider,) = ctx.transcription_providers
    monkeypatch.delenv("HASS_TOKEN", raising=False)
    # Secret scope may still resolve from a profile overlay; force the scope
    # itself to empty when present so this asserts the token gate, not the env.
    monkeypatch.setattr(provider_mod, "_scoped_secret_str", lambda *a, **k: "")
    assert provider.is_available() is False
    assert "HASS_TOKEN" in provider.transcribe("/nonexistent.wav")["error"]


def test_transcribe_missing_file_returns_envelope_not_raise(ctx, monkeypatch, tmp_path):
    (provider,) = ctx.transcription_providers
    monkeypatch.setenv("HASS_TOKEN", "test-token")
    result = provider.transcribe(str(tmp_path / "nope.wav"))
    assert result["success"] is False
    assert result["transcript"] == ""
    assert result["provider"] == "ha_cloud"
    assert "not found" in result["error"].lower()
