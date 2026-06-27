# Step 3 — Orchestrator extraction + telephony split + health/status endpoints

**Status:** ✅ done
**Risk to working pipeline:** medium (route + orchestration moves across multiple files)
**Time:** ~3 hours

## Why

Pre-Step-3, `run_server.py` was ~250 lines doing four things at once:
FastAPI app bootstrap, route handlers, per-call orchestration (the
`twilio_handler` body), and the `WebSocketAdapter` shim. That's too many
concerns in one file.

Step 3 splits these along their natural axes:

- **Per-call orchestration** → its own class in its own module
  (`orchestrator/manager.py`). The class owns queues, the barge-in event,
  the transcript logger, and the call context.
- **HTTP/WS routing** → `telephony/server.py`. Includes the FastAPI app,
  the `WebSocketAdapter`, and all four routes.
- **Construction** → `orchestrator/factory.py`. Today a thin wrapper, but
  this is where pool acquisition (Step 6) and feature-flag gating (Step
  9) will land.
- **Entry point** → `run_server.py` shrinks to ~30 lines: import config,
  import `app` from `telephony.server`, run uvicorn.

Step 3 also folded in two improvements that were natural to ship together:

1. **`/healthz` + `/api/call-status`** — two of the simplest endpoints
   from the company's `telephony/server.py`. Liveness probe + Twilio
   call-lifecycle webhook.
2. **The gather-pattern fix** for auto-hangup (see "Auto-hangup fix"
   section below). The refactor touched the gather pattern anyway, so
   shipping the fix in the same step kept the diff coherent.

## File reshuffle

| File | Before | After |
|---|---|---|
| `run_server.py` | ~250 lines: app + routes + orchestration + adapter | **~30 lines: imports + uvicorn boot** |
| `telephony/server.py` | commented-out company code (43 KB) | **~150 lines: FastAPI app + 4 routes + WebSocketAdapter** |
| `orchestrator/manager.py` | commented-out company code (164 KB) | **~170 lines: `VoiceOrchestrator` class** |
| `orchestrator/factory.py` | commented-out company code (5.8 KB) | **~20 lines: `create_default_orchestrator`** |

The commented-out company versions of other files in `orchestrator/`
(`brain.py`, `context_extractor.py`, `interfaces.py`, `mocks.py`,
`session.py`, `session_manager.py`, `session_timer_manager.py`) and
`telephony/concurrency.py` stay commented out — they'll be replaced when
their corresponding step lands (e.g. `brain.py` in Step 8, `concurrency.py`
in Step 9).

## What each new module does

### `orchestrator/manager.py` — `VoiceOrchestrator`

Per-call lifecycle coordinator. One instance per inbound call.

State held by the orchestrator:
- `audio_queue` — Twilio → STT
- `transcript_queue` — STT → LLM
- `text_queue` — LLM → TTS
- `streamsid_queue` — captures the streamSid from Twilio's `start` event
- `barge_in_event` — shared interrupt between STT and TTS
- `logger` — `TranscriptLogger` instance (created after streamSid arrives)
- `context` — `CallContext` Pydantic model (from Step 1)

Two methods:
- `_twilio_receiver(twilio_ws)` — reads Twilio's media-stream events
  (unchanged from the original `twilio_receiver`)
- `handle_audio_stream(twilio_ws)` — drives the full call lifecycle

### `orchestrator/factory.py` — `create_default_orchestrator()`

```python
async def create_default_orchestrator() -> VoiceOrchestrator:
    return VoiceOrchestrator()
```

A one-liner today. Will grow when Step 6 (connection pooling) adds
`stt_pool.acquire()` and `tts_pool.acquire()` calls before construction.

### `telephony/server.py` — FastAPI app

Routes (4 total):

| Route | Method(s) | Purpose |
|---|---|---|
| `/voice` | GET, POST | Returns TwiML telling Twilio to open the Media Stream WS, then `<Hangup/>` so call ends cleanly when our WS closes |
| `/` | WS | Twilio Media Stream lands here. Spawns a fresh `VoiceOrchestrator` per call. |
| `/healthz` | GET | Liveness probe — returns `{"status":"ok","service":"cila-voice-agent"}` |
| `/api/call-status` | POST | Twilio status callback. Logs `CallSid`, `CallStatus`, `CallDuration`. Hook for Step 9's concurrency-cap release. |

### `run_server.py` — thin entry

```python
import asyncio
import uvicorn
import config
from telephony.server import app

async def main() -> None:
    print("[MAIN] Starting modular voice pipeline server on port 5000")
    print(f"       STT -> Deepgram {config.DEEPGRAM_MODEL}")
    print(f"       LLM -> Groq     {config.GROQ_MODEL}")
    print(f"       TTS -> ElevenLabs {config.ELEVENLABS_MODEL_ID}")

    cfg = uvicorn.Config(app, host="127.0.0.1", port=5000,
                         log_level="warning", access_log=False)
    server = uvicorn.Server(cfg)
    print("[MAIN] Waiting for Twilio calls...")
    await server.serve()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[MAIN] Shutting down")
```

