# Week 1 — Doc 4: End-to-End Call Trace
**Repo:** `ai-voice-agent-dev` | **Date:** 2026-06-17 | **Author:** Cowork Audit

> Traces one full call from Twilio dial-in through STT → RAG → LLM → TTS → log seal.
> File references are exact — no code was modified.

---

## 1. High-Level Flow Diagram

```
Phone Call
    │
    ▼
Twilio PSTN
    │  HTTPS POST /voice
    ▼
[telephony/server.py] handle_incoming_call()
    │  ── concurrency gate (Redis Lua) ──
    │  ── returns TwiML with wss:// URL ──
    ▼
Twilio opens WebSocket
    │  WSS /media-stream?sid=<CallSid>&from=<From>
    ▼
[telephony/server.py] handle_media_stream()
    │  ── creates CallLogger ──
    │  ── calls create_default_orchestrator() ──
    ▼
[orchestrator/factory.py] create_default_orchestrator()
    │  ── acquires PooledTranscriber (Deepgram WS) from stt_pool ──
    │  ── creates Synthesizer (Deepgram TTS HTTP) ──
    ▼
[orchestrator/manager.py] VoiceOrchestrator.handle_audio_stream()
    │  ── secondary concurrency check ──
    │  ── opens session context (session_manager) ──
    │  ── starts silence monitor, session timer ──
    │  ── logs call to CRM (background) ──
    │  ── speaks GREETING ──
    │
    │  (main media loop)
    │
    ├── [media event] Twilio audio chunk (mulaw 8kHz, base64)
    │       ▼
    │   [stt/transcriber.py] Transcriber.send_audio()
    │       ▼
    │   Deepgram Nova-2 WebSocket (streaming)
    │       ▼
    │   [stt/transcriber.py] _listen() → on_transcript_callback()
    │       ▼
    │   [orchestrator/manager.py] _on_transcript()
    │       │
    │       ├── [partial] → barge-in check, pre-fetch RAG
    │       │
    │       └── [final] → governance chain:
    │               1. Restricted topic gate (policy.py)
    │               2. Competitor query gate (STAB-07)
    │               3. Language gate (LanguageGovernanceInterceptor)
    │               4. Low-confidence gate (<0.35)
    │               5. Endpointing buffer (dangling words)
    │               6. Intent classification (ResponsePolicyEngine)
    │               7. Context extraction (ContextManager)
    │               8. Barge-in handling (if AI was speaking)
    │               └── generate_and_speak() ──────────────────────────┐
    │                                                                   │
    ▼                                                                   ▼
[orchestrator/brain.py] Brain.generate_stream()              [orchestrator/manager.py]
    1. RAG: KnowledgeBase.search()                           generate_and_speak()
    2. CRM lookup (ticket ID / phone auto-ID)                    │
    3. Confidence gate (category threshold)                      │
    4. Gemini LLM streaming (primary → fast fallback)            │
    5. Sentence-level chunking                                   │
    Yields (sentence, metadata) per sentence                     │
            │                                                    │
            ▼                                                    │
    [tts/synthesizer.py] Synthesizer.speak()                    │
        Deepgram Aura TTS HTTP streaming                         │
        Yields audio bytes                                       │
            │                                                    │
            ▼                                                    │
    [orchestrator/manager.py] _send_response_chunk()            │
        base64-encodes → sends Twilio media JSON over WS ◄───────┘
            │
            ▼
    Twilio plays audio to caller
    
    ── call ends (hangup / silence / strike-3 / timeout) ──
    
[telephony/server.py] handle_media_stream() finally:
    ├── manager.cleanup() → releases STT/TTS back to pool
    ├── call_logger.generate_summary_line()
    ├── call_logger.save_log() → writes JSON + syncs to S3
    ├── session_manager.unregister_orchestrator()
    └── session_manager.end_session()
```

---

## 2. Detailed Step-by-Step Trace

### Phase 0 — Server Startup (before any call)

**File:** `telephony/server.py` → `startup_event()`

