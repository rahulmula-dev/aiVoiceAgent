"""
orchestrator/factory.py — VoiceOrchestrator factory + connection pool + concurrency gate lifecycle.

Singletons held here (one per server process):
  _pools  — pre-warmed Deepgram + ElevenLabs connections (Step 6)
  _gate   — Redis Lua-CAS concurrency gate (Step 9, optional)

Lifecycle:
  1. run_server.py calls await warmup_pools() then await init_gate() before uvicorn starts.
  2. create_default_orchestrator() is called per incoming call by the WS route.
  3. The /voice HTTP route calls get_gate() directly for the concurrency check
     (before a WS is even opened, so the gate must be accessible here).
"""

import config
from utils.connection_pool import ConnectionPools
from utils.redis_gate import ConcurrencyGate
from .manager import VoiceOrchestrator

_pools: ConnectionPools | None = None
_gate:  ConcurrencyGate | None = None


async def warmup_pools() -> None:
    """
    Open pre-warmed STT WebSocket connections and fire a TTS warmup request.
    Must be awaited before uvicorn.Server.serve().
    """
    global _pools
    _pools = ConnectionPools(stt_size=2)
    await _pools.warmup()


async def init_gate() -> None:
    """
    Connect to Redis and initialise the concurrency gate singleton.

    No-ops silently when CONCURRENCY_GATE_ENABLED=false (default) so the
    server starts cleanly without Redis. On any connection error the gate
    is left as None and the /voice route skips the cap check entirely.
    """
    global _gate
    if not config.CONCURRENCY_GATE_ENABLED:
        return
    try:
        import redis.asyncio as aioredis
        client = aioredis.from_url(config.REDIS_URL, decode_responses=True)
        await client.ping()
        _gate = ConcurrencyGate(client, max_calls=config.MAX_CONCURRENT_CALLS)
        print(f"[GATE] Concurrency gate ready — max {config.MAX_CONCURRENT_CALLS} concurrent calls")
    except Exception as e:
        print(f"[GATE] Redis init failed ({type(e).__name__}: {e}) — gate disabled, all calls admitted")
        _gate = None


def get_gate() -> ConcurrencyGate | None:
    """Return the active concurrency gate, or None if disabled / unavailable."""
    return _gate


async def create_default_orchestrator() -> VoiceOrchestrator:
    """Return a fresh, fully wired VoiceOrchestrator ready to handle one call."""
    return VoiceOrchestrator(pools=_pools)
