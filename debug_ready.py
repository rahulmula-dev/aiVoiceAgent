import asyncio
import os
import sys

# Set root dir
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

from retrieval.vector_store import KnowledgeBase
from telephony.concurrency import check_redis_health
from crm.client import CRMClient

async def debug_ready():
    print(">>> Starting Health Check Debug...")
    
    kb = KnowledgeBase()
    crm = CRMClient()
    
    async def _safe(name, coro):
        try:
            res = await asyncio.wait_for(coro, timeout=3.5)
            print(f"RESULT: {name} = {res}")
            return res
        except Exception as e:
            print(f"FAILED: {name} with error: {e}")
            return False

    kb_ok = await _safe("KnowledgeBase", kb.check_health())
    crm_ok = await _safe("CRM", crm.check_health())
    redis_ok = await _safe("Redis", asyncio.to_thread(check_redis_health))
    stt_ok = bool(os.getenv("DEEPGRAM_API_KEY"))
    print(f"RESULT: STT (Deepgram API Key) = {stt_ok}")
    
    is_ready = all([kb_ok, redis_ok, stt_ok])
    print(f"\nREADY STATUS: {'✅ READY' if is_ready else '❌ NOT READY'}")
    
    if not is_ready:
        print("\nREASONS:")
        if not kb_ok: print("- KnowledgeBase (PGVector) failed")
        if not redis_ok: print("- Redis failed")
        if not stt_ok: print("- Deepgram API Key missing")

if __name__ == "__main__":
    asyncio.run(debug_ready())
