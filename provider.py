"""Home Assistant STT via the Assist pipeline websocket.

Streams normalized 16 kHz mono PCM to ``assist_pipeline/run`` (``start_stage``
= ``end_stage`` = ``stt``) on the configured Home Assistant instance, which
transcribes with whatever STT engine its Assist pipeline uses (HA Cloud,
local whisper, Vosk, ... — the plugin does not care).

Wire framing (``websocket_api/http.py``): every binary frame is
``bytes([handler_id]) + payload`` where ``handler_id`` is the
``stt_binary_handler_id`` from the ``run-start`` event's ``runner_data``. The
stream ends by sending the bare handler byte (empty payload); a zero-length
frame would disconnect. The transcript arrives in the ``stt-end`` event at
``data.stt_output.text`` (older servers: ``data.stt_text``).

Pipeline selection: ``stt.ha_cloud.pipeline`` (or ``stt.ha_cloud.model``, or the
``model`` override) wins when set; otherwise the plugin asks the server for
``assist_pipeline/pipeline/list`` and uses its *preferred* pipeline. Nothing
about any particular home is hardcoded: the only default URL is the
mDNS name Home Assistant itself advertises (``http://homeassistant.local:8123``).

This module is deliberately self-contained: it imports only the
:mod:`agent` ABC / secret scope plus the stdlib, so it works against any core
new enough to offer ``PluginContext.register_transcription_provider`` with no
core patch. Every failure returns the standard error envelope
(``success`` False, ``transcript`` "", ``error`` str, ``provider``
``"ha_cloud"``) so callers can fall back to local STT — ``transcribe`` never
raises.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import threading
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from agent.secret_scope import get_secret_str as _scoped_secret_str
except Exception:  # core predates the secret scope: fall back to the process env
    _scoped_secret_str = None

try:
    from agent.transcription_provider import TranscriptionProvider
except Exception:  # core predates the STT plugin hook: refuse loudly at register()
    TranscriptionProvider = None  # type: ignore[assignment]

logger = logging.getLogger("ha_cloud_stt")

NAME = "ha_cloud"
TOKEN_ENV = "HASS_TOKEN"
URL_ENV = "HASS_URL"
DEFAULT_HA_URL = "http://homeassistant.local:8123"
SAMPLE_RATE = 16000

_WS_CHUNK_BYTES = 8192
_WS_RUN_ID = 10
_WS_TIMEOUT_SECONDS = 90.0
_REACHABLE_TIMEOUT_SECONDS = 3.0
# NOTE: HA rejects reused or decreasing command ids (``id_reuse``), so the
# pipeline/list probe always takes ``msg_id`` and the run takes ``msg_id + 1``.


class HaCloudError(Exception):
    """A Home Assistant STT failure; always converted to the error envelope."""


# ---- Config / credentials -------------------------------------------------

def _get_secret(name: str, default: str = "") -> str:
    """Credential via the shared secret scope (profile .env overlay + process env)."""
    if _scoped_secret_str is not None:
        try:
            return _scoped_secret_str(name, default) or default
        except Exception:
            pass
    return os.environ.get(name, default) or default


def _token() -> str:
    """Long-lived HA token (``HASS_TOKEN``) via the standard secret flow."""
    return _get_secret(TOKEN_ENV, "").strip()


def _load_stt_config() -> Dict[str, Any]:
    """The ``stt`` section of the active profile config, or {} when unavailable."""
    try:
        from hermes_cli.config import load_config
        cfg = load_config().get("stt")
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _section(stt_config: Dict[str, Any], name: str) -> Dict[str, Any]:
    section = (stt_config or {}).get(name)
    return section if isinstance(section, dict) else {}


def _settings(stt_config: Dict[str, Any]) -> tuple[str, str]:
    """``(ha_url, pipeline_id)`` — empty pipeline means "resolve the server's
    preferred pipeline at runtime"."""
    cfg = _section(stt_config, NAME)
    url = (
        str(cfg.get("url") or "").strip()
        or _get_secret(URL_ENV, DEFAULT_HA_URL).strip()
        or DEFAULT_HA_URL
    )
    pipeline = str(cfg.get("pipeline") or cfg.get("model") or "").strip()
    return url.rstrip("/"), pipeline


