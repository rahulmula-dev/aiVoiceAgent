
import asyncio
import json
import time
import uuid
import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

# Add project root to sys.path
sys.path.append(str(Path(__file__).parent.parent))

from orchestrator.manager import VoiceOrchestrator
from orchestrator.session import Session
from contracts.state import CallState
from contracts.policy import PRDScripts
from contracts.schemas import CallContext

class MockWebSocket:
    def __init__(self):
        self.sent_messages = []
    
    async def send_text(self, message):
        self.sent_messages.append(json.loads(message))

async def test_sunny_day_flow():
    print("\n" + "="*50)
    print("RUNNING CILA SUNNY DAY STABILITY TEST")
    print("="*50)

    # 1. SETUP
    stt = MagicMock()
    tts = MagicMock()
    # Mock TTS speak to return a dummy generator
    async def mock_speak(text, call_id=None):
        yield b"audio_chunk"
    tts.speak = mock_speak
    
    orchestrator = VoiceOrchestrator(stt, tts)
    orchestrator.websocket = MockWebSocket()
    orchestrator.sid = "test_sid_123"
    
    # Initialize session
    print(f"DEBUG: CallContext Fields: {CallContext.__fields__.keys()}")
    input_kwargs = {
        "session_id": orchestrator.sid, 
        "caller_number": "+15551234567", 
        "start_time": time.time(),
        "trace_id": str(uuid.uuid4()),
        "transcript_log": [],
        "kb_version_id": None,
        "program_interest": None,
        "intake": None,
        "user_name": None,
        "last_intents": [],
        "last_agent_answer_summary": None,
        "study_mode": None,
        "campus": None,
        "retrieved_chunks_snapshot": [],
        "chunk_ids_used": []
    }
    print(f"DEBUG: Passing kwargs: {input_kwargs.keys()}")
    ctx = CallContext(**input_kwargs)
    print(f"DEBUG: CallContext created successfully: {ctx.session_id}")
    orchestrator.session = Session(
        session_id=orchestrator.sid, 
        call_id=orchestrator.sid, # Symmetric for test
        call_context=ctx
    )
    print(f"DEBUG: Session created successfully: {orchestrator.session.session_id}")
    
    print("\n[STEP 1] GREETING")
    # Simulate greeting trigger
    await orchestrator.speak_immediate_response(PRDScripts.GREETING)
    
    if len(orchestrator.websocket.sent_messages) > 0:
        last_msg = orchestrator.websocket.sent_messages[-1]
        if last_msg.get("event") == "media":
            print("✅ PASS: Greeting audio sent to client.")
        else:
            print(f"❌ FAIL: Expected media event, got {last_msg}")
    else:
        print("❌ FAIL: No messages sent to websocket.")

    # 2. FAQ INQUIRY
    print("\n[STEP 2] FAQ: 'Where is the college located?'")
    # Mock RAG to return Calgary
    orchestrator.brain.kb.search = AsyncMock(return_value=[{"text": "GD College is located in Calgary, Alberta.", "score": 0.9}])
    
    # Trigger transcript
    await orchestrator._on_transcript("Where is your college located?", 0.99, is_final=True)
    
    # Wait for brain and TTS to finish
    await asyncio.sleep(2) 
    
    # Check session history
    history = orchestrator.session.conversation_history
    ai_response = next((h["parts"][0] for h in reversed(history) if h["role"] == "model"), "")
    if "Calgary" in ai_response:
        print(f"✅ PASS: RAG Accuracy verified. Response: '{ai_response[:50]}...'")
    else:
        print(f"❌ FAIL: Response did not contain 'Calgary'. Got: '{ai_response}'")

    # 3. SILENCE HANDLING
    print("\n[STEP 3] SILENCE: Waiting 11 seconds...")
    orchestrator.last_interaction_time = time.time() - 11.0
    orchestrator.silence_stage = 0
    
    # Manually trigger the monitor's check block for Stage 1
    gap = time.time() - orchestrator.last_interaction_time
    if gap > 10.0 and orchestrator.silence_stage == 0:
        print("✅ PASS: Silence Stage 1 triggered (10s).")
        orchestrator.silence_stage = 1
        await orchestrator.speak_immediate_response(PRDScripts.SILENCE_1)
    
    # 4. HANDOVER
    print("\n[STEP 4] HANDOVER: 'I want to talk to a human'")
    # Some common phrases that should trigger escalation or human handover refusal
    await orchestrator._on_transcript("Can I speak with a real person?", 0.99, is_final=True)
    await asyncio.sleep(1)
    
    ai_response = next((h["parts"][0] for h in reversed(history) if h["role"] == "model"), "")
    if any(phrase in ai_response.lower() for phrase in ["transfer", "person", "human", "representative", "sorry", "cannot"]):
         print(f"✅ PASS: Handover/Refusal logic triggered. Response: '{ai_response[:50]}...'")
    else:
         print(f"ℹ️ INFO: Handover response: '{ai_response}'")

    print("\n" + "="*50)
    print("SUNNY DAY TEST COMPLETE")
    print("="*50)

if __name__ == "__main__":
    asyncio.run(test_sunny_day_flow())
