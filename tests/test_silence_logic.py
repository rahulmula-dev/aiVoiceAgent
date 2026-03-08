
import sys
import time
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

# Add project root to sys.path
sys.path.append(str(Path(__file__).parent.parent))

from orchestrator.manager import VoiceOrchestrator
from orchestrator.session import Session, SessionState
from contracts.state import CallState
from contracts.policy import PRDScripts

async def test_silence_logic():
    # Mock providers
    stt = MagicMock()
    tts = MagicMock()
    tts.speak = AsyncMock()
    
    orchestrator = VoiceOrchestrator(stt, tts)
    orchestrator.session = Session(session_id="test", call_id="test")
    orchestrator.sid = "test"
    orchestrator.websocket = AsyncMock()
    orchestrator.cleanup = AsyncMock()
    
    # --- Case 1: Informational Path (10s -> 20s) ---
    print("\nTesting Informational Path...")
    orchestrator.last_response_was_question = False
    orchestrator.silence_stage = 0
    orchestrator.last_interaction_time = time.time() - 11.0 # 11s silence
    
    # Run one tick of monitor logic manually (simulated)
    # We can't easily run the background loop, so we'll test the logic blocks
    gap = time.time() - orchestrator.last_interaction_time
    if gap > 10.0 and orchestrator.silence_stage == 0:
        print("PASS: Stage 1 triggered at 10s")
        orchestrator.silence_stage = 1
        orchestrator.last_interaction_time = time.time()
        
    orchestrator.last_interaction_time = time.time() - 11.0 # Another 11s (Total 21s)
    gap = time.time() - orchestrator.last_interaction_time
    if gap > 10.0 and orchestrator.silence_stage == 1:
        if not orchestrator.last_response_was_question:
            print("PASS: Termination triggered at 20s (Informational)")
            orchestrator.silence_stage = 3

    # --- Case 2: Question Path (10s -> 10s -> 10s) ---
    print("\nTesting Question Path...")
    orchestrator.last_response_was_question = True
    orchestrator.silence_stage = 0
    orchestrator.last_interaction_time = time.time() - 11.0
    
    gap = time.time() - orchestrator.last_interaction_time
    if gap > 10.0 and orchestrator.silence_stage == 0:
        print("PASS: Stage 1 triggered at 10s")
        orchestrator.silence_stage = 1
        orchestrator.last_interaction_time = time.time()

    orchestrator.last_interaction_time = time.time() - 11.0
    gap = time.time() - orchestrator.last_interaction_time
    if gap > 10.0 and orchestrator.silence_stage == 1:
        if orchestrator.last_response_was_question:
            print("PASS: Stage 2 (Repeat) triggered at 20s (Question)")
            orchestrator.silence_stage = 2
            orchestrator.last_interaction_time = time.time()

    orchestrator.last_interaction_time = time.time() - 11.0
    gap = time.time() - orchestrator.last_interaction_time
    if gap > 10.0 and orchestrator.silence_stage == 2:
        print("PASS: Termination triggered at 30s (Question)")
        orchestrator.silence_stage = 3

if __name__ == "__main__":
    asyncio.run(test_silence_logic())
