"""ha-cloud-stt — Home Assistant STT as a standalone Hermes plugin.

Registers a :class:`agent.transcription_provider.TranscriptionProvider` named
``ha_cloud`` through the supported plugin hook, so ``stt.provider: ha_cloud``
resolves with no core patch. Set ``stt.ha_cloud.url`` / ``HASS_URL`` and a
``HASS_TOKEN``; the Assist pipeline defaults to the server's preferred one.
"""

from __future__ import annotations


def register(ctx) -> None:
    """Called once by the plugin loader."""
    from .provider import HaCloudSttProvider

    ctx.register_transcription_provider(HaCloudSttProvider())
