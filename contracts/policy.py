from .schemas import EscalationEvent, CallContext
import logging

class PRDScripts:
    # Greetings
    GREETING = "Hello! I am CILA from GD College."
    GREETING_TEXT = "Hello! I am CILA from GD College. (Text Mode)"
    
    # Refusals
    REFUSAL_SENSITIVE = "I cannot continue this conversation due to a violation of our safety policy. Goodbye."
    REFUSAL_IMMIGRATION = "As an AI for GD College, I cannot provide immigration or visa advice. Please contact a specialized consultant."
    REFUSAL_MEDICAL = "I am not authorized to provide medical advice. Please consult a healthcare professional."
    REFUSAL_LEGAL = "I cannot offer legal advice. Please contact a qualified attorney."
    REFUSAL_INTERNAL_STAFF = "I cannot discuss internal staff or HR matters."
    REFUSAL_POLITICS = "I cannot discuss political opinions."
    REFUSAL_COMPETITORS = "I can only provide information about GD College and cannot compare us with other institutions."
    REFUSAL_FINANCIAL_DISPUTES = "I cannot assist with fee disputes or refund policies over the phone. A human agent will follow up to assist you."
    REFUSAL_LANGUAGE = "I am currently designed to support English only. Please contact the GD College admissions team for assistance."
    # Task 3: Hard Language Refusal Scripts
    REFUSAL_LANGUAGE_1 = "I am currently designed to support English only. Please continue in English."
    REFUSAL_LANGUAGE_2 = "I can only understand English. If the next input is not in English, I will have to end the call."
    REFUSAL_LANGUAGE_3 = "I am ending the call now as I can only assist in English. Goodbye."
    REFUSAL_KB_MISS = "I do not have that information. A member of the GD College admissions team will follow up."
    REFUSAL_DEFAULT = "I am unable to assist with that specific request. Please contact the GD College admissions team."
    
    # Apologies
    APOLOGY_CLARIFICATION = "I didn't quite catch that. Could you please repeat?"
    APOLOGY_OVERLOADED = "I am currently overloaded with requests. Please try again in a few seconds."
    APOLOGY_CAPACITY = "I am currently at capacity, please try again later."
    APOLOGY_FATAL = "I am having technical trouble. Please wait while reconnecting or try calling back later. Goodbye."
    APOLOGY_INTERNAL_ERROR = "I am having a moment of silence. Please try again later."
    APOLOGY_STRUCTURAL_UPDATE = "I am currently undergoing a structural update. Check back in a few minutes!"

    # Latency Fallback
    LATENCY_FALLBACK = "I am experiencing a system delay, I will have a human agent follow up with you. Goodbye."

    # Escalation
    ESCALATION = "I apologize for the frustration. I will create a ticket so a human team member can follow up with you. Goodbye."

    # Silence
    SILENCE_1 = "Are you still there?"
    SILENCE_2 = "I haven't heard from you for a while. I will have to end the call soon if you don't respond."
    SILENCE_TERMINATION = "Disconnecting due to silence. Goodbye."

    # Interruption
    INTERRUPTION = "Should I continue from where I left off?"

