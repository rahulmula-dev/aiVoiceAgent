import os
import logging
import redis

logger = logging.getLogger("concurrency")

# Connect to Redis
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
try:
    # Use extremely low timeouts (100ms) so if Redis is offline locally, 
    # it instantly fails over to the local RAM counter without freezing the Uvicorn event loop.
    redis_client = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=0.1, socket_connect_timeout=0.1)
except Exception as e:
    logger.error(f"Failed to connect to Redis: {e}")
    redis_client = None

COUNTER_KEY = "active_inbound_calls"
MAX_INBOUND_CALLS = 30
TTL_SECONDS = 3600  # 1 hour safety TTL

# Fallback for local testing without redis
_local_counter = 0

def reset_active_calls() -> None:
    global _local_counter
    _local_counter = 0
    if redis_client:
        try:
            redis_client.set(COUNTER_KEY, 0)
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

def increment_active_calls() -> int:
    global _local_counter
    if not redis_client:
        _local_counter += 1
        return _local_counter
    try:
        current = redis_client.incr(COUNTER_KEY)
        # Set a TTL for safety against zombie increments
        redis_client.expire(COUNTER_KEY, TTL_SECONDS)
        return current
    except Exception as e:
        logger.error(f"Redis incr error: {e}")
        _local_counter += 1
        return _local_counter

def increment_if_under_cap(max_cap: int) -> tuple[bool, int]:
    """
    Atomically checks if the counter is under the capacity limit and increments it.
    Solves the TOCTOU (Time-Of-Check to Time-Of-Use) race condition.
    Returns: (is_accepted, new_count)
    """
    global _local_counter
    
    # Local fallback logic (uses GIL for atomic-like behavior in single process)
    if not redis_client:
        if _local_counter >= max_cap:
            return False, _local_counter
        _local_counter += 1
        return True, _local_counter
        
    try:
        # Atomic INCR first
        current = redis_client.incr(COUNTER_KEY)
        redis_client.expire(COUNTER_KEY, TTL_SECONDS)
        
        # If we exceeded the cap, immediately DECR and reject
        if current > max_cap:
            redis_client.decr(COUNTER_KEY)
            return False, current - 1
            
        return True, current
    except Exception as e:
        logger.error(f"Redis increment_if_under_cap error: {e}")
        # Fallback to local
        if _local_counter >= max_cap:
            return False, _local_counter
        _local_counter += 1
        return True, _local_counter

def decrement_active_calls() -> int:
    global _local_counter
    if not redis_client:
        _local_counter = max(0, _local_counter - 1)
        return _local_counter
    try:
        current = redis_client.decr(COUNTER_KEY)
        if current < 0:
            redis_client.set(COUNTER_KEY, 0)
            return 0
        return current
    except Exception as e:
        logger.error(f"Redis decr error: {e}")
        _local_counter = max(0, _local_counter - 1)
        return _local_counter
