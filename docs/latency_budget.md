# Latency Budget Specification v1.0

## 1. Overview
This document defines the latency budget for the GD College AI Voice Agent (CILA). The system is optimized for real-time telephony, requiring a fluid, non-robot performance.

## 2. Latency Thresholds (PRD Targets)
The following thresholds represent the **p90 ceiling** for each stage:

| Stage | Target (p90) | Measurement Point |
|---|---|---|
| **STT Handshake** | **< 10ms (Warm)** | Call start to socket ready |
| **STT Transcription** | **< 300ms** | Audio end to transcript received |
| **Brain / LLM** | **< 800ms** | Transcript to first stream chunk |
| **TTS TTFA** | **< 200ms** | Text sent to first audio chunk |
| **E2E Turn** | **< 1.6s** | User speech end to AI audio start |

---

## 3. Warm-Start Optimization: Persistent Connection Pools
To meet the aggressive ≤300ms (STT) and ≤200ms (TTS) targets, the system uses **Persistent WebSocket Connection Pools**.

### 3.1. Architectural Shift: Cold vs. Warm Start
*   **Cold Start (Legacy):** Opening a new WebSocket to Deepgram/ElevenLabs at call start. 
    *   *Latency Penalty:* 1.2s - 2.5s (TCP + TLS + Handshake).
*   **Warm Start (Current):** Acquiring a "Live" socket from a pre-established pool.
    *   *Latency Gain:* Reduces initiation delay to < 50ms.

### 3.2. Pool Implementation Details
- **Provider-Level Pooling:** Separate pools for Deepgram STT and ElevenLabs TTS.
- **Pre-Initialization:** Pools are initialized at server startup (`telephony/server.py`). The gateway blocks until the minimum connection count is reached.
- **Acquisition Protocol:**
    - `OrchestratorFactory` requests a connection.
    - Pool performs a ~10ms health check via "KeepAlive" metadata.
    - If healthy, the warm connection is injected into the call.
    - If the pool is exhausted, the factory falls back to a fresh connection (with a latency warning) or graceful rejection.

### 3.3. Performance Impact
Under simulated 20-call load (`test_prd_harness.py`), the warm-start architecture consistently achieves a **median TTFA of 180ms**, well within the 200ms PRD budget.
