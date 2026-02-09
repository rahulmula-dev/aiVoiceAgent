from pydantic import BaseModel, Field
from typing import List, Optional, Any, Callable
from typing_extensions import Protocol

# Pydantic Schemas (The "Data" moving between modules)

class TranscriptSegment(BaseModel):
    """
    Representation of a piece of transcribed audio.
    Used by STT module to communicate text to Orchestrator.
    """
    text: str = Field(..., description="The recognized text")
    is_final: bool = Field(False, description="Is this a final, corrected transcript?")
    confidence: float = Field(0.0, description="Confidence score from provider (Deepgram)")
    speaker: str = Field("USER", description="Who spoke this segment?")
    timestamp: float = Field(0.0, description="Relative timestamp in stream")

class LLMRequest(BaseModel):
    """
    The full context sent to the LLM Brain.
    """
    prompt: str = Field(..., description="The user's latest query")
    history: List[Any] = Field(default_factory=list, description="Chat history list (Gemini format)")
    rag_context: Optional[str] = Field(None, description="Injected knowledge from RAG search")

class LLMResponse(BaseModel):
    """
    The structured response from the Brain.
    """
    text_content: str = Field(..., description="The full or partial response text")
    sentiment: str = Field("Neutral", description="Detected user sentiment")
    suggested_actions: List[str] = Field(default_factory=list, description="Any function calls or escalation needed")

class CallContext(BaseModel):
    """
    Metadata for the current phone call.
    """
    session_id: str
    caller_number: str
    start_time: float
    transcript_log: List[str] = Field(default_factory=list)

class EscalationEvent(BaseModel):
    """
    When the AI gives up and needs a human.
    """
    reason: str
    target_department: str = "General"
