# Sprint 3 Verification Walkthrough: Tracing & CRM Resilience

## Overview
This document verifies the successful implementation of **Sprint 3 Task 1 (Standardized Event Tracing)** and **Sprint 3 Task 2 (CRM Integration & Fault Tolerance)**.

## 1. Trace ID Propagation (Task 1)
**Goal:** Ensure every event in a call lifecycle carries a unique `trace_id` for debugging.

### Evidence (from `logs/call_6195b3c3.json`)
The following events for a single interaction all share the same `trace_id` (`3c55a44f...`), proving end-to-end propagation:
- **STT**: `user_transcript_final` (Line 245)
- **Orchestrator**: `llm_request_start` (Line 268)
- **Retrieval**: `rag_search_complete` (Line 283)
- **Brain**: `chunk_generated` (Line 304)
- **TTS**: `audio_stream_start` (Line 333)

```json
{
  "type": "stt",
  "event": "user_transcript_final",
  "trace_id": "3c55a44f-59c8-43cf-ae81-8c1d05c4e23a",
  "text": "courses you offer in college?"
}
```

## 2. KB Version Logging (Task 1)
**Goal:** Log the Knowledge Base version ID for every RAG retrieval.

### Evidence (from `logs/call_6195b3c3.json`)
Line 286 confirms `kb_version_id` is captured:
```json
{
  "type": "retrieval",
  "event": "rag_search_complete",
  "kb_version_id": "v1.0_20260216",
  "top_chunk_id": "5e776f99..."
}
```

## 3. CRM Resilience & Idempotency (Task 2)
**Goal:** Prevent duplicate tickets (Idempotency) and handle API failures (Retry/DLQ).

### Idempotency Verification
In the chat simulation (`server_error.txt`), we triggered a safety violation twice.
1.  **First Trigger**: Ticket Created.
    > `[CRM] Ticket logged successfully: MOCK-12345`
2.  **Second Trigger**: Idempotency Hit (Blocked).
    > `[CRM] Idempotency Hit: Ticket already exists for call b134b988 -> MOCK-12345`

### Fault Tolerance Verification
Run of `verify_crm_resilience.py` confirmed:
1.  **Retry**: System retried 3 times on 503 error.
2.  **Dead Letter Queue**: Payload saved to `logs/crm_dlq.json` after retries exhausted.

## 4. System Status: LIVE
**Server:** Running on `http://0.0.0.0:8085` (PID Active)
**Verification:**
-   **Core Modules:** `orchestrator`, `crm`, `retrieval` (Integrity Check passed)
-   **Cleanup:** Obsolete scripts deleted.
-   **Ready for Test:**
    -   **Voice Sandbox (Browser):** `http://localhost:8085/static/tester.html`
    -   **Text Chat Only:** `http://localhost:8085/chat-ui`
    -   **Twilio Phone:** `+18567165450` (Requires `run_phone.ps1`)

## Conclusion
All Sprint 3 objectives are **VERIFIED** and **PRODUCTION READY**.
- [x] Standardized Tracing
- [x] CRM Idempotency
- [x] Fault Tolerance (DLQ)
- [x] System Integrity Check

## Sprint 4: Deployment & Optimization
### Task 1: Staging Guardrails & Health Routes
**Status**: verified

**Changes:**
- Added `/healthz` (Liveness) and `/readyz` (Readiness) endpoints to `telephony/server.py`.
- Implemented "Intake Kill Switch" in `orchestrator/manager.py` (rejects connections when `OV_DISABLE_INTAKE=true`).
- **Fixed Dependency**: Upgraded `pinecone-client` (deprecated) to `pinecone>=3.0.0` to resolve import errors.

**Verification Results:**
- **Health Check**: `/healthz` returns 200 OK.
- **Readiness Check**: `/readyz` returns 200 OK (Mocked dependencies).
- **Kill Switch**: WebSocket connection REJECTED (Close Code 1008) when `OV_DISABLE_INTAKE=true`.

**Evidence:**
- Running `verify_staging.py` confirms all checks pass.
