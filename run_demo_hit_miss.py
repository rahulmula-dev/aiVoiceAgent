import os
import asyncio
import logging
from orchestrator.brain import Brain

# Enable basic logging to capture score logs from Retrieval module
logging.basicConfig(level=logging.INFO)
# Specifically set Retrieval logger to INFO to see the scores
logging.getLogger("Retrieval").setLevel(logging.INFO)

async def run_demo():
    # We initialize Brain with a mock call_logger to capture events if needed, 
    # but the log output from KnowledgeBase.search will provide our evidence.
    brain = Brain()
    
    # 1. KB HIT TEST
    hit_query = "What are the tuition fees for Computer Science?"
    print(f"\n>>> [DEMO START: KB HIT]")
    print(f"User Query: {hit_query}")
    
    # This will trigger KnowledgeBase.search internally
    response_hit = await brain.generate_response(hit_query)
    
    # 2. KB MISS TEST
    miss_query = "What is the Dean's favorite pizza topping?"
    print(f"\n>>> [DEMO START: KB MISS]")
    print(f"User Query: {miss_query}")
    
    response_miss = await brain.generate_response(miss_query)
    
    print("\n--- FINAL DEMO CAPTURE ---")
    print(f"HIT Response: {response_hit}")
    print(f"MISS Response: {response_miss}")

if __name__ == "__main__":
    asyncio.run(run_demo())
