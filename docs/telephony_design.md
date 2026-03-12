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
- **Hard Cap Enforcement:** The pool size (e.g., 30) acts as a physical ceiling. If the pool is exhausted and the "lifeboat" fresh connection also fails, the call is rejected.
- **Graceful Rejection:** Rejected calls trigger the `CRM_FAILOVER` logic, playing a "Lines busy" message and creating a callback ticket.
