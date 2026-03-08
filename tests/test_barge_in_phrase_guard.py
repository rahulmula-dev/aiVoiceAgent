import unittest
from contracts.policy import PRDScripts, ResponsePolicyEngine
from unittest.mock import MagicMock

class TestBargeInPhraseGuard(unittest.TestCase):
    def test_interruption_constant_metadata_removed(self):
        """
        [CRITICAL-P5-01] Ensure the Forbidden Phrase constant is strictly undefined.
        """
        print("\n--- Testing Forbidden Phrase Removal ---")
        try:
            # This should raise AttributeError if the constant is removed
            phrase = PRDScripts.INTERRUPTION
            self.fail(f"FORBIDDEN PHRASE DETECTED: PRDScripts.INTERRUPTION still exists with value: '{phrase}'")
        except AttributeError:
            print("PASS: PRDScripts.INTERRUPTION is undefined.")

    def test_ambiguous_intent_classification(self):
        """
        [MEDIUM-P5-01] Ensure short/garbage input is classified as AMBIGUOUS.
        """
        print("\n--- Testing AMBIGUOUS Intent ---")
        policy = ResponsePolicyEngine()
        
        # Test empty/short noise
        self.assertEqual(policy.classify_intent(""), "AMBIGUOUS")
        self.assertEqual(policy.classify_intent("   "), "AMBIGUOUS")
        self.assertEqual(policy.classify_intent("the"), "AMBIGUOUS")
        self.assertEqual(policy.classify_intent("mhm"), "AMBIGUOUS") # Common word in list
        
        # Test valid intent
        self.assertEqual(policy.classify_intent("tell me about the fees"), "PROCEED")
        print("PASS: Ambiguity gate correctly identifies non-intents.")

    def test_partial_match_sensitive_logic(self):
        """
        [HARDENING] Ensure substring matches for sensitive categories are enforced.
        """
        print("\n--- Testing Partial Match Governance ---")
        policy = ResponsePolicyEngine()
        
        # Test compound word bypass
        self.assertEqual(policy.classify_intent("i need visastatus info"), "HARD_REFUSAL_IMMIGRATION")
        self.assertEqual(policy.classify_intent("the salarypayment is wrong"), "HARD_REFUSAL_INTERNAL_STAFF")
        print("PASS: Partial match logic prevents simple character-level bypass.")

if __name__ == "__main__":
    unittest.main()