def _reachable(url: str, timeout: float = _REACHABLE_TIMEOUT_SECONDS) -> bool:
    """True when *url* answers any HTTP status (even 401 — that still proves HA is up)."""
    try:
        from urllib.error import HTTPError, URLError
        from urllib.request import Request, urlopen
        try:
            with urlopen(Request(url.rstrip("/") + "/api/", method="GET"), timeout=timeout):
                return True
        except HTTPError:
            return True  # the server responded; auth state is irrelevant to reachability
        except (URLError, OSError, ValueError):
            return False
    except Exception:  # pragma: no cover — defensive; reachability must never raise
        return False


def check_availability(stt_config: Optional[Dict[str, Any]] = None) -> bool:
    """Full gate for this backend: a token is configured AND the HA instance answers.

    (The ABC's ``is_available()`` stays token-only on purpose — core calls it on
    every picker paint and forbids network I/O there. Use this helper, or just
    call :meth:`transcribe` and handle the error envelope, when reachability
    matters.)
    """
    if not _token():
        return False
    try:
        url, _ = _settings(stt_config if isinstance(stt_config, dict) else _load_stt_config())
    except Exception:
        return False
    return _reachable(url)


# ---- Audio normalization ----------------------------------------------------

def _find_ffmpeg() -> Optional[str]:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for directory in ("/usr/local/bin", "/opt/homebrew/bin"):
        candidate = Path(directory) / "ffmpeg"
        if candidate.is_file():
            return str(candidate)
    return None


def _normalize_to_16k_mono_pcm(file_path: str) -> bytes:
    """Decode *file_path* to 16 kHz mono signed-16-bit PCM bytes.

    ffmpeg handles every container phone uploads arrive in (wav/mp3/ogg/opus);
    without it, only an already-16k-mono-16-bit WAV can be passed through via
    the stdlib ``wave`` module. Raises :class:`HaCloudError` on failure.
    """
    name = Path(file_path).name
    ffmpeg = _find_ffmpeg()
    if ffmpeg:
        try:
            proc = subprocess.run(
                [ffmpeg, "-y", "-v", "error", "-i", file_path,
                 "-ac", "1", "-ar", str(SAMPLE_RATE),
                 "-f", "s16le", "-acodec", "pcm_s16le", "-"],
                check=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=120)
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or b"").decode("utf-8", errors="replace").strip() or str(exc)
            raise HaCloudError(f"ffmpeg could not decode {name} for HA STT: {detail}")
        except subprocess.TimeoutExpired:
            raise HaCloudError(f"ffmpeg decode timed out for {name}")
        if not proc.stdout:
            raise HaCloudError(f"ffmpeg produced no audio for {name}")
        return proc.stdout
    if Path(file_path).suffix.lower() != ".wav":
        raise HaCloudError(
            f"ffmpeg not found: HA STT needs ffmpeg to decode {name} "
            "(only 16 kHz mono 16-bit WAV works without it)")
    try:
        with wave.open(file_path, "rb") as wf:
            if (wf.getnchannels(), wf.getsampwidth(), wf.getframerate()) != (1, 2, SAMPLE_RATE):
                raise HaCloudError(
                    f"{name} is not 16 kHz mono 16-bit audio and ffmpeg is not "
                    "installed to resample it")
            return wf.readframes(wf.getnframes())
    except HaCloudError:
        raise
    except Exception as exc:
        raise HaCloudError(f"could not read {name} as WAV: {exc}")


# ---- Wire framing (pure; unit-tested without a network) ---------------------

