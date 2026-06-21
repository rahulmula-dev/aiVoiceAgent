"""
STT Module: Deepgram Nova-3 (streaming WebSocket — listen-only, NOT the agent endpoint)

To swap provider:
  1. Create stt/<new_provider>_stt.py
  2. Implement: async def run_stt(audio_queue, transcript_queue, barge_in_event)
  3. Update the import in main.py — nothing else changes.

Contract:
  - Reads raw mulaw bytes from `audio_queue`
  - Pushes final transcript strings into `transcript_queue`
  - Sets `barge_in_event` when VAD detects user starting to speak
  - Stops cleanly when `audio_queue` yields None (sentinel)

Finalization strategy (per Deepgram's official guidance):
  - Buffer text from every `is_final: true` Results event.
  - Flush to LLM on `speech_final: true`  → endpointer detected end of speech.
  - Flush to LLM on `UtteranceEnd` event   → word-timing-based silence backstop.
  Reference: https://developers.deepgram.com/docs/understanding-end-of-speech-detection
"""

import asyncio
import json
import time

import websockets

import config


# Terminal phrases — if the caller's finalized utterance ENDS with any of these,
# we treat it as a hangup signal. The LLM still gets the text (so it can say a
# brief farewell), and immediately after we push a None sentinel to
# transcript_queue. That causes the LLM loop to exit after one more turn, TTS
# drains the goodbye, and main.py closes the Twilio websocket — call ends.
_HANGUP_PHRASES = (
    " goodbye", " good bye", " bye", " bye bye",
    " that's all", " that is all", " thats all",
    " thanks bye", " thank you bye",
    " thanks goodbye", " thank you goodbye",
    " okay bye", " ok bye", " alright bye",
    " hang up", " end the call", " end call",
    " i'm done", " i am done",
)


def _dominant_language(lang_tags: list[str]) -> str | None:
    """
    Given the per-word language tags Deepgram emits in multi mode, return the
    single dominant language code, or None if there's nothing usable.

    Deepgram tags codes like "en", "hi", "es". We normalise region variants
    ("en-US" -> "en") and return the most frequent. Used by the governance
    layer to decide whether the caller spoke English.
    """
    if not lang_tags:
        return None
    counts: dict[str, int] = {}
    for tag in lang_tags:
        norm = tag.split("-")[0].lower() if tag else ""
        if norm:
            counts[norm] = counts.get(norm, 0) + 1
    if not counts:
        return None
    return max(counts, key=counts.get)


def _is_hangup_phrase(text: str) -> bool:
    """Return True if `text` ends with a decisive 'I'm done' phrase."""
    if not text:
        return False
    # Leading space + strip trailing punctuation/whitespace so " goodbye"
    # matches both "Goodbye." and "Okay, goodbye!".
    t = " " + text.lower().rstrip(" .!?,;:")
    return any(t.endswith(p) for p in _HANGUP_PHRASES)

# Deepgram STT-only WebSocket (separate from the agent endpoint).
# Both endpointing (acoustic) AND utterance_end_ms (word-timing) — whichever
# fires first finalizes the turn. Required reading:
#   https://developers.deepgram.com/docs/endpointing
#   https://developers.deepgram.com/docs/utterance-end
_STT_URL = (
    "wss://api.deepgram.com/v1/listen"
    f"?model={config.DEEPGRAM_MODEL}"
    f"&encoding={config.AUDIO_ENCODING}"
    f"&sample_rate={config.SAMPLE_RATE}"
    "&channels=1"
    "&punctuate=true"
    "&interim_results=true"   # required for utterance_end_ms
    "&vad_events=true"        # gives us SpeechStarted for barge-in detection
    "&endpointing=300"        # 300 ms silence → mark utterance as speech_final
    "&utterance_end_ms=1000"  # backstop: 1 s of word-timing silence → UtteranceEnd
    "&no_delay=true"          # commit words faster; reduces mid-utterance retraction
                              #   (e.g., "stomach upset" losing "upset" on phone audio)
    "&language=multi"         # nova-3 multilingual: transcribe foreign speech AND tag
                              #   each word with a language code. The governance layer
                              #   reads these tags to refuse non-English callers.
                              #   (detect_language=true is REJECTED on nova-3 live — 400;
                              #    language=multi is the supported path.)
)

# Send {"type":"KeepAlive"} as a text frame this often, when no audio has been
# forwarded recently. Deepgram closes the socket after 10 s of no data with
# NET-0001 — 3 s gives us comfortable margin.
#   https://developers.deepgram.com/docs/audio-keep-alive
_KEEPALIVE_INTERVAL_S = 3.0

# Ignore SpeechStarted events that arrive within this window of the previous one
# — Deepgram's VAD fires once per silence→speech transition, including breaths
# and lip closures inside a single utterance.
_BARGE_IN_DEBOUNCE_S = 0.5

