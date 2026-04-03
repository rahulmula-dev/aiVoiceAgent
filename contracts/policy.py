import logging
import re
import asyncio
from enum import Enum
from typing import Optional
from .schemas import EscalationEvent, CallContext
from langdetect import detect_langs, DetectorFactory
DetectorFactory.seed = 0  # Deterministic results — without this langdetect is non-deterministic
# Module-level logger for Policy Engine
logger = logging.getLogger("Policy")

class PRDScripts:
    # Greetings
    GREETING = "Hello! I am CILA from GD College."
    GREETING_TEXT = "Hello! I am CILA from GD College. (Text Mode)"
    
    # Refusals
    REFUSAL_SENSITIVE = "I cannot continue this conversation due to a violation of our safety policy. Goodbye."
    REFUSAL_COMPETITORS = "I can only provide information about GD College and cannot compare us with other institutions."
    REFUSAL_FINANCIAL_DISPUTES = "I cannot assist with fee disputes or refund policies over the phone. A human agent will follow up to assist you."
    REFUSAL_LANGUAGE = "I am currently designed to support English only. Please contact the GD College admissions team for assistance."
    # 3-Strike Language Refusal Scripts (PRD §Language Governance)
    REFUSAL_LANGUAGE_1 = "I'm sorry, I am programmed to assist in English only for the college help desk. Could you please repeat that in English?"
    REFUSAL_LANGUAGE_2 = "I'm sorry, I am programmed to assist in English only for the college help desk. Could you please repeat that in English?"
    REFUSAL_LANGUAGE_3 = "I'm sorry, since I can only assist in English, I will have to end this call now. Please call back when you are ready to proceed in English. Goodbye."
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
        "outside", "inside", "between", "around", "through", "without", "within",
        "near", "behind", "below", "above", "across", "along", "among", "during",
        "since", "until", "towards", "against",
        # Numbers (six-five already covered above)
        "six", "seven", "eight", "nine", "ten", "eleven", "twelve", "thirteen",
        "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen",
        "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety",
        "hundred", "thousand", "million",
        # Common colloquial contractions / fillers
        "gonna", "wanna", "gotta", "kinda", "sorta", "lemme", "gimme", "cause",
        "check", "got", "getting", "looking", "trying", "talking", "asking",
        "want", "wanted", "needs", "needed", "came", "let",
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
        # Common nouns/verbs missing from list (causing false positives on valid English)
        "job", "jobs", "gap", "gap", "career", "work", "life", "age", "year", "years",
        "point", "place", "thing", "things", "way", "part", "right", "left",
        "side", "area", "city", "town", "state", "country", "world",
        "money", "cost", "pay", "paid", "free", "price", "rate", "data",
        "type", "kind", "form", "format", "level", "size", "set", "group",
        "key", "note", "plan", "idea", "goal", "focus", "step", "steps",
        "open", "close", "end", "full", "main", "major", "total", "local",
        "real", "clear", "early", "later", "better", "best", "well", "past",
        "long", "short", "small", "large", "big", "little", "young", "own",
        "both", "same", "different", "possible", "important", "able",
        "line", "week", "month", "read", "write", "learn", "study", "class",
        "offer", "provide", "include", "access", "use", "used", "using",
        "interest", "interested", "available", "option", "options", "choice",
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
            
    @staticmethod
    def _normalise_lang_code(code: str) -> str:
        """Normalise BCP-47 codes like 'en-US' → 'en' for comparison."""
        return code.split("-")[0].lower() if code else ""

    def _is_english(self, text: str, detected_lang: str = None) -> bool:
        """
        [GOVERNANCE] Language gate — Phase 1: English-only.
        Hardened to handle non-Latin characters (Hindi/Bengali) without crashing.

        Detection priority:
          1. Deepgram acoustic detection (detected_lang) — PRIMARY
          2. Non-Latin script check                      — FAST-PATH
          3. langdetect + density COMBINED                — SECONDARY (≥3 words)
          4. Density fallback                             — TERTIARY (short inputs)

        Phase 2: rename to _is_supported_language() and check against
        config.supported_languages for multi-language support.
        """
        import re
        import logging
        from langdetect import detect_langs, DetectorFactory
        DetectorFactory.seed = 0
        policy_logger = logging.getLogger("Policy")
        from contracts.config import config
        supported = config.supported_languages  # Phase 1: ['en']

        text = text.strip()
        if not text:
            return True

        lower_text = text.lower()

        # SPECIAL CASE: Name-introduction phrases should never trigger language strikes.
        intro_regex = r"^(hi|hello)?[\s.,!]*?(my name is|i am|this is|it's)\b"
        if re.search(intro_regex, lower_text):
            return True

        words = re.findall(r'\b\w+\b', lower_text)

        # Single-word purely alphabetical — could be a name (Akansha) or a foreign word (Hola, Theek).
        # Run langdetect to distinguish: block if non-English with high confidence.
        if len(words) == 1 and words[0].isalpha():
            if words[0] in self.COMMON_ENGLISH_WORDS:
                return True  # Known English word — pass immediately
            try:
                detected_langs = detect_langs(text)
                if detected_langs:
                    top = detected_langs[0]
                    if top.lang != 'en' and top.prob >= 0.85:
                        policy_logger.warning(f"[GOVERNANCE] Single-word non-English: {top.lang} ({top.prob:.2f}): '{text}'")
                        return False
            except Exception:
                pass
            return True  # Unknown single word — treat as name/affirmation
        if not words:
            return True

        common_words_found = [w for w in words if w in self.COMMON_ENGLISH_WORDS]
        num_common = len(common_words_found)
        density = num_common / len(words)

        # Short purely-alphabetical inputs (≤2 words) are likely names
        is_name_like = len(words) <= 2 and all(w.isalpha() for w in words)

        # Fast-path: non-Latin script (Hindi, Arabic, CJK) — no English chars
        if len(text) >= 3:
            clean_text_alpha = re.sub(r'[^a-zA-Z]', '', text)
            if clean_text_alpha and len(re.findall(r'[a-zA-Z]', clean_text_alpha)) / len(clean_text_alpha) < 0.4:
                policy_logger.warning(f"[GOVERNANCE] Blocked via Non-Latin Check: '{text}'")
                return False

        # ── PRIMARY GATE: Deepgram acoustic language detection ─────────────
        # With detect_language=true, Deepgram reports the language it detected
        # at the audio/phoneme level. This is the most reliable signal because
        # it operates on raw audio, not text that may be garbled by the
        # English-only transcription model.
        if detected_lang:
            norm_lang = self._normalise_lang_code(detected_lang)

            if norm_lang in supported:
                # Deepgram confirms a supported language. Fast-track approve
                # unless density is suspiciously low (garbled audio that
                # Deepgram misidentified as English).
                if density >= 0.40 or len(words) <= 2:
                    policy_logger.debug(
                        f"[GOVERNANCE] APPROVED by Deepgram (lang={norm_lang}, "
                        f"density={density:.2f}): '{text}'"
                    )
                    return True
                # Low density despite Deepgram saying English — fall through
                # to secondary checks for additional validation.
                policy_logger.info(
                    f"[GOVERNANCE] Deepgram says '{norm_lang}' but density "
                    f"is low ({density:.2f}). Running secondary checks: '{text}'"
                )
            else:
                # Deepgram detected a NON-supported language at acoustic level.
                # This is the strongest non-English signal. Only override for:
                #   1. Near-perfect density (≥0.90) → likely accented English
                #   2. Name-like inputs (≤2 alphabetical words)
                if density >= 0.90:
                    policy_logger.info(
                        f"[GOVERNANCE] Overriding Deepgram lang='{norm_lang}' — "
                        f"near-perfect English density ({density:.2f}): '{text}'"
                    )
                    return True

                if is_name_like:
                    policy_logger.info(
                        f"[GOVERNANCE] Permitting name-like input despite "
                        f"Deepgram lang='{norm_lang}': '{text}'"
                    )
                    return True

                policy_logger.warning(
                    f"[GOVERNANCE] BLOCKED by Deepgram (lang='{norm_lang}', "
                    f"density={density:.2f}): '{text}'"
                )
                return False

        # ── SECONDARY CHECKS (when Deepgram metadata is unavailable) ──────
        # These run only when detected_lang is None OR when Deepgram said
        # English but density was suspiciously low.

        # --- MULTI-WORD PATH (≥3 words): langdetect + density COMBINED ---
        # langdetect alone is unreliable for short colloquial English — it frequently
        # misidentifies English as Welsh (cy), Somali (so), German (de), Portuguese (pt), etc.
        # Key insight: real non-English sentences (Spanish, Hindi) have near-zero English word
        # density, while English sentences (even with slang) have decent density.
        # Therefore: only BLOCK if langdetect says non-English AND density is very low (< 0.50).
        # If density >= 0.50, the sentence contains mostly English words → treat as English.
        if len(words) >= 3:
            try:
                detected_langs = detect_langs(text)
                policy_logger.debug(f"[GOVERNANCE] Langdetect Raw: {detected_langs}")
                if detected_langs:
                    top = detected_langs[0]
                    if top.lang == 'en':
                        policy_logger.debug(f"[GOVERNANCE] PASSED (en={top.prob:.2f}, density={density:.2f}): '{text}'")
                        return True
                    if top.lang != 'en' and top.prob >= 0.60:
                        # Non-English detected with moderate-high confidence.
                        # Only block if density is also low — sentences with >= 50% English words
                        # are likely English that langdetect got wrong (cy/so/de false positives).
                        if density >= 0.50:
                            policy_logger.info(
                                f"[GOVERNANCE] Overriding langdetect={top.lang} ({top.prob:.2f}) — "
                                f"density={density:.2f} >= 0.50 indicates English. Text='{text}'"
                            )
                            return True
                        policy_logger.warning(
                            f"[GOVERNANCE] Non-English detected: {top.lang} ({top.prob:.2f}), "
                            f"density={density:.2f} < 0.50. Blocking. Text='{text}'"
                        )
                        return False
                    # Weak non-English signal (0.40–0.59) but near-zero English word density —
                    # virtually no English words in the sentence → almost certainly non-English.
                    if top.lang != 'en' and top.prob >= 0.40 and density < 0.15:
                        policy_logger.warning(
                            f"[GOVERNANCE] Very-low-density ({density:.2f}) + weak non-English signal "
                            f"({top.lang}: {top.prob:.2f}). Blocking. Text='{text}'"
                        )
                        return False
                    # langdetect low-confidence (< 0.60): don't block on its signal alone.
                    # langdetect is very unreliable on short/ambiguous text — it routinely
                    # returns Hungarian, Slovenian, Somali, etc. for valid English phrases
                    # like "A job gap". Only the moderate-confidence path (>= 0.60) is actionable.
            except Exception as e:
                policy_logger.error(f"[GOVERNANCE] langdetect failed: {e}")
                return True  # Fail-safe: don't penalize if detection crashes

            # langdetect uncertain or low-confidence — density as last resort.
            # Use a very low threshold (0.20) so short English sentences with uncommon nouns
            # (e.g. "A job gap" density=0.33) are not penalised. Only block near-zero density
            # which indicates virtually no English words at all.
            policy_logger.debug(f"[GOVERNANCE] Langdetect uncertain, density fallback ({density:.2f}): '{text}'")
            return density >= 0.20

        # --- SHORT PATH (<3 words) ---
        if len(text) < 3:
            return True

        if is_name_like:
            if num_common >= 1:
                policy_logger.debug(f"[GOVERNANCE] Permitting short input (has English word): '{text}'")
                return True
            # density=0: both words unknown — could be a name or a foreign phrase
            try:
                detected_langs = detect_langs(text)
                if detected_langs:
                    top = detected_langs[0]
                    if top.lang != 'en' and top.prob >= 0.55:
                        policy_logger.warning(f"[GOVERNANCE] 2-word non-English by langdetect ({top.lang} {top.prob:.2f}): '{text}'")
                        return False
                    # Even weak signal (≥0.45) with zero density → block
                    if top.lang != 'en' and top.prob >= 0.45 and density == 0:
                        policy_logger.warning(f"[GOVERNANCE] 2-word zero-density non-English ({top.lang} {top.prob:.2f}): '{text}'")
                        return False
            except Exception:
                pass
            policy_logger.debug(f"[GOVERNANCE] Permitting 2-word name-like input: '{text}'")
            return True

        # Very short non-name strings: require at least one English word
        return density >= 0.50

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
                from langdetect import detect_langs, DetectorFactory
                DetectorFactory.seed = 0

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
            
        if intent == "HARD_REFUSAL_LANGUAGE":
            return PRDScripts.REFUSAL_LANGUAGE
            
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

