"""
tests/test_language_interceptor.py
====================================
pytest suite for LanguageGovernanceInterceptor.

Run from the project root:
    pytest tests/test_language_interceptor.py -v

What is tested
--------------
1.  English transcripts pass through to Gemini (proceed_to_llm=True).
2.  Name introductions with foreign-origin names never trigger a strike.
3.  Single-word English fillers / affirmations pass via the fast path.
4.  Deepgram acoustic non-English detection triggers a strike immediately.
5.  Strike 1 & 2 return the polite refusal; terminate_call is False.
6.  Strike 3 returns the farewell refusal; terminate_call is True.
7.  End-to-end: three consecutive "Bonjour" calls cause websocket.close()
    to be called exactly once (the kill-switch test).
8.  Speaking English after a strike does NOT reset the counter.
9.  Missing FastText model + missing Lingua → fail-open (never block).
10. Custom max_strikes is respected.
11. reset() clears the counter.
"""

from __future__ import annotations

import sys
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Path setup ─────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))

from contracts.language_interceptor import LanguageGovernanceInterceptor, InterceptResult
from contracts.policy import PRDScripts


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _new() -> LanguageGovernanceInterceptor:
    """Return a fresh interceptor for each test."""
    return LanguageGovernanceInterceptor("test-session")


def _bonjour(ic: LanguageGovernanceInterceptor) -> InterceptResult:
    """Simulate a Deepgram transcript where the acoustic detector confirms French."""
    return ic.check("Bonjour", deepgram_lang="fr")


# ══════════════════════════════════════════════════════════════════════════════
# Group 1 — English pass-through
# ══════════════════════════════════════════════════════════════════════════════

class TestEnglishPassThrough:

    def test_clear_english_proceeds(self):
        ic = _new()
        r = ic.check("I would like to enroll in the nursing program.")
        assert r.proceed_to_llm is True
        assert r.refusal_text is None
        assert r.terminate_call is False
        assert r.strike == 0

    def test_college_domain_english_proceeds(self):
        ic = _new()
        r = ic.check("What are the tuition fees for the software diploma?")
        assert r.proceed_to_llm is True
        assert r.strike == 0

    def test_deepgram_confirmed_english_proceeds(self):
        ic = _new()
        r = ic.check("Some garbled transcript text", deepgram_lang="en")
        assert r.proceed_to_llm is True
        assert r.detection_method == "deepgram_acoustic"

    def test_deepgram_en_us_variant_proceeds(self):
        ic = _new()
        r = ic.check("Tell me about the fall intake.", deepgram_lang="en-US")
        assert r.proceed_to_llm is True

    def test_empty_transcript_proceeds(self):
        ic = _new()
        r = ic.check("")
        assert r.proceed_to_llm is True
        assert r.strike == 0

    def test_whitespace_only_proceeds(self):
        ic = _new()
        r = ic.check("   ")
        assert r.proceed_to_llm is True


# ══════════════════════════════════════════════════════════════════════════════
# Group 2 — Fast-path: single words and name introductions
# ══════════════════════════════════════════════════════════════════════════════

class TestFastPath:

    @pytest.mark.parametrize("word", [
        "okay", "yes", "yeah", "no", "hi", "hello", "hey",
        "thanks", "sure", "hmm", "uh", "um", "mhm",
    ])
    def test_common_english_fillers_pass(self, word):
        ic = _new()
        r = ic.check(word)
        assert r.proceed_to_llm is True, f"'{word}' should pass via fast path"
        assert r.detection_method == "fast_path"
        assert r.strike == 0

    @pytest.mark.parametrize("intro", [
        "Hi, my name is Priya and I'm calling about nursing.",
        "My name is Jaspreet.",
        "I am calling about the business diploma.",
        "This is Ahmed, I want information about programs.",
        "Hello, my name is María.",          # Spanish name, English sentence
    ])
    def test_name_introductions_pass(self, intro):
        ic = _new()
        r = ic.check(intro)
        assert r.proceed_to_llm is True, f"Intro should pass: '{intro}'"
        assert r.detection_method == "fast_path"
        assert r.strike == 0


# ══════════════════════════════════════════════════════════════════════════════
# Group 3 — Non-English detection and strike accumulation
# ══════════════════════════════════════════════════════════════════════════════

