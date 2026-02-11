# 🎙️ AI Voice Agent (v2 Flash Lite Release)

A production-ready AI Voice Agent for GD College, built with Python, FastAPI, and Twilio.
Certified "Antigravity Compliant" for modularity, observability, and resilience.

## 🚀 Key Features

- **Architecture**: Fully modular (`/telephony`, `/orchestrator`, `/stt`, `/tts`, `/retrieval`).
- **Intelligence**: Powered by Google Gemini (`gemini-flash-lite-latest`) with RAG.
- **Observability**: Structured JSON logging with `trace_id` and `latency_ms`.
- **Hygiene**: "Silent Mode" (Zero console noise) & Safe Cleanup patterns.
- **Reliability**: Robust error handling and quota management.

## 📂 Project Structure

```
/src
├── telephony/       # Twilio WebSocket Server (FastAPI)
├── orchestrator/    # Core Logic (Brain, Manager, Factory)
├── stt/             # Speech-to-Text (Deepgram Nova-2)
├── tts/             # Text-to-Speech (Deepgram Aura)
├── retrieval/       # RAG Knowledge Base (Pinecone)
├── logging/         # Centralized Logging Module
└── crm/             # CRM Integration (LeadSquared)
```

  **Install dependencies**:
    ```bash
    pip install -r requirements.txt
    ```

  **Configure Environment**:
    Create a `.env` file with:
    ```ini
    DEEPGRAM_API_KEY=your_key
    GEMINI_API_KEY=your_key
    PINECONE_API_KEY=your_key
    TWILIO_ACCOUNT_SID=your_sid
    TWILIO_AUTH_TOKEN=your_token
    LEADSQUARED_ACCESS_KEY=your_key
    NGROK_URL=your_ngrok_url
    ```

 **Run the Server**:
    ```bash
    python run_server.py
    ```

  **Expose Localhost**:
    ```bash
    ngrok http 8085
    ```

## 📊 Logging

Logs are written to `logs/`:
- `voice_agent.log`: Full structured JSON logs.
- `call_summary.log`: One-line summary per call.
- `logs/call_{id}.json`: Individual call session logs.

## 🏆 Certification

This project has passed the **Master Architecture Audit** (v2 Release Candidate).
- **Architecture**: Modular & Decoupled.
- **Zero-Print**: Core logic is free of `print()` statements.
- **Session-Aware**: All logs are traceable.

---
**Status**: 🟢 PRODUCTION READY