1. `default_session_manager.start_collector()` — starts background zombie-session reaper.
2. `reset_active_calls()` — zeroes Redis call counter (prevents stale counts from prior crash).
3. `stt_pool.initialize()` — opens `DEEPGRAM_MIN_CONNECTIONS` (2 dev / 10 prod) WebSocket connections to `wss://api.deepgram.com/v1/listen` and keeps them alive.
4. `elevenlabs_pool.initialize()` — only if `TTS_PROVIDER=elevenlabs`.
5. `start_background_worker()` — starts CRM DLQ reconciliation (retries failed CRM writes from S3 queue).

---

### Phase 1 — Incoming Call: `/voice`

**File:** `telephony/server.py` → `handle_incoming_call()`

1. **Twilio** sends HTTPS POST to `https://<ngrok>/voice` with `CallSid` and `From` form fields.
2. `is_twilio_request()` checks for `X-Twilio-Signature` header (bypassed if `BYPASS_TWILIO_AUTH=true`).
3. `increment_if_under_cap(MAX_INBOUND_CALLS, call_sid)` — runs Redis Lua `LUA_INCREMENT_IF_UNDER_CAP`:
   - If at cap (30): speaks `APOLOGY_CAPACITY`, creates CRM ticket, returns TwiML `<Hangup/>`.
   - If under cap: atomically increments counter and registers `call_sid` in Redis set.
4. Reads `NGROK_URL` env var to build WebSocket URL (`wss://` if https, `ws://` if http).
5. Returns TwiML:
   ```xml
   <Response>
     <Connect>
       <Stream url="wss://<host>/media-stream?sid=<CallSid>&from=<From>" />
     </Connect>
   </Response>
   ```

---

### Phase 2 — WebSocket Opened: `/media-stream`

**File:** `telephony/server.py` → `handle_media_stream()`

1. Assigns `session_id` = 8-char UUID prefix (e.g. `119cab75`).
2. Extracts `sid` and `from` from WebSocket query params.
3. Creates `CallLogger(call_id=session_id, caller_number=from_number)` — **immediately** writes `call_<id>.events.jsonl` to `logs/` (append-only event stream).
4. `bind_call_context(session_id, from_number)` — Python logging context binding.
5. `websocket.accept()`.
6. Calls `create_default_orchestrator(session_id, call_logger, session_manager, websocket, session_metadata)`.

**File:** `orchestrator/factory.py` → `create_default_orchestrator()`

7. `stt_pool.acquire(timeout=2.0)` — gets a pre-warmed `Transcriber` from the pool (LIFO strategy). Wraps it as `PooledTranscriber`.
8. Creates `Synthesizer()` (Deepgram TTS, default) or acquires from `elevenlabs_pool`.
9. Returns `VoiceOrchestrator(stt_provider, tts_provider, call_logger, session_manager)`.

Back in `handle_media_stream()`:

10. `default_session_manager.register_orchestrator(session_id, manager)` — for zombie recovery.
11. Calls `manager.handle_audio_stream(websocket)`.

---

### Phase 3 — Audio Stream Starts

**File:** `orchestrator/manager.py` → `handle_audio_stream()`

12. **Secondary concurrency gate** — `is_over_capacity_atomic()` Lua check. If over cap (race condition): decrement counter, play fallback audio, close WebSocket. 
13. **Intake guardrail** — if `INTAKE_ENABLED=false`, close WebSocket immediately.
14. Sets STT callback: `transcriber.set_callback(self._on_transcript)`.
15. `transcriber.connect()` → on `PooledTranscriber` this is a no-op (already connected).
16. Waits for Twilio `start` event to get `streamSid` (5s timeout).
17. Opens `session_scope(canonical_session_id)` → creates `CallSession` in `SessionManager`.
18. Creates `CallRecorder(session.session_id, encoding='mulaw', sample_rate=8000)` — writes raw audio to disk.
19. Starts `asyncio.create_task(self._monitor_silence())` — fires at 10s and 20s of silence.
20. Starts `SessionTimerManager` — soft warning at configurable time, hard end at max session time.
21. Fires `_log_call_bg()` as background task — calls `CRMClient.log_call()` to create CRM record (non-blocking).
22. Transitions state: `CALL_INIT → LISTENING`.
23. Sends GREETING: `asyncio.create_task(self.generate_and_speak(PRDScripts.GREETING, is_greeting=True))`.
24. Enters main media loop: `while True: message = await websocket.receive()`.

