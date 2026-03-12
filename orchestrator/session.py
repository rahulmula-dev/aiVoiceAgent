from enum import Enum
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from datetime import datetime
from contracts.schemas import CallContext

class SessionState(str, Enum):
    NEW = "NEW"
    LISTENING = "LISTENING"
    THINKING = "THINKING"
    SPEAKING = "SPEAKING"
    INTERRUPTED = "INTERRUPTED"
    ENDED = "ENDED"

class RetrievalChunk(BaseModel):
    text: str
    source: str
    score: float

class Session(BaseModel):
    # Identity (Pillar 1)
    session_id: str
    call_id: str
    crm_call_id: Optional[str] = None
    caller_number: str = "unknown"
    caller_type: str = "unknown_lead"
    metadata: Dict[str, Any] = Field(default_factory=dict)
    start_time: datetime = Field(default_factory=datetime.now)
    end_time: Optional[datetime] = None
    last_active: datetime = Field(default_factory=datetime.now)
    
    # State Machine (Pillar 2)
    current_state: SessionState = SessionState.NEW
    
    # Memory & Context (Pillar 1)
    call_context: CallContext = Field(default_factory=lambda: CallContext(session_id="temp", caller_number="unknown", start_time=0.0))
    conversation_history: List[Dict[str, Any]] = []
    retrieved_chunks_cache: List[RetrievalChunk] = []
    last_intent: Optional[str] = None
    
    # Barge-In Snapshot (Pillar 1)
    interruption_snapshot: Optional[Dict[str, Any]] = None
    structured_turns: List[Dict[str, Any]] = []
    current_speaking_turn_id: Optional[int] = None

    # Resilience Tracking (Pillar 2 & 3)
    termination_reason: str = "normal"
    sentiment_label: str = "Neutral"
    confidence_scores: List[float] = []

    # Governance: persist language warnings across the call (Phase 1 English-only)
    language_warning_count: int = 0

    # Stream Buffering (Task 10-11): Pre-fetch RAG context tasks (Exclude from persistence)
    prefetched_context_task: Optional[Any] = Field(None, exclude=True)

    class Config:
        arbitrary_types_allowed = True

    def touch(self):
        """Update last_active timestamp for TTL."""
        self.last_active = datetime.now()