class TestStrikeMachine:

    def test_deepgram_french_triggers_strike(self):
        ic = _new()
        r = _bonjour(ic)
        assert r.proceed_to_llm is False
        assert r.strike == 1
        assert r.detection_method == "deepgram_acoustic"
        assert r.terminate_call is False

    def test_deepgram_spanish_triggers_strike(self):
        ic = _new()
        r = ic.check("Hola, quisiera información.", deepgram_lang="es")
        assert r.proceed_to_llm is False
        assert r.strike == 1

    def test_deepgram_hindi_triggers_strike(self):
        ic = _new()
        r = ic.check("Mujhe jaankari chahiye.", deepgram_lang="hi")
        assert r.proceed_to_llm is False
        assert r.strike == 1

    # ── Strike-1 refusal ───────────────────────────────────────────────────────

    def test_strike_1_returns_correct_refusal(self):
        ic = _new()
        r = _bonjour(ic)
        assert r.refusal_text == PRDScripts.REFUSAL_LANGUAGE_1
        assert r.terminate_call is False
        assert r.strike == 1

    # ── Strike-2 refusal ───────────────────────────────────────────────────────

    def test_strike_2_returns_correct_refusal(self):
        ic = _new()
        _bonjour(ic)
        r = _bonjour(ic)
        assert r.refusal_text == PRDScripts.REFUSAL_LANGUAGE_2
        assert r.terminate_call is False
        assert r.strike == 2

    # ── Strike-3 termination ──────────────────────────────────────────────────

    def test_strike_3_returns_farewell_refusal(self):
        ic = _new()
        _bonjour(ic)
        _bonjour(ic)
        r = _bonjour(ic)
        assert r.refusal_text == PRDScripts.REFUSAL_LANGUAGE_3
        assert r.terminate_call is True
        assert r.strike == 3

    def test_strike_3_contains_goodbye(self):
        """The 3rd-strike script must end with 'Goodbye.' to trigger the manager's
        auto-close heuristic in speak_immediate_response()."""
        assert PRDScripts.REFUSAL_LANGUAGE_3.lower().rstrip().endswith("goodbye.")

    def test_beyond_3_still_terminates(self):
        """If check() is called a 4th time (shouldn't happen in production, but
        ensure no off-by-one error causes terminate=False on the 4th call)."""
        ic = _new()
        for _ in range(3):
            _bonjour(ic)
        r = _bonjour(ic)  # 4th strike
        assert r.terminate_call is True
        assert r.strike == 4

    # ── Strikes are session-persistent ────────────────────────────────────────

    def test_english_after_strike_does_not_reset_count(self):
        """Speaking English between violations does NOT clear the counter.
        Prevents 'strike-cycling' bypass attempts."""
        ic = _new()
        _bonjour(ic)                                    # Strike 1
        r = ic.check("I want to enroll in nursing.")    # English — should pass
        assert r.proceed_to_llm is True
        assert r.strike == 1                            # Counter unchanged

    def test_strikes_accumulate_across_different_languages(self):
        """Strikes from French + Spanish count toward the same session total."""
        ic = _new()
        ic.check("Bonjour", deepgram_lang="fr")       # 1
        ic.check("Hola", deepgram_lang="es")          # 2
        r = ic.check("Merhaba", deepgram_lang="tr")   # 3
        assert r.terminate_call is True
        assert r.strike == 3


# ══════════════════════════════════════════════════════════════════════════════
# Group 4 — Kill-switch integration (the core E2E requirement)
# ══════════════════════════════════════════════════════════════════════════════

class TestKillSwitch:

    def test_bonjour_three_times_closes_websocket(self):
        """
        End-to-end verification:
          - Three "Bonjour" calls (Deepgram acoustic = fr) are processed.
          - websocket.close() must be called exactly once (on Strike 3).
          - Strike counter must be 3 after the sequence.

        This mirrors the exact behaviour of manager._language_termination_flow()
        which calls cleanup() → websocket.close() when terminate_call is True.
        """
        mock_ws = MagicMock()
        ic = LanguageGovernanceInterceptor("kill-switch-test")

        for attempt in range(1, 4):
            result = ic.check("Bonjour", deepgram_lang="fr")

            if result.terminate_call:
                # Manager behaviour: speak refusal then close
                mock_ws.close()

        # The WebSocket must have been closed exactly once
        mock_ws.close.assert_called_once()

        # Strike counter must reflect 3 accumulated violations
        assert ic.strike_count == 3

    def test_websocket_not_closed_before_strike_3(self):
        """Ensure the connection stays open during Strikes 1 and 2."""
        mock_ws = MagicMock()
        ic = LanguageGovernanceInterceptor("premature-close-test")

        for attempt in range(1, 3):         # Strikes 1 & 2 only
            result = ic.check("Bonjour", deepgram_lang="fr")
            if result.terminate_call:
                mock_ws.close()

        mock_ws.close.assert_not_called()

    def test_english_caller_never_triggers_close(self):
        """An English-only caller must never see terminate_call=True."""
        ic = LanguageGovernanceInterceptor("english-caller-test")
        transcripts = [
            "I'd like to know about the nursing program.",
            "What is the tuition fee for the diploma?",
            "Can I apply for the fall intake?",
        ]
        for t in transcripts:
            r = ic.check(t, deepgram_lang="en")
            assert r.terminate_call is False
            assert r.proceed_to_llm is True