---

### Phase 4 — Audio Processing (Per Frame)

**File:** `orchestrator/manager.py` → media loop + `orchestrator/manager.py` STT path

For each Twilio `media` event (20ms mulaw chunks at 8kHz):

25. Extracts base64 payload, decodes to bytes.
26. `self.recorder.write_chunk(payload)` — appends to WAV file.
27. `await self.transcriber.send_audio(payload)` → `Transcriber.send_audio()`.

**File:** `stt/transcriber.py` → `send_audio()`

28. **VAD heuristic** — counts non-silence bytes; logs voice-start / voice-end transitions.
29. Updates `_last_voice_timestamp` on voice packets (used to compute `stt_latency`).
30. `await self.ws.send(audio_chunk)` → raw bytes to Deepgram WebSocket.

**File:** `stt/transcriber.py` → `_listen()` (running in background task)

31. Receives Deepgram JSON response.
32. Extracts: `transcript`, `confidence`, `is_final`, `detected_language`.
33. **Latching heuristic** — if final transcript is shorter than best partial and partial was high-confidence, use the partial instead.
34. Calls `await self.on_transcript_callback(sentence, confidence, stt_latency, is_final, detected_lang)`.

---

### Phase 5 — Transcript Processing: `_on_transcript()`

**File:** `orchestrator/manager.py` → `_on_transcript()`

**5a — Partial transcript path (is_final=False):**
35. Barge-in: if AI is SPEAKING and transcript matches no echo → `synthesizer.stop_current_speech()`, send `clear` event to Twilio, transition to INTERRUPTED.
36. Pre-fetch RAG: if `len(raw_text) > 15` and intent=PROCEED → fire `asyncio.create_task(brain.kb.search(raw_text, ...))` as prefetch.
37. Mutation tracker: counts word-level changes in partials (Hindi hallucination detection).

**5b — Final transcript path (is_final=True):**
38. **Restricted topic gate** — `detect_restricted_topic(raw_text)` from `contracts/policy.py`. If restricted, sets `terminate_session=True`, calls `handle_restricted_topic()`, speaks refusal, returns.
39. **Competitor gate (STAB-07)** — `classify_intent(raw_text)` == `HARD_REFUSAL_COMPETITOR_QUERY` → speaks `REFUSAL_COMPETITORS`, returns (no CRM ticket).
40. **Callback offer intercept (STAB-05)** — if `_pending_callback_offer=True`, intercepts Yes/No, routes to callback confirmation or "anything else", returns.
41. **Language gate** — `LanguageGovernanceInterceptor.check(raw_text, deepgram_lang)`:
    - If non-English: increments `language_strike_count`, creates CRM ticket, speaks `REFUSAL_LANGUAGE_[1/2/3]`. Strike 3 → `_language_termination_flow()` (speaks farewell + closes WS).
42. **Low-confidence gate** — if `confidence < 0.35` → speaks `APOLOGY_CLARIFICATION`.
43. **Endpointing buffer** — if text ends on a dangling function word (e.g. "about") and ≤8 words, buffers and waits 3s for continuation.
44. State transition: `LISTENING → TRANSCRIBING`.
45. **Intent classification** — `ResponsePolicyEngine.classify_intent(text)`. Returns: `PROCEED`, `HARD_REFUSAL_*`, `ESCALATION_REQUIRED`, `AMBIGUOUS`.
46. If non-PROCEED: cancel pending response task, speak refusal script, return.
47. **Context extraction** — `ContextManager.update_context(session.call_context, text, intent)` — slots name, program, intake, campus.
48. **Barge-in handling** — if `response_task` is running and AI was SPEAKING → `handle_barge_in()`.
49. Normal path: `asyncio.create_task(self.generate_and_speak(text, intent, trace_id, turn_start_time, stt_latency))`.

---

### Phase 6 — Response Generation: `generate_and_speak()`

**File:** `orchestrator/manager.py` → `generate_and_speak()` → delegates to `Brain.generate_stream()`

**File:** `orchestrator/brain.py` → `generate_stream()`

