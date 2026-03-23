from .schemas import EscalationEvent, CallContext
import logging
import re
from langdetect import detect_langs

# Module-level logger for Policy Engine
logger = logging.getLogger("Policy")

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
    REFUSAL_KB_MISS = "I'm sorry, I don't have that specific information right now. Let me have an admissions officer follow up with you to provide more details. I can, however, help with general information about programs and admissions!"
    REFUSAL_DEFAULT = "I am unable to assist with that specific request. Please contact the GD College admissions team."
    
    # Apologies
    APOLOGY_CLARIFICATION = "I didn't quite catch that. Could you please repeat?"
    APOLOGY_OVERLOADED = "I am currently overloaded with requests. Please try again in a few seconds."
    APOLOGY_CAPACITY = "All our lines are currently busy. As a Reliability Guard, I will ensure a team member calls you back as soon as possible."
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

    # Session Wrap-up
    WRAP_UP = "Before we wrap up, is there anything else I can help with?"
    WRAP_UP_TERMINATION = "Our maximum session time has been reached. Thank you for calling GD College. Goodbye."

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

    # --- 4. ANGER / HIGH-SENTIMENT DETECTION (Escalation Guard) ---
    ANGER_KEYWORDS = [
        "unacceptable",
        "this is unacceptable",
        "complaint",
        "file a complaint",
        "frustrated",
        "angry",
        "very angry",
        "upset",
        "disappointed",
        "escalate",
        "want to speak to a manager",
        "speak to a manager",
        "manager",
        "supervisor"
    ]

    # --- 5. TONE & PERSONALITY (Governance Validation - PRD S4-5) ---
    RUDE_KEYWORDS = [
        "stupid", "idiot", "dumb", "shut up", "crazy", "moron", "fool"
    ]

    PERSUASIVE_KEYWORDS = [
        "you must buy", "act now", "guaranteed", "limited time offer",
        "don't miss out", "buy now", "click here", "subscribe now", "special offer",
        "must enroll", "sign up immediately"
    ]

    # --- 6. LANGUAGE DETECTION (Story S1-4) ---
    COMMON_ENGLISH_WORDS = {
        # Pronouns
        "a", "an", "the", "i", "m", "my", "me", "you", "your", "he", "she", "it", "we", "they",
        "him", "her", "his", "its", "our", "their", "them", "us",
        # Core verbs
        "is", "am", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did",
        "will", "would", "could", "should", "may", "might", "shall", "must", "can",
        "need", "get", "got", "make", "made", "go", "gone", "went", "come", "came",
        "take", "took", "see", "saw", "know", "knew", "think", "thought",
        "look", "use", "find", "give", "tell", "ask", "seem", "let", "put",
        "say", "said", "try", "keep", "hold", "show", "hear", "feel",
        "work", "mean", "call", "move", "live", "run", "turn", "start", "stop",
        # Prepositions / Conjunctions
        "of", "to", "in", "and", "or", "but", "if", "for", "with", "at", "by", "from",
        "on", "up", "out", "into", "over", "after", "before", "about", "than", "as",
        "not", "also", "just", "then", "so", "too",
        # Question words
        "what", "where", "how", "when", "why", "who", "which",
        # Demonstratives / location
        "this", "that", "these", "those", "here", "there",
        # Common affirmations / fillers
        "ok", "okay", "fine", "yes", "yup", "no", "mhm", "mhmm", "hmm", "uh", "um", "ah",
        "hello", "hi", "hey", "yeah", "yep", "nope", "sure", "great", "good", "bad",
        "thanks", "thank", "please", "welcome", "bye", "goodbye", "later",
        # Common adjectives / adverbs
        "more", "less", "very", "much", "many", "little", "few", "most", "none",
        "only", "just", "really", "quite", "high", "low", "actually", "still",
        "almost", "already", "soon", "late", "often", "sometimes", "always", "never",
        "again", "together", "probably", "definitely", "basically", "literally",
        "honestly", "totally", "absolutely", "completely", "mostly", "slightly",
        "fairly", "pretty", "rather", "somewhat", "instead", "otherwise",
        "meanwhile", "anyway", "however", "therefore", "thus", "hence",
        "new", "old", "first", "second", "third", "last", "next",
        "one", "two", "three", "four", "five", "all", "any", "some", "every", "each", "other", "another",
        # Contractions (post-apostrophe split)
        "m", "s", "re", "ve", "ll", "d", "t", "isn", "wasn", "don", "didn", "can",
        # Common nouns
        "name", "time", "year", "week", "month", "day", "number", "phone", "email",
        "something", "anything", "nothing", "someone", "anyone", "everyone",
        "now", "today", "doing", "going", "waiting", "listening", "speaking",
        # College-specific domain
        "admission", "admissions", "course", "courses", "college", "fees", "fee", "cost",
        "available", "program", "programs", "certificate", "diploma", "degree",
        "applied", "info", "information", "details", "process", "apply", "online",
        "campus", "student", "students", "asking", "query", "regarding",
        "would", "like", "get", "brief", "price", "duration", "date", "dates",
        "batch", "start", "location", "address", "branch", "office", "contact",
        "back", "human", "agent", "representative", "support", "team",
        "gd", "cila", "ged", "school", "university", "faculty", "staff",
        "scholarship", "scholarships", "tuition", "payment", "installment",
        "deadline", "schedule", "orientation", "lab", "library", "placement",
        "internship", "graduation", "alumni", "transcript", "enrollment", "enroll",
        "enrolled", "join", "joining", "registration", "exam", "test", "grade",
        "morning", "afternoon", "evening", "night",
        "continue", "restart", "give", "list", "people", "india",
        "africa", "america", "visa", "status",
        "hospital", "beauty", "cosmetology", "makeup", "hairstyling", "massage",
        "business", "marketing", "portfolio", "interview", "preparation",
        "skills", "mission", "vision", "values", "career", "vocational",
        "issue", "question", "help", "know", "about", "speak", "call",
        "gmail", "logic", "empower", "financial", "independence",
        "requirement", "requirements", "specific", "international", "aid",
        "instructor", "instructors", "professor", "professors", "department",
        "facility", "workshop", "qualification", "examination", "assessment",
        "results", "approvals", "measured",
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
        # 0. DEPRECATED: Authoritative STT Metadata Guard 
        # [REMOVED] This hard gate was blocking short English phrases (e.g. "Wait") 
        # when STT incorrectly guessed a foreign language. Using density checks below instead.
        pass
            
        import re
        import logging
        from langdetect import detect_langs
        policy_logger = logging.getLogger("Policy")

        text = text.strip()
        if not text:
            return True # Ignore truly empty strings
            
        lower_text = text.lower()

        # SPECIAL CASE: Name-introduction phrases should never trigger language strikes.
        # Examples: "Hi, my name is Akansha.", "My name is John.", "This is Maria."
        # Use regex to match introduction phrases regardless of punctuation
        intro_regex = r"^(hi|hello)?[\s.,!]*?(my name is|i am|this is|it's)\b"
        if re.search(intro_regex, lower_text):
            return True
        words = re.findall(r'\b\w+\b', lower_text)

        # SPECIAL CASE: Single-word utterances that are purely alphabetical (likely names like "Leila")
        # should not be treated as non-English for governance purposes.
        if len(words) == 1 and words[0].isalpha():
            return True
        if not words:
            return True
            
        common_words_found = [w for w in words if w in self.COMMON_ENGLISH_WORDS]
        num_common = len(common_words_found)
        density = num_common / len(words)
        
        # 1. Density Check: Strict thresholds for English-only enforcement.
        # [FIX] Lowered from 0.85 to 0.60. 85% was too aggressive and blocked valid English 
        # sentences that contained regular nouns, verbs, or names not in the 150-word whitelist.
        # We rely on langdetect (below) to catch Hinglish code-switching robustly.
        threshold = 0.60 if len(words) >= 3 else 0.40
        is_mixed_danger = density <= threshold  # <= catches exact-boundary cases like density=0.60
        
        # Short purely-alphabetical inputs (≤2 words, all letters) are likely names —
        # skip density block and let langdetect be the gate instead.
        is_name_like = len(words) <= 2 and all(w.isalpha() for w in words)
        if is_mixed_danger and not is_name_like:
            # Special bypass for introductions which have specific regex coverage
            if not re.search(intro_regex, lower_text):
                policy_logger.warning(f"[GOVERNANCE] Blocked via Density ({density:.2f} < {threshold}): '{text}'")
                return False

        if detected_lang and detected_lang != 'en':
            # EXPERT OVERRIDE: langdetect is notoriously bad at short strings.
            # If it's a very short sentence (1-2 words), we only block if it's 
            # definitely NOT a common word and NOT purely alphabetical (names).
            
            # TRUST STT metadata aggressively if density isn't near perfect.
            if density >= 0.95:
                policy_logger.info(f"[GOVERNANCE] Overriding STT Metadata ({detected_lang}) due to Near-Perfect Density ({density:.2f}): '{text}'")
                return True
                
            if len(words) <= 2:
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

        # 3. Probabilistic Check (Catches Spanish, French, German, Hinglish, etc.)
        # Avoid running statistical detection on 1-2 words as it generates massive false positives
        if len(words) < 3:
            policy_logger.debug(f"[GOVERNANCE] Short input ({len(words)} words): '{text}'")
            if is_name_like:
                # If any word is a known English word, accept immediately — no langdetect needed.
                if num_common >= 1:
                    policy_logger.debug(f"[GOVERNANCE] Permitting short input (has English word): '{text}'")
                    return True
                # density=0: both words are unknown. Could be a name (Akansha Kumar) or
                # a foreign phrase (theek hai). Run langdetect with a strict threshold.
                try:
                    detected_langs = detect_langs(text)
                    if detected_langs:
                        top = detected_langs[0]
                        if top.lang != 'en' and top.prob >= 0.70:
                            policy_logger.warning(f"[GOVERNANCE] 2-word non-English by langdetect ({top.lang} {top.prob:.2f}): '{text}'")
                            return False
                except Exception:
                    pass
                policy_logger.debug(f"[GOVERNANCE] Permitting 2-word name-like input: '{text}'")
                return True
            # For very short non-name strings, require at least one English word.
            return density >= 0.50

        try:
            detected_langs = detect_langs(text)
            policy_logger.debug(f"[GOVERNANCE] Langdetect Raw: {detected_langs}")

            if detected_langs:
                top = detected_langs[0]
                
                # PHASE 1 RULE: Any strong non-English detection is an immediate violation.
                if top.lang != 'en' and top.prob >= 0.30:
                    # Only override if density is nearly perfect.
                    if density >= 0.90:
                        policy_logger.info(
                            f"[GOVERNANCE] Overriding langdetect={top.lang} ({top.prob:.2f}) due to English Density "
                            f"({density:.2f} >= 0.90). Text='{text}'"
                        )
                        return True
                    policy_logger.warning(
                        f"[GOVERNANCE] Non-English detected by langdetect: {top.lang} ({top.prob:.2f}), "
                        f"density={density:.2f}. Blocking by design (Phase 1 English-only). Text='{text}'"
                    )
                    return False
                
                if top.lang == 'en':
                    # When langdetect says English, accept the input as English.
                    # We already applied a density guard earlier for clearly mixed sentences.
                    policy_logger.debug(
                        f"[GOVERNANCE] PASSED (en={top.prob:.2f}, density={density:.2f}): '{text}'"
                    )
                    return True

            # Default fallback: require very high English density
            return density >= 0.85

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
                # Guard: langdetect is unreliable on very short chunks; still, in Phase 1 we want
                # to be aggressive and block anything confidently non-English.
                words = re.findall(r'\b\w+\b', lower_text)
                common_words_found = [w for w in words if w in self.COMMON_ENGLISH_WORDS]
                density = len(common_words_found) / len(words) if words else 0

                if len(response_text) >= 20:
                    detected = detect_langs(response_text)
                    if detected:
                        top = detected[0]
                        if top.lang != 'en' and top.prob >= 0.40:
                            # If density is high, it's likely just a list of nouns that 
                            # statistical models struggle with. Trust density more.
                            if density >= 0.45:
                                _logger.info(f"[OUTPUT GOVERNANCE] Overriding langdetect={top.lang} for high-density output ({density:.2f}).")
                                return True
                            _logger.warning(
                                f"[OUTPUT GOVERNANCE] Blocking non-English model output {top.lang} "
                                f"({top.prob:.2f}), density={density:.2f}. Text='{response_text[:80]}...'"
                            )
                            return False

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
        Classifies user intent into: 'PROCEED', 'SENSITIVE', 'HARD_REFUSAL_IMMIGRATION', 'AMBIGUOUS', etc.
        Hardened with multi-layered confidence gates and partial match logic (P5-01).
        """
        # [AUDIT] L1: Explicit log line at the pre-check entry point to verify ordering.
        logger.info(f"Policy Pre-Check: Classifying intent for input: '{user_text[:50]}...'")
        
        lower = user_text.lower().strip()
        
        # 1. Check Sensitive (Highest Priority - Full & Substring match) [SECURITY-P1]
        for keyword in self.SENSITIVE_KEYWORDS:
            if self._contains_word(lower, keyword):
                return "SENSITIVE"
        
        # 2. Check Hard Refusals (Layer 2 - Partial Match Logic) [P5-01]
        # Uses more aggressive substring matching to prevent bypasses like "visastatus"
        found_refusal = None
        for category, keywords in self.HARD_REFUSAL_KEYWORDS.items():
            for k in keywords:
                if self._contains_word(lower, k):
                    found_refusal = f"HARD_REFUSAL_{category.upper()}"
                    break
            if found_refusal: break
            
        if found_refusal:
            return found_refusal

        # 3. Check Language (Layer 3 - Governance Gate)
        # Block non-English input if it didn't trigger a specific refusal above.
        if not self._is_english(user_text, detected_lang=detected_lang):
            return "HARD_REFUSAL_LANGUAGE"

        # 3. High-Sentiment / Angry Caller Detection
        for keyword in self.ANGER_KEYWORDS:
            if keyword in lower:
                return "ESCALATION_REQUIRED"

        # 4. Ambiguity Gate (Layer 3)
        # If the input is extremely short or doesn't follow a clear pattern, mark as AMBIGUOUS
        # to prevent hallucinations in the downstream barge-in/thought logic.
        words = lower.split()
        if len(words) == 0:
            return "AMBIGUOUS"
        
        # If it's just a single common word without context, it might be ambiguous
        if len(words) == 1 and words[0] in self.COMMON_ENGLISH_WORDS and words[0] not in ["hello", "hi", "hey", "ok", "okay", "yes", "no", "yup", "thank", "thanks", "wait", "welcome"]:
            return "AMBIGUOUS"

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

        if intent == "AMBIGUOUS":
            return PRDScripts.APOLOGY_CLARIFICATION

        # T4 fix: Translation/jailbreak bypass → English-only refusal
        if intent == "HARD_REFUSAL_LANGUAGE_BYPASS":
            return PRDScripts.REFUSAL_LANGUAGE

        return PRDScripts.REFUSAL_DEFAULT
