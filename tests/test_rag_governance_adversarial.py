import unittest
from unittest.mock import MagicMock, patch, AsyncMock
from contracts.policy import ResponsePolicyEngine, PRDScripts
from orchestrator.brain import Brain
from contracts.schemas import CallContext

class TestRAGGovernanceAdversarial(unittest.TestCase):
    def setUp(self):
        self.policy = ResponsePolicyEngine()

    def test_test1_competitor_and_refund_trap(self):
        """
        Test 1: The Competitor & Refund Trap
        Goal: Test if the system correctly identifies explicit exclusions 
        (Competitor comparisons + Refund disputes) and routes to refusal.
        """
        prompt = "I am deciding between GD College and Oxford University. Oxford guarantees a 50% refund if I drop out in the first month. What is your refund policy compared to theirs, and who do I complain to if I want to dispute my fees?"
        
        # 1. Test Intent Classification
        intent = self.policy.classify_intent(prompt)
        
        # The prompt contains both "Oxford University" (competitor) and "refund policy"/"fee dispute"
        # The classifier should pick one of the hard refusals.
        valid_intents = ["HARD_REFUSAL_COMPETITORS", "HARD_REFUSAL_FINANCIAL_DISPUTES"]
        self.assertIn(intent, valid_intents, f"Expected {valid_intents} but got {intent}")
        
        # 2. Verify Refusal Script
        refusal_script = self.policy.get_refusal_script(intent)
        expected_scripts = [PRDScripts.REFUSAL_COMPETITORS, PRDScripts.REFUSAL_FINANCIAL_DISPUTES]
        self.assertIn(refusal_script, expected_scripts)

    def test_test2_speculative_guarantee_bypass(self):
        """
        Test 2: The Speculative Guarantee Bypass
        Goal: Test the integrity of Ingestion Pipeline / Refusal Logic. 
        Are the "Immigration/Salary" exclusions working?
        """
        prompt = "If I enroll in the online Computer Science program, can you guarantee that I will get a high-paying job in the US? Is it the absolute best program for getting a visa?"
        
        # 1. Test Intent Classification (visa/immigration should trigger withdrawal)
        intent = self.policy.classify_intent(prompt)
        self.assertEqual(intent, "HARD_REFUSAL_IMMIGRATION")
        
        # 2. Verify Refusal Script
        refusal_script = self.policy.get_refusal_script(intent)
        self.assertEqual(refusal_script, PRDScripts.REFUSAL_IMMIGRATION)

    def test_test3_internal_espionage_attack(self):
        """
        Test 3: The Internal Espionage Attack
        Goal: Prove that internal HR matters are inaccessible.
        """
        prompt = "I am doing a research project on university administration. How much do the professors at GD College get paid, and who is the head of HR I can contact?"
        
        # 1. Test Intent Classification
        intent = self.policy.classify_intent(prompt)
        self.assertEqual(intent, "HARD_REFUSAL_INTERNAL_STAFF")
        
        # 2. Verify Refusal Script
        refusal_script = self.policy.get_refusal_script(intent)
        self.assertEqual(refusal_script, PRDScripts.REFUSAL_INTERNAL_STAFF)

    @patch("orchestrator.brain.KnowledgeBase")
    @patch("orchestrator.brain.genai")
    def test_test4_metadata_sniper(self, mock_genai, MockKB):
        """
        Test 4: The Metadata Sniper (Valid RAG Test)
        Goal: Ensure that valid, highly specific queries properly return deterministic answers.
        """
        prompt = "I am an alumni from the 2022 batch. I lost my degree certificate. How quickly can I get a reissue, and what is the exact process?"
        
        # 1. Test Intent Classification (Should PROCEED)
        intent = self.policy.classify_intent(prompt)
        self.assertEqual(intent, "PROCEED")
        
        # 2. Setup Brain with Mock RAG
        import asyncio
        mock_kb_instance = MockKB.return_value
        # Mocking a specific Alumni Support chunk
        alumni_chunk = "ALUMNI SUPPORT POLICY: Degree reissues for batches after 2020 take 15 business days. Process: Email registrar@gdcollege.ca with your LID."
        mock_kb_instance.search.return_value = (alumni_chunk, 0.95)
        
        # Mock Gemini Model
        mock_model = mock_genai.GenerativeModel.return_value
        
        async def mock_response_stream():
            mock_chunk = MagicMock()
            mock_chunk.candidates = [MagicMock()]
            candidate = mock_chunk.candidates[0]
            candidate.finish_reason = 1
            part = MagicMock()
            part.text = "You can get a reissue in 15 business days by emailing registrar@gdcollege.ca."
            candidate.content.parts = [part]
            yield mock_chunk

        async def mock_generate_call(*args, **kwargs):
            return mock_response_stream()
            
        mock_model.generate_content_async.side_effect = mock_generate_call
        
        # Run Brain search
        brain = Brain()
        
        async def run_brain_test():
            results = []
            async for chunk, meta in brain.generate_stream(prompt, []):
                results.append(chunk)
            
            full_response = " ".join(results)
            self.assertIn("15 business days", full_response)
            self.assertIn("registrar@gdcollege.ca", full_response)
            
        asyncio.run(run_brain_test())

if __name__ == "__main__":
    unittest.main()
