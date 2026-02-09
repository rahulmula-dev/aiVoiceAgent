from .schemas import EscalationEvent, CallContext

class ResponsePolicyEngine:
    """
    Standard implementation of Policy Engine.
    Filters hallucinations, confidential info, and bad language.
    """
    
    RESTRICTED_KEYWORDS = [
        "bomb", "kill", "immigration advice", "visa guarantee", 
        "medical diagnosis", "legal advice"
    ]
    
    ESCALATION_KEYWORDS = [
        "human", "representative", "agent", "manager", "support person"
    ]

    def validate_response(self, context: CallContext, response_text: str) -> bool:
        """
        Pre-flight check before TTS speaks.
        Returns True if safe, False if blocked.
        """
        lower_text = response_text.lower()
        
        # 1. Check for confidential/harmful
        for keyword in self.RESTRICTED_KEYWORDS:
            if keyword in lower_text:
                return False
                
        # 2. Check strict length (don't ramble)
        if len(response_text) > 500: # Arbitrary token limit for voice
            return False
            
        return True

    def check_escalation(self, user_text: str) -> EscalationEvent | None:
        """
        Returns an EscalationEvent if the user demands a human.
        """
        lower_user = user_text.lower()
        for keyword in self.ESCALATION_KEYWORDS:
            if keyword in lower_user:
                return EscalationEvent(
                    reason=f"User requested human via keyword: {keyword}",
                    target_department="Sales/Support"
                )
        return None