50. **RAG retrieval** — uses prefetched task if available, otherwise calls `KnowledgeBase.search(query, call_logger, top_k=3)`:

    **File:** `retrieval/vector_store.py` → `KnowledgeBase.search()`

    a. `get_query_embedding(query)` → `retrieval/embeddings.py`:
       - `LOCAL_TEST=true`: returns `[1.0] * 1536` (mock — all results equal distance)
       - Production: calls AWS Bedrock Titan v2 → 1536-dim L2-normalized vector
    b. Ensures asyncpg connection pool exists (`min_size=10, max_size=40`).
    c. SQL query against `rag.chunks JOIN rag.embeddings JOIN rag.documents`:
       - Computes `cosine = 1 - (embedding <=> query_vector)` and `semantic = similarity(content, query)` (trigram via pg_trgm)
       - Ensemble score: `0.7 * cosine + 0.3 * semantic`
       - Confidence gate: score must exceed category threshold (0.58–0.65 depending on topic)
       - Returns top-k chunks sorted by ensemble score
    d. Returns: `(context_text, best_score, best_category, "pgvector-ensemble-v1", chunk_ids)`

51. **CRM lookup** — if text contains `MOCK-\d{5}` ticket ID or is a status query → calls `CRMClient.get_ticket_status()` / `get_ticket_by_phone()`.
52. **Confidence gate (STAB-05)** — if `rag_score < category_threshold` and not conversational → yields `LOW_CONFIDENCE_FALLBACK` directly, **no LLM call**. Back in manager: speaks fallback + `CALLBACK_OFFER`, sets `_pending_callback_offer=True`.
53. **Gemini LLM streaming** — builds `rag_prompt` with KB context + CRM data + call context slots. Calls `genai.GenerativeModel.generate_content_async(history, stream=True)`:
    - TTFT enforced: first chunk must arrive within `LLM_TIMEOUT` (0.5s prod / 10s dev)
    - On timeout/429: switches to `gemini-1.5-flash-8b` fast model
    - Buffers text in `sentence_buffer`, yields complete sentences on punctuation
    - Each yield: `(sentence_text, sent_metadata)`

---

### Phase 7 — TTS and Audio Delivery

Back in `orchestrator/manager.py` → `generate_and_speak()`:

54. For each yielded sentence from `Brain.generate_stream()`:
    a. `ResponsePolicyEngine.validate_response(sentence)` — ASCII ratio check.
    b. `self.state.transition_to(CallState.SPEAKING)`.
    c. `async for chunk in self.synthesizer.speak(sentence, call_id=self.sid)`:

    **File:** `tts/synthesizer.py` → `Synthesizer.speak()`

    d. POSTs to `https://api.deepgram.com/v1/speak?model=aura-asteria-en&encoding=mulaw&sample_rate=8000`
    e. TTFA enforced: first byte must arrive within `ttfa_budget` (0.3s prod / 15.0s dev)
    f. Yields audio bytes in 1024-byte chunks
    g. On `call_id in _stop_signals`: breaks (barge-in detected)

    Back in manager for each audio chunk:

55. `_send_response_chunk(chunk)` — encodes base64, sends Twilio JSON:
    ```json
    {"event": "media", "streamSid": "<sid>", "media": {"payload": "<base64>"}}
    ```
56. Twilio receives audio and plays it to the caller.

---

### Phase 8 — Call Teardown and Log Seal

Triggered by: WebSocket disconnect, `CallState.CALL_END`, silence termination, strike-3 language termination, session timer hard end, or unhandled exception.

**File:** `telephony/server.py` → `handle_media_stream()` finally block:

57. `await manager.cleanup()`:
    - Cancels `silence_task`, `_session_timer`, `_buffer_flush_task`, `_vad_safety_task`.
    - `await self.transcriber.close()` → `PooledTranscriber.close()` → `stt_pool.release(delegate)` (resets state, returns to pool — no WS teardown).
    - `await self.synthesizer.close()` → clears active texts / stop signals (shared HTTP client stays alive).
    - `await self.recorder.stop()` — seals WAV file.
    - Sends final CRM ticket via `CRMClient.create_ticket()` with full transcript and sentiment.

58. `call_logger.generate_summary_line()` — computes p50/p90/p95/p99 latency stats for LLM, STT, RAG, TTS. Writes to `logs/call_summary.log`. Idempotent guard (only runs once).

