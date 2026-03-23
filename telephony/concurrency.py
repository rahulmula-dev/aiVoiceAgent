import os
import logging
import redis

logger = logging.getLogger("concurrency")

# Connect to Redis
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
try:
    # Increase timeouts for local dev stability (Windows context)
    redis_client = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=1.0, socket_connect_timeout=1.0)
    # [FIX] Perform a lightweight ping at startup to avoid 1s hangs during call induction
    if os.getenv("LOCAL_TEST", "false").lower() == "true":
        logger.info(f"Local test mode detected. Verifying Redis at {REDIS_URL}...")
        try:
            redis_client.ping()
            logger.info("Redis is available.")
        except Exception:
            logger.warning("Redis not found. Falling back to local RAM counters for development.")
            redis_client = None
except Exception as e:
    logger.error(f"Failed to connect to Redis: {e}")
    redis_client = None

from contracts.config import config
COUNTER_KEY = "active_inbound_calls"
ACTIVE_SIDS_KEY = "active_call_sids"
MAX_INBOUND_CALLS = config.max_inbound_calls
TTL_SECONDS = 3600  # 1 hour safety TTL

# Lua script for atomic conditional increment:
# KEYS[1]: counter key
# KEYS[2]: set of active SIDs
# ARGV[1]: call_sid
# ARGV[2]: max capacity
# ARGV[3]: TTL
# Returns: new count if accepted, -1 if rejected (cap), -2 if already counted
LUA_INCREMENT_IF_UNDER_CAP = """
if (redis.call('SISMEMBER', KEYS[2], ARGV[1]) == 1) then
    return -2
end

local current = redis.call('GET', KEYS[1])
if (current and tonumber(current) >= tonumber(ARGV[2])) then
    return -1
else
    local new = redis.call('INCR', KEYS[1])
    redis.call('SADD', KEYS[2], ARGV[1])
    redis.call('EXPIRE', KEYS[1], ARGV[3])
    redis.call('EXPIRE', KEYS[2], ARGV[3])
    return new
end
"""

# Lua script for atomic SID-based decrement:
# KEYS[1]: counter key
# KEYS[2]: set of active SIDs
# ARGV[1]: call_sid
# Returns: new count if decremented, -1 if was not in set
LUA_DECREMENT_ONLY_IF_TRACKED = """
if (redis.call('SREM', KEYS[2], ARGV[1]) == 1) then
    local current = redis.call('DECR', KEYS[1])
    if (tonumber(current) < 0) then
        redis.call('SET', KEYS[1], 0)
        return 0
    end
    return current
else
    return -1
end
"""

# Lua script for atomic capacity check:
# KEYS[1]: counter key
# KEYS[2]: set of active SIDs
# ARGV[1]: call_sid
# ARGV[2]: max capacity
# Returns: 1 if rejected (over cap), 0 if allowed
LUA_CHECK_CAPACITY_ATOMIC = """
local is_member = redis.call('SISMEMBER', KEYS[2], ARGV[1])
local current = redis.call('GET', KEYS[1])
local count = current and tonumber(current) or 0
if (is_member == 0 and count >= tonumber(ARGV[2])) then
    return 1 -- Reject: Not tracked and at/over cap
end
if (count > tonumber(ARGV[2])) then
    return 1 -- Reject: Extreme overflow (leak)
end
return 0 -- Safe
"""

# Fallback for local testing without redis
_local_counter = 0
_local_sids = set()
_local_lock = None

import asyncio

def _get_lock() -> asyncio.Lock:
    """Lazy initializer for the local fallback lock."""
    global _local_lock
    if _local_lock is None:
        _local_lock = asyncio.Lock()
    return _local_lock

def check_redis_health() -> bool:
    """Verifies Redis connectivity for the readiness probe."""
    if not redis_client:
        # For local testing, we fall back to local RAM counters, so it's "ready"
        return os.getenv("LOCAL_TEST", "false").lower() == "true"
    try:
        return redis_client.ping()
    except Exception as e:
        logger.warning(f"Redis health check failed: {e}")
        return False

def reset_active_calls() -> None:
    global _local_counter, _local_sids
    _local_counter = 0
    _local_sids = set()
    if redis_client:
        try:
            redis_client.set(COUNTER_KEY, 0)
            redis_client.delete(ACTIVE_SIDS_KEY)
            logger.info("Active call counter reset to 0 in Redis.")
        except Exception as e:
            logger.error(f"Redis reset error: {e}")
    else:
        logger.info("Active call counter reset to 0 locally.")

