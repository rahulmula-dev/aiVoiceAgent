# Step 6 — Connection pooling for STT + TTS

**Status:** ✅ done  
**Risk to working pipeline:** MEDIUM — touches STT/TTS connection lifecycle,
but all new parameters default to `None` (old behaviour preserved as fallback)  
**Time:** ~3 hours  

## Why

Before Step 6 every inbound call paid three sequential network round-trips before
the caller heard a single word:

| # | What happens | Typical latency |
|---|---|---|
| 1 | `websockets.connect()` to Deepgram (TCP + TLS + WS upgrade) | ~200 ms |
| 2 | `httpx.AsyncClient()` to ElevenLabs for the greeting (TCP + TLS) | ~150 ms |
| 3 | ElevenLabs returns first audio chunk | ~75 ms (eleven_flash_v2_5) |

Steps 1 and 2 can be pushed to server startup time — the caller experiences
them as **zero latency** because the connection is already open by the time the
phone rings.

## What was added

| File | Change |
|---|---|
| `utils/connection_pool.py` | `DeepgramPool`, `ElevenLabsPool`, `ConnectionPools` |
| `stt/deepgram_stt.py` | `stt_pool=None` param; uses `pool.acquire()` when provided |
| `tts/elevenlabs_tts.py` | `http_client=None` in `synthesize_and_stream`; `tts_pool=None` in `run_tts` |
| `orchestrator/manager.py` | `pools=None` in `__init__`; threads pool refs to STT, greeting TTS, pipeline TTS |
| `orchestrator/factory.py` | Singleton `_pools`; new `warmup_pools()` async function |
| `run_server.py` | `await warmup_pools()` before uvicorn starts |

## How each pool works

### DeepgramPool (STT)

Deepgram STT sessions are **stateful per-call**: one WebSocket carries one
call's audio from start to finish and cannot be reset for a different caller.
The pool uses a **consume-and-replenish** model:

1. At startup, `warmup()` opens `stt_size` (default: 2) WebSocket connections
   concurrently and puts them in an `asyncio.Queue`.
2. Each idle connection has a lightweight keepalive task that sends
   `{"type":"KeepAlive"}` every 3 s — Deepgram drops idle sockets after 10 s
   without data.
3. When a call starts, `pool.acquire()` (async context manager) pops a
   pre-connected socket from the queue instantly and yields it to `run_stt`.
   The idle keepalive is cancelled; `run_stt` has its own.
4. When the `async with pool.acquire() as dg_ws:` block exits, the socket is
   closed and `asyncio.create_task(_open_one())` replenishes the pool in the
   background for the next call.
5. If the pool is empty at acquire-time (burst or startup race), a fresh
   connection is opened synchronously (fallback — same latency as before
   Step 6 but the pool restores itself immediately after).

### ElevenLabsPool (TTS)

ElevenLabs uses HTTPS. `httpx.AsyncClient` maintains an internal TCP keep-alive
pool. Before Step 6 a new `AsyncClient` was created per `synthesize_and_stream`
call, which meant a new TCP+TLS handshake per call **and per sentence** within a
turn.

The pool creates one shared `AsyncClient` at startup and:
1. Fires a `GET /v1/models` warmup request to pre-establish the TCP+TLS
   connection (the first synthesis call would otherwise pay this cost).
2. All `synthesize_and_stream` calls use the shared client — connections are
   reused via HTTP keep-alive. The client is NOT closed between calls.
3. `run_tts` extracts `tts_pool.client` once and passes it to every `_safe_synth`
   call inside the loop.

## Interface contract

All new parameters default to `None`:

```python
run_stt(..., stt_pool=None)
synthesize_and_stream(..., http_client=None)
run_tts(..., tts_pool=None)
VoiceOrchestrator(pools=None)
```

When `None`, the original code path runs unchanged — a fresh WebSocket or
AsyncClient is opened per call. This means:

- Tests that call `run_stt` / `run_tts` directly without a pool still work.
- `VoiceOrchestrator(pools=None)` is the safe no-pool fallback used if
  `warmup_pools()` failed at startup.

## Startup output (after Step 6)

```
[MAIN] Starting modular voice pipeline server on port 5000
       STT -> Deepgram nova-3
       LLM -> Groq     llama-3.1-8b-instant
       TTS -> ElevenLabs eleven_flash_v2_5
[MAIN] Warming up connection pools...
[POOL/STT] Pre-warming 2 Deepgram connections...
[POOL/TTS] ElevenLabs connection warmed (HTTP 200)
[POOL/STT] 2/2 connections ready
[MAIN] Waiting for Twilio calls...
```

Per-call output (pool fast path):
```
[POOL/STT] Acquired pre-warmed connection (idle 4210ms)
[STT] Connected to Deepgram STT (Nova-3)
```

## Expected latency impact

| Metric | Before Step 6 | After Step 6 |
|---|---|---|
| Greeting TTFA (time to first audio chunk) | ~350 ms connection setup + ~75 ms EL | ~0 ms setup + ~75 ms EL |
| p50 end-to-end turn latency | unchanged (dominated by LLM + TTS) | unchanged |
| Sentences 2+ within a turn | each pays ~150 ms TCP+TLS | ~5-10 ms keep-alive reuse |

The `latency_ms` block in `logs/calls/*.json` (added in Step 5) measures
*user-final → TTS first audio* end-to-end, which includes LLM time. The
greeting has no user-final event so it isn't captured there. For now, the
clearest measurement is to watch the wall-clock gap between
`[ORCH] Playing greeting...` and `[TTS] Sentence: ...` in the console.

## Files NOT touched in Step 6

- `llm/groq_llm.py` — LLM is already a stateless HTTP call; no pooling needed.
- `contracts/`, `models/`, `agent_logging/` — no changes.
- `telephony/server.py` — no changes.

## Known follow-ups (not in Step 6 scope)

- **Graceful pool shutdown on SIGTERM** — `ConnectionPools.shutdown()` is
  implemented but not yet wired to the uvicorn shutdown signal. Low priority
  since the OS reclaims sockets on exit anyway.
- **Pool size config** — hardcoded to 2 STT connections in `factory.py`.
  Could be `POOL_STT_SIZE = int(os.getenv("POOL_STT_SIZE", "2"))` for tuning
  without code changes.
- **Pool metrics in CallLogger** — `pool_hit` / `pool_miss` counters would let
  you detect burst-pool-exhaustion in production. Deferred to Step 10 (tests).
