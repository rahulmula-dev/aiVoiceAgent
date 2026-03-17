import asyncio
import os
import logging
from retrieval.vector_store import KnowledgeBase

# Setup logging
logging.basicConfig(level=logging.INFO)

async def test_threshold_enforcement():
    print("\n--- STARTING M1 VERIFICATION (THRESHOLD ENFORCEMENT) ---")
    kb = KnowledgeBase()
    
    # 1. Test a valid query
    print("\n[PART 1] Testing Valid Query (Should pass threshold)...")
    q1 = "What massage programs do you have?"
    ctx1, score1, cat1, v1, ids1 = await kb.search(q1)
    print(f"Query: {q1}\nScore: {score1}\nPass: {score1 > 0}")

    # 2. Test an 'irrelevant' query that should be rejected by the gate
    print("\n[PART 2] Testing Low-Confidence Query (Should be rejected)...")
    # Using random text that shouldn't match any beauty college docs
    q2 = "How do I build a rocket ship to Mars?"
    ctx2, score2, cat2, v2, ids2 = await kb.search(q2)
    print(f"Query: {q2}\nScore: {score2}\nResult: {ctx2}")

    if "LOW_CONFIDENCE_FALLBACK" in ctx2:
        print("\n✅ SUCCESS: M1 Threshold Enforcement is ACTIVE.")
        print("Note: Default threshold is 0.58. Low-score results are correctly filtered.")
    else:
        print("\n❌ WARNING: Threshold gate might be too low or not filtering correctly.")

if __name__ == "__main__":
    asyncio.run(test_threshold_enforcement())