# ======================
# RESTRICTED TOPIC ENUMS
# ======================
class RestrictedTopicResult:
    def __init__(self, is_restricted: bool, category: Optional[str]):
        self.is_restricted = is_restricted
        self.category = category

# ======================
# DETECTION LOGIC
# ======================
def detect_restricted_topic(user_input: str) -> RestrictedTopicResult:
    if not user_input:
        return RestrictedTopicResult(False, None)
        
    text = user_input.lower()
    
    # hr_salary
    if ("how much do" in text and "staff" in text) or ("salary" in text) or ("paid" in text and "staff" in text):
        return RestrictedTopicResult(True, "hr_salary")
        
    # medical_advice
    if ("medical" in text) or ("medication" in text) or ("prescription" in text) or ("anxiety" in text and "advice" in text):
        return RestrictedTopicResult(True, "medical_advice")
        
    # immigration_guarantee
    if ("guarantee" in text and "permit" in text) or ("guarantee" in text and "visa" in text) or ("promise" in text and "immigration" in text) or ("guarantee" in text and "enrol" in text):
        return RestrictedTopicResult(True, "immigration_guarantee")
        
    # internal_dispute
    if "dispute" in text and "staff" in text:
        return RestrictedTopicResult(True, "internal_dispute")
        
    # internal_staff_issue
    if "staff issue" in text or ("staff" in text and "fired" in text):
        return RestrictedTopicResult(True, "internal_staff_issue")
        
    # political_opinion
    if "political" in text and ("opinion" in text or "view" in text):
        return RestrictedTopicResult(True, "political_opinion")
        
    # legal_interpretation
    if "legal interpretation" in text or ("legal" in text and "meaning" in text) or ("interpret" in text and "law" in text):
        return RestrictedTopicResult(True, "legal_interpretation")
        
    return RestrictedTopicResult(False, None)

