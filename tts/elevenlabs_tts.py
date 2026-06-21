"""
TTS Module: ElevenLabs eleven_flash_v2_5 (HTTP streaming, ulaw_8000 output)

To swap provider:
  1. Create tts/<new_provider>_tts.py
  2. Implement: async def run_tts(text_queue, twilio_ws, streamsid, barge_in_event)
               async def synthesize_and_stream(text, twilio_ws, streamsid)
  3. Update the import in main.py — nothing else changes.

Contract (run_tts):
  - Reads text CHUNKS from `text_queue` (streaming tokens from LLM)
  - Buffers tokens into complete sentences before synthesizing (reduces API calls)
  - Flushes buffer when it receives '\n' (LLM end-of-turn signal)
  - Checks `barge_in_event` before each synthesis call:
      → clears audio buffer, sends Twilio "clear", discards queued text
  - Stops cleanly when `text_queue` yields None (sentinel)

Contract (synthesize_and_stream):
  - Accepts a complete text string
  - Sends it to ElevenLabs, streams raw ulaw_8000 audio chunks directly to Twilio
  - No audio format conversion needed — ElevenLabs outputs mulaw 8kHz natively
"""

import asyncio
import base64
import json
import httpx
import websockets

import config


class TwilioSocketClosed(Exception):
    """Raised internally when synthesize_and_stream notices the Twilio WS is gone."""

# ElevenLabs HTTP streaming endpoint (no SDK required)
_EL_URL = (
    f"https://api.elevenlabs.io/v1/text-to-speech"
    f"/{config.ELEVENLABS_VOICE_ID}/stream"
    f"?output_format={config.ELEVENLABS_OUTPUT_FORMAT}"
)

_EL_HEADERS = {
    "xi-api-key": config.ELEVENLABS_API_KEY,
    "Content-Type": "application/json",
}

# Sentence boundaries — synthesize as soon as we hit one for low latency.
# '!' and '?' always terminate. '.' is handled separately to avoid splitting
# on decimals ($5.99), abbreviations (Dr., Mr.), and ellipses.
_HARD_ENDINGS  = frozenset({'!', '?'})
_ABBREVIATIONS = frozenset({
    "mr", "mrs", "ms", "dr", "st", "jr", "sr",
    "vs", "etc", "e.g", "i.e", "no",
})


def _is_sentence_boundary(buf: str) -> bool:
    """
    Return True if `buf` ends at a real sentence boundary.

    Rules:
      - '!' or '?' at end                              → boundary
      - '.' at end, preceded by a digit                → NOT a boundary (decimal)
      - '.' at end, last word is a known abbreviation  → NOT a boundary
      - '.' at end, immediately followed by '.'        → NOT a boundary (ellipsis in progress)
      - '.' at end, otherwise                          → boundary
    """
    s = buf.rstrip()
    if not s:
        return False
    last = s[-1]
    if last in _HARD_ENDINGS:
        return True
    if last != '.':
        return False
    # Ellipsis still being typed — wait for it to finish
    if s.endswith('..'):
        return True   # treat any "..." sequence as terminal once it stops growing
    # Decimal: digit immediately before the dot
    if len(s) >= 2 and s[-2].isdigit():
        return False
    # Abbreviation: last word (without the dot) is in the abbrev list
    last_word = s[:-1].rsplit(None, 1)[-1].lower() if s[:-1].strip() else ""
    if last_word in _ABBREVIATIONS:
        return False
    return True


