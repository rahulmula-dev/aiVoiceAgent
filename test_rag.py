"""
Direct test of KnowledgeBase RAG search against PGVector.
"""
import asyncio
import os
import sys

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from dotenv import load_dotenv
load_dotenv()

from retrieval.vector_store import KnowledgeBase, get_default_threshold

TEST_QUERIES = [
    "What programs are offered?",
    "How much are the fees?",
    "What are admission requirements?",
    "Tell me about the MBA program",
    "When is the next intake?",
]

async def main():
    kb = KnowledgeBase()

    print("=" * 60)
    print("PGVector RAG Retrieval Test")
    print("=" * 60)
    print(f"DEFAULT_THRESHOLD: {get_default_threshold()}")
    print()

    health = await kb.check_health()
    status = "CONNECTED" if health else "FAILED"
    print(f"KnowledgeBase Health: {status}")
    print()

    if not health:
        print("ABORT: KnowledgeBase is not connected. Check PG_DATABASE_URL.")
        return
    
    for query in TEST_QUERIES:
        print(f"QUERY: '{query}'")
        print("-" * 40)
        result, score, topic, kb_version, chunk_ids = await kb.search(query, None, 3)
        print(f"  Score:      {score:.4f}")
        print(f"  Topic:      {topic}")
        print(f"  Chunk IDs:  {chunk_ids}")
        if result and "No specific documents found" not in result:
            short = result[:200].replace('\n', ' ')
            print(f"  STATUS: HIT")
            print(f"  Answer: {short}...")
        else:
            print(f"  STATUS: MISS")
            print(f"  Result: {result}")
        print()
    
    print("=" * 60)
    print("Done.")

if __name__ == "__main__":
    asyncio.run(main())