# Suppress barge-in for this long after pushing a finalized transcript. Without
# this, Deepgram's tail-end interim transcripts (or speaker-echo mis-transcribed
# as user speech) can cancel the very first sentence of the agent's reply —
# fatal for short governance refusals which have nothing to fall back on.
# 0.6 s is enough for the LLM + TTS first-audio to start playing while still
# letting the caller interrupt longer replies.
_POST_FINAL_COOLDOWN_S = 0.6


async def run_stt(
    audio_queue: asyncio.Queue,
    transcript_queue: asyncio.Queue,
    barge_in_event: asyncio.Event,
    logger=None,
    stt_pool=None,
) -> None:
    """
    Main STT coroutine. Connects to Deepgram, streams audio, returns transcripts.
    Runs for the lifetime of a single call.

    If `stt_pool` (DeepgramPool) is provided, acquires a pre-warmed connection
    instead of opening a fresh one — eliminates per-call TCP+TLS latency.
    If `logger` (TranscriptLogger) is provided, each FINALIZED user utterance
    is appended to the transcript file — one line per consolidated turn, never
    interims.
    """
    _connect_ctx = (
        stt_pool.acquire() if stt_pool is not None
        else websockets.connect(_STT_URL, subprotocols=["token", config.DEEPGRAM_API_KEY])
    )
    try:
        async with _connect_ctx as dg_ws:
            print("[STT] Connected to Deepgram STT (Nova-3)")

            # Shared mutable state between coroutines below.
            state = {
                "last_send": time.monotonic(),   # for keepalive watchdog
                "last_barge_in": 0.0,            # for barge-in debounce
                "barge_in_blocked_until": 0.0,   # post-final cooldown end timestamp
                "utterance_buffer": [],          # is_final segments awaiting flush
                "utterance_langs": [],           # per-word language tags (multi mode)
            }

            async def _sender():
                """Pull audio from the queue and forward it to Deepgram."""
                while True:
                    chunk = await audio_queue.get()
                    if chunk is None:          # sentinel → end of call
                        try:
                            await dg_ws.send(json.dumps({"type": "CloseStream"}))
                        except Exception:
                            pass
                        return
                    try:
                        await dg_ws.send(chunk)
                        state["last_send"] = time.monotonic()
                    except Exception as e:
                        print(f"[STT] sender error: {e}")
                        return

            async def _keepalive():
                """
                Send a KeepAlive text frame whenever audio has been idle ≥3 s.
                Deepgram closes the socket after 10 s without any frame.
                """
                while True:
                    await asyncio.sleep(1.0)
                    idle = time.monotonic() - state["last_send"]
                    if idle >= _KEEPALIVE_INTERVAL_S:
                        try:
                            await dg_ws.send(json.dumps({"type": "KeepAlive"}))
                            state["last_send"] = time.monotonic()
                        except Exception:
                            return

            async def _flush_utterance(reason: str) -> None:
                """Concatenate buffered is_final segments and push to the LLM."""
                if not state["utterance_buffer"]:
                    return
                text = " ".join(state["utterance_buffer"]).strip()
                # Compute the dominant language of this utterance from the
                # per-word tags Deepgram emits in multi mode, then reset.
                detected_lang = _dominant_language(state["utterance_langs"])
                state["utterance_buffer"] = []
                state["utterance_langs"] = []
                if text:
                    lang_note = f"  [lang={detected_lang}]" if detected_lang else ""
                    print(f"[STT] Final ({reason}): '{text}'{lang_note}")
                    # transcript_queue carries (text, detected_lang, turn_hint)
                    # tuples. turn_hint is None for normal turns and "final_turn"
                    # for the last utterance before hangup — the LLM uses this
                    # signal to keep its farewell to one short sentence.
                    is_hangup = _is_hangup_phrase(text)
                    turn_hint = "final_turn" if is_hangup else None

                    # When the caller signals hangup, drain any earlier
                    # transcripts that the LLM hasn't picked up yet. They
                    # are stale — the caller has decided to leave. Without
                    # this drain, in fast conversations the agent processes
                    # a backlog of buffered questions AFTER the goodbye,
                    # producing a long tail of unwanted responses before
                    # honoring the hangup. We can't cancel an in-flight LLM
                    # turn (barge-in handles that separately), but we can at
                    # least prevent the queue from feeding it more work.
                    if is_hangup:
                        drained = 0
                        while not transcript_queue.empty():
                            try:
                                transcript_queue.get_nowait()
                                drained += 1
                            except asyncio.QueueEmpty:
                                break
                        if drained:
                            print(f"[STT] Drained {drained} stale transcript(s) before hangup")

                    await transcript_queue.put((text, detected_lang, turn_hint))
                    # Clear any stale barge-in flag set by this same utterance's
                    # interim transcripts. Without this, the LLM's first sentence
                    # (or a short governance refusal) can be dropped by TTS's
                    # next-token barge-in check before it ever reaches the speaker.
                    barge_in_event.clear()
                    # Block fresh barge-ins for the next _POST_FINAL_COOLDOWN_S so
                    # tail-end interims and speaker-echo can't kill the start of
                    # the agent's reply. A genuine new utterance after the cooldown
                    # window will still barge in normally.
                    state["barge_in_blocked_until"] = time.monotonic() + _POST_FINAL_COOLDOWN_S
                    if logger is not None:
                        logger.log_user(text)
                        logger.mark_user_finalized()
                    if is_hangup:
                        # Let the LLM say its farewell on this turn, then exit.
                        print("[STT] 👋 Hangup phrase detected — call will end after farewell")
                        await transcript_queue.put(None)

            async def _receiver():
                """Listen to Deepgram events and dispatch accordingly."""
                async for raw in dg_ws:
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    msg_type = data.get("type", "")

                    # ── VAD event — diagnostic only ──────────────────────────
                    # Deepgram's SpeechStarted fires on raw acoustic energy and
                    # triggers on fan noise, AC hum, breathing, etc. We DO NOT
                    # barge in here — barge-in is gated on actually-recognized
                    # words below (under "Transcript result"). We also do NOT
                    # treat empty-transcript speech as a foreign-language strike
                    # — false positives on PSTN noise made that approach
                    # unworkable. Strikes are now only fired when Deepgram
                    # returns a transcript and the language layer classifies
                    # the text or per-word language tag as non-English.
                    if msg_type == "SpeechStarted":
                        pass

                    # ── Transcript result ────────────────────────────────────
                    elif msg_type == "Results":
                        channel      = data.get("channel", {})
                        alternatives = channel.get("alternatives", [{}])
                        transcript   = alternatives[0].get("transcript", "").strip()
                        is_final     = data.get("is_final", False)
                        speech_final = data.get("speech_final", False)

                        if not transcript:
                            # Empty transcript — could be brief noise, breathing,
                            # or an unsupported language. Either way, no text means
                            # no signal to act on. Skip silently.
                            continue

                        # Barge-in is fired on the FIRST recognized word of a
                        # new utterance. Fan/AC noise produces SpeechStarted
                        # events but very rarely produces non-empty interim
                        # transcripts — so this filters >95% of false positives.
                        # Require ≥3 chars to also filter the occasional one-token
                        # hallucination from steady-state noise ("the", "uh").
                        # Also respect the post-final cooldown window so the agent
                        # can start its reply without being killed by stale interims.
                        now = time.monotonic()
                        if (len(transcript) >= 3 and
                                now - state["last_barge_in"] >= _BARGE_IN_DEBOUNCE_S and
                                now >= state["barge_in_blocked_until"]):
                            state["last_barge_in"] = now
                            print(f"[STT] Speech recognised -> barge-in ('{transcript[:30]}')")
                            barge_in_event.set()

                        if is_final:
                            # Commit this segment to the utterance buffer.
                            state["utterance_buffer"].append(transcript)
                            # Collect per-word language tags (multi mode) so the
                            # flush can compute a dominant language for governance.
                            for w in alternatives[0].get("words", []):
                                lang = w.get("language")
                                if lang:
                                    state["utterance_langs"].append(lang)
                            if speech_final:
                                # Endpointer detected end of speech — flush now.
                                await _flush_utterance("speech_final")
                            else:
                                print(f"[STT] Segment: '{transcript}'")
                        else:
                            print(f"[STT] Interim: '{transcript}'")

                    # ── UtteranceEnd: word-timing silence backstop ───────────
                    elif msg_type == "UtteranceEnd":
                        # Only flush if speech_final didn't already do it.
                        await _flush_utterance("UtteranceEnd")

                    # ── Metadata / ack — ignore silently ─────────────────────
                    elif msg_type in ("Metadata",):
                        pass

                    elif msg_type == "Error":
                        print(f"[STT] Deepgram error: {data}")

            sender_task    = asyncio.create_task(_sender())
            receiver_task  = asyncio.create_task(_receiver())
            keepalive_task = asyncio.create_task(_keepalive())

            # Wait for sender to finish (triggered by None sentinel from twilio_receiver).
            await sender_task

            # Give the receiver a short grace window to drain any in-flight
            # final Results / UtteranceEnd before cancelling — otherwise the
            # last user utterance can be lost when the call ends.
            try:
                await asyncio.wait_for(receiver_task, timeout=0.5)
            except asyncio.TimeoutError:
                receiver_task.cancel()
            except Exception:
                pass

            keepalive_task.cancel()

    except Exception as e:
        print(f"[STT] ❌ Connection to Deepgram failed: {type(e).__name__}: {e}")
        print("[STT] 💡 Check your DEEPGRAM_API_KEY in .env")

    finally:
        await transcript_queue.put(None)
        print("[STT] Disconnected from Deepgram STT")
