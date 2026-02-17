import os
import asyncio
import unittest
from unittest.mock import MagicMock, patch

# Must set env BEFORE importing FeatureConfig if it cached, but it's a property so it reads on access.
# However, Brain init sets up things.

from contracts.config import FeatureConfig
from orchestrator.brain import Brain

class TestFeatureOverrides(unittest.TestCase):
    
    def setUp(self):
        # Save original env
        self.original_env = dict(os.environ)
    
    def tearDown(self):
        # Restore env
        os.environ.clear()
        os.environ.update(self.original_env)

    def test_production_safety(self):
        print("\n--- Testing Production Safety ---")
        os.environ["APP_ENV"] = "production"
        os.environ["OV_DISABLE_INTAKE"] = "true"
        os.environ["OV_FORCE_ESCALATION"] = "true"
        os.environ["OV_DISABLE_RETRIEVAL"] = "true"
        
        config = FeatureConfig()
        
        # Should be False because we are in production
        self.assertFalse(config.override_intake, "Intake override should be disabled in PROD")
        self.assertFalse(config.override_escalation, "Escalation override should be disabled in PROD")
        self.assertFalse(config.override_retrieval, "Retrieval override should be disabled in PROD")
        print("PASS: Production safety locks working.")

    def test_staging_overrides(self):
        print("\n--- Testing Staging Overrides ---")
        os.environ["APP_ENV"] = "staging"
        os.environ["OV_DISABLE_INTAKE"] = "true"
        os.environ["OV_FORCE_ESCALATION"] = "true"
        os.environ["OV_DISABLE_RETRIEVAL"] = "true"
        
        config = FeatureConfig()
        
        self.assertTrue(config.override_intake, "Intake override should be active in STAGING")
        self.assertTrue(config.override_escalation, "Escalation override should be active in STAGING")
        self.assertTrue(config.override_retrieval, "Retrieval override should be active in STAGING")
        print("PASS: Staging overrides working.")

    @patch("orchestrator.brain.KnowledgeBase")
    @patch("orchestrator.brain.genai") # Mock genai to prevent init errors
    def test_brain_retrieval_override(self, mock_genai, MockKB):
        print("\n--- Testing Brain Retrieval Override ---")
        os.environ["APP_ENV"] = "staging"
        os.environ["OV_DISABLE_RETRIEVAL"] = "true"
        os.environ["GEMINI_API_KEY"] = "mock_key"
        
        # Setup KB mocks
        mock_kb_instance = MockKB.return_value
        mock_kb_instance.search.return_value = ("KB Result", 0.9)
        
        # Setup GenAI mocks
        mock_model = mock_genai.GenerativeModel.return_value
        
        # Create a proper async iterator for the response stream
        async def valid_response_stream():
            mock_chunk = MagicMock()
            mock_chunk.candidates = [MagicMock()]
            candidate = mock_chunk.candidates[0]
            candidate.finish_reason = 0 
            part = MagicMock()
            part.text = "Test Response."
            candidate.content.parts = [part]
            yield mock_chunk

        # generate_content_async must be awaitable and return the async iterator
        async def mock_call(*args, **kwargs):
            return valid_response_stream()
            
        mock_model.generate_content_async.side_effect = mock_call
        
        # Initialize Brain
        brain = Brain()
        
        async def run_check():
            # Consume the generator
            result_text = ""
            async for chunk, meta in brain.generate_stream("test query", []):
                result_text += chunk
            
            # Verify KB search was NOT called
            mock_kb_instance.search.assert_not_called()
            print("PASS: Brain skipped KB search due to override.")
            # print(f"DEBUG: Brain Output: {result_text}")

        asyncio.run(run_check())

if __name__ == "__main__":
    unittest.main()