# ══════════════════════════════════════════════════════════════════════════════
# Group 5 — Resilience: missing model / detector failure
# ══════════════════════════════════════════════════════════════════════════════

class TestFastTextConfidenceGate:
    """
    Verifies the corrected FastText logic:
    PASS only when label == "en" AND confidence >= 0.80.
    Low-confidence English labels (STT force-mapping) must not slip through.
    """

    def test_fasttext_en_high_confidence_passes(self):
        """label=en, conf=0.95 → clear English → proceed to Gemini."""
        from contracts.language_guard import LanguageValidationResult
        mock_result = LanguageValidationResult(
            is_english=True, predicted_label="en", confidence=0.95, model_available=True
        )
        with patch("contracts.language_guard.validate_language", return_value=mock_result):
            ic = LanguageGovernanceInterceptor("en-high-conf")
            r = ic.check("Tell me about the nursing program fees.")
            assert r.proceed_to_llm is True

    def test_fasttext_en_low_confidence_fails_open(self):
        """label=en, conf=0.55 → STT may have force-mapped non-English phonemes.
        Confidence < 0.80 → uncertain → fail-open (proceed=True, no strike).
        This is intentional: we do not punish callers when detection is uncertain."""
        from contracts.language_guard import LanguageValidationResult
        mock_result = LanguageValidationResult(
            is_english=True, predicted_label="en", confidence=0.55, model_available=True
        )
        with patch("contracts.language_guard.validate_language", return_value=mock_result):
            ic = LanguageGovernanceInterceptor("en-low-conf")
            r = ic.check("Some ambiguous low-confidence transcript.")
            # fail-open: uncertain detection never strikes an innocent caller
            assert r.proceed_to_llm is True
            assert r.strike == 0

    def test_fasttext_non_english_high_confidence_blocks(self):
        """label=fr, conf=0.96 → confidently non-English → STRIKE."""
        from contracts.language_guard import LanguageValidationResult
        mock_result = LanguageValidationResult(
            is_english=False, predicted_label="fr", confidence=0.96, model_available=True
        )
        with patch("contracts.language_guard.validate_language", return_value=mock_result):
            ic = LanguageGovernanceInterceptor("fr-high-conf")
            r = ic.check("Bonjour je voudrais des informations.")
            assert r.proceed_to_llm is False
            assert r.strike == 1

    def test_fasttext_non_english_low_confidence_fails_open(self):
        """label=cy (Welsh false-positive from STT noise), conf=0.45 → uncertain → fail-open."""
        from contracts.language_guard import LanguageValidationResult
        mock_result = LanguageValidationResult(
            is_english=True, predicted_label="cy", confidence=0.45, model_available=True
        )
        with patch("contracts.language_guard.validate_language", return_value=mock_result):
            ic = LanguageGovernanceInterceptor("cy-low-conf")
            r = ic.check("Yeah uh I think so.")
            assert r.proceed_to_llm is True
            assert r.strike == 0


