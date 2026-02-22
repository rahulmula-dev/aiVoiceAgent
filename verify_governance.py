import asyncio
import os
import json
import logging
from unittest.mock import MagicMock, AsyncMock

# Mock dependencies before imports
import sys
crm_mock_mod = MagicMock()
crm_mock_client = MagicMock()
crm_mock_client.create_ticket = AsyncMock()
crm_mock_client.log_call = AsyncMock()
crm_mock_mod.CRMClient.return_value = crm_mock_client
sys.modules['crm.client'] = crm_mock_mod

sys.modules['agent_logging'] = MagicMock()
sys.modules['audit_logging.recorder'] = MagicMock()

from orchestrator.manager import VoiceOrchestrator
from contracts.policy import PRDScripts
from contracts.state import CallState
from contracts.schemas import CallContext

async def test_governance_strikes():
    print("\n--- Testing 2-Strike Warning System ---")
    
    # 1. Setup Orchestrator with Mocks
    stt = MagicMock()
    tts = AsyncMock()
    # Mock tts.speak to return an async generator
    async def mock_speak(text):
        yield b"audio_chunk"
    tts.speak.side_effect = mock_speak
    
    manager = VoiceOrchestrator(stt_provider=stt, tts_provider=tts)
    manager.websocket = AsyncMock()
    manager.session = MagicMock()
    manager.session.call_context = CallContext(
        session_id="test_session",
        caller_number="1234567890",
        start_time="2026-02-22T23:00:00"
    )
    manager.session.conversation_history = []
    
    # 2. Strike 1: "Hola, ¿cómo estás?" (Non-English)
    print("Action: Sending 'Hola, ¿cómo estás?' (Strike 1)")
    await manager._on_transcript("Hola, ¿cómo estás?", confidence=1.0)
    await asyncio.sleep(0.1) # Wait for background task
    print(f"Result: Strike Count = {manager.language_strike_count}")
    assert manager.language_strike_count == 1
    # Check if correct refusal was used (via mock call analysis or looking at history)
    last_msg = manager.session.conversation_history[-1]['parts'][0]
    print(f"AI Response: {last_msg}")
    assert last_msg == PRDScripts.REFUSAL_LANGUAGE_1

    # 3. Reset Check: "Tell me about computer science" (English)
    print("\nAction: Sending 'Tell me about computer science' (Valid English)")
    await manager._on_transcript("Tell me about computer science", confidence=1.0)
    print(f"Result: Strike Count = {manager.language_strike_count}")
    assert manager.language_strike_count == 0

    # 4. Strike 1 again: "Comment ça va?"
    print("\nAction: Sending 'Comment ça va?' (Strike 1)")
    await manager._on_transcript("Comment ça va?", confidence=1.0)
    assert manager.language_strike_count == 1

    # 5. Strike 2: "Donde esta la biblioteca"
    print("\nAction: Sending 'Donde esta la biblioteca' (Strike 2)")
    await manager._on_transcript("Donde esta la biblioteca", confidence=1.0)
    await asyncio.sleep(0.1) # Wait for background task
    print(f"Result: Strike Count = {manager.language_strike_count}")
    assert manager.language_strike_count == 2
    last_msg = manager.session.conversation_history[-1]['parts'][0]
    print(f"AI Response: {last_msg}")
    assert last_msg == PRDScripts.REFUSAL_LANGUAGE_2

    # 6. Strike 3: "Auf Wiedersehen" (Termination)
    print("\nAction: Sending 'Auf Wiedersehen' (Strike 3 - Termination)")
    # Need to mock cleanup to avoid real side effects in unit test if necessary
    manager.cleanup = AsyncMock()
    
    await manager._on_transcript("Auf Wiedersehen", confidence=1.0)
    
    # Wait a bit for the async task to trigger
    await asyncio.sleep(0.5)
    
    print(f"Result: Strike Count = {manager.language_strike_count}")
    assert manager.language_strike_count == 3
    assert manager.state.get_state() == CallState.CALL_END
    manager.cleanup.assert_called_once()
    print("SUCCESS: Call terminated gracefully after 3rd strike.")

if __name__ == "__main__":
    asyncio.run(test_governance_strikes())
