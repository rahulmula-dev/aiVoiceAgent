"""
tests/test_policy.py

Tests for the response policy layer in contracts/policy.py:

  - PRDScripts holds canned refusal text used across the agent
  - detect_restricted_topic classifies caller utterances against a
    fixed pattern set (immigration, legal, competitors, financial disputes)
    and returns ``(category, response_text)`` or ``None``
  - ResponsePolicyEngine flags hallucinated policy claims from the LLM
    before they reach TTS
"""
import unittest

from contracts.policy import (
    PRDScripts,
    detect_restricted_topic,
    ResponsePolicyEngine,
)


class TestPRDScriptsConstants(unittest.TestCase):
    """The canned refusal strings must exist and reference English."""

    def test_language_refusal_1_mentions_english(self):
        self.assertIn("English", PRDScripts.REFUSAL_LANGUAGE_1)

    def test_language_refusal_3_indicates_termination(self):
        # The 3rd-strike script must signal call-end so the caller knows.
        self.assertIn("end this call", PRDScripts.REFUSAL_LANGUAGE_3.lower())

    def test_topic_refusals_exist(self):
        for attr in (
            "REFUSAL_IMMIGRATION",
            "REFUSAL_LEGAL",
            "REFUSAL_COMPETITORS",
            "REFUSAL_FINANCIAL_DISPUTES",
            "REFUSAL_OFF_TOPIC",
        ):
            with self.subTest(attr=attr):
                self.assertTrue(getattr(PRDScripts, attr).strip(),
                                f"{attr} must not be empty")

    def test_low_confidence_fallback_exists(self):
        self.assertIn("specific information", PRDScripts.LOW_CONFIDENCE_FALLBACK)


class TestDetectImmigration(unittest.TestCase):
    """Visa / study-permit / IRCC queries → immigration refusal."""

    def test_visa_question(self):
        result = detect_restricted_topic("Can I get a study visa to attend?")
        self.assertIsNotNone(result)
        category, response = result
        self.assertEqual(category, "immigration")
        self.assertIn("IRCC", response)

    def test_study_permit(self):
        self.assertIsNotNone(detect_restricted_topic("How do I get a study permit?"))

    def test_immigration_word(self):
        self.assertIsNotNone(detect_restricted_topic("I have immigration questions"))


class TestDetectLegal(unittest.TestCase):
    """Lawsuit / threat-to-sue / legal department → legal refusal."""

    def test_lawsuit(self):
        r = detect_restricted_topic("I'm filing a lawsuit against the school")
        self.assertIsNotNone(r)
        self.assertEqual(r[0], "legal")

    def test_threatens_to_sue(self):
        self.assertIsNotNone(detect_restricted_topic("I will sue you"))


class TestDetectCompetitor(unittest.TestCase):
    """Comparison with named competitor colleges → competitor refusal."""

    def test_compare_with_humber(self):
        r = detect_restricted_topic("How does GD College compare with Humber College?")
        self.assertIsNotNone(r)
        self.assertEqual(r[0], "competitor")


class TestDetectFinancialDispute(unittest.TestCase):
    """Refund / chargeback / fee dispute → financial-disputes refusal."""

    def test_refund_request(self):
        r = detect_restricted_topic("I want a refund for my deposit")
        self.assertIsNotNone(r)
        self.assertEqual(r[0], "financial_dispute")

    def test_chargeback(self):
        self.assertIsNotNone(detect_restricted_topic("I'll do a chargeback"))


class TestHarmlessQueriesArentRestricted(unittest.TestCase):
    """Normal questions should return None — they go to the LLM."""

    def test_program_question_passes(self):
        self.assertIsNone(detect_restricted_topic("What programs do you offer?"))

    def test_fee_question_passes(self):
        # Asking about cost is fine — only "refund"/"dispute"/"chargeback" is restricted.
        self.assertIsNone(detect_restricted_topic("How much is the nail tech course?"))

    def test_admissions_question_passes(self):
        self.assertIsNone(detect_restricted_topic("How do I apply?"))

    def test_greeting_passes(self):
        self.assertIsNone(detect_restricted_topic("Hello there"))

    def test_empty_input_passes(self):
        self.assertIsNone(detect_restricted_topic(""))
        self.assertIsNone(detect_restricted_topic("   "))


class TestResponsePolicyEngine(unittest.TestCase):
    """Output-side check: flags hallucinated policies the LLM sometimes invents."""

    def test_five_minute_limit_is_flagged(self):
        # LLM occasionally hallucinates a "5-minute call limit" policy.
        self.assertTrue(ResponsePolicyEngine.violates(
            "Our calls have a 5-minute limit, so please be quick."
        ))

    def test_personal_callback_is_flagged(self):
        self.assertTrue(ResponsePolicyEngine.violates(
            "Don't worry, I'll personally call you back."
        ))

    def test_normal_response_passes(self):
        self.assertFalse(ResponsePolicyEngine.violates(
            "Our Nail Tech program is 4 months long and costs $5,500."
        ))


if __name__ == "__main__":
    unittest.main()