59. `call_logger.save_log(session_obj=manager.session)`:
    - Compiles full `log_data` dict: call_id, kb_version_id, chunk_ids, confidence_scores, sentiment, termination_reason, latency_metrics, structured_turns, all events.
    - **Atomic write**: writes to `logs/call_<id>.json.tmp`, renames to `logs/call_<id>.json`.
    - `S3Storage.upload_file(events_file, "events/...")` — uploads JSONL event stream.
    - `S3Storage.upload_file(summary_file, "summaries/...")` — uploads call summary.
    - Deletes `call_<id>.events.jsonl` after successful seal.

60. `default_session_manager.unregister_orchestrator(session_id)`.
61. `default_session_manager.end_session(session_id)` — removes from in-memory session map.

> **Note:** Call counter decrement (`decrement_active_calls`) is handled by the **Twilio StatusCallback** at `/api/call-status`, not by the WebSocket teardown. Twilio POSTs to this endpoint when the call reaches `completed/failed/busy/no-answer/canceled` status. This is the authoritative decrement to prevent counter leakage.

---

## 3. State Machine Transitions (Normal Happy Path)

```
CALL_INIT
  → LISTENING          (first media event received)
  → TRANSCRIBING       (final STT transcript arrives)
  → LISTENING          (if low-confidence or empty)
  → SPEAKING           (generate_and_speak starts streaming TTS)
  → LISTENING          (TTS finished, back to waiting)
  ... (repeats per turn) ...
  → CALL_END           (silence timeout / termination / hangup)
```

**Barge-in extension:**
```
SPEAKING → INTERRUPTED  (partial STT arrives while speaking)
INTERRUPTED → SPEAKING  (handle_barge_in completes and speaks response)
SPEAKING → LISTENING    (TTS finishes)
```

**Escalation path:**
```
LISTENING → TRANSCRIBING → ESCALATION → SPEAKING → CALL_END
```

---

## 4. File Notes (E2E Critical Files)

### `telephony/server.py`
```
File: telephony/server.py
Importance: P0
Purpose: FastAPI app — all HTTP routes + WebSocket endpoints.
Key functions: handle_incoming_call (/voice), handle_media_stream (/media-stream), startup_event, readyz, healthz
Inputs: Twilio HTTPS POST (/voice), Twilio WebSocket (/media-stream), .env
Outputs: TwiML XML responses, WebSocket audio messages, audit logs
Runtime role: Process gateway — all external requests enter here
Risks: BYPASS_TWILIO_AUTH=true must not be deployed to production; AuditLogger runs on every /api/ and /admin/ path
Questions for human: Is BYPASS_TWILIO_AUTH checked in CI? Is there a way it could accidentally be left true in production?
```

### `orchestrator/manager.py`
```
File: orchestrator/manager.py
Importance: P0
Purpose: Central call brain — state machine, STT callbacks, governance chain, RAG, TTS streaming.
Key functions: handle_audio_stream, _on_transcript, generate_and_speak, handle_barge_in, speak_immediate_response, cleanup
Inputs: STT transcript callbacks, Twilio audio WebSocket, Brain yields
Outputs: Twilio audio media events, CRM tickets, call logs
Runtime role: One instance per active call
Risks: 2821 lines — extremely complex; multiple asyncio tasks; silence monitor + session timer + buffer flush task + STT watchdog all running concurrently
Questions for human: Is there a test that validates the silence monitor does not fire during SPEAKING state? (STAB-04 tests this)
```

### `orchestrator/brain.py`
```
File: orchestrator/brain.py
Importance: P0
Purpose: RAG + LLM integration. Retrieves KB context, calls Gemini, streams sentences.
Key functions: generate_stream (normal), generate_with_classification (barge-in race)
Inputs: Caller transcript, session conversation history, prefetched RAG task
Outputs: (sentence_text, metadata) generator
Runtime role: Called once per user turn
Risks: LLM_TIMEOUT is 0.5s in production — very tight; Gemini rate limits (429) fall back to gemini-1.5-flash-8b; LOCAL_TEST=true means all embeddings are [1.0]*1536 so cosine similarity is undefined/equal for all chunks
Questions for human: What happens in LOCAL_TEST when all RAG scores are equal and all pass/fail the threshold at the same value? (Answer: they all pass, so top-k is random)
```

