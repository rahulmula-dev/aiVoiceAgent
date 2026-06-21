"""
orchestrator/manager.py — VoiceOrchestrator

Per-call lifecycle coordinator. Extracted from run_server.py so that the
HTTP/WebSocket route handlers in telephony/server.py stay thin — they just
accept a connection and hand it to a VoiceOrchestrator instance.

Pipeline topology (per call):

  Twilio (inbound mulaw)
       |
       v  audio_queue
  +-------------------+
  |  STT              |  stt/deepgram_stt.py
  |  Deepgram Nova-3  |
  +---------+---------+
            | transcript_queue           also sets barge_in_event ---------------+
            v                                                                     |
  +-------------------+                                                           |
  |  LLM              |  llm/groq_llm.py
  |  Groq Llama 3.1   |
  +---------+---------+
            | text_queue
            v
  +-------------------+   <-- barge_in_event -------------------------------------+
  |  TTS              |  tts/elevenlabs_tts.py
  |  ElevenLabs       |
  +---------+---------+
            | mulaw audio (base64-encoded media messages)
            v
  Twilio (outbound audio -> phone speaker)

End-of-call lifecycle (the key fix vs. the old run_server.py implementation):

  1. STT detects a hangup phrase -> puts None into transcript_queue (after
     the final user turn).
  2. LLM consumes the last turn, generates farewell, receives None, exits.
  3. TTS consumes the farewell, synthesizes it, receives None, exits.
  4. We gather() on **LLM + TTS only** — not on STT + receiver. Those two are
     "input" tasks that don't have a natural end signal while the WS is open;
     we cancel them after the productive tasks finish.
  5. After LLM + TTS exit, we explicitly close `twilio_ws`. Twilio's TwiML
     execution then continues to the `<Hangup/>` verb after `<Connect>` and
     terminates the phone call.
  6. STT and receiver are cancelled; both clean up (Deepgram WS closes via
     its async-context-manager; audio_queue gets a None sentinel for safety).

The old pattern gathered on all four tasks, but the receiver only exited when
Twilio sent `stop` — which Twilio only sent when the call ended. So
`twilio_ws.close()` never ran and `<Hangup/>` never fired.
"""

import asyncio
import base64
import json
import time
import traceback

import config
from stt.deepgram_stt import run_stt
# LLM provider selection — match config.LLM_PROVIDER to the right module.
# Both implementations expose the same run_llm() signature so this stays a
# one-line swap. Default is Groq for backwards compatibility.
if config.LLM_PROVIDER == "gemini":
    from llm.gemini_llm import run_llm
    print(f"[BOOT] LLM provider = Gemini ({config.GEMINI_MODEL})")
else:
    from llm.groq_llm import run_llm
    print(f"[BOOT] LLM provider = Groq ({config.GROQ_MODEL})")
from tts.elevenlabs_tts import run_tts, synthesize_and_stream
from logs.transcript_logger import TranscriptLogger
from agent_logging.call_logger import CallLogger
from agent_logging.voice_logger import mask_phone
from contracts.schemas import CallContext