# ======================
# HANDLER LOGIC
# ======================
async def handle_restricted_topic(context, category: str):
    logger.info("restricted_topic_detected=true")
    logger.info(f"restricted_category={category}")
    logger.info("pre_retrieval_block=true")
    logger.info("kb_query_attempted=false")
    
    exact_response = "I'm not able to help with that topic. I'll arrange for a team member to follow up."
    
    session = getattr(context, "session", None)
    sid = getattr(context, "sid", "unknown_call_id")
    call_id = session.crm_call_id or session.session_id if session else sid
    
    context._create_task_with_log(
        context.crm.create_ticket(
            transcript=f"System interjected a restricted topic query: {category}",
            summary=f"Restricted Topic Blocked - {category}",
            sentiment="SECURITY_ALERT",
            call_logger=context.call_logger,
            call_id=str(call_id),
            title=f"Restricted Topic - {category}",
            session_obj=session,
            callback_required=True,
            hard_refusal_category=category,
            sensitive_topic_flag=True
        )
    )
    logger.info("crm_ticket_created=true")
    
    if getattr(context, "response_task", None) and not context.response_task.done():
        context.response_task.cancel()
        
    trace_id = f"restricted_topic_term_{category}"
    from contracts.state import CallState
    if hasattr(context.state, "transition_to"):
        context.state.transition_to(CallState.ESCALATION, trace_id=trace_id)
        
    async def termination_flow():
        await context.speak_immediate_response(exact_response, trace_id=trace_id)
        
        if hasattr(context, "wait_for_tts_flush"):
            await context.wait_for_tts_flush()
        else:
            await asyncio.sleep(len(exact_response) * 0.06)  # dynamic fallback
            
        if hasattr(context, "close_connection"):
            await context.close_connection()
        else:
            await context.cleanup()
            
        logger.info("call_terminated=true")
        
    context._language_termination_active = True
    context.response_task = asyncio.create_task(termination_flow())