### `retrieval/vector_store.py`
```
File: retrieval/vector_store.py
Importance: P0
Purpose: PGVector-backed KB search with ensemble scoring and confidence gates.
Key functions: search(), _ensure_pool(), get_query_embedding()
Inputs: Query string, asyncpg pool, embeddings.py
Outputs: (context_text, best_score, category, kb_version, chunk_ids)
Runtime role: Called once per user turn (or prefetched on partial)
Risks: LOCAL_TEST=true → constant mock embedding → all cosine scores equal → confidence gate behavior is unpredictable; RDS residency check only verifies hostname pattern (not actual region)
Questions for human: Is the pgvector database populated with real GD College KB data? The schema exists but no data was visible in this audit.
```

### `contracts/state.py`
```
File: contracts/state.py
Importance: P1
Purpose: Strict state machine for call lifecycle with transition validation.
Key functions: transition_to(), get_state()
Inputs: Desired new state, optional trace_id
Outputs: Raises ValueError on invalid transitions; logs to call_logger
Runtime role: Referenced throughout manager.py for every state change
Risks: CALL_END is reachable from any state (hardcoded bypass on line 73) — this is intentional
Questions for human: Should ESCALATION always be a terminal state or can a call recover from escalation?
```

### `contracts/config.py`
```
File: contracts/config.py
Importance: P0
Purpose: All feature flags and env-aware thresholds in one place.
Key functions: All @property methods — ttfa_budget, stt_connect_timeout, max_inbound_calls, rag_search_timeout, etc.
Inputs: os.environ (reads at property access time, not at import)
Outputs: Config values
Runtime role: Used by Brain, Transcriber, Synthesizer, concurrency.py, manager.py
Risks: APP_ENV defaults to "production" — if not set locally, PRD-strict timeouts apply (0.3s TTFA, 0.5s STT connect) which will fail on typical home internet
Questions for human: Should APP_ENV default to "development" instead of "production" to be safer for new devs?
```

### `agent_logging/call_logger.py`
```
File: agent_logging/call_logger.py
Importance: P1
Purpose: Per-call structured JSON event logger with S3 sync at teardown.
Key functions: log_event(), generate_summary_line(), save_log()
Inputs: Event data from all subsystems throughout the call
Outputs: logs/call_<id>.events.jsonl (live), logs/call_<id>.json (sealed at end), S3 upload
Runtime role: One instance per call; all components hold a reference to it
Risks: S3 upload can fail silently — the local JSON is the source of truth; two duplicate Exception clauses at lines 321 and 323 (bug)
Questions for human: Is there a log rotation policy? logs/ will grow indefinitely on local dev.
```

### `telephony/concurrency.py`
```
File: telephony/concurrency.py
Importance: P1
Purpose: Redis-backed atomic concurrency enforcement with local RAM fallback.
Key functions: increment_if_under_cap(), decrement_active_calls(), is_over_capacity_atomic(), check_redis_health()
Inputs: Redis connection, call_sid, max_cap
Outputs: (bool accepted, int count)
Runtime role: Called on every call admission and termination
Risks: Local fallback (_local_counter) is per-process — does not work in multi-process deployments; Redis connection uses 1s timeouts which may cause brief hangs on cold connect
Questions for human: Is the /api/call-status webhook registered in Twilio for every phone number?
```

---

## 5. Latency Budget Reference

From `docs/latency_budget.md`:

| Stage | PRD Target (p90) | How Enforced |
|---|---|---|
| STT Handshake | < 10ms (warm) | Pre-warmed pool |
| STT Transcription | < 300ms | Deepgram Nova-2 |
| Brain/LLM (TTFT) | < 800ms | `LLM_TIMEOUT=0.5s` + fast model fallback |
| TTS TTFA | < 200ms | `ttfa_budget=0.3s` in production |
| E2E Turn | < 1.6s | Circuit-breaker at `turn_latency_circuit_break_s=5.0s` |

In **development** (`APP_ENV=development`), all timeouts are relaxed significantly (TTFA=15s, STT=5s, LLM=10s, circuit-break=35s).