def _frame_audio_chunk(handler_id: int, chunk: bytes) -> bytes:
    """One binary WS frame: handler byte + PCM payload."""
    return bytes([handler_id]) + chunk


def _termination_frame(handler_id: int) -> bytes:
    """End-of-stream marker: the bare handler byte (empty payload)."""
    return bytes([handler_id])


def _extract_stt_transcript(event_data: Dict[str, Any]) -> str:
    """Transcript from an ``stt-end`` event: ``data.stt_output.text`` (``data.stt_text`` legacy)."""
    data = event_data or {}
    output = data.get("stt_output")
    if isinstance(output, dict) and isinstance(output.get("text"), str):
        return output["text"]
    legacy = data.get("stt_text")
    return legacy if isinstance(legacy, str) else ""


def _extract_handler_id(event_data: Dict[str, Any]) -> int:
    """``stt_binary_handler_id`` from a ``run-start`` event; raises when absent/invalid."""
    data = event_data or {}
    handler = (data.get("runner_data") or {}).get("stt_binary_handler_id")
    if not isinstance(handler, int) or not 0 <= handler <= 255:
        raise HaCloudError(
            "Home Assistant run-start carried no stt_binary_handler_id for the audio stream")
    return handler


def _pick_preferred_pipeline(pipelines: Any) -> str:
    """Server's preferred pipeline id (first ``preferred`` entry, else first id, else "")."""
    if not isinstance(pipelines, list):
        return ""
    fallback = ""
    for entry in pipelines:
        if not isinstance(entry, dict):
            continue
        pid = entry.get("id")
        if not isinstance(pid, str) or not pid:
            continue
        if not fallback:
            fallback = pid
        if entry.get("preferred") is True:
            return pid
    return fallback


def _ha_ws_url(http_url: str) -> str:
    """``http(s)://host:port`` -> ``ws(s)://host:port/api/websocket``; raises on garbage."""
    from urllib.parse import urlparse
    parts = urlparse(http_url)
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme.lower())
    if not scheme or not parts.hostname:
        raise HaCloudError(f"invalid Home Assistant URL: {http_url!r}")
    return f"{scheme}://{parts.hostname}{f':{parts.port}' if parts.port else ''}/api/websocket"


# ---- Assist pipeline exchange -------------------------------------------------

async def _resolve_preferred_pipeline(ws: Any, *, msg_id: int, time_left) -> str:
    """Ask the server for its pipeline list; return the preferred pipeline id."""
    await ws.send(json.dumps({"id": msg_id, "type": "assist_pipeline/pipeline/list"}))
    while True:
        raw = await asyncio.wait_for(ws.recv(), time_left())
        if isinstance(raw, bytes):
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(msg, dict) or msg.get("id") != msg_id:
            continue
        if msg.get("type") != "result":
            continue
        if not msg.get("success", True):
            raise HaCloudError(f"Home Assistant pipeline list failed: {msg.get('error')}")
        result = msg.get("result") or {}
        pipeline_id = _pick_preferred_pipeline(result.get("pipelines"))
        if not pipeline_id:
            raise HaCloudError("Home Assistant has no Assist pipelines (add one under Settings > Voice assistants)")
        return pipeline_id


