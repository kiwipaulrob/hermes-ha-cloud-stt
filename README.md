# HA Cloud STT for Hermes Agent

Speech-to-text for [Hermes Agent](https://github.com/NousResearch/hermes-agent)
via a Home Assistant Assist pipeline. Set `stt.provider: ha_cloud` and Hermes
transcribes voice messages, Desktop dictation, and CLI voice mode through your
Home Assistant — using whatever STT engine its Assist pipeline is configured
with (HA Cloud, local Whisper, Vosk, …).

Community plugin. Not affiliated with Nous Research or Nabu Casa.

## How it works

1. Hermes hands the plugin a recorded audio file.
2. The plugin normalises it to 16 kHz mono PCM (`ffmpeg` when available,
   stdlib `wave` fallback for already-conforming WAVs).
3. It opens the Home Assistant websocket (`/api/websocket`), authenticates
   with your long-lived access token, and runs `assist_pipeline/run` with
   `start_stage: stt` / `end_stage: stt`.
4. Audio chunks stream as binary frames prefixed with the run's
   `stt_binary_handler_id`; the transcript comes back in the `stt-end` event.
5. Every failure returns the standard error envelope (`success: false` + human
   text) so Hermes can fall back to local STT — `transcribe()` never raises.

Pipeline selection: set `stt.ha_cloud.pipeline` (or `model`) to pin an Assist
pipeline id, or leave it empty and the plugin uses the server's *preferred*
pipeline at runtime. Language always follows the pipeline.

## Install

```bash
hermes plugins install ha-cloud-stt
```

Then in `config.yaml`:

```yaml
stt:
  provider: ha_cloud
  ha_cloud:
    url: http://homeassistant.local:8123
    # pipeline: "01ABCDEF..."   # optional: pin an Assist pipeline id
```

And store a Home Assistant long-lived access token (HA profile > Security >
Long-lived access tokens) via `hermes auth` or the environment:

```bash
HASS_TOKEN=...          # required
HASS_URL=http://homeassistant.local:8123   # optional, overrides config url
```

| Variable | Purpose |
|---|---|
| `HASS_TOKEN` | Home Assistant long-lived access token (**required**) |
| `HASS_URL` | Home Assistant base URL if not set in `config.yaml` |

System needs: `ffmpeg` (decodes mp3/ogg/opus phone uploads; without it only
16 kHz mono 16-bit WAV works) and the `websockets` Python package (declared in
`pyproject.toml`, installed with the plugin).

## Disclosure (catalog review)

- **Network:** one outbound websocket to *your own* Home Assistant instance
  (`HASS_URL`, default `http://homeassistant.local:8123`); audio + token go
  there and nowhere else. No third-party service is contacted by this plugin.
- **Credentials:** read only through Hermes's secret scope
  (`agent.secret_scope.get_secret`, honouring the active profile), with a
  process-env fallback only on cores that predate the scope. Never reads
  `.env` files, `auth.json`, or other credential stores; never writes tokens.
- **Shell:** runs `ffmpeg -i <file> -ac 1 -ar 16000 -f s16le -` for audio
  normalisation (read-only transcode of Hermes's own recording to stdout).
- **Processes/state:** none — no background tasks, no daemons, no files
  written, no telemetry.
- **Approvals:** N/A — pure STT backend, no tools, no commands, no unattended
  behaviour. Under cron/gateway it behaves identically (transcribe on demand).

## Compatibility

- `requires_hermes: ">=0.21.6"` (needs `PluginContext.register_transcription_provider`).
- Registering under the name `ha_cloud` coexists with all 8 built-in STT
  providers; built-ins always win on name collision, and a same-name
  `stt.providers.<name>: type: command` entry wins over this plugin.
- The plugin does **not** implement live-streaming transcription
  (`TranscriptionProvider.streaming_capable`); Desktop dictation and CLI voice
  use the file path and degrade gracefully.

## Development

Tests import Hermes core, so run them with a hermes-agent checkout on
`PYTHONPATH`:

```bash
PYTHONPATH=/usr/local/lib/hermes-agent python -m pytest -q
hermes plugins validate ./ha-cloud-stt
```

Layout follows the official-plugin convention: `plugin.yaml`, `__init__.py`,
`provider.py`, `pyproject.toml`, `README.md`, `tests/`.

## Catalog submission

Third-party plugins live in their author's repo and enter the
[plugin catalog](https://github.com/NousResearch/hermes-agent/tree/main/plugin-catalog)
via a PR to `hermes-agent` adding `plugin-catalog/ha-cloud-stt.yaml`:

```yaml
name: ha-cloud-stt
repo: https://github.com/kiwipaulrob/hermes-ha-cloud-stt
sha: <40-char reviewed commit sha>
subdir: ""
description: "Speech-to-text for Hermes via a Home Assistant Assist pipeline (HA Cloud, local Whisper, ...). Needs HASS_TOKEN."
maintainer: kiwipaulrob
tier: community
category: voice
requires_hermes: ">=0.21.6"
version: "1.0.0"
capabilities:
  provides_tools: []
  provides_hooks: []
  provides_middleware: []
  requires_env: [HASS_TOKEN]
```

## License

MIT. See [LICENSE](LICENSE).
