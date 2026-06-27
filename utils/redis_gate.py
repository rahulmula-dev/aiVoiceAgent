"""
utils/redis_gate.py — Redis-backed concurrency gate for inbound Twilio calls.

Pattern: Lua CAS on /voice (atomic check+increment), idempotent release on
/api/call-status when the call reaches a terminal state.

Two Lua scripts keep each operation atomic:

  _LUA_ACQUIRE  — check counter < cap, increment, set TTL safety net
  _LUA_RELEASE  — only decrement if this call_sid was previously admitted
                  (prevents double-decrement from duplicate status callbacks)

The call-specific key (cila:call:<sid>) acts as both a guard and a TTL safety
net: if Twilio never delivers a status callback (network drop, etc.) the key
expires after _CALL_TTL seconds and the slot is recovered on the next counter
reset. The global counter resets itself via EXPIRE too.

Usage:
    gate = ConcurrencyGate(redis_client, max_calls=5)

    admitted = await gate.acquire(call_sid)   # /voice
    if not admitted:
        return busy_twiml

    await gate.release(call_sid)              # /api/call-status (terminal)
"""

_COUNTER_KEY    = "cila:active_calls"
_CALL_KEY_PFX   = "cila:call:"
_CALL_TTL       = 400   # seconds — 5 min max call + 100 s buffer
_COUNTER_TTL    = 3600  # counter key lifetime (rolling — reset on each INCR)

_LUA_ACQUIRE = """
local k   = KEYS[1]
local cap = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local cur = tonumber(redis.call('GET', k) or 0)
if cur >= cap then return -1 end
local new = redis.call('INCR', k)
redis.call('EXPIRE', k, ttl)
return new
"""

_LUA_RELEASE = """
local counter_key = KEYS[1]
local call_key    = KEYS[2]
if redis.call('EXISTS', call_key) == 0 then return 0 end
redis.call('DEL', call_key)
local n = redis.call('DECR', counter_key)
if tonumber(n) < 0 then redis.call('SET', counter_key, 0) end
return n
"""


class ConcurrencyGate:
    """
    Atomic call-slot manager backed by Redis.

    Thread-safe for asyncio — all mutations are single Lua scripts (no
    read-modify-write race between Python calls).
    """

    def __init__(self, redis_client, max_calls: int = 5) -> None:
        self._r         = redis_client
        self._max       = max_calls

    async def acquire(self, call_sid: str) -> bool:
        """
        Attempt to reserve a slot for call_sid.

        Returns True  → slot granted, call may proceed.
        Returns False → at capacity, return busy TwiML to Twilio.
        """
        result = await self._r.eval(
            _LUA_ACQUIRE,
            1,                  # numkeys
            _COUNTER_KEY,       # KEYS[1]
            self._max,          # ARGV[1]
            _COUNTER_TTL,       # ARGV[2]
        )
        if result == -1:
            return False
        # Per-call tracking key — guards against double-decrement
        await self._r.set(
            f"{_CALL_KEY_PFX}{call_sid}",
            1,
            ex=_CALL_TTL,
        )
        return True

    async def release(self, call_sid: str) -> int:
        """
        Release the slot held by call_sid.

        Idempotent — safe to call multiple times for the same sid
        (Twilio sometimes delivers duplicate status callbacks).

        Returns the new active-call count (0 if call was not tracked).
        """
        result = await self._r.eval(
            _LUA_RELEASE,
            2,                              # numkeys
            _COUNTER_KEY,                   # KEYS[1]
            f"{_CALL_KEY_PFX}{call_sid}",   # KEYS[2]
        )
        return int(result)

    async def current_count(self) -> int:
        """Current active-call count (informational — not atomic with acquire)."""
        val = await self._r.get(_COUNTER_KEY)
        return int(val or 0)
