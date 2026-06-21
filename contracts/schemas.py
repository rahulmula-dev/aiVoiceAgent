"""
contracts/schemas.py
=====================
Pydantic data schemas — the canonical data types that flow between modules.

Defines five Pydantic ``BaseModel`` classes that represent the primary data
objects exchanged between the voice agent's subsystems:

  - ``TranscriptSegment`` : A piece of transcribed audio from STT.
  - ``LLMRequest``        : The full context bundle sent to the LLM Brain.
  - ``LLMResponse``       : The structured response returned by the Brain.
  - ``CallContext``       : Per-call metadata and persistent memory.
  - ``EscalationEvent``   : Trigger object for human-agent handoff.

Why a separate schemas module (vs. interfaces.py):
  1. Single source of truth — every module that needs a ``CallContext``
     imports it from here. Changing a field in one place updates it
     everywhere.
  2. Validation at boundaries — Pydantic validates field types and applies
     defaults when objects are constructed, catching type errors at creation
     time rather than deep inside business logic.

Ported from the company project (Chakraview LABS / ai-voice-agentdev) with
LLMRequest.history kept permissive so it accepts either Gemini's
``{"role": "user", "parts": [...]}`` shape or OpenAI's
``{"role": "user", "content": "..."}`` shape — the concrete LLM provider
swap happens later (Step 8 in the parity roadmap).
"""

from pydantic import BaseModel, Field
from typing import List, Optional, Any


class TranscriptSegment(BaseModel):
    """
    One piece of transcribed audio from the STT engine.

    Produced by the STT adapter (e.g. DeepgramSTTEngine) and consumed by the
    orchestrator's transcript callback. The ``is_final`` flag distinguishes
    streaming interim results from the final, corrected transcript that
    should be forwarded to the LLM.
    """
    text: str = Field(..., description="The recognized text")
    is_final: bool = Field(False, description="Is this a final, corrected transcript?")
    confidence: float = Field(0.0, description="Confidence score from provider (0.0-1.0)")
    speaker: str = Field("USER", description="Who spoke this segment? Always 'USER' for inbound")
    timestamp: float = Field(0.0, description="Relative timestamp in stream (seconds)")


class LLMRequest(BaseModel):
    """
    The full context bundle sent to the LLM Brain for response generation.

    Built by the orchestrator after the STT transcript is received and the
    RAG search has completed. The Brain uses all three fields to construct
    the prompt that is sent to the LLM API.

    Note: ``history`` is intentionally typed as ``List[Any]`` so it can carry
    either Gemini-format messages (``{"role": ..., "parts": [...]}``) or
    OpenAI-format messages (``{"role": ..., "content": "..."}``). Tighten this
    when the LLM provider is finalised (Step 8 in the parity roadmap).
    """
    prompt: str = Field(..., description="The user's latest query")
    history: List[Any] = Field(
        default_factory=list,
        description="Chat history in the provider's native format",
    )
    rag_context: Optional[str] = Field(
        None,
        description="Injected knowledge from RAG search; None if disabled or no hits",
    )


class LLMResponse(BaseModel):
    """
    Structured response returned by the LLM Brain to the orchestrator.

    The orchestrator uses ``text_content`` for TTS, ``sentiment`` for CRM
    ticket creation, and ``suggested_actions`` to trigger downstream
    workflows (escalation, callbacks).
    """
    text_content: str = Field(..., description="The full or partial response text")
    sentiment: str = Field(
        "Neutral",
        description="Detected user sentiment: Positive, Negative, Neutral, Frustrated",
    )
    suggested_actions: List[str] = Field(
        default_factory=list,
        description="Action strings the Brain recommends, e.g. ['escalate']",
    )


class CallContext(BaseModel):
    """
    Metadata and persistent memory for a single phone call.

    Created at call start and updated throughout the session. Passed to the
    PolicyEngine, Brain, and CRM adapter. Persistent-memory fields allow the
    agent to maintain coherent context across turns without re-asking for
    information the caller already provided.

    Field groups:
      - Core identity: session_id, caller_number, start_time
      - Persistent memory: program_interest, intake, user_name, last_intents,
        last_agent_answer_summary, study_mode, campus
      - RAG tracking: retrieved_chunks_snapshot, chunk_ids_used
    """
    # ── Core identity ─────────────────────────────────────────────────────
    # session_id accepts either a UUID (company convention) or the Twilio
    # streamSid that the clean build currently uses — both are unique per call.
    session_id: str
    caller_number: str
    start_time: float                                       # Unix timestamp
    transcript_log: List[str] = Field(default_factory=list) # Running transcript
    trace_id: Optional[str] = Field(
        None,
        description="Unique ID for request tracing across subsystems",
    )
    kb_version_id: Optional[str] = Field(
        None,
        description="Knowledge Base version identifier active during this call",
    )

    # ── Persistent Memory (populated progressively as caller reveals info) ─
    program_interest: Optional[str] = None             # e.g. "Esthetician Diploma"
    intake: Optional[str] = None                       # e.g. "September 2026"
    user_name: Optional[str] = None                    # e.g. "Priya"
    last_intents: List[str] = Field(default_factory=list)
    last_agent_answer_summary: Optional[str] = None    # Prevents repetition
    study_mode: Optional[str] = None                   # "full-time" or "part-time"
    campus: Optional[str] = None                       # e.g. "Calgary"

    # ── RAG tracking ──────────────────────────────────────────────────────
    retrieved_chunks_snapshot: List[str] = Field(default_factory=list)
    chunk_ids_used: List[str] = Field(
        default_factory=list,
        description="IDs of all KB chunks used across this call (for audit)",
    )


class EscalationEvent(BaseModel):
    """
    Trigger object for a human-agent handoff.

    Created by the PolicyEngine when the caller requests a human, uses a
    sensitive keyword, or the agent is unable to answer after retries.
    Consumed by the orchestrator to initiate CRM ticket creation and the
    call-termination flow.
    """
    reason: str
    target_department: str = "General"
