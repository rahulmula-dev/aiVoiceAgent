
import asyncio
import time
import os
from orchestrator.manager import VoiceOrchestrator, LatencyBreachError
from orchestrator.brain import Brain
from contracts.policy import PRDScripts

class MockTranscriber:
    def set_callback(self, cb): self.cb = cb
    async def connect(self): return True
    def close(self): pass

class MockSynthesizer:
    async def speak(self, text):
        print(f"[TTS] Speaking: {text}")
        yield b"audio_chunk"
    async def close(self): pass

class MockContext:
    def __init__(self):
        self.last_intents = []
        self.program_interest = "Nursing"
        self.intake = None
        self.user_name = "Test"
        self.study_mode = None
        self.campus = None

class MockSession:
    def __init__(self):
        self.session_id = "test_lat_123"
        self.current_state = "active"
        self.caller_number = "12345"
        self.conversation_history = []
        self.crm_call_id = "crm_call_123"
        self.call_context = MockContext()
        self.termination_reason = None
    def touch(self): pass

class MockCRM:
    async def create_ticket(self, **kwargs):
        print(f"[CRM] Ticket Created: {kwargs.get('title')} - {kwargs.get('summary')}")
        return {"status": "success", "ticket_id": "TKT-LAT-999"}
    async def log_call(self, **kwargs): return "crm_call_123"

async def test_latency_breach():
    print("--- Testing Latency Circuit Breaker (5s) ---")
    
    # Setup Orchestrator with Mocks
    orch = VoiceOrchestrator(MockTranscriber(), MockSynthesizer())
    orch.crm = MockCRM()
    orch.session = MockSession()
    
    # Mock Brain to simulate 6s delay
    async def slow_stream(text, history, **kwargs):
        print("[Brain] Starting slow RAG (6s delay)...")
        await asyncio.sleep(6.0) # Hit 5s limit
        yield ("Too Late!", {"rag_score": 0.5})
    
    orch.brain.generate_stream = slow_stream
    
    # Simulate user transcript receipt
    # turn_start_time will be taken inside _on_transcript
    print("Simulating User Transcript: 'Help me'...")
    try:
        # We call _on_transcript directly
        await orch._on_transcript("Help me", confidence=1.0, stt_latency=0.5, is_final=True)
        # Wait for the background task to actually run and hit the latency breach
        if orch.response_task:
            await orch.response_task
    except Exception as e:
        print(f"Caught top-level error: {e}")

    await asyncio.sleep(1.0) # Wait for background CRM/Cleanup tasks
    
    if orch.session.termination_reason == "latency_breach":
        print("PASS: Session termination reason set correctly.")
    else:
        print(f"FAIL: Termination reason is {orch.session.termination_reason}")

if __name__ == "__main__":
    asyncio.run(test_latency_breach())
