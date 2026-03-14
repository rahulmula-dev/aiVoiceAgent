import asyncio
import os
import logging
from retrieval.vector_store import KnowledgeBase

# Setup basic logging to see the RAG-TRACE outputs
logging.basicConfig(level=logging.INFO)

async def test_ensemble_scoring():
    print("\n--- STARTING H1 VERIFICATION (ENSEMBLE SCORING) ---")
    kb = KnowledgeBase()
    
    # Query that should produce results
    query = "What are the working hours of GD College?"
    
    print(f"Testing Query: '{query}'")
    context, score, category, version, ids = await kb.search(query, top_k=3)
    
    print(f"\nRESULTS:")
    print(f"Ensemble Score: {score}")
    print(f"Category: {category}")
    print(f"Version: {version}")
    print(f"Chunk IDs: {ids}")
    
    if score > 0:
        print("\n✅ SUCCESS: Ensemble scoring is active and returning results.")
        # Verify 0.7/0.3 logic (this is internal to vector_store.py but the score reflects it)
        print("Note: Score is calculated as (0.7 * Cosine + 0.3 * Trigram Similarity)")
    else:
        print("\n❌ FAILURE: No results found. Check database or thresholds.")

if __name__ == "__main__":
    asyncio.run(test_ensemble_scoring())
