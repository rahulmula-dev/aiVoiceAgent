"""
models/schemas.py — Shared Pydantic Data Models for Conversation Turns

Defines the schemas (`BaseTurn`, `StandardTurn`, `BargeInTurn`) that represent
a single conversational turn in the voice-agent session.

Why these exist:
  - The orchestrator attaches per-turn metadata at runtime (topic, status,
    barge-in classification, etc.).
  - The CRM client serializes turns into JSON for CRM tickets.
  - The session model maintains a typed history list.
  - Test and audit tooling can analyse turn-level data programmatically.

`StandardTurn` and `BargeInTurn` currently share the same fields via
`BaseTurn`. They are kept as separate classes so:
  1. Type annotations can distinguish turn types in pattern-matching code.
  2. Either class can grow type-specific fields later without breaking the
     other (e.g. `BargeInTurn` may gain `partial_audio_bytes`).

Ported verbatim from the company project.
"""

from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional


class BaseTurn(BaseModel):
    """
    Base schema for a single conversation turn.

    Every turn — normal exchange or barge-in — records these common fields.
    Subclasses (`StandardTurn`, `BargeInTurn`) inherit all fields and may
    extend them.

    Field reference:
      turn_id                 : 1-based sequential ID assigned by the orchestrator.
      caller_input            : Verbatim STT transcript for this turn (None for
                                agent-initiated turns like the greeting).
      topic                   : RAG category / intent label, used for CRM dashboard
                                filtering and analytics.
      agent_response_status   : "completed" | "interrupted" | "abandoned"
      agent_partial_response  : Text spoken before interruption; populated only when
                                ``agent_response_status == "interrupted"``.
      barge_in_classification : "NEW_TOPIC" | "SAME_TOPIC" | "AMBIGUOUS" | None
      is_multi_step           : True if the agent's response was a structured,
                                multi-step answer (numbered list, sequential steps).
      continuation_offered    : True if the soft continuation prompt was appended
                                after a NEW_TOPIC barge-in on a multi-step answer.
      timestamp               : ISO-8601 timestamp of when the turn was created.
    """

    turn_id: int
    caller_input: Optional[str] = None
    topic: Optional[str] = None
    agent_response_status: str = "completed"
    agent_partial_response: Optional[str] = None
    barge_in_classification: Optional[str] = None
    is_multi_step: bool = False
    continuation_offered: bool = False
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


class StandardTurn(BaseTurn):
    """
    A normal, uninterrupted conversational exchange.

    The caller spoke, the agent retrieved context, generated a response,
    and delivered it via TTS without interruption.

    Typical ``agent_response_status`` value: ``"completed"``.
    """
    pass


class BargeInTurn(BaseTurn):
    """
    A conversational turn where the caller interrupted the agent mid-speech.

    Recorded whenever the orchestrator's barge-in handler processes a caller
    interruption. The ``barge_in_classification`` field captures the Brain's
    assessment of whether the caller switched topics, sought clarification,
    or had ambiguous intent.

    Typical ``agent_response_status`` value: ``"interrupted"`` or ``"abandoned"``.
    """
    pass
