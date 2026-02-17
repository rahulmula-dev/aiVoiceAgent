import asyncio
from retrieval.vector_store import KnowledgeBase

async def test_safety_gate():
    kb = KnowledgeBase()
    
    queries = [
        "What are the tuition fees?",  # Should PASS (Score ~0.62)
        "Tell me about visa approval", # Should BLOCK (Score should be high for sensitive chunk)
    ]
    
    for q in queries:
        print(f"\n--- Testing Query: {q} ---")
        context, score = kb.search(q)
        print(f"Result: {context[:100]}...")
        print(f"Top Score: {score}")

if __name__ == "__main__":
    asyncio.run(test_safety_gate())
