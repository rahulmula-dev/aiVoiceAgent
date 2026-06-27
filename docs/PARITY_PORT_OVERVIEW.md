# Parity Port — Overview

This document summarises the effort to port features from the company project
(`Chakraview LABS/ai-voice-agent-dev`) into this clean build, one step at a
time. The clean build mirrors the company project's folder structure with
many files stubbed (empty `__init__.py` or commented-out company code);
each step replaces stubs with working clean-build implementations.

## Why

The clean build started as a from-scratch reimplementation of the company
voice agent. It demonstrates the same pipeline (Twilio → STT → LLM → TTS)
with simpler code, fewer bugs, and better latency characteristics. The
parity port adds the company's production-grade features back in — RAG,
governance, pooling, audit logging, CRM — without inheriting the original's
bugs or architectural tangles.

## Where to find things

| Resource | Location |
|---|---|
| Step-by-step build docs (this doc + per-step) | `docs/parity_*.md` |
| Live roadmap with current status | `C:\Users\rmxhi\.claude\plans\c-users-rmxhi-desktop-chakraview-labs-a-crystalline-swan.md` |
| Daily cold-start runbook (Twilio + ngrok + Dev Phone) | `C:\Users\rmxhi\.claude\plans\so-from-tomorrow-uh-scalable-music.md` |
| Company project (reference, do not run) | `C:\Users\rmxhi\Desktop\Chakraview LABS\ai-voice-agent-dev` |
| Per-call transcripts (timestamps + latency metrics) | `logs/transcripts/<datetime>.json` |
| Pretty-print latest transcript | `uv run .\view_call.py` |
| Measured latency benchmarks (Jun 22 baseline vs Jun 24 post-pool) | `docs/latency_benchmarks.md` |
| Errors encountered + fixes (Steps 6–9) | `docs/error_log.md` |
| Premium API keys to request from manager | `docs/api_keys_needed.md` |
| Session notes (what done / what left) | `docs/session_2026-06-24.md` |
| Sandbox testing (no-Twilio local simulator) | `docs/sandbox_testing.md` |

## Status

| # | Step | Status | Detail doc |
|---|---|---|---|
| Bootstrap | Twilio Dev Phone, ngrok, `/voice` endpoint, FastAPI port, hangup phrase, barge-in-on-words | ✅ done | (covered inline in Step 2/3 docs) |
| 1 | Data models + interfaces | ✅ done | `docs/parity_01_data_models.md` |
| 2 | Web migration: aiohttp → FastAPI + uvicorn | ✅ done | `docs/parity_02_fastapi_migration.md` |
| 3 | Orchestrator extraction + telephony split + `/healthz` + `/api/call-status` | ✅ done | `docs/parity_03_orchestrator_extraction.md` |
| 4 | Governance layer (language interceptor + restricted topics + response policy) | ✅ done | `docs/parity_04_governance.md` |
| 5 | Audit + agent logging upgrade (call_logger, audit_logger, voice_logger) | ✅ done | `docs/parity_05_logging.md` |
| 6 | Connection pooling for STT/TTS | ✅ done | `docs/parity_06_connection_pooling.md` |
| 7 | RAG with pgvector + Postgres + Bedrock-or-mock embeddings | ✅ done — infra up, migration ran (16 vectors) | `docs/parity_07_rag.md` |
| 8 | LLM swap: Groq → Gemini | ✅ done — toggle via `LLM_PROVIDER=gemini`; Groq is active default | `docs/parity_08_gemini_swap.md` |
| 9 | Concurrency cap (Redis Lua CAS in `/voice`) | ✅ done — `CONCURRENCY_GATE_ENABLED` flag, fails open | `docs/parity_09_redis_gate.md` |
| 10 | Test suite (pytest, mocks, smoke) | queued ongoing | — |
| — | CRM | skipped — other dev | — |
| — | K8s + GitHub Actions CI | deferred — deployment, not features | — |

## Current architecture (after Step 3)

