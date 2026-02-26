import asyncio
from unittest.mock import MagicMock
from orchestrator.brain import Brain

async def test_no_procedural_continuation_phrase():
    """
    S4-11 Regression Guard: Ensure the AI never uses clunky procedural phrases.
    """
    forbidden_phrase = "Should I continue from where I left off?"
    
    # Mock session and caller input
    mock_session = MagicMock()
    mock_session.conversation_history = [
        {"role": "user", "parts": ["Tell me about nursing."]},
        {"role": "model", "parts": ["Nursing is a great program. It takes 2 years..."]}
    ]
    
    # Initialize Brain
    brain = Brain()
    
    print("\n[TEST] Running Barge-in Phrase Guard...")
    
    # Test barge-in classification and response
    caller_input = "Wait, what's the fee?"
    classification, response, is_multi_step, topic = await brain.generate_with_classification(
        session=mock_session,
        caller_input=caller_input
    )
    
    print(f"Brain Classification: {classification}")
    print(f"Brain Response: {response}")
    
    # Assertion
    assert forbidden_phrase.lower() not in response.lower(), f"FAILED: Forbidden phrase '{forbidden_phrase}' found!"
    print("[SUCCESS] Phrase guard passed. No procedural continuation detected.")

if __name__ == "__main__":
    try:
        asyncio.run(test_no_procedural_continuation_phrase())
    except AssertionError as e:
        print(e)
        exit(1)
    except Exception as e:
        print(f"Test Error: {e}")
        exit(1)
