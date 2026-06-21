# Step 1 — Data models + interfaces

**Status:** ✅ done
**Risk to working pipeline:** none (purely additive)
**Time:** ~1 hour

## Why this first

Every subsequent step (orchestrator extract, governance, RAG, LLM swap)
needs to pass typed objects between layers. Without shared schemas, each
step ends up inventing ad-hoc dicts and the interfaces churn every time
something new is added.

Doing schemas first is also zero-risk: no code currently imports from
`contracts/` or `models/` (both were empty `__init__.py` stubs), so even if
a schema has a bug, nothing fails at runtime.

## What was added

Three new files, ~330 lines total, all pure Pydantic / typing.Protocol —
no external services touched.

| File | Contents |
|---|---|
| `contracts/schemas.py` | Pydantic v2 models: `TranscriptSegment`, `LLMRequest`, `LLMResponse`, `CallContext`, `EscalationEvent` |
| `contracts/interfaces.py` | `typing.Protocol` definitions: `STTEngine`, `TTSEngine`, `KnowledgeBaseEngine`, `LLMEngine`, `CRMEngine`, `PolicyEngine` |
| `models/schemas.py` | Conversation-turn schemas: `BaseTurn`, `StandardTurn`, `BargeInTurn` |

## Field overview

**`TranscriptSegment`** — one chunk of transcribed audio from STT.
Fields: `text`, `is_final`, `confidence`, `speaker`, `timestamp`.

**`LLMRequest`** — full context bundle sent to the LLM.
Fields: `prompt`, `history` (permissive `List[Any]`), `rag_context`.

**`LLMResponse`** — structured response from the LLM.
Fields: `text_content`, `sentiment`, `suggested_actions`.

**`CallContext`** — per-call metadata + persistent memory. Updated turn-by-turn.
Fields: `session_id`, `caller_number`, `start_time`, `transcript_log`,
`program_interest`, `intake`, `user_name`, `last_intents`,
`last_agent_answer_summary`, `study_mode`, `campus`,
`retrieved_chunks_snapshot`, `chunk_ids_used`.

**`EscalationEvent`** — trigger for human handoff.
Fields: `reason`, `target_department`.

**`BaseTurn`** + `StandardTurn` + `BargeInTurn` — per-turn metadata for
CRM/audit serialization. Tracks topic, response status, barge-in
classification, multi-step flag, timestamps.

## Interface contracts

Each Protocol is a structural type (PEP 544). Any class that implements
the required methods satisfies the contract; no explicit inheritance
needed. This is what lets us swap vendors (e.g. Deepgram → AssemblyAI)
later without touching the orchestrator.

Key signatures:
- `STTEngine`: `connect()`, `send_audio(bytes)`, `close()`
- `TTSEngine`: `speak(text, call_id) -> AsyncGenerator[bytes]`, `stop_current_speech(call_id)`, `close()`
- `LLMEngine`: `start_new_session()`, `generate_stream(text, history) -> AsyncGenerator[str]`, `generate_response(text, history) -> str`
- `KnowledgeBaseEngine`: `search(query, top_k) -> tuple[str, float]`
- `CRMEngine`: `create_ticket`, `log_call`, `create_callback`, `get_ticket_status`, `get_ticket_by_phone`
- `PolicyEngine`: `validate_response(context, response_text) -> bool`, `check_escalation(user_text) -> EscalationEvent`

## Adaptations vs. company verbatim

Three minor changes from the company's `contracts/schemas.py` / `contracts/interfaces.py`:

1. **`LLMRequest.history` typed as `List[Any]`** — company uses Gemini's
   chat-content format (`{"role": ..., "parts": [...]}`); the clean build
   uses OpenAI/Groq style (`{"role": ..., "content": "..."}`). Permissive
   typing accepts both. Tightens when we do the LLM swap (Step 8).
2. **`CallContext.session_id` accepts any string** — company uses UUIDs,
   the clean build currently uses Twilio `streamSid`. Same field, both
   formats welcome.
3. **`from typing import Protocol`** — company uses `from typing_extensions
   import Protocol` for back-compat. We're on Python 3.11+, no backport
   needed.

Otherwise the schemas are ported verbatim, including docstrings.

## Verification

1. **Compile + import** — `uv run python -c "import contracts.schemas;
   import contracts.interfaces; import models.schemas; print('OK')"` → OK.
2. **Instantiate each schema** — created `TranscriptSegment`, `LLMRequest`,
   `LLMResponse`, `CallContext`, `EscalationEvent`, `StandardTurn`,
   `BargeInTurn` with sample data; all serialise via `.model_dump()`.
3. **Protocol contracts importable** — all six Protocols import, methods
   visible.
4. **Existing pipeline unaffected** — `run_server`, `stt.deepgram_stt`,
   `llm.groq_llm`, `tts.elevenlabs_tts`, `config`,
   `logs.transcript_logger` all import unchanged.

## Dependencies added

- `pydantic>=2,<3` — already in the venv as a transitive dep of `openai`,
  so no install was needed. Confirmed `pydantic==2.13.4` already present.

## What this unlocks

Now any later port that needs to type its arguments can `from
contracts.schemas import CallContext`. For example, Step 3 wired
`CallContext` into `VoiceOrchestrator.handle_audio_stream()` to carry
per-call state. Step 4 will type `PolicyEngine.validate_response()` to
take a `CallContext` and an LLM response, and `check_escalation()` to
return an `EscalationEvent`.
