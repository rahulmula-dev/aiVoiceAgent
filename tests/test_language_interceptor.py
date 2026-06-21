"""
tests/test_language_interceptor.py

Unit tests for LanguageGovernanceInterceptor.

The interceptor classifies every finalized user transcript and decides:
  - ALLOW  → forward to the LLM as usual
  - STRIKE → speak a refusal and either continue (strikes 1, 2) or terminate (strike 3)

These tests cover the detection order documented in
``contracts/language_interceptor.py:check``:
  1. empty input            → allow
  2. fast-path affirmations → allow
  3. name introduction      → allow
  4. non-Latin script ratio → strike
  5. Lingua text model      → strike if confidently non-English
  6. fail open              → allow

Critical regression-guard: the Deepgram acoustic ``detected_lang`` tag must NOT
fire a strike on English text. Earlier production calls saw Deepgram mis-tag
clear English as ``hi``, which used to terminate calls. The current logic
demotes the tag to informational and trusts Lingua's text-based verdict.
"""
import unittest

from contracts.language_interceptor import LanguageGovernanceInterceptor


class TestEmptyAndFastPath(unittest.TestCase):
    """Cheap early-exits in detection order."""

    def setUp(self):
        self.ic = LanguageGovernanceInterceptor(session_id="t-fast")

    def test_empty_input_allows(self):
        r = self.ic.check("", detected_lang=None)
        self.assertTrue(r.proceed_to_llm)
        self.assertEqual(r.detection_method, "empty_input")
        self.assertEqual(self.ic.strike_count, 0)

    def test_whitespace_only_allows(self):
        r = self.ic.check("   \n\t  ", detected_lang="hi")  # even with wrong tag
        self.assertTrue(r.proceed_to_llm)
        self.assertEqual(self.ic.strike_count, 0)

    def test_short_affirmation_allows(self):
        for word in ("ok", "okay", "yes", "yeah", "hello", "thanks", "bye"):
            with self.subTest(word=word):
                ic = LanguageGovernanceInterceptor(session_id=f"t-{word}")
                r = ic.check(word, detected_lang="es")  # tag wrong on purpose
                self.assertTrue(r.proceed_to_llm)
                self.assertEqual(r.detection_method, "fast_path")
                self.assertEqual(ic.strike_count, 0)


class TestNameIntroduction(unittest.TestCase):
    """The 'my name is X' pattern must never strike — names from any culture."""

    def test_name_intro_with_indian_name_allows(self):
        # Earlier bug: Deepgram tagged "my name is Vinod" as lang=hi
        # because of the South-Asian name. Must still allow.
        ic = LanguageGovernanceInterceptor(session_id="t-name")
        r = ic.check("Hi, my name is Rajesh Kumar", detected_lang="hi")
        self.assertTrue(r.proceed_to_llm)
        self.assertEqual(r.detection_method, "name_intro")
        self.assertEqual(ic.strike_count, 0)

    def test_name_intro_im_pattern(self):
        ic = LanguageGovernanceInterceptor(session_id="t-name2")
        r = ic.check("I'm Sarah", detected_lang=None)
        self.assertTrue(r.proceed_to_llm)
        self.assertEqual(r.detection_method, "name_intro")


class TestNonLatinScriptStrikes(unittest.TestCase):
    """Native-script Hindi, Bengali, Japanese, Chinese should all strike."""

    def test_devanagari_hindi_strikes(self):
        ic = LanguageGovernanceInterceptor(session_id="t-hi")
        r = ic.check("नमस्ते आप कैसे हैं", detected_lang="hi")
        self.assertFalse(r.proceed_to_llm)
        self.assertEqual(ic.strike_count, 1)
        self.assertGreaterEqual(r.confidence, 0.9)

    def test_japanese_strikes(self):
        ic = LanguageGovernanceInterceptor(session_id="t-ja")
        r = ic.check("こんにちは元気ですか", detected_lang="ja")
        self.assertFalse(r.proceed_to_llm)
        self.assertEqual(ic.strike_count, 1)


class TestLinguaPrimary(unittest.TestCase):
    """Latin-script text → Lingua, NOT Deepgram's acoustic tag."""

    def test_clear_english_with_wrong_tag_allows(self):
        # The exact case from the production log that used to terminate
        # the call at strike 3.
        ic = LanguageGovernanceInterceptor(session_id="t-english-wrong-tag")
        r = ic.check(
            "Ok, which is the best courses of your college?",
            detected_lang="hi",  # Deepgram mis-tagged clearly English text
        )
        self.assertTrue(r.proceed_to_llm,
                        msg="English text must be allowed even with hi tag")
        self.assertEqual(r.detection_method, "lingua")
        self.assertEqual(r.lang_code, "en")

    def test_plain_english_allows(self):
        ic = LanguageGovernanceInterceptor(session_id="t-en")
        r = ic.check("Tell me about the admissions process please", detected_lang="en")
        self.assertTrue(r.proceed_to_llm)
        self.assertEqual(r.lang_code, "en")

    def test_clear_spanish_strikes(self):
        ic = LanguageGovernanceInterceptor(session_id="t-es")
        r = ic.check(
            "Hola, me gustaría obtener información sobre las clases",
            detected_lang="es",
        )
        self.assertFalse(r.proceed_to_llm)
        self.assertEqual(ic.strike_count, 1)

    def test_short_utterance_low_confidence_allows(self):
        # Short utterances Lingua can't classify confidently must fail open,
        # not strike. Protects accented English from false strikes.
        ic = LanguageGovernanceInterceptor(session_id="t-short")
        r = ic.check("Hmm", detected_lang="es")
        self.assertTrue(r.proceed_to_llm)


class TestThreeStrikePolicy(unittest.TestCase):
    """Strikes accumulate; strike 3 terminates."""

    def test_strikes_accumulate_and_terminate_at_three(self):
        ic = LanguageGovernanceInterceptor(session_id="t-strikes")
        r1 = ic.check("नमस्ते आप कैसे हैं", detected_lang="hi")
        self.assertEqual(r1.strike, 1)
        self.assertFalse(r1.terminate_call)

        r2 = ic.check("こんにちは元気ですか", detected_lang="ja")
        self.assertEqual(r2.strike, 2)
        self.assertFalse(r2.terminate_call)

        r3 = ic.check("আপনি কেমন আছেন", detected_lang="bn")  # Bengali
        self.assertEqual(r3.strike, 3)
        self.assertTrue(r3.terminate_call)

    def test_refusal_text_provided_on_strike(self):
        ic = LanguageGovernanceInterceptor(session_id="t-text")
        r = ic.check("नमस्ते आप कैसे हैं", detected_lang="hi")
        self.assertIsNotNone(r.refusal_text)
        self.assertIn("English", r.refusal_text)

    def test_english_between_strikes_does_not_clear_count(self):
        # Strike count is monotonic — speaking English between strikes
        # doesn't reset it. Mirrors real-call behavior.
        ic = LanguageGovernanceInterceptor(session_id="t-mixed")
        ic.check("नमस्ते आप कैसे हैं", detected_lang="hi")
        ic.check("Tell me about the programs", detected_lang="en")
        r3 = ic.check("こんにちは元気ですか", detected_lang="ja")
        self.assertEqual(r3.strike, 2)


if __name__ == "__main__":
    unittest.main()