class ResponsePolicyEngine:
    """
    Standard implementation of Policy Engine.
    Filters hallucinations, confidential info, bad language, and enforces PRD tone.
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
        "legal": ["legal", "lawyer", "sue", "court", "attorney", "contract"],
        "internal_staff": ["salary", "hr", "staff issues", "employee", "paycheck", "hiring"],
        "politics": ["politics", "political", "election", "government opinion", "democrat", "republican", "liberal", "conservative"],
        "competitors": ["better than", "worse than", "compare to", "vs", "versus", "other college", "other university"],
        "financial_disputes": ["fee dispute", "refund policy", "want my money back", "stole my money", "overcharged"]
    }

    ESCALATION_KEYWORDS = [
        "human", "representative", "agent", "manager", "support person"
    ]

    # --- 3. SPECULATIVE LANGUAGE (Uncertainty Ban) ---
    SPECULATIVE_PHRASES = [
        "maybe", "might", "i think", "i believe", "possibly", "not sure", 
        "i guess", "could be", "probably"
    ]

    # --- 4. TONE & PERSONALITY (Governance Validation - PRD S4-5) ---
    RUDE_KEYWORDS = [
        "stupid", "idiot", "dumb", "shut up", "crazy", "moron", "fool"
    ]

    PERSUASIVE_KEYWORDS = [
        "you must buy", "act now", "guaranteed", "limited time offer",
        "don't miss out", "buy now", "click here", "subscribe now", "special offer",
        "must enroll", "sign up immediately"
    ]

    # --- 5. LANGUAGE DETECTION (Story S1-4) ---
    COMMON_ENGLISH_WORDS = {
        "ok", "okay", "fine", "yes", "yup", "no", "mhm", "hello", "hi", 
        "can", "you", "about", "your", "more", "tell", "me", "the", "be", 
        "what", "where", "how", "when", "why", "this", "that", "these", 
        "those", "good", "bad", "thanks", "thank", "please", "help"
    }

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
            
    def _is_english(self, text: str, detected_lang: str = None) -> bool:
        """
        [GOVERNANCE] Bulletproof Failsafe English Detection (Expert Debugger Version).
        Hardened to handle non-Latin characters (Hindi/Bengali) without crashing.
        """
        # 0. Authoritative STT Metadata Guard (Fixes "English Hallucinations" bypass)
        if detected_lang and detected_lang != 'en':
            return False
            
        import re
        import logging
        from langdetect import detect_langs
        policy_logger = logging.getLogger("Policy")
        
        text = text.strip()
        if not text:
            return True # Ignore truly empty strings

        # 1. Dictionary-Based Priority (Prevents Langdetect false positives on short text)
        words = re.findall(r'\b\w+\b', text.lower())
        
        # If any word in human-readable text is from our common list, we grant a bypass
        # "Okay. Fine. Can you" -> "okay" is in list -> Bypass langdetect
        if any(w in self.COMMON_ENGLISH_WORDS for w in words):
            policy_logger.debug(f"[GOVERNANCE] Dictionary Bypass for: '{text}'")
            return True
            
        if len(text) < 4:
            return True # Too short to reliably detect, give benefit of the doubt

        # 2. ASCII/Latin Check (Catches Hindi/Bengali/Mandarin instantly)
        # If less than 50% of the text is standard Latin characters, it's definitely foreign.
        # Strips numbers and punctuation before checking ratio.
        clean_text = re.sub(r'[^a-zA-Z\u00C0-\u017F]', '', text)
        if not clean_text or len(re.findall(r'[a-zA-Z]', clean_text)) / len(clean_text) < 0.5:
            policy_logger.warning(f"[GOVERNANCE] Blocked via Non-Latin Check: '{text}'")
            return False

        # 3. Probabilistic Check (Catches Spanish, French, German)
        try:
            langs = detect_langs(text)
            policy_logger.debug(f"[GOVERNANCE] Langdetect: {langs}")
            
            # If the top detected language is not English, block it.
            if langs[0].lang != 'en':
                policy_logger.warning(f"[GOVERNANCE] Blocked via Langdetect ({langs[0].lang}): '{text}'")
                return False
                
            return True
            
        except Exception as e:
            # IF IT CRASHES, DEFAULT TO BLOCKED (False).
            policy_logger.error(f"[GOVERNANCE] langdetect crashed on '{text}': {e}. Defaulting to BLOCKED.")
            return False

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
            
        # 5. Tone & Personality Governance
        for rp in self.RUDE_KEYWORDS + self.PERSUASIVE_KEYWORDS:
            if rp in lower_text:
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

    def classify_intent(self, user_text: str, detected_lang: str = None) -> str:
        """
        Classifies user intent into: 'PROCEED', 'SENSITIVE', 'HARD_REFUSAL_IMMIGRATION', etc.
        """
        # 0. Check Language (Layer 1 - Hard-coded Gate)
        # Block non-English input immediately using STT metadata + Text heuristics
        if not self._is_english(user_text, detected_lang=detected_lang):
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
            return PRDScripts.REFUSAL_SENSITIVE
            
        if intent == "HARD_REFUSAL_IMMIGRATION":
            return PRDScripts.REFUSAL_IMMIGRATION
            
        if intent == "HARD_REFUSAL_MEDICAL":
            return PRDScripts.REFUSAL_MEDICAL
            
        if intent == "HARD_REFUSAL_LEGAL":
            return PRDScripts.REFUSAL_LEGAL
            
        if intent == "HARD_REFUSAL_LANGUAGE":
            return PRDScripts.REFUSAL_LANGUAGE
            
        if intent == "HARD_REFUSAL_INTERNAL_STAFF":
            return PRDScripts.REFUSAL_INTERNAL_STAFF
            
        if intent == "HARD_REFUSAL_POLITICS":
            return PRDScripts.REFUSAL_POLITICS
            
        if intent == "HARD_REFUSAL_COMPETITORS":
            return PRDScripts.REFUSAL_COMPETITORS
            
        if intent == "HARD_REFUSAL_FINANCIAL_DISPUTES":
            return PRDScripts.REFUSAL_FINANCIAL_DISPUTES

        return PRDScripts.REFUSAL_DEFAULT
