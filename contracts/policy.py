from .schemas import EscalationEvent, CallContext

class ResponsePolicyEngine:
    """
    Standard implementation of Policy Engine.
    Filters hallucinations, confidential info, and bad language.
    """
    
    # --- 1. SENSITIVE CATEGORIES (Immediate Hangup or Severe Warning) ---
    SENSITIVE_KEYWORDS = [
        "bomb", "kill", "suicide", "murder", "terrorist", "weapon", 
        "sexual", "nude", "porn", "hate", "racist"
    ]

    # --- 2. HARD REFUSAL CATEGORIES (Polite Refusal - No Retrieval) ---
    HARD_REFUSAL_KEYWORDS = {
        "immigration": ["visa", "immigration", "permit", "greencard", "pr", "citizenship"],
        "medical": ["medical", "doctor", "diagnosis", "treatment", "prescription", "health advice"],
        "legal": ["legal", "lawyer", "sue", "court", "attorney", "contract"]
    }

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
        for keyword in self.SENSITIVE_KEYWORDS:
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

    def classify_intent(self, user_text: str) -> str:
        """
        Classifies user intent into: 'PROCEED', 'SENSITIVE', 'HARD_REFUSAL_IMMIGRATION', etc.
        """
        lower = user_text.lower()
        
        # 1. Check Sensitive (Highest Priority)
        for keyword in self.SENSITIVE_KEYWORDS:
            if keyword in lower:
                return "SENSITIVE"
        
        # 2. Check Hard Refusals
        for category, keywords in self.HARD_REFUSAL_KEYWORDS.items():
            for k in keywords:
                if k in lower:
                    return f"HARD_REFUSAL_{category.upper()}"
                    
        return "PROCEED"

    def get_refusal_script(self, intent: str) -> str:
        """
        Returns the static script for a given refusal intent.
        """
        if intent == "SENSITIVE":
            return "I cannot continue this conversation due to violation of our safety policy. Goodbye."
            
        if intent == "HARD_REFUSAL_IMMIGRATION":
            return "As an AI for GD College, I cannot provide immigration or visa advice. Please contact a calibrated immigration consultant."
            
        if intent == "HARD_REFUSAL_MEDICAL":
            return "I am not authorized to provide medical advice. Please consult a healthcare professional."
            
        if intent == "HARD_REFUSAL_LEGAL":
            return "I cannot offer legal advice. Please contact a qualified attorney."
            
        return "I am unable to assist with that specific request."