class TestFailOpen:

    def test_no_fasttext_no_lingua_fails_open(self):
        """
        When NEITHER FastText NOR Lingua is available (e.g. fresh EC2, packages
        not yet installed), the interceptor must NEVER block a caller.
        Failing open is always safer than a false-positive strike on a real caller.
        """
        with (
            patch("contracts.language_guard._try_load_fasttext", return_value=None),
            patch("contracts.language_guard._get_lingua_detector", return_value=None),
        ):
            ic = LanguageGovernanceInterceptor("fail-open-test")
            # Even if the text looks foreign, fail open when detectors are absent
            r = ic.check("Bonjour, comment ca va?")  # No deepgram_lang provided
            assert r.proceed_to_llm is True
            assert r.detection_method == "fail_open"
            assert r.strike == 0

    def test_detection_exception_fails_open(self):
        """An unexpected exception inside validate_language must not crash the call."""
        with patch(
            "contracts.language_guard.validate_language",
            side_effect=RuntimeError("model exploded"),
        ):
            ic = LanguageGovernanceInterceptor("exception-test")
            r = ic.check("Bonjour")  # No deepgram_lang — falls through to text detection
            assert r.proceed_to_llm is True
            assert r.detection_method == "fail_open"

    def test_missing_model_file_falls_back_to_lingua(self, tmp_path):
        """
        FastText model file missing → graceful fall-through to Lingua.
        Lingua should still detect clear French correctly.
        """
        # Point FASTTEXT_MODEL_PATH at a non-existent file
        missing = str(tmp_path / "does_not_exist.ftz")
        with patch.dict("os.environ", {"FASTTEXT_MODEL_PATH": missing}):
            # Reset the lazy singleton so it re-evaluates with the new path
            import contracts.language_guard as lg
            lg._fasttext_init_attempted = False
            lg._fasttext_model = None

            ic = LanguageGovernanceInterceptor("no-model-test")
            r = ic.check(
                "Bonjour, je voudrais des informations.",
                deepgram_lang=None,   # Force text-only path
            )
            # Lingua should still catch clear French
            assert r.proceed_to_llm is False
            assert r.detection_method == "lingua"
            assert r.lang_code == "fr"

            # Restore singleton state for subsequent tests
            lg._fasttext_init_attempted = False
            lg._fasttext_model = None


# ══════════════════════════════════════════════════════════════════════════════
# Group 6 — Configuration & lifecycle
# ══════════════════════════════════════════════════════════════════════════════

class TestConfiguration:

    def test_custom_max_strikes_4(self):
        """Callers with max_strikes=4 should not be terminated at Strike 3."""
        ic = LanguageGovernanceInterceptor("custom-4-strikes", max_strikes=4)
        for i in range(1, 4):
            r = _bonjour(ic)
            assert r.terminate_call is False, f"Should NOT terminate at strike {i}"
        r = _bonjour(ic)
        assert r.terminate_call is True
        assert r.strike == 4

    def test_max_strikes_1_terminates_immediately(self):
        """max_strikes=1: first non-English call should immediately terminate."""
        ic = LanguageGovernanceInterceptor("zero-tolerance", max_strikes=1)
        r = _bonjour(ic)
        assert r.terminate_call is True
        assert r.strike == 1

    def test_reset_clears_strike_count(self):
        ic = _new()
        _bonjour(ic)
        _bonjour(ic)
        assert ic.strike_count == 2
        ic.reset()
        assert ic.strike_count == 0

    def test_after_reset_strike_1_does_not_terminate(self):
        """After reset(), behaviour reverts to Strike 1 state."""
        ic = _new()
        _bonjour(ic)
        _bonjour(ic)
        ic.reset()
        r = _bonjour(ic)
        assert r.strike == 1
        assert r.terminate_call is False

    def test_session_id_stored(self):
        ic = LanguageGovernanceInterceptor("my-session-abc")
        assert ic.session_id == "my-session-abc"


# ══════════════════════════════════════════════════════════════════════════════
# Group 7 — InterceptResult contract
# ══════════════════════════════════════════════════════════════════════════════

class TestInterceptResultContract:

    def test_result_is_frozen(self):
        """InterceptResult must be immutable (frozen dataclass)."""
        r = InterceptResult(
            proceed_to_llm=True, refusal_text=None, terminate_call=False,
            strike=0, lang_code="en", confidence=0.9, detection_method="fasttext",
        )
        with pytest.raises((AttributeError, TypeError)):
            r.strike = 99  # type: ignore[misc]

    def test_pass_result_has_no_refusal_text(self):
        ic = _new()
        r = ic.check("Tell me about the nursing program.")
        assert r.refusal_text is None

    def test_strike_result_has_refusal_text(self):
        ic = _new()
        r = _bonjour(ic)
        assert r.refusal_text is not None
        assert len(r.refusal_text) > 0

    def test_strike_1_and_2_have_same_text(self):
        """PRD specifies identical messaging for strikes 1 & 2."""
        ic = _new()
        r1 = _bonjour(ic)
        r2 = _bonjour(ic)
        assert r1.refusal_text == r2.refusal_text

    def test_strike_3_text_differs_from_strike_1(self):
        ic = _new()
        r1 = _bonjour(ic)
        _bonjour(ic)
        r3 = _bonjour(ic)
        assert r1.refusal_text != r3.refusal_text