## Auto-hangup fix (gather pattern)

A latent bug existed pre-Step-3 in `twilio_handler`:

```python
# OLD (broken)
await asyncio.gather(
    receiver_task, stt_task, llm_task, tts_task,
    return_exceptions=True,
)
# ... then close twilio_ws in finally
```

The deadlock: `receiver_task` only exits when Twilio sends a `stop` event.
Twilio only sends `stop` when the call ends. So when STT detected a
hangup phrase and pushed `None` into transcript_queue, the LLM and TTS
finished their farewell, but `gather()` kept waiting on `receiver_task`
forever. `twilio_ws.close()` never ran, so Twilio never hit the
`<Hangup/>` verb in the TwiML, so the call never auto-terminated. The
caller had to hang up manually.

The fix:

```python
# NEW (fixed) — in VoiceOrchestrator.handle_audio_stream
try:
    await asyncio.gather(llm_task, tts_task, return_exceptions=True)
finally:
    # Close WS first → Twilio's TwiML continues to <Hangup/> → call ends
    try:
        await twilio_ws.close()
    except Exception:
        pass

    # Now cancel the "input" tasks. STT will see audio_queue close;
    # receiver will exit on the WS close.
    for q in (audio_queue, transcript_queue, text_queue):
        try: q.put_nowait(None)
        except Exception: pass
    for t in (stt_task, receiver_task):
        if not t.done(): t.cancel()
    await asyncio.gather(stt_task, receiver_task, return_exceptions=True)
```

The insight: **STT and the receiver are "input" tasks** — they don't have
a natural end signal while the WS is open. They're designed to be
cancelled when the conversation is over. Only LLM and TTS are
"productive" — they finish when there's no more work. So we wait on the
productive set, close the WS to trigger Twilio's `<Hangup/>`, and cancel
the input set.

This fixes task #12 (deferred from the earlier observation).

**Caveat:** hangup-phrase matching itself (in `stt/deepgram_stt.py`) is
still narrow. `_is_hangup_phrase` uses `endswith`, so "Hang up the call,
please" or "Fine, thank you" don't trigger. Clear phrases like "bye",
"goodbye", "okay bye" do. Broadening the match is task #11 (deferred to
its own step).

## Dependencies added

| Package | Version | Purpose |
|---|---|---|
| `python-multipart` | 0.0.32 | Required by FastAPI to parse Twilio's form-encoded `/api/call-status` POST body. ~30 KB. |

## Optional Twilio configuration

`/api/call-status` only fires if the Twilio number is configured to send
status callbacks. Not required for the basic call flow. To enable:

```powershell
twilio api:core:incoming-phone-numbers:update \
  --sid PNe626f6d9628d06e85a8081058f1e9da5 \
  --status-callback=https://<your-ngrok>/api/call-status \
  --status-callback-method=POST
```

When configured, every call-end produces a `[STATUS]` log line in
Terminal A.

## Smoke-test results

| Test | Result |
|---|---|
| `GET /voice` | 200 + TwiML with `<Hangup/>` after `<Connect>`. `Server: uvicorn` |
| `POST /voice` | 200 + TwiML |
| `GET /healthz` | 200 + `{"status":"ok","service":"cila-voice-agent"}` |
| `POST /api/call-status` (form-encoded) | 200 + `{"received":true}` |
| All four modules import clean | ✅ |
| `run_server.app is telephony.server.app` | True (correctly shared) |
| Auto-hangup on live call after "bye" | (pending user verification) |

## What this unlocks

- **Step 4 (governance)** can hook into `VoiceOrchestrator` cleanly:
  guardrails run inside `handle_audio_stream()` before STT events reach
  the LLM. The class structure makes this straightforward; would have
  been a tangle inside `run_server.py`.
- **Step 6 (connection pooling)** has a home in `factory.py`.
- **Step 9 (concurrency cap)** has a home in `telephony/concurrency.py`
  (the commented-out file is ready to be unpacked) and plugs into
  `/voice` via FastAPI middleware or dependency injection.
- **Step 10 (tests)** can mock `VoiceOrchestrator` cleanly — it's a real
  class with a single entry point, easy to fake out.

## New log prefixes (cosmetic)

Pre-Step-3 logs used `[MAIN]` for the per-call lifecycle events. Post-step-3:
- `[MAIN]` — only for the uvicorn startup/shutdown messages in `run_server.py`
- `[WS]` — WebSocket accept/disconnect events from `telephony/server.py`
- `[ORCH]` — per-call lifecycle inside `VoiceOrchestrator`
- `[TWILIO]` — stream events from the receiver
- `[TWIML]` — `/voice` endpoint hit
- `[STATUS]` — `/api/call-status` events (when configured)

If you've gotten used to grepping `[MAIN]` for "is the call going OK",
switch to `[ORCH]`.
