from typing import AsyncGenerator, Protocol, List, Any, Optional
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
    async def speak(self, text: str) -> AsyncGenerator[bytes, None]: ...
    async def close(self) -> None: ...

class KnowledgeBaseEngine(Protocol):
    """
    Contract for RAG/Knowledge Retrieval.
    (Supported: Pinecone, Weaviate, FAISS)
    """
    def search(self, query: str, top_k: int = 2) -> str: ...

class LLMEngine(Protocol):
    """
    Contract for Large Language Model logic.
    (Supported: Gemini, OpenAI GPT-4, Anthropic Claude)
    """
    def start_new_session(self) -> List[Any]: ...
    
    async def generate_stream(
        self, 
        text: str, 
        history: List[Any]
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
        sentiment: str
    ) -> Any: ...
    
    async def schedule_callback(self, phone_number: str) -> bool: ...

class PolicyEngine(Protocol):
    """
    Contract for safety and compliance filtering.
    """
    def validate_response(self, context: CallContext, response_text: str) -> bool: ...
    
    def check_escalation(self, user_text: str) -> EscalationEvent: ...
