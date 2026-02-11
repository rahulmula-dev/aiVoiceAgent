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

    # --- 3. SPECULATIVE LANGUAGE (Uncertainty Ban) ---
    SPECULATIVE_PHRASES = [
        "maybe", "might", "i think", "i believe", "possibly", "not sure", 
        "i guess", "could be", "probably"
    ]

    def _contains_word(self, text: str, keyword: str) -> bool:
        """
        Helper: Checks if keyword exists in text as a distinct word or substring depending on type.
        For short acronyms (<= 3 chars), use strict word boundary.
        For longer words, use substring matching (safer for variations like 'murderer', 'killing').
        """
        import re
        if len(keyword) <= 3:
            # Word boundary check for short terms like "PR", "sue"
            pattern = r'\b' + re.escape(keyword) + r'\b'
            return re.search(pattern, text) is not None
        else:
            # Substring match for longer distinct terms
            return keyword in text
            
    def _is_english(self, text: str) -> bool:
        """
        Simple heuristic: Ratio of ASCII characters to total length.
        """
        if not text: return True
        try:
            ascii_chars = sum(1 for c in text if ord(c) < 128)
            ratio = ascii_chars / len(text)
            return ratio >= 0.8 # Allow 20% for accents/emojis
        except:
            return True

    def validate_response(self, context: CallContext, response_text: str) -> bool:
        """
        Pre-flight check before TTS speaks.
        Returns True if safe, False if blocked.
        """
        lower_text = response_text.lower()
        
        # 1. Check for confidential/harmful
        for keyword in self.SENSITIVE_KEYWORDS:
            if keyword in lower_text: # Keep strict substring for sensitive (e.g. 'killing')
                return False
                
        # 2. Check strict length (don't ramble)
        if len(response_text) > 500: # Arbitrary token limit for voice
            return False
            
        # 3. Check Speculative Language (Story S1-4)
        for phrase in self.SPECULATIVE_PHRASES:
            if phrase in lower_text:
                return False

        # 4. English Heuristic Check (Story S1-4)
        # Ratio of ASCII characters to total length
        try:
            ascii_chars = sum(1 for c in response_text if ord(c) < 128)
            ratio = ascii_chars / len(response_text)
            if ratio < 0.8: # Allow 20% for names/accents, but block full foreign text
                return False
        except:
            pass # Safety fallback
            
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
        # 0. Check Language (Layer 1 - New)
        # Block non-English input immediately
        if not self._is_english(user_text):
            return "HARD_REFUSAL_LANGUAGE"

        lower = user_text.lower()
        
        # 1. Check Sensitive (Highest Priority)
        for keyword in self.SENSITIVE_KEYWORDS:
            if keyword in lower:
                return "SENSITIVE"
        
        # 2. Check Hard Refusals
        for category, keywords in self.HARD_REFUSAL_KEYWORDS.items():
            for k in keywords:
                # Use smart boundary check for keys like 'pr', 'visa'
                if self._contains_word(lower, k):
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
            
        if intent == "HARD_REFUSAL_LANGUAGE":
            return "I can only speak English. Please ask your question in English."

        return "I am unable to assist with that specific request."
