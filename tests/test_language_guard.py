"""
tests/test_language_guard.py
============================
Pytest-compatible tests for the language guard layer (Lingua / FastText).

Migrated from root-level test_fasttext.py and the guard-layer section of
test_governance.py as part of CTO code review Point 8.

Run:
    pytest tests/test_language_guard.py -v
"""

from __future__ import annotations

import pytest
from contracts.language_guard import validate_language


# ---------------------------------------------------------------------------
# validate_language() — guard layer tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected_english, label", [
    # ── Clear English ────────────────────────────────────────────────────────
    ("Hello, I would like information about the nursing program.", True,
     "Clear English query"),
    ("What are the admission requirements for the business diploma?", True,
     "College-domain English"),
    ("Can you tell me about tuition fees and payment plans?", True,
     "Multi-word English"),
    ("Hi, my name is Priya.", True,
     "Name introduction with accented name"),
    ("Yeah okay, sounds good.", True,
     "Casual English filler"),
    ("I want to apply for the fall semester.", True,
     "English with date reference"),
    ("I would like to enroll in the nursing program.", True,
     "Standard admissions query"),
    ("I live on Rue de la Gare and I want to apply.", True,
     "Canadian-French street name embedded in English — must NOT block"),

    # ── Edge cases ───────────────────────────────────────────────────────────
    ("", True, "Empty string — pass-through"),
    ("okay", True, "Single English word"),
    ("Yeah, I think... uh... okay.", True,
     "Voice filler — should PASS (fail-open)"),

    # ── Non-English (should BLOCK) ───────────────────────────────────────────
    ("Bonjour, je voudrais des informations sur les programmes.", False,
     "French — Canadian bilingual risk"),
    ("Hola, quisiera información sobre los programas disponibles.", False,
     "Spanish"),
    ("كيف يمكنني التسجيل في البرنامج؟", False,
     "Arabic script — non-Latin"),
    ("ਮੈਨੂੰ ਦਾਖਲੇ ਬਾਰੇ ਜਾਣਕਾਰੀ ਚਾਹੀਦੀ ਹੈ", False,
     "Punjabi script — non-Latin"),
    ("Ich möchte mich über die Aufnahmebedingungen informieren.", False,
     "German"),

    # ── Romanised Hindi (Latin script) — guard layer fails open ──────────────
    # FastText/Lingua fail-open on romanised Hindi because Latin script provides
    # no non-English cues. The policy density+langdetect chain catches it.
    ("Mujhe admission ke baare mein jaankari chahiye.", True,
     "Romanised Hindi — guard fails open; policy chain blocks downstream"),
])
def test_validate_language(text: str, expected_english: bool, label: str):
    result = validate_language(text)
    assert result.is_english == expected_english, (
        f"[{label}] Expected is_english={expected_english}, "
        f"got is_english={result.is_english} "
        f"(lang={result.predicted_lang_code}, conf={result.confidence:.3f})"
    )


# ---------------------------------------------------------------------------
# Policy chain — _is_english() tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected_english, label", [
    ("I would like to enroll in the nursing program.", True,
     "Clean English"),
    ("Hi, my name is Priya and I'm calling about the fall intake.", True,
     "Indian name in English sentence"),
    ("I live on Rue de la Gare and I want to apply.", True,
     "CA French street name in English"),
    ("Yeah, I think... uh... okay.", True,
     "Voice filler — must NOT strike"),
    ("Bonjour, je voudrais des informations sur les programmes.", False,
     "Pure French"),
    ("Hola, quisiera información sobre los programas disponibles.", False,
     "Spanish"),
    # Mixed English tokens (main, program, enroll) give density > 0.20 floor.
    # Policy lets it through to Gemini secondary filter.
    ("Main nursing program mein enroll hona chahta hoon.", True,
     "Hinglish (mixed English tokens) — reaches Gemini secondary filter"),
    # Pure romanised Hindi with no English tokens → density + langdetect blocks.
    ("Mujhe admission ke baare mein batao.", False,
     "Romanised Hindi — no English tokens, density+langdetect blocks it"),
])
def test_policy_is_english(text: str, expected_english: bool, label: str):
    from contracts.policy import ResponsePolicyEngine
    engine = ResponsePolicyEngine()
    result = engine._is_english(text)
    assert result == expected_english, (
        f"[{label}] Expected _is_english={expected_english}, got {result}"
    )
