import asyncio
import os
from retrieval.vector_store import KnowledgeBase

import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("TestRAG")

async def test_search():
    kb = KnowledgeBase()
    
    queries = [
        "Where is the college located?",
        "What is the fee structure?",
        "Tell me about class schedules"
    ]
    
    for q in queries:
        print(f"\nQuery: {q}")
        content, score, category, version, chunk_ids = await kb.search(q, top_k=10)
        print(f"Top Result: {content[:100]}...")
        print(f"Category: {category}")
        print(f"Version: {version}")

if __name__ == "__main__":
    asyncio.run(test_search())
