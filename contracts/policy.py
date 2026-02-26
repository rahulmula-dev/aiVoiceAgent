from .schemas import EscalationEvent, CallContext
import logging
import re
from langdetect import detect_langs

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
        "financial_disputes": ["fee dispute", "refund policy", "want my money back", "stole my money", "overcharged"],
        # T4 fix: Catch explicit jailbreak translation commands before they reach the LLM.
        # "translate", "en español", "traduce" etc. are injection vectors, not college queries.
        "language_bypass": [
            "translate", "traduce", "en español", "español", "in spanish",
            "in french", "in hindi", "auf deutsch", "en français",
            "other language", "different language", "switch language"
        ]
    }

    ESCALATION_KEYWORDS = [
        "human", "representative", "agent", "manager", "support person"
    ]

    # --- 3. SPECULATIVE LANGUAGE (Uncertainty Ban) ---
    # These phrases must only match speculative *facts*, not conversational phrases.
    # e.g. block "the fee might be $10,000" but NOT "I'm not sure I understand."
    SPECULATIVE_PHRASES = [
        "maybe", "might", "i think", "i believe", "possibly",
        "not sure about",   # narrowed from "not sure" to avoid catching clarification phrases
        "i guess", "could be around", "probably"  # "could be" alone too broad
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
        "a", "an", "the", "i", "m", "my", "me", "you", "your", "he", "she", "it", "we", "they",
        "is", "am", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did",
        "of", "to", "in", "and", "or", "but", "if", "for", "with", "at", "by", "from",
        "what", "where", "how", "when", "why", "who", "which",
        "this", "that", "these", "those", "here", "there",
        "ok", "okay", "fine", "yes", "yup", "no", "mhm", "hello", "hi", "hey",
        "can", "more", "tell", "good", "bad", "thanks", "thank", "please", "help",
        "admission", "admissions", "course", "courses", "college", "fees",
        "available", "program", "programs", "certificate", "diploma",
        "applied", "last", "week", "month", "year", "want", "need", "info", "information",
        "structure", "details", "process", "apply", "online", "campus",
        "student", "asking", "query", "regarding", "saying", "speak", "know", "about",
        "would", "like", "get", "brief", "fee", "cost", "price", "duration", "time", "date",
        "batch", "next", "start", "location", "address", "branch", "office", "contact",
        "number", "email", "phone", "call", "back", "human", "agent", "representative",
        "support", "team", "gd", "college", "cila", "goodbye", "bye", "see", "later",
        "morning", "afternoon", "evening", "night", "one", "two", "three", "four", "five",
        "first", "second", "third", "all", "any", "some", "every", "each", "other",
        "another", "new", "old", "still", "waiting", "listen", "hearing", "catch", "repeat",
        "m", "s", "re", "ve", "ll", "d", "t", "can", "t", "isn", "wasn", "don", "didn",
        "something", "anything", "nothing", "someone", "anyone", "everyone",
        "sushmita", "now", "name", "doing", "gmail", "great", "sure", "maybe", "logic",
        "hospital", "beauty", "cosmetology", "makeup", "hairstyling", "massage", "esthetics",
        "robot", "going", "since", "empower", "empowers", "empowering", "financial",
        "independence", "business", "marketing", "portfolio", "building", "interview",
        "preparation", "gender", "genders", "skills", "mission", "vision", "values",
        "career", "vocational", "technical", "gd college", "cila agent"
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
        policy_logger = logging.getLogger("Policy")

        text = text.strip()
        if not text:
            return True # Ignore truly empty strings
            
        lower_text = text.lower()
        words = re.findall(r'\b\w+\b', lower_text)
        if not words:
            return True
            
        common_words_found = [w for w in words if w in self.COMMON_ENGLISH_WORDS]
        num_common = len(common_words_found)
        density = num_common / len(words)
        
        # 1. Density Check: Strict thresholds for English-only enforcement.
        # [REFINEMENT]: Even if keywords are present, we must calculate density.
        # Example: "Mujhe Cosmetology join karna hai" = low density = FAIL.
        is_very_short = len(words) <= 2
        
        # User feedback: "Strict for user input (0.60)"
        threshold = 0.60
        is_mixed_danger = not is_very_short and density < threshold
        
        if is_mixed_danger:
            policy_logger.warning(f"[GOVERNANCE] Blocked via Density ({density:.2f} < {threshold}): '{text}'")
            return False

        if detected_lang and detected_lang != 'en':
            # EXPERT OVERRIDE: langdetect is notoriously bad at short strings.
            # If it's a very short sentence (1-2 words), we only block if it's 
            # definitely NOT a common word and NOT purely alphabetical (names).
            
            # Trust density more for short samples.
            if density >= 0.70:
                policy_logger.info(f"[GOVERNANCE] Overriding STT Metadata ({detected_lang}) due to High Density ({density:.2f}): '{text}'")
                return True
                
            if is_very_short:
                # If it contains at least one common word ("is", "my", "hi")
                if num_common >= 1:
                    policy_logger.info(f"[GOVERNANCE] Overriding STT Metadata ({detected_lang}) for short English phrase: '{text}'")
                    return True
                
                # NAME PROTECTION: If it's a single word and purely alphabetical, it's likely a name.
                # Deepgram usually capitalizes it.
                if len(words) == 1 and words[0].isalpha():
                    policy_logger.info(f"[GOVERNANCE] Permitting single alphabetical word (potential name/affirmation): '{text}'")
                    return True

            policy_logger.warning(f"[GOVERNANCE] Blocked via STT Metadata ({detected_lang}) - Density: {density:.2f}: '{text}'")
            return False

        if len(text) < 3:
            return True # Too short to reliably detect

        # 1. ASCII Check
        clean_text_alpha = re.sub(r'[^a-zA-Z]', '', text)
        if not clean_text_alpha or len(re.findall(r'[a-zA-Z]', clean_text_alpha)) / len(clean_text_alpha) < 0.4:
            policy_logger.warning(f"[GOVERNANCE] Blocked via Non-Latin Check: '{text}'")
            return False

        # 2. Probabilistic Check
        try:
            detected_langs = detect_langs(text)
            policy_logger.debug(f"[GOVERNANCE] Langdetect Raw: {detected_langs}")

            if detected_langs:
                top = detected_langs[0]
                
                if top.lang != 'en':
                    # Only override if density is decent
                    if density >= 0.75:
                        policy_logger.info(f"[GOVERNANCE] Overriding {top.lang} detection ({top.prob:.2f}) due to Density ({density:.2f}).")
                        return True
                    elif top.prob > 0.40: # Less sensitive than 0.25
                        policy_logger.warning(f"[GOVERNANCE] Non-English suspected: {top.lang} ({top.prob:.2f}). Density: {density:.2f}. Triggering Strike.")
                        return False
                
                if top.lang == 'en':
                    # Even if langdetect says 'en', if density is too low, it's mixed language
                    if density < 0.60:
                        policy_logger.warning(f"[GOVERNANCE] Blocked Hinglish/Mixed (en={top.prob:.2f}, density={density:.2f}): '{text}'")
                        return False
                    policy_logger.debug(f"[GOVERNANCE] PASSED (en={top.prob:.2f}, density={density:.2f}): '{text}'")
                    return True

            return density >= 0.80 # Default fallback

        except Exception as e:
            policy_logger.error(f"[GOVERNANCE] langdetect failed: {e}")
            return True # Fail-safe

    def validate_response(self, context: CallContext, response_text: str) -> bool:
        """
        Pre-flight check before TTS speaks.
        Returns True if safe to speak, False if the output should be blocked.
        When False, the caller (manager.py) substitutes PRDScripts.REFUSAL_LANGUAGE.
        """
        import logging as _logging
        _logger = _logging.getLogger("Policy")
        lower_text = response_text.lower()

        # 1. Check for harmful / sensitive content
        for keyword in self.SENSITIVE_KEYWORDS:
            if keyword in lower_text:
                return False

        # 2. Strict length cap
        if len(response_text) > 500:
            return False

        # 3. Speculative language ban
        for phrase in self.SPECULATIVE_PHRASES:
            if phrase in lower_text:
                return False

        # 4. OUTPUT LANGUAGE GATE — Hard deterministic check (T1 fix)
        # Prevents jailbreak prompts from leaking RAG data in a foreign language.
        # Two layers:
        #   a) Non-Latin script fast-path (Hindi, Mandarin, Arabic, etc.)
        #   b) Latin-script foreign language slow-path (Spanish, French, German)
        if response_text:
            try:
                from langdetect import detect_langs

                # 4a. Fast ASCII ratio check — catches Devanagari / CJK / Arabic instantly
                ascii_ratio = sum(c.isascii() for c in response_text) / len(response_text)
                if ascii_ratio < 0.85:
                    _logger.warning(f"[OUTPUT GOVERNANCE] Non-ASCII ratio {ascii_ratio:.2f} — blocking output.")
                    return False

                # 4b. Langdetect check — catches Latin-script foreign output (Spanish, French, etc.)
                # Guard: langdetect is unreliable on short chunks.
                # Heuristic: Check common English density first as a safety net.
                words = re.findall(r'\b\w+\b', lower_text)
                common_words_found = [w for w in words if w in self.COMMON_ENGLISH_WORDS]
                density = len(common_words_found) / len(words) if words else 0

                # [REFINEMENT]: Bifurcated thresholds for AI vs User
                # Output Guard (Internal Sentry): uses 0.45 for technical RAG data.
                threshold = 0.45
                
                if len(response_text) >= 40:
                    detected = detect_langs(response_text)
                    if detected:
                        top = detected[0]
                        if top.lang != 'en' and top.prob > 0.95:
                            # Only block if density is also low
                            if density < threshold:
                                _logger.warning(f"[OUTPUT GOVERNANCE] LLM output in {top.lang} ({top.prob:.2f}), density {density:.2f} < {threshold} — blocking.")
                                return False
                            else:
                                _logger.info(f"[OUTPUT GOVERNANCE] Overriding {top.lang} detection ({top.prob:.2f}) for AI output due to English Density ({density:.2f}).")

            except Exception as e:
                # If detection fails, allow output — a detection crash ≠ a violation.
                _logger.error(f"[OUTPUT GOVERNANCE] Language detection failed: {e}")

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

        # T4 fix: Translation/jailbreak bypass → English-only refusal
        if intent == "HARD_REFUSAL_LANGUAGE_BYPASS":
            return PRDScripts.REFUSAL_LANGUAGE

        return PRDScripts.REFUSAL_DEFAULT
