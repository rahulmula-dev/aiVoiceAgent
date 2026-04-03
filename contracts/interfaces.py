from typing import AsyncGenerator, Protocol, List, Any, Optional, Dict
from .schemas import TranscriptSegment, LLMRequest, LLMResponse, CallContext, EscalationEvent

class STTEngine(Protocol):
    """
    Contract for Speech-To-Text Modules 
    (Supported: Deepgram, AssemblyAI, Vosk, etc.)
    """
    async def connect(self) -> bool: ...
    async def send_audio(self, audio_chunk: bytes) -> None: ...
    async def close(self) -> None: ...
    # The callback should effectively take a TranscriptSegment or text
    # on_transcript_callback: Callable[[TranscriptSegment], Awaitable[None]]

class TTSEngine(Protocol):
    """
    Contract for Text-To-Speech Modules.
    (Supported: Deepgram Aura, ElevenLabs, OpenAI)
    """
    async def speak(self, text: str, call_id: Optional[str] = None) -> AsyncGenerator[bytes, None]: ...
    def stop_current_speech(self, call_id: str) -> str: ...
    async def close(self) -> None: ...

class KnowledgeBaseEngine(Protocol):
    """
    Contract for RAG/Knowledge Retrieval.
    (Supported: PGVector/PostgreSQL)
    """
    def search(self, query: str, top_k: int = 2) -> tuple[str, float]: ...

class LLMEngine(Protocol):
    """
    Contract for Large Language Model logic.
    (Supported: Gemini, OpenAI GPT-4, Anthropic Claude)
    """
    def start_new_session(self) -> List[Any]: ...
    
    async def generate_stream(
        self, 
        text: str, 
        history: List[Any],
        caller_number: Optional[str] = None
    ) -> AsyncGenerator[str, None]: ...
    
    async def generate_response(
        self, 
        text: str, 
        history: Optional[List[Any]]
    ) -> str: ...

class CRMEngine(Protocol):
    """
    Contract for Customer Relationship Management integration.
    (Supported: LeadSquared, Salesforce, HubSpot)
    """
    async def create_ticket(
        self, 
        transcript: str, 
        summary: str, 
        sentiment: str,
        structured_turns: Optional[List[Dict[str, Any]]] = None,
        **kwargs
    ) -> Any: ...

    async def log_call(
        self,
        call_id: str,
        caller_phone: str,
        caller_type: str = "unknown",
        summary: str = "",
        transcript: str = "",
        sentiment: str = "neutral",
        duration_seconds: int = 0
    ) -> Any: ...
    
    async def create_callback(
        self,
        ticket_id: str,
        phone_number: str,
        reason: str,
        preferred_time: str = "ASAP"
    ) -> Any: ...
    


    async def get_ticket_status(self, ticket_id: str) -> dict: ...
    
    async def get_ticket_by_phone(self, phone_number: str) -> dict: ...

class PolicyEngine(Protocol):
    """
    Contract for safety and compliance filtering.
    """
    def validate_response(self, context: CallContext, response_text: str) -> bool: ...
    
    def check_escalation(self, user_text: str) -> EscalationEvent: ...
