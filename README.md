# 🎙️ CILA: AI Voice Agent

A production-ready AI Voice Agent for GD College, designed for high-performance telephony and automated admission support.

## � Simple Flow

```mermaid
sequenceDiagram
    participant User
    participant Twilio
    participant Orchestrator
    participant Brain
    participant TTS
    
    User->>Twilio: Makes Phone Call
    Twilio->>Orchestrator: Streams Audio (WebSocket)
    Orchestrator->>Brain: Transcribes & Processes Query
    Brain-->>Orchestrator: Generates Response (RAG)
    Orchestrator->>TTS: Streams Audio Chunks
    TTS-->>Twilio: Plays Audio back to User
```

## 🚀 Quick Start

1. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Run the Server**:
   ```bash
   python run_server.py
   ```

3. **Expose to Twilio**:
   ```bash
   ngrok http 8085
   ```

---
**Status**: 🟢 PRODUCTION READY