```
                              ┌─────────────────┐
Twilio inbound call           │  run_server.py  │
  (audio over Media Streams)  │   uvicorn boot  │
                              └────────┬────────┘
                                       │ imports app
                                       ▼
                              ┌─────────────────┐
                              │ telephony/server│  FastAPI app
                              │      .py        │  routes:
                              │                 │   /voice (GET+POST)
                              │  WebSocket      │   /  (WS)
                              │  Adapter        │   /healthz
                              │                 │   /api/call-status
                              └────────┬────────┘
                                       │ create_default_orchestrator()
                                       ▼
                              ┌─────────────────┐
                              │ orchestrator/   │  VoiceOrchestrator
                              │   manager.py    │  - audio_queue
                              │   factory.py    │  - transcript_queue
                              │                 │  - text_queue
                              │                 │  - barge_in_event
                              │                 │  - CallContext
                              │                 │  - TranscriptLogger
                              └────────┬────────┘
                                       │
                       ┌───────────────┼───────────────┐
                       ▼               ▼               ▼
                ┌───────────┐  ┌──────────┐  ┌────────────────┐
                │ stt/      │  │ llm/     │  │ tts/           │
                │ deepgram  │  │ groq     │  │ elevenlabs     │
                └───────────┘  └──────────┘  └────────────────┘
                                       │
                                       ▼
                              ┌─────────────────┐
                              │ contracts/      │  Pydantic schemas +
                              │   schemas.py    │  Protocol interfaces
                              │   interfaces.py │  (added in Step 1)
                              │ models/         │
                              │   schemas.py    │
                              └─────────────────┘
```

## Tech stack diff (target = company project)

| Layer | Clean build today | Target | Step |
|---|---|---|---|
| Web | **FastAPI + uvicorn** ✅ | FastAPI + uvicorn | Done in Step 2 |
| STT | **Deepgram nova-3 `language=multi`** ✅ (was English-only) | nova-3 multilingual | Changed in Step 4 for language detection |
| LLM | **Groq llama-3.1-8b-instant** (default) / Gemini 2.0 Flash (via flag) ✅ | OpenAI 4o + 4o-mini (pending manager approval) | Step 8 done; OpenAI queued |
| TTS | **ElevenLabs eleven_flash_v2_5 + connection pool** ✅ (~231 ms warm avg) | EL Flash primary, Azure Neural backup | Step 6 done; Azure fallback queued |
| Knowledge | **pgvector + Bedrock-or-mock embeddings** ✅ (RAG_ENABLED flag, Docker + 16 vectors) | pgvector + Bedrock Titan v2 (live embeddings) | Step 7 done; real Bedrock needs `LOCAL_TEST=false` + AWS creds |
| State store | **Postgres** ✅ (Docker port 5433) | Redis + Postgres | Step 7 Postgres done; Redis in Step 9 |
| Validation | **Pydantic v2** ✅ | Pydantic v2 | Done in Step 1 |
| Governance | **Code-level (default ON)**: language guard + topic detector + response policy ✅ | same | Done in Step 4 |
| Logging | **Per-call transcript JSON + crash-safe events.jsonl + sealed summary + access audit + PII masking** ✅ | + WAV audio recorder (deferred) | Done in Step 5 |

## Stable identifiers

| Thing | Value |
|---|---|
| Twilio number to dial (GD College) | **+18567165450** |
| Twilio number SID (for CLI updates) | **PNe626f6d9628d06e85a8081058f1e9da5** |
| Twilio CLI profile | **calltestai** |
| Local server port | **5000** |
| Dev Phone "from" number | **+18563936660** |

## Conventions

- Each step delivers a still-working live demo. If a step would break the live
  call flow, it's broken into sub-steps so each individual change is reversible.
- Each step is additive where possible: schemas first, then code that uses them.
- Each step is feature-flag-gated where the risk is non-trivial (Step 4 onwards).
- The plan file at `C:\Users\rmxhi\.claude\plans\...crystalline-swan.md` is the
  living roadmap. This docs folder is the historical record.
- Daily ngrok URL rotation is a known operational quirk handled by the
  cold-start runbook — not addressed by any step.