async def _stt_exchange(
    pcm: bytes, ws_url: str, token: str, pipeline: str,
    *, msg_id: int = _WS_RUN_ID, timeout: float = _WS_TIMEOUT_SECONDS,
) -> str:
    """Run one STT-only assist pipeline and return the transcript ("" when none).

    An empty *pipeline* resolves the server's preferred pipeline first. The
    language always follows the pipeline — no language override is sent.
    """
    try:
        import websockets
    except ImportError:
        raise HaCloudError("websockets package not installed (HA STT needs it)")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    def _time_left() -> float:
        return max(deadline - loop.time(), 0.1)

    try:
        async with websockets.connect(ws_url, max_size=4 * 1024 * 1024) as ws:
            await asyncio.wait_for(ws.recv(), _time_left())  # auth_required greeting
            await ws.send(json.dumps({"type": "auth", "access_token": token}))
            auth = json.loads(await asyncio.wait_for(ws.recv(), _time_left()))
            if auth.get("type") != "auth_ok":
                raise HaCloudError(
                    f"Home Assistant auth failed ({auth.get('type')}: "
                    f"{auth.get('message') or 'check HASS_TOKEN'})")
            if not pipeline:
                pipeline = await _resolve_preferred_pipeline(ws, msg_id=msg_id, time_left=_time_left)
                run_id = msg_id + 1  # ids must increase (HA answers id_reuse otherwise)
            else:
                run_id = msg_id
            await ws.send(json.dumps({
                "id": run_id, "type": "assist_pipeline/run",
                "start_stage": "stt", "end_stage": "stt",
                "input": {"sample_rate": SAMPLE_RATE},
                "pipeline": pipeline,
            }))
            handler: Optional[int] = None
            sent = False
            while True:
                raw = await asyncio.wait_for(ws.recv(), _time_left())
                if isinstance(raw, bytes):
                    continue  # no binary traffic is expected server -> client here
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(msg, dict) or msg.get("id") != run_id:
                    continue
                if msg.get("type") == "result" and not msg.get("success", True):
                    raise HaCloudError(
                        f"Home Assistant pipeline error: {msg.get('error')}")
                event = msg.get("event") or {}
                etype = event.get("type")
                data = event.get("data") or {}
                if etype == "run-start" and handler is None:
                    handler = _extract_handler_id(data)
                elif etype == "stt-start" and not sent:
                    if handler is None:
                        raise HaCloudError(
                            "Home Assistant sent stt-start before run-start (no handler id)")
                    for i in range(0, len(pcm), _WS_CHUNK_BYTES):
                        await ws.send(_frame_audio_chunk(handler, pcm[i:i + _WS_CHUNK_BYTES]))
                    await ws.send(_termination_frame(handler))  # bare handler byte ends the stream
                    sent = True
                elif etype == "stt-end":
                    return _extract_stt_transcript(data)
                elif etype == "error":
                    raise HaCloudError(f"Home Assistant STT error: {data}")
                elif etype == "run-end":
                    raise HaCloudError("Home Assistant pipeline run ended with no transcript")
    except HaCloudError:
        raise
    except asyncio.TimeoutError:
        raise HaCloudError("Home Assistant STT timed out waiting for the pipeline")
    except Exception as exc:
        raise HaCloudError(f"Home Assistant STT websocket failed: {exc}")


def _run_ha_stt(pcm: bytes, url: str, token: str, pipeline: str) -> str:
    """Synchronous wrapper for :func:`_stt_exchange` (isolated loop when nested)."""
    ws_url = _ha_ws_url(url)
    try:
        return asyncio.run(_stt_exchange(pcm, ws_url, token, pipeline))
    except RuntimeError:
        # Already inside a running loop (embedded gateway thread): isolate ours in a worker.
        outcome: Dict[str, Any] = {}

        def _worker() -> None:
            try:
                outcome["result"] = asyncio.run(_stt_exchange(pcm, ws_url, token, pipeline))
            except Exception as exc:  # propagate verbatim to the caller below
                outcome["error"] = exc

        thread = threading.Thread(target=_worker, name="ha-cloud-stt", daemon=True)
        thread.start()
        thread.join(_WS_TIMEOUT_SECONDS + 30)
        if "result" in outcome:
            return outcome["result"]
        if "error" in outcome:
            raise outcome["error"]
        raise HaCloudError("Home Assistant STT worker timed out")


# ---- Envelopes ------------------------------------------------------------------

def _ok_result(transcript: str, provider: str = NAME) -> Dict[str, Any]:
    return {"success": True, "transcript": transcript, "provider": provider}


