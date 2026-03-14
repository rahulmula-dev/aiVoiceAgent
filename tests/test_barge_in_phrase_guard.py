import unittest
import asyncio
from contracts.policy import PRDScripts, ResponsePolicyEngine
from unittest.mock import MagicMock, AsyncMock, patch

class TestBargeInPhraseGuard(unittest.IsolatedAsyncioTestCase):
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

    async def test_llm_timeout_fallback(self):
        """
        [LOW-L1] Ensure the brain returns the 'I'm listening' fallback on LLM timeout.
        """
        print("\n--- Testing LLM Timeout Fallback ---")
        from orchestrator.brain import Brain
        from unittest.mock import AsyncMock
        
        # Mock the entire generative model to trigger a timeout
        mock_model = MagicMock()
        mock_model.generate_content_async = AsyncMock(side_effect=asyncio.TimeoutError("LLM Timed Out"))
        
        brain = Brain()
        brain.model = mock_model
        brain.fast_model = mock_model # Both fail
        
        mock_session = MagicMock()
        mock_session.conversation_history = []
        
        classification, response, multi_step, topic, _, _ = await brain.generate_with_classification(
            session=mock_session,
            caller_input="hello?"
        )
        
        self.assertEqual(classification, "AMBIGUOUS")
        self.assertIn("I'm listening", response)
        print(f"PASS: Fallback triggered on timeout. Response: '{response}'")

if __name__ == "__main__":
    unittest.main()
