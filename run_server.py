"""
run_server.py — thin entry point.

All routes live in telephony/server.py (FastAPI `app`).
All per-call orchestration lives in orchestrator/manager.py (VoiceOrchestrator).

This file just:
  1. Imports `config` (triggers .env load + validates API keys are present)
  2. Imports the FastAPI `app` from telephony.server
  3. Configures and runs uvicorn

Pipeline topology (per call) is documented in orchestrator/manager.py.
"""

import asyncio

import uvicorn

import config
from telephony.server import app
from orchestrator.factory import warmup_pools, init_gate


async def main() -> None:
    print("[MAIN] Starting modular voice pipeline server on port 5000")
    print(f"       STT -> Deepgram {config.DEEPGRAM_MODEL}")
    if config.LLM_PROVIDER == "gemini":
        print(f"       LLM -> Gemini   {config.GEMINI_MODEL}")
    else:
        print(f"       LLM -> Groq     {config.GROQ_MODEL}")
    print(f"       TTS -> ElevenLabs {config.ELEVENLABS_MODEL_ID}")
    if config.CONCURRENCY_GATE_ENABLED:
        print(f"       Gate -> Redis    max {config.MAX_CONCURRENT_CALLS} concurrent calls")

    print("[MAIN] Warming up connection pools...")
    await warmup_pools()
    await init_gate()

    cfg = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=5000,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(cfg)
    print("[MAIN] Waiting for Twilio calls...")
    await server.serve()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[MAIN] Shutting down")