class VoiceOrchestrator:
    """
    Coordinates one inbound call. Create a fresh instance per call (via
    `orchestrator.factory.create_default_orchestrator`), then call
    ``handle_audio_stream(twilio_ws)``.
    """

    def __init__(self, pools=None) -> None:
        # pools: ConnectionPools | None — injected by factory when available.
        # When None, STT + TTS open fresh connections per call (Step 5 behaviour).
        self._pools = pools
        # ── Queues — the only coupling between pipeline stages ──────────
        self.audio_queue:      asyncio.Queue = asyncio.Queue()  # mulaw bytes: Twilio -> STT
        self.transcript_queue: asyncio.Queue = asyncio.Queue()  # str:         STT    -> LLM
        self.text_queue:       asyncio.Queue = asyncio.Queue()  # str:         LLM    -> TTS
        self.streamsid_queue:  asyncio.Queue = asyncio.Queue()  # streamSid from Twilio start

        # ── Shared interrupt signal ─────────────────────────────────────
        # STT sets this when VAD detects user starting to speak mid-response.
        # TTS reads this to cancel in-flight synthesis + clear Twilio's buffer.
        self.barge_in_event: asyncio.Event = asyncio.Event()

        # Populated after Twilio sends the 'start' event
        # CallLogger is a TranscriptLogger subclass — typed as the parent so
        # callers see the same API surface.
        self.logger:  TranscriptLogger | None = None
        self.context: CallContext | None = None

        # ── End-of-call audio drain ─────────────────────────────────────
        # Twilio buffers a few seconds of audio for jitter smoothing. If we
        # close the WS the moment TTS finishes shipping bytes, the tail of
        # the goodbye / strike-3 refusal gets dropped. Twilio echoes back
        # any 'mark' message AFTER it has played all preceding audio, so we
        # use that as a drain signal before closing.
        self._final_mark_name = "final_drain"
        self._final_mark_event: asyncio.Event = asyncio.Event()

    # ─────────────────────────────────────────────────────────────────────
    # Twilio WebSocket receiver
    # ─────────────────────────────────────────────────────────────────────

    async def _twilio_receiver(self, twilio_ws) -> None:
        """
        Read Twilio media-stream JSON events.

        - Extracts streamSid from the 'start' event into streamsid_queue.
        - Forwards each inbound mulaw frame (~160 B / 20 ms) directly to
          audio_queue so Deepgram's endpointer sees silence in isolation
          (size-based batching would hide pauses inside 400 ms chunks).
        - Guarantees a None sentinel into audio_queue on exit so the
          downstream STT sender never deadlocks on a parse error.
        """
        try:
            async for message in twilio_ws:
                try:
                    data = json.loads(message)
                except json.JSONDecodeError as e:
                    print(f"[TWILIO] JSON parse error: {e}")
                    continue

                event = data.get("event", "")

                if event == "start":
                    streamsid = data["start"]["streamSid"]
                    self.streamsid_queue.put_nowait(streamsid)
                    print(f"[TWILIO] Stream started  SID={streamsid}")

                elif event == "media":
                    media = data["media"]
                    if media.get("track") == "inbound":
                        self.audio_queue.put_nowait(base64.b64decode(media["payload"]))

                elif event == "mark":
                    # Twilio echoes our 'mark' messages after the audio sent
                    # before each mark has been played. We use a named mark
                    # ("final_drain") as the end-of-call playback signal.
                    mark_name = data.get("mark", {}).get("name", "")
                    if mark_name == self._final_mark_name:
                        print("[TWILIO] Final mark received — playback drained")
                        self._final_mark_event.set()

                elif event == "stop":
                    print("[TWILIO] Stream stopped")
                    # Caller hung up — the WebSocket is closing. The end-of-call
                    # 'mark' drain has nothing to wait for (no buffered audio
                    # will ever play). Unblock it immediately so we don't sit
                    # in the 8 s timeout fallback.
                    self._final_mark_event.set()
                    break

        except Exception as e:
            print(f"[TWILIO] Receiver error: {type(e).__name__}: {e}")
        finally:
            # Always signal STT to shut down so it doesn't deadlock waiting
            # for audio after the call ends.
            await self.audio_queue.put(None)

    # ─────────────────────────────────────────────────────────────────────
    # Per-call main handler
    # ─────────────────────────────────────────────────────────────────────

    async def handle_audio_stream(self, twilio_ws) -> None:
        """
        Drive a single call from greeting through farewell.

        Sequence:
          1. Start the Twilio receiver — captures stream SID + feeds audio
          2. Wait for the 'start' event to learn the stream SID
          3. Create the per-call transcript logger and CallContext
          4. Play the greeting via TTS
          5. Launch STT, LLM, TTS as concurrent tasks
          6. Wait for LLM + TTS to finish (productive end-of-conversation tasks)
          7. Close the Twilio WS — Twilio then hits <Hangup/> in the TwiML
          8. Cancel STT + receiver, drain queues, write transcript, return
        """
        # ── (1) start the receiver so it can capture the stream SID ──────
        receiver_task = asyncio.create_task(self._twilio_receiver(twilio_ws))

        # ── (2) wait for Twilio's 'start' event ──────────────────────────
        print("[ORCH] Waiting for stream SID from Twilio...")
        streamsid = await self.streamsid_queue.get()

        # ── (3) per-call logger (crash-safe events + sealed summary) ─────
        # CallLogger is a drop-in subclass of TranscriptLogger — STT / LLM /
        # TTS callers see the same API. It additionally writes
        # logs/calls/<datetime>_<id>.events.jsonl (appended live) and
        # logs/calls/<datetime>_<id>.json (atomic summary on close).
        caller_raw = "unknown"  # TODO: extract from Twilio start event when CRM lands
        caller_masked = mask_phone(caller_raw) if caller_raw and caller_raw != "unknown" else "<unknown>"
        self.logger = CallLogger(call_id=streamsid, caller_number_masked=caller_masked)
        self.context = CallContext(
            session_id=streamsid,
            caller_number=caller_masked,   # always masked before storage
            start_time=time.time(),
        )

        # ── (3a) governance interceptor (only if the feature flag is on) ─
        # One LanguageGovernanceInterceptor instance per call carries the
        # 3-strike state. When GOVERNANCE_ENABLED is False this stays None
        # and run_llm behaves exactly as before (prompt-level guardrails only).
        interceptor = None
        if getattr(config, "GOVERNANCE_ENABLED", False):
            from contracts.language_interceptor import LanguageGovernanceInterceptor
            interceptor = LanguageGovernanceInterceptor(session_id=streamsid)
            print("[ORCH] Governance ENABLED — language gate + restricted-topic filter active")

        # ── (3b) RAG knowledge base (only if RAG_ENABLED and PG_DATABASE_URL set) ─
        # KnowledgeBase is instantiated per-server (not per-call) ideally, but
        # for Step 7 we create it per-call to keep the orchestrator self-contained.
        # The asyncpg pool inside KnowledgeBase is created lazily on first search.
        # When RAG_ENABLED is False (default), run_llm uses the inline SYSTEM_PROMPT
        # corpus unchanged — zero regression risk.
        kb = None
        if getattr(config, "RAG_ENABLED", False):
            try:
                from retrieval.vector_store import KnowledgeBase
                kb = KnowledgeBase()
                print("[ORCH] RAG ENABLED — KnowledgeBase (pgvector) active")
            except Exception as e:
                print(f"[ORCH] RAG init failed (falling back to inline corpus): {type(e).__name__}: {e}")

        # ── (4) play greeting before the pipeline starts listening ───────
        print("[ORCH] Playing greeting...")
        self.logger.log_bot(config.GREETING)
        _tts_client = self._pools.tts.client if self._pools is not None else None
        await synthesize_and_stream(config.GREETING, twilio_ws, streamsid, http_client=_tts_client)

        # ── (5) launch the three pipeline tasks ──────────────────────────
        print("[ORCH] Pipeline active — call in progress")
        _stt_pool = self._pools.stt if self._pools is not None else None
        _tts_pool = self._pools.tts if self._pools is not None else None

        stt_task = asyncio.create_task(
            run_stt(self.audio_queue, self.transcript_queue, self.barge_in_event, self.logger,
                    stt_pool=_stt_pool)
        )
        llm_task = asyncio.create_task(
            run_llm(self.transcript_queue, self.text_queue, self.logger, interceptor, kb=kb,
                    barge_in_event=self.barge_in_event)
        )
        tts_task = asyncio.create_task(
            run_tts(self.text_queue, twilio_ws, streamsid, self.barge_in_event, self.logger,
                    tts_pool=_tts_pool)
        )

        # ── (6) wait for LLM + TTS only — the productive tasks ───────────
        # STT and receiver are "input" tasks that don't have a natural end
        # signal while the WS is open; they're cancelled after the productive
        # tasks finish (e.g. after the hangup-phrase farewell).
        try:
            results = await asyncio.gather(llm_task, tts_task, return_exceptions=True)
            for name, r in zip(["llm", "tts"], results):
                if isinstance(r, Exception):
                    print(f"[ORCH] {name} task crashed: {type(r).__name__}: {r}")
        except Exception as e:
            print(f"[ORCH] Pipeline error: {type(e).__name__}: {e}")
            traceback.print_exc()
        finally:
            # ── (6b) wait for Twilio to finish playing the goodbye ───────
            # TTS shipped its last bytes seconds ago, but Twilio's jitter
            # buffer is still playing them. Send a 'mark' and wait for
            # Twilio to echo it back before closing — otherwise the tail
            # of the farewell / refusal is dropped mid-syllable.
            try:
                await twilio_ws.send(json.dumps({
                    "event": "mark",
                    "streamSid": streamsid,
                    "mark": {"name": self._final_mark_name},
                }))
                # 15 s covers the strike-3 refusal (~10 s of audio) plus
                # Twilio's jitter buffer. Normal goodbyes drain in 2-3 s and
                # fire the event immediately — the timeout is only a safety
                # net for the slow case.
                await asyncio.wait_for(self._final_mark_event.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                print("[ORCH] Final mark not received within 15s — closing anyway")
            except Exception as e:
                # Caller may have already hung up — that's fine, no audio to drain.
                print(f"[ORCH] Drain step skipped ({type(e).__name__}: {e})")

            # ── (7) close Twilio WS so <Hangup/> in the TwiML fires ──────
            try:
                await twilio_ws.close()
            except Exception:
                pass

            # ── (8) cancel STT + receiver and drain queues ───────────────
            # Push sentinels in case the tasks are blocked on a queue.get().
            for q in (self.audio_queue, self.transcript_queue, self.text_queue):
                try:
                    q.put_nowait(None)
                except Exception:
                    pass

            for t in (stt_task, receiver_task):
                if not t.done():
                    t.cancel()
            await asyncio.gather(stt_task, receiver_task, return_exceptions=True)

            if self.logger is not None:
                self.logger.close()
            print("[ORCH] Call ended — all tasks complete")