def get_active_call_count() -> int:
    global _local_counter
    if not redis_client:
        return max(0, _local_counter)
    try:
        val = redis_client.get(COUNTER_KEY)
        return int(val) if val else 0
    except Exception as e:
        logger.error(f"Redis get error: {e}")
        return max(0, _local_counter)

def _debug_force_increment(call_sid: str = "unknown") -> int:
    """
    WARNING: Internal testing helper only.
    This function bypasses the atomic admission control (Lua) and MUST NOT 
    be used in production admission paths. 
    Production code must use increment_if_under_cap().
    """
    global _local_counter, _local_sids
    if not redis_client:
        if call_sid not in _local_sids:
            _local_counter += 1
            _local_sids.add(call_sid)
        return _local_counter
    try:
        # Best effort standard INCR for legacy non-cap paths
        current = redis_client.incr(COUNTER_KEY)
        redis_client.sadd(ACTIVE_SIDS_KEY, call_sid)
        redis_client.expire(COUNTER_KEY, TTL_SECONDS)
        redis_client.expire(ACTIVE_SIDS_KEY, TTL_SECONDS)
        return current
    except Exception as e:
        logger.error(f"Redis incr error: {e}")
        _local_counter += 1
        return _local_counter

async def increment_if_under_cap(max_cap: int, call_sid: str = "unknown") -> tuple[bool, int]:
    """
    Atomically checks if the counter is under the capacity limit and increments it.
    Solves the TOCTOU (Time-Of-Check to Time-Of-Use) race condition using Redis Lua.
    Returns: (is_accepted, new_count)
    """
    global _local_counter, _local_sids
    
    if not redis_client:
        async with _get_lock():
            if call_sid in _local_sids:
                return True, _local_counter
            if _local_counter >= max_cap:
                return False, _local_counter
            _local_counter += 1
            _local_sids.add(call_sid)
            return True, _local_counter
        
    try:
        # Atomic execution of Lua script
        result = redis_client.eval(LUA_INCREMENT_IF_UNDER_CAP, 2, COUNTER_KEY, ACTIVE_SIDS_KEY, call_sid, max_cap, TTL_SECONDS)
        
        if result == -1:
            # CAP HIT: Explicitly log for monitoring/metrics
            logger.warning(f"[METRIC] Concurrency cap hit! Limit: {max_cap}. Rejecting SID: {call_sid}")
            return False, get_active_call_count()
        
        if result == -2:
            return True, get_active_call_count()
            
        return True, int(result)
    except Exception as e:
        logger.error(f"Redis increment_if_under_cap error: {e}")
        # Fallback to local
        async with _get_lock():
            if _local_counter >= max_cap:
                return False, _local_counter
            _local_counter += 1
            _local_sids.add(call_sid)
            return True, _local_counter

async def decrement_active_calls(call_sid: str = "unknown") -> int:
    global _local_counter, _local_sids
    if not redis_client:
        async with _get_lock():
            if call_sid in _local_sids:
                _local_sids.remove(call_sid)
                _local_counter = max(0, _local_counter - 1)
            return _local_counter
    try:
        result = redis_client.eval(LUA_DECREMENT_ONLY_IF_TRACKED, 2, COUNTER_KEY, ACTIVE_SIDS_KEY, call_sid)
        if result == -1:
            # SID wasn't tracked, ignore decrement to prevent counter leakage
            return get_active_call_count()
        return int(result)
    except Exception as e:
        logger.error(f"Redis decr error: {e}")
        async with _get_lock():
            if call_sid in _local_sids:
                _local_sids.remove(call_sid)
                _local_counter = max(0, _local_counter - 1)
            return _local_counter

async def is_over_capacity_atomic(max_cap: int, call_sid: str = "unknown") -> bool:
    """
    Atomically checks if the current SID is already tracked or if we are at capacity.
    Resolves M1 TOCTOU race condition using Lua compare-and-act.
    Returns: True if over capacity (should reject), False if safe.
    """
    global _local_counter, _local_sids
    
    if not redis_client:
        async with _get_lock():
            is_member = call_sid in _local_sids
            if not is_member and _local_counter >= max_cap:
                return True
            if _local_counter > max_cap:
                return True
            return False
            
    try:
        result = redis_client.eval(LUA_CHECK_CAPACITY_ATOMIC, 2, COUNTER_KEY, ACTIVE_SIDS_KEY, call_sid, max_cap)
        return bool(result)
    except Exception as e:
        logger.error(f"Redis is_over_capacity_atomic error: {e}")
        # Fallback to local
        async with _get_lock():
            is_member = call_sid in _local_sids
            if not is_member and _local_counter >= max_cap:
                return True
            if _local_counter > max_cap:
                return True
            return False
