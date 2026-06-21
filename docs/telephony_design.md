# Telephony Layer Design Spec v1.0

## 1. Introduction
This document describes the telephony layer of the GD College AI Voice Agent (CILA). The layer is designed for high-concurrency (30 concurrent calls) and low-latency interaction.

## 2. Core Architecture
The telephony layer is built upon a WebSocket-based streaming architecture, connecting Twilio Media Streams to the Voice Orchestrator.

### 2.1. Key Components
- **Voice Gateway:** FastAPI WebSocket endpoint (`telephony/server.py`).
- **Voice Orchestrator:** The central state machine managing call flow (`orchestrator/manager.py`).
- **Connection Pools:** Persistent resources for STT and TTS services (`stt/stt_pool.py`, `tts/elevenlabs_pool.py`).

---

## 3. Persistent Connection Pool Lifecycle
The system maintains a pool of warm WebSocket connections to eliminate the handshake latency during call ingestion.

### 3.1. Startup Phase (Pre-Call)
- **Initialization:** Upon `run_server.py` startup, the `startup_event` in `server.py` triggers `stt_pool.initialize()` and `elevenlabs_pool.initialize()`.
- **Parallel Handshake:** Connections are opened concurrently to reach the `MIN_CONNECTIONS` threshold (e.g., 5 for STT, 10 for TTS).
- **Blocking Guard:** The server will not start accepting calls until the minimum pool density is reached, ensuring no "cold" first calls.

### 3.2. Acquisition Phase (Call Start)
- When a call connects (Twilio Media Stream `start` event), the `OrchestratorFactory` calls `stt_pool.acquire()`.
- The pool follows a **Last-In, First-Out (LIFO)** strategy to ensure the most recently used (and likely healthy) connections are handed out first.
- **Fast-Pass Connectivity:** Since the connection is pre-established, the orchestrator begins streaming audio to Deepgram within 10ms of the call connection.

### 3.3. Call Duration (Maintenance)
- **Heartbeat Guard:** A background `_heartbeat_loop` in the `Transcriber` class sends periodic JSON `{"type": "KeepAlive"}` packets every 10 seconds to satisfy the vendor's inactivity timeout (e.g., 12s for Deepgram).
- **Proactive Health Monitor:** A dedicated background thread in the pool periodically polls idle connections via a "KeepAlive" metadata event. Dead connections are silently replaced.

### 3.4. Release Phase (Call Termination)
- When the call ends, the orchestrator calls `transcriber.close()` and `synthesizer.close()`.
- **Resource Recycle:** Instead of terminating the WebSocket, the `PooledTranscriber` proxy intercepts the close signal and returns the connection to the `stt_pool.release()`.
- **Atomic Reset:** Before being put back in the idle queue, the connection's state (callbacks, buffers) is reset using `reset_state()`, ensuring zero data leakage between callers.

---

## 4. Concurrency & Failure Modes
The system implements a strict **30-call hard cap** to ensure predictable performance and resource availability. This is enforced through a two-layer atomic safety gate system.

### 4.1. Layered Concurrency Enforcement
The architecture uses two independent but consistent admission gates to prevent "zombie" sessions and slot leakage during high-concurrency bursts.

1.  **Primary Admission Gate (`telephony/server.py`):**
    - Executes immediately upon the Twilio HTTPS `/voice` webhook.
    - Uses `increment_if_under_cap()` (Redis Lua) to atomically check the limit and claim a slot.
    - If rejected here, the caller hears a "Lines busy" TwiML message and the call never reaches the WebSocket stage.

2.  **Secondary Authoritative Backstop (`orchestrator/manager.py`):**
    - Executes during the WebSocket `media-stream` handshake.
    - Uses `is_over_capacity_atomic()` (Redis Lua) to verify the call is still within the valid set of active SIDs.
    - This layer handles the TOCTOU (Time-Of-Check to Time-Of-Use) race window between the TwiML response and the persistent connection upgrade, ensuring that if multiple calls bypass the primary gate due to a network glitch or Redis restart, the orchestrator acts as a final authoritative barrier.

### 4.2. Atomic Logic (Redis Lua)
Both gates rely on shared Lua scripts in `telephony/concurrency.py` to guarantee atomicity:
- **`LUA_INCREMENT_IF_UNDER_CAP`**: Atomic compare-and-increment.
- **`LUA_CHECK_CAPACITY_ATOMIC`**: Atomic membership check + capacity verification.
- **`LUA_DECREMENT_ONLY_IF_TRACKED`**: Atomic decrement that prevents "counter-leakage" if a non-tracked call ends.

### 4.3. Failure Modes
- **Hard Cap Rejection:** If the pool is exhausted or the capacity check fails at either gate, the call is rejected.
- **Graceful Rejection:** Rejected calls trigger the `CRM_FAILOVER` logic, playing a "Lines busy" message and creating a high-priority callback ticket in the CRM.
- **Redis Offline:** In local development or during Redis outages, the system fails over to shared `asyncio.Lock` protected RAM counters, maintaining consistency within a single instance.
