from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional

class BaseTurn(BaseModel):
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
    """Represents a normal conversational exchange."""
    pass

class BargeInTurn(BaseTurn):
    """Represents an interrupted exchange."""
    pass