def _error_result(error: str, provider: str = NAME, **extra: Any) -> Dict[str, Any]:
    """Standard failure envelope shared by every path below (never raises)."""
    return {"success": False, "transcript": "", "error": error, "provider": provider, **extra}


# ---- Provider ---------------------------------------------------------------------

class HaCloudSttProvider(TranscriptionProvider):  # type: ignore[valid-type,misc]
    """Speech-to-text via a Home Assistant Assist pipeline (``stt.provider: ha_cloud``).

    Config (``stt.ha_cloud`` in config.yaml): ``url`` (default
    ``http://homeassistant.local:8123``, or the shared ``HASS_URL`` secret) and
    ``pipeline`` (an Assist pipeline id; empty resolves the server's preferred
    pipeline at runtime). Credentials: ``HASS_TOKEN`` via the standard secret
    flow. Language always follows the pipeline — a ``language`` hint is
    accepted and ignored.
    """

    @property
    def name(self) -> str:
        return NAME

    @property
    def display_name(self) -> str:
        return "HA Cloud"

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "HA",
            "tag": "Home Assistant Assist STT",
            "env_vars": [{
                "key": TOKEN_ENV,
                "prompt": "Home Assistant long-lived access token "
                          "(HA profile > Security > Long-lived access tokens)",
                "url": "http://homeassistant.local:8123/profile/security",
            }],
        }

    def is_available(self) -> bool:
        """Token-only by design: core calls this on every picker paint and forbids
        network I/O here. Reachability is enforced per call in :meth:`transcribe`."""
        try:
            return bool(_token())
        except Exception:
            return False

    def list_models(self) -> List[Dict[str, Any]]:
        return []

    def default_model(self) -> Optional[str]:
        return None

    def transcribe(
        self, file_path: str, *, model: Optional[str] = None,
        language: Optional[str] = None, **extra: Any,
    ) -> Dict[str, Any]:
        """Transcribe via Home Assistant STT. ``model`` carries a pipeline-id
        override; every failure returns the error envelope so the caller can
        fall back to local — nothing raises out of here."""
        if extra.get("prompt") or language:
            logger.debug("STT provider 'ha_cloud' follows the pipeline for language/prompts "
                         "— proceeding without them.")
        stt_config = _load_stt_config()
        url, configured_pipeline = _settings(stt_config if isinstance(stt_config, dict) else {})
        # The dispatcher already resolves ``model`` from ``stt.ha_cloud.model``;
        # an explicit ``stt.ha_cloud.pipeline`` and any caller override win here.
        pipeline = (model or "").strip() or configured_pipeline
        token = _token()
        if not token:
            return _error_result(
                "HASS_TOKEN not set: HA STT needs a Home Assistant long-lived "
                "access token (create one under your HA profile > Security, then "
                "store it via `hermes auth` or the environment)",
                no_speech=False)
        if not os.path.exists(file_path):
            return _error_result(f"Audio file not found: {file_path}")
        try:
            pcm = _normalize_to_16k_mono_pcm(file_path)
        except HaCloudError as exc:
            return _error_result(str(exc))
        except Exception as exc:  # noqa: BLE001 — normalization must never raise out
            logger.error("HA STT audio prep failed for %s: %s", file_path, exc, exc_info=True)
            return _error_result(f"HA STT audio prep failed: {exc}")
        try:
            transcript = _run_ha_stt(pcm, url, token, pipeline)
        except HaCloudError as exc:
            return _error_result(str(exc))
        except Exception as exc:  # noqa: BLE001 — transport failures are envelope errors
            logger.error("HA STT transcription failed for %s: %s", file_path, exc, exc_info=True)
            return _error_result(f"HA STT transcription failed: {exc}")
        if not transcript:
            return _error_result("HA STT returned an empty transcript", no_speech=True)
        logger.info("Transcribed %s via HA STT (pipeline %s, %d chars)",
                    Path(file_path).name, pipeline or "<server preferred>", len(transcript))
        return _ok_result(transcript)
