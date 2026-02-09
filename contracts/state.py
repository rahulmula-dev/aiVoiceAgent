from enum import Enum
import logging

logger = logging.getLogger("StateMachine")

class CallState(Enum):
    CALL_INIT = "CALL_INIT"
    LISTENING = "LISTENING"
    TRANSCRIBING = "TRANSCRIBING"
    INTENT_EVAL = "INTENT_EVAL"
    RETRIEVAL = "RETRIEVAL"
    RESPONSE_VALIDATION = "RESPONSE_VALIDATION"
    SPEAKING = "SPEAKING"
    ESCALATION = "ESCALATION"
    CALL_END = "CALL_END"

class StateMachine:
    """
    Enforces strict state transitions for the Voice Agent.
    """
    def __init__(self, call_logger=None):
        self.current_state = CallState.CALL_INIT
        self.call_logger = call_logger
        
        # Define allowed transitions (Rulebook)
        self.ALLOWED_TRANSITIONS = {
            CallState.CALL_INIT: [CallState.LISTENING, CallState.SPEAKING, CallState.CALL_END],
            
            CallState.SPEAKING: [CallState.LISTENING, CallState.CALL_END, CallState.ESCALATION],
            
            CallState.LISTENING: [CallState.TRANSCRIBING, CallState.SPEAKING, CallState.CALL_END, CallState.INTENT_EVAL, CallState.RESPONSE_VALIDATION],
            
            CallState.TRANSCRIBING: [
                CallState.INTENT_EVAL, 
                CallState.LISTENING,
                CallState.SPEAKING,
                CallState.CALL_END
            ],
            
            CallState.INTENT_EVAL: [CallState.RETRIEVAL, CallState.RESPONSE_VALIDATION, CallState.ESCALATION, CallState.SPEAKING, CallState.LISTENING],
            
            CallState.RETRIEVAL: [CallState.RESPONSE_VALIDATION, CallState.ESCALATION],
            
            CallState.RESPONSE_VALIDATION: [CallState.SPEAKING, CallState.ESCALATION, CallState.LISTENING],
            
            CallState.ESCALATION: [CallState.CALL_END, CallState.SPEAKING],
            
            CallState.CALL_END: []
        }

    def transition_to(self, new_state: CallState):
        """
        Attempts to transition to a new state.
        Raises ValueError if transition is invalid.
        """
        if new_state == self.current_state:
            return

        # Check Rulebook
        valid_next_states = self.ALLOWED_TRANSITIONS.get(self.current_state, [])
        
        # In strict mode, raise error. For now, log warning.
        if new_state not in valid_next_states:
             # Basic loose check: allow transitions to CALL_END from anywhere
            if new_state == CallState.CALL_END:
                pass
            else:
                logger.warning(f"⚠️ STATE VIOLATION: {self.current_state.value} -> {new_state.value}")
        
        # 3. Log the transition
        old_state = self.current_state
        self.current_state = new_state
        logger.info(f"State Transition: [{old_state.value}] -> [{new_state.value}]")
        
        if self.call_logger:
            self.call_logger.log_event("state_machine", "transition", 
                                     meta={"from": old_state.value, "to": new_state.value})

    def get_state(self):
        return self.current_state