async def synthesize_and_stream(
    text: str,
    twilio_ws,
    streamsid: str,
    barge_in_event: asyncio.Event | None = None,
    logger=None,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    """
    POST text to ElevenLabs and stream the resulting mulaw audio to Twilio.
    Called for the greeting and for each completed sentence during a turn.

    If `http_client` (from ElevenLabsPool) is provided, the caller's shared
    AsyncClient is used and NOT closed on exit — connection keep-alive is
    preserved across calls. If None, a fresh client is created and closed.
    If barge_in_event is provided, streaming aborts as soon as it fires.
    If logger is provided, mark_tts_first_audio() fires the first time a chunk
    is sent — idempotent per turn (no-op for greeting since the turn marker is
    only set after a real user utterance finalizes).
    """
    if not text.strip():
        return

    payload = {
        "text": text,
        "model_id": config.ELEVENLABS_MODEL_ID,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.0,
            "use_speaker_boost": True,
        },
    }

    _owns_client = http_client is None
    http = http_client if http_client is not None else httpx.AsyncClient(timeout=30)
    try:
        async with http.stream("POST", _EL_URL, headers=_EL_HEADERS, json=payload) as resp:
            # Read the body INSIDE the stream context — once we exit it,
            # the response is closed and aread() can no longer recover it.
            if resp.status_code >= 400:
                raw = await resp.aread()
                try:
                    parsed = json.loads(raw)
                    detail = parsed.get("detail", parsed)
                except Exception:
                    detail = raw.decode("utf-8", errors="replace")
                print(f"[TTS] ElevenLabs HTTP error: {resp.status_code} — {detail}")
                if resp.status_code == 402:
                    print(
                        "[TTS] 💡 402 = quota/credits or Free-tier blocked voice. "
                        "If the `detail.status` above mentions a voice restriction, "
                        "the voice ID in config.py is from the shared Voice Library "
                        "and is not callable on Free tier. Run `python list_voices.py` "
                        "to pick a premade voice ID."
                    )
                return

            sent_any = False
            async for audio_chunk in resp.aiter_bytes(chunk_size=640):
                if barge_in_event is not None and barge_in_event.is_set():
                    print("[TTS] Barge-in mid-synthesis -> aborting stream")
                    return
                if audio_chunk:
                    media_msg = {
                        "event": "media",
                        "streamSid": streamsid,
                        "media": {
                            "payload": base64.b64encode(audio_chunk).decode("ascii")
                        },
                    }
                    try:
                        await twilio_ws.send(json.dumps(media_msg))
                    except websockets.ConnectionClosed as e:
                        print(f"[TTS] Twilio socket closed mid-stream ({e.code}); aborting.")
                        raise TwilioSocketClosed() from e
                    if not sent_any and logger is not None:
                        logger.mark_tts_first_audio()
                    sent_any = True
            if not sent_any:
                print("[TTS] ElevenLabs returned 0 audio bytes — caller heard silence.")
    except asyncio.CancelledError:
        raise
    except TwilioSocketClosed:
        raise
    except httpx.HTTPError as e:
        print(f"[TTS] httpx error during synthesis: {type(e).__name__}: {e}")
    except Exception as e:
        print(f"[TTS] Error during synthesis: {type(e).__name__}: {e}")
    finally:
        if _owns_client:
            await http.aclose()


async def run_tts(
    text_queue: asyncio.Queue,
    twilio_ws,
    streamsid: str,
    barge_in_event: asyncio.Event,
    logger=None,
    tts_pool=None,
) -> None:
    """
    Main TTS coroutine.  Buffers LLM tokens into sentences, synthesizes each
    sentence via ElevenLabs, and handles barge-in interruption.

    If `tts_pool` (ElevenLabsPool) is provided, its shared httpx.AsyncClient
    is used for all synthesis calls — avoids TCP+TLS setup on each sentence.
    """
    _http_client = tts_pool.client if tts_pool is not None else None
    text_buffer = ""

    async def _handle_barge_in() -> None:
        """Drop queued tokens, clear Twilio's audio buffer, reset the flag."""
        nonlocal text_buffer
        print("[TTS] Barge-in → clearing buffer and Twilio audio queue")
        text_buffer = ""
        while not text_queue.empty():
            try:
                text_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            await twilio_ws.send(json.dumps({"event": "clear", "streamSid": streamsid}))
        except Exception:
            pass
        barge_in_event.clear()

    async def _safe_synth(text: str) -> bool:
        """Synthesize; return False (and stop) if the Twilio socket has died."""
        try:
            await synthesize_and_stream(text, twilio_ws, streamsid, barge_in_event, logger, _http_client)
            return True
        except TwilioSocketClosed:
            return False

    try:
        while True:
            # ── Read next token (short timeout to catch lingering buffer) ────
            try:
                token = await asyncio.wait_for(text_queue.get(), timeout=0.15)
            except asyncio.TimeoutError:
                # Nothing new from LLM — flush any remaining text
                if text_buffer.strip():
                    if not barge_in_event.is_set():
                        print(f"[TTS] Timeout flush: '{text_buffer.strip()[:60]}'")
                        if not await _safe_synth(text_buffer.strip()):
                            return
                    text_buffer = ""
                if barge_in_event.is_set():
                    await _handle_barge_in()
                continue

            # ── Sentinel: end of call ────────────────────────────────────────
            if token is None:
                if text_buffer.strip() and not barge_in_event.is_set():
                    await _safe_synth(text_buffer.strip())
                print("[TTS] Shutting down")
                return

            # ── Barge-in: user spoke — discard everything in flight ──────────
            if barge_in_event.is_set():
                await _handle_barge_in()
                continue

            # ── '\n' is the LLM end-of-turn flush signal ─────────────────────
            if token == "\n":
                if text_buffer.strip():
                    print(f"[TTS] Turn flush: '{text_buffer.strip()[:60]}'")
                    if not await _safe_synth(text_buffer.strip()):
                        return
                text_buffer = ""
                continue

            # ── Accumulate token ─────────────────────────────────────────────
            text_buffer += token

            # ── Synthesize at sentence boundaries for low latency ────────────
            if _is_sentence_boundary(text_buffer):
                sentence = text_buffer.strip()
                print(f"[TTS] Sentence: '{sentence[:60]}...'" if len(sentence) > 60 else f"[TTS] Sentence: '{sentence}'")
                if not await _safe_synth(sentence):
                    return
                text_buffer = ""
    except asyncio.CancelledError:
        print("[TTS] Cancelled")
        raise
