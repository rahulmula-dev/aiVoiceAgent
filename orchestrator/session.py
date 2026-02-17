from enum import Enum
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from datetime import datetime

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
    caller_number: str = "unknown"
    start_time: datetime = Field(default_factory=datetime.now)
    end_time: Optional[datetime] = None
    last_accessed: datetime = Field(default_factory=datetime.now)
    
    # State Machine (Pillar 2)
    current_state: SessionState = SessionState.NEW
    
    # Memory & Context (Pillar 1)
    conversation_history: List[Dict[str, str]] = []
    retrieved_chunks_cache: List[RetrievalChunk] = []
    last_intent: Optional[str] = None
    
    # Barge-In Snapshot (Pillar 1)
    interruption_snapshot: Optional[Dict[str, Any]] = None

    # Resilience Tracking (Pillar 2 & 3)
    termination_reason: str = "normal"

    class Config:
        arbitrary_types_allowed = True

    def touch(self):
        """Update last_accessed timestamp for TTL."""
        self.last_accessed = datetime.now()
