"""
contracts/policy.py — restricted-topic detection + response scripts.

Two responsibilities:

  1. **PRDScripts**: a collection of canned response strings used by the
     governance layer when a request is refused or escalated. Keeping these
     verbatim in one place means QA can audit the exact words the agent
     speaks; the LLM never invents these phrases.

  2. **ResponsePolicyEngine** + ``detect_restricted_topic()``: code-level
     guard that classifies inbound user text BEFORE it reaches the LLM.
     If the text matches a restricted category (immigration, legal,
     competitor query, etc.) the orchestrator short-circuits — it speaks
     the canned refusal directly and never calls the LLM. This stops the
     LLM from hallucinating policy or being talked out of the rule.

Adapted from the company project's contracts/policy.py (819 lines). The
clean-build version focuses on the categories that matter for the GD
College demo and skips the longer-tail keyword lists. Add more later if
QA flags missing coverage.
"""

import re


# ─────────────────────────────────────────────────────────────────────────────
# Canned response scripts. Keep verbatim — QA / legal sign off on the exact
# words. The LLM is forbidden (via SYSTEM_PROMPT) from inventing these.
# ─────────────────────────────────────────────────────────────────────────────


class PRDScripts:
    # ── Refusals (topic-specific) ─────────────────────────────────────────
    REFUSAL_IMMIGRATION = (
        "I can't help with immigration questions — please contact IRCC "
        "directly through canada.ca."
    )
    REFUSAL_LEGAL = (
        "Please direct any legal matters to our legal department."
    )
    REFUSAL_COMPETITORS = (
        "I can only provide information about GD College and cannot compare "
        "us with other institutions."
    )
    REFUSAL_FINANCIAL_DISPUTES = (
        "I cannot assist with fee disputes or refund policies over the "
        "phone. A team member will follow up with you."
    )
    REFUSAL_SENSITIVE = (
        "I cannot continue this conversation due to a violation of our "
        "safety policy. Goodbye."
    )
    REFUSAL_OFF_TOPIC = (
        "I can only help with GD College questions. Is there anything I can "
        "help you with regarding our programs?"
    )

    # ── 3-Strike Language Refusals ────────────────────────────────────────
    # Strikes 1-2: warn but continue.  Strike 3: warn and terminate the call.
    REFUSAL_LANGUAGE_1 = (
        "I'm sorry, I'm programmed to assist in English only. "
        "Could you please repeat that in English?"
    )
    REFUSAL_LANGUAGE_2 = REFUSAL_LANGUAGE_1
    REFUSAL_LANGUAGE_3 = (
        "I'm sorry, since I can only assist in English, I will have to end "
        "this call now. Please call back when you can continue in English. "
        "Goodbye."
    )

    # ── Coverage-gap fallbacks ────────────────────────────────────────────
    LOW_CONFIDENCE_FALLBACK = "I don't have that specific information on hand."
    CALLBACK_OFFER = (
        "If you can give me your full name and phone number, a college "
        "associate will follow up with you."
    )
    ANYTHING_ELSE = "Is there anything else I can help you with?"


# ─────────────────────────────────────────────────────────────────────────────
# Restricted topic detection — input side
# ─────────────────────────────────────────────────────────────────────────────


# Map: category name -> (response_text, list_of_match_phrases)
# Match is case-insensitive substring with word-boundary tightening for short
# acronyms (<=3 chars). Keep phrases short and unambiguous.
_RESTRICTED_PATTERNS: dict[str, tuple[str, list[str]]] = {
    "immigration": (
        PRDScripts.REFUSAL_IMMIGRATION,
        [
            "visa",
            "study permit",
            "work permit",
            "permanent residency",
            "permanent residence",
            "immigration",
            "ircc",
            "study in canada",
            "international student visa",
            "pr application",
        ],
    ),
    "legal": (
        PRDScripts.REFUSAL_LEGAL,
        [
            "lawsuit",
            "i will sue",
            "i'll sue",
            "sue you",
            "sue the college",
            "legal department",
            "harassment claim",
            "discrimination claim",
        ],
    ),
    "competitor": (
        PRDScripts.REFUSAL_COMPETITORS,
        [
            "compare with",
            "better than",
            "worse than",
            "humber college",
            "sheridan college",
            "seneca college",
            "george brown",
            "centennial college",
            "conestoga college",
            "fanshawe college",
            "bcit",
        ],
    ),
    "financial_dispute": (
        PRDScripts.REFUSAL_FINANCIAL_DISPUTES,
        [
            "refund",
            "fee dispute",
            "money back",
            "chargeback",
        ],
    ),
}


# Tightened pattern for the most ambiguous-but-dangerous phrases.
# These need word boundaries to avoid false positives ("prepare" should not
# match "PR" as a substring, etc.).
_TIGHT_KEYWORDS_WITH_BOUNDARY = {"pr", "ircc", "bcit"}


def _phrase_matches(text_lower: str, phrase: str) -> bool:
    if phrase in _TIGHT_KEYWORDS_WITH_BOUNDARY:
        return re.search(rf"\b{re.escape(phrase)}\b", text_lower) is not None
    return phrase in text_lower


def detect_restricted_topic(user_text: str) -> tuple[str, str] | None:
    """
    Classify ``user_text`` against the restricted-topic patterns.

    Returns
    -------
    (category, response_text) tuple if a category matches, else None.

    Example
    -------
        >>> detect_restricted_topic("I'm from India, do I need a visa?")
        ('immigration', "I can't help with immigration questions — please...")
        >>> detect_restricted_topic("What's the course duration?")
        None
    """
    text_lower = user_text.lower().strip()
    if not text_lower:
        return None

    for category, (response_text, phrases) in _RESTRICTED_PATTERNS.items():
        for phrase in phrases:
            if _phrase_matches(text_lower, phrase):
                return (category, response_text)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# ResponsePolicyEngine — output-side validator (Step 4 stub; expands later)
# ─────────────────────────────────────────────────────────────────────────────


class ResponsePolicyEngine:
    """
    Output-side validator. Scans LLM-generated text BEFORE it is spoken by
    the TTS engine. The Step 4 implementation is intentionally minimal — it
    only flags the most obvious hallucinations (e.g. invented "5-minute
    call limit" responses). A richer detector lands in a later step.
    """

    # Phrases the LLM sometimes invents that are not actual policies.
    _HALLUCINATION_MARKERS: tuple[str, ...] = (
        "5-minute limit",
        "five-minute limit",
        "5 minute call limit",
        "limited number of digits for security",
        "i'll personally call",
        "i will personally call",
    )

    @classmethod
    def violates(cls, response_text: str) -> bool:
        """Return True if ``response_text`` contains a hallucinated policy."""
        text_lower = response_text.lower()
        return any(marker in text_lower for marker in cls._HALLUCINATION_MARKERS)
