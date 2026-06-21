# """
# telephony/concurrency.py
# ------------------------
# Distributed concurrency control for inbound telephony calls.

# WHAT THIS FILE DOES:
#     Implements a hard cap on the number of simultaneous active calls the voice
#     agent will accept. Every inbound call must pass through `increment_if_under_cap`
#     before any audio processing begins. When a call ends, `decrement_active_calls`
#     is called to free that slot.

# WHY IT EXISTS:
#     Without a concurrency cap, a burst of inbound calls could overwhelm the STT/TTS
#     connection pools, exhaust the ElevenLabs/Deepgram API rate limits, or saturate
#     the server's CPU — degrading audio quality for all active callers. A naive
#     in-process Python counter would race under concurrent asyncio tasks and could
#     admit more calls than the cap allows. This module solves that with Redis + Lua
#     scripts that perform atomic Compare-And-Increment operations, which are safe
#     even across multiple uvicorn worker processes or Kubernetes pods.

# HOW IT FITS IN THE SYSTEM:
#     ┌──────────────┐    /voice webhook      ┌─────────────────┐
#     │   Twilio     │ ─────────────────────► │  server.py       │
#     └──────────────┘                        │  handle_incoming_call │
#                                             │  calls increment_if_under_cap ──► concurrency.py
#                                             │  rejected? → TwiML apology + hangup
#                                             │  accepted? → TwiML Stream URL
#                                             └──────────┬──────────┘
#                                                        │ /api/call-status callback (terminal state)
#                                                        ▼
#                                             decrement_active_calls (concurrency.py)

#     The counter lives in Redis so all pods share one source of truth.
#     In local dev (LOCAL_TEST=true) or when Redis is absent, all operations degrade
#     to an in-process asyncio.Lock + Python counter, which is safe for single-process
#     deployments.

# KEY EXPORTS:
#     increment_if_under_cap(max_cap, call_sid)  -- atomically admit or reject a new call
#     decrement_active_calls(call_sid)           -- release a call slot on terminal status
#     is_over_capacity_atomic(max_cap, call_sid) -- non-mutating capacity check (read-only)
#     get_active_call_count()                    -- read the current live counter value
#     reset_active_calls()                       -- zero-out counters on pod startup
#     check_redis_health()                       -- called by /readyz to verify Redis
#     MAX_INBOUND_CALLS                          -- the configured hard cap value

# FALLBACK BEHAVIOUR:
#     When Redis is unreachable (e.g. local development without Docker), every Redis
#     operation falls back to in-process Python variables (_local_counter, _local_sids)
#     protected by an asyncio.Lock. This is ONLY safe for single-process deployments
#     (one uvicorn worker, no horizontal pod scaling).
# """

# import os                  # Read environment variables (REDIS_URL, LOCAL_TEST, etc.)
# import logging             # Standard Python logging — output propagates to the root logger
# import redis               # redis-py: synchronous Redis client used inside async code via eval()

# # ---------------------------------------------------------------------------
# # Module-scoped logger
# # ---------------------------------------------------------------------------

# # All log lines from this module are prefixed with "concurrency" in the log output,
# # making it easy to grep for admission-control events in production logs.
# logger = logging.getLogger("concurrency")

# # ---------------------------------------------------------------------------
# # Redis connection setup
# # ---------------------------------------------------------------------------

# # Read the Redis connection URL from the environment.
# # Default: local Redis instance used during development.
# # Production: set REDIS_URL to point at the Redis service in the Docker/K8s network,
# #             e.g. "redis://redis-service:6379".
# REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

# try:
#     # Build a synchronous Redis client from the URL.
#     # decode_responses=True  — return str instead of bytes for all GET/EVAL results.
#     # socket_timeout=1.0     — individual socket operations time out after 1 second,
#     #                          preventing a hung Redis from blocking an inbound call.
#     # socket_connect_timeout=1.0 — TCP connect attempt caps at 1 second so the server
#     #                              starts cleanly even when Redis is temporarily down.
#     redis_client = redis.from_url(
#         REDIS_URL,
#         decode_responses=True,
#         socket_timeout=1.0,
#         socket_connect_timeout=1.0
#     )

#     # [FIX] Perform a lightweight PING at startup to detect Redis availability early.
#     # Without this, the first call would hang for up to 1 second before failing over
#     # to the local counter, adding latency to the very first inbound call.
#     if os.getenv("LOCAL_TEST", "false").lower() == "true":
#         # In LOCAL_TEST mode we eagerly verify the connection so we can switch to
#         # the in-process fallback immediately rather than discovering the failure
#         # on the first real call.
#         logger.info(f"Local test mode detected. Verifying Redis at {REDIS_URL}...")
#         try:
#             redis_client.ping()          # Raises redis.ConnectionError if Redis is down
#             logger.info("Redis is available.")
#         except Exception:
#             # Redis is not running locally — degrade gracefully to RAM counters.
#             logger.warning("Redis not found. Falling back to local RAM counters for development.")
#             redis_client = None          # Signal to all functions to use the local fallback

# except Exception as e:
#     # Any exception during client construction (bad URL format, DNS failure, etc.)
#     # must not crash the process — set to None and fall back to the local counter.
#     logger.error(f"Failed to connect to Redis: {e}")
#     redis_client = None

# # ---------------------------------------------------------------------------
# # Configuration constants
# # ---------------------------------------------------------------------------

# # Import the global runtime configuration object.
# # `config` reads MAX_INBOUND_CALLS (and other settings) from environment variables
# # or a config file, so the cap can be changed without a code deploy.
# from contracts.config import config

# # Redis key that holds the integer count of currently active calls.
# # We use SET/INCR/DECR on this key to maintain the counter.
# COUNTER_KEY = "active_inbound_calls"

# # Redis key for a SET of call SIDs that are currently counted as active.
# # Storing SIDs separately from the integer counter enables two critical properties:
# #   1. Idempotency — the same SID is never counted twice (handles duplicate Twilio webhooks).
# #   2. Safe decrement — we only decrement for SIDs we actually admitted, preventing
# #      the counter from going negative when rejected or stale status callbacks arrive.
# ACTIVE_SIDS_KEY = "active_call_sids"

# # Maximum number of simultaneous inbound calls the system will accept.
# # Sourced from config.max_inbound_calls so it can be changed via an env var without
# # redeploying the container image.
# MAX_INBOUND_CALLS = config.max_inbound_calls

# # Safety TTL applied to both Redis keys after every write.
# # If the server crashes mid-call and leaves the counter elevated, Redis will
# # automatically expire both keys after 1 hour, resetting the state without
# # manual intervention.
# TTL_SECONDS = 3600  # 1 hour — long enough for the longest plausible call

# # ---------------------------------------------------------------------------
# # Lua scripts for atomic Redis operations
# # ---------------------------------------------------------------------------
# # All critical admission-control logic runs inside Lua scripts.
# # Redis executes Lua scripts as a single indivisible command — no other Redis
# # operation from any client can interleave between the script's READ and WRITE steps.
# # This eliminates the classic TOCTOU (Time-Of-Check to Time-Of-Use) race that would
# # occur if we used separate GET then INCR commands:
# #
# #   Race without Lua:
# #     Pod A: GET → 29  (one below cap of 30)
# #     Pod B: GET → 29  (same value, Pod A hasn't incremented yet)
# #     Pod A: INCR → 30  ✓ admitted
# #     Pod B: INCR → 31  ✗ OVER CAP — should have been rejected!
# #
# #   With Lua: Pod B's eval cannot start until Pod A's eval finishes → 31 never happens.

# # ------------------------------------------------------------------
# # LUA_INCREMENT_IF_UNDER_CAP
# # ------------------------------------------------------------------
# # Purpose : Atomically admit a new call if the counter is under the cap.
# # KEYS[1] : COUNTER_KEY        — the integer active-call counter
# # KEYS[2] : ACTIVE_SIDS_KEY    — the set of admitted call SIDs
# # ARGV[1] : call_sid           — the Twilio SID for the incoming call
# # ARGV[2] : max capacity       — the configured cap (MAX_INBOUND_CALLS)
# # ARGV[3] : TTL in seconds     — applied to both keys after every write
# #
# # Return values:
# #   -2   → SID already in the set (duplicate Twilio webhook) — caller is admitted, no double-count
# #   -1   → counter is at/above cap — caller must be rejected
# #   N>0  → new counter value after increment — caller is admitted
# LUA_INCREMENT_IF_UNDER_CAP = """
# if (redis.call('SISMEMBER', KEYS[2], ARGV[1]) == 1) then
#     return -2
# end

# local current = redis.call('GET', KEYS[1])
# if (current and tonumber(current) >= tonumber(ARGV[2])) then
#     return -1
# else
#     local new = redis.call('INCR', KEYS[1])
#     redis.call('SADD', KEYS[2], ARGV[1])
#     redis.call('EXPIRE', KEYS[1], ARGV[3])
#     redis.call('EXPIRE', KEYS[2], ARGV[3])
#     return new
# end
# """

# # ------------------------------------------------------------------
# # LUA_DECREMENT_ONLY_IF_TRACKED
# # ------------------------------------------------------------------
# # Purpose : Safely decrement the counter only if the given SID was admitted.
# #           This prevents the counter from going negative when Twilio sends
# #           status callbacks for calls that were rejected at the /voice endpoint
# #           or when duplicate "completed" events arrive.
# # KEYS[1] : COUNTER_KEY
# # KEYS[2] : ACTIVE_SIDS_KEY
# # ARGV[1] : call_sid
# #
# # Return values:
# #   -1   → SID was not in the set — no decrement performed (idempotent no-op)
# #   N>=0 → new counter value after successful decrement (clamped to 0 if negative)
# LUA_DECREMENT_ONLY_IF_TRACKED = """
# if (redis.call('SREM', KEYS[2], ARGV[1]) == 1) then
#     local current = redis.call('DECR', KEYS[1])
#     if (tonumber(current) < 0) then
#         redis.call('SET', KEYS[1], 0)
#         return 0
#     end
#     return current
# else
#     return -1
# end
# """

# # ------------------------------------------------------------------
# # LUA_CHECK_CAPACITY_ATOMIC
# # ------------------------------------------------------------------
# # Purpose : Read-only capacity probe — does NOT modify any Redis state.
# #           Used for mid-stream checks (e.g. inside the orchestrator) to
# #           verify we haven't exceeded capacity without side effects.
# # KEYS[1] : COUNTER_KEY
# # KEYS[2] : ACTIVE_SIDS_KEY
# # ARGV[1] : call_sid      — already-admitted SIDs pass through (return 0)
# # ARGV[2] : max capacity
# #
# # Return values:
# #   0 → safe to continue (either already tracked, or counter is under cap)
# #   1 → over capacity — call should be rejected or warned
# LUA_CHECK_CAPACITY_ATOMIC = """
# local is_member = redis.call('SISMEMBER', KEYS[2], ARGV[1])
# local current = redis.call('GET', KEYS[1])
# local count = current and tonumber(current) or 0
# if (is_member == 0 and count >= tonumber(ARGV[2])) then
#     return 1 -- Reject: Not tracked and at/over cap
# end
# if (count > tonumber(ARGV[2])) then
#     return 1 -- Reject: Extreme overflow (counter leak guard)
# end
# return 0 -- Safe
# """

# # ---------------------------------------------------------------------------
# # Local (in-process) fallback state
# # ---------------------------------------------------------------------------
# # These variables mirror the Redis counter and SID set for environments where
# # Redis is not available (LOCAL_TEST mode, unit tests, bare-metal dev without Docker).
# # IMPORTANT: The local fallback is only safe for a SINGLE uvicorn worker process.
# # In production (multiple pods or workers) Redis is mandatory for correctness.

# # Shadow integer counter — mirrors COUNTER_KEY when Redis is unavailable.
# _local_counter = 0

# # Shadow SID set — mirrors ACTIVE_SIDS_KEY for idempotency in the fallback path.
# _local_sids = set()

# # asyncio.Lock protecting the local counter — created lazily inside _get_lock()
# # because asyncio.Lock() must be instantiated inside a running event loop
# # (Python 3.10+ issues a DeprecationWarning; Python 3.12+ raises outright).
# _local_lock = None

# import asyncio   # Needed for asyncio.Lock and async def functions in this module

# def _get_lock() -> asyncio.Lock:
#     """
#     Returns the module-level asyncio.Lock, creating it on first call.

#     WHY LAZY: asyncio.Lock() must be created inside a running event loop.
#     If we called asyncio.Lock() at module import time (before uvicorn starts the
#     event loop), Python 3.12+ would raise a RuntimeError. Deferring construction
#     to the first use inside an async context avoids this entirely.
#     """
#     global _local_lock
#     if _local_lock is None:
#         # Create the lock inside the currently running event loop.
#         _local_lock = asyncio.Lock()
#     return _local_lock

# # ---------------------------------------------------------------------------
# # Public utility / health functions
# # ---------------------------------------------------------------------------

# def check_redis_health() -> bool:
#     """
#     Verifies Redis connectivity for the /readyz readiness probe.

#     Called from server.py's /readyz handler (via asyncio.to_thread so the
#     synchronous socket operation does not block the event loop).

#     Returns:
#         True  — Redis responded to PING, or LOCAL_TEST mode is active
#                 (in which case the in-process fallback IS the intentional backend).
#         False — Redis is unreachable or raised an exception.
#     """
#     if not redis_client:
#         # If we have no client, we are in local-test / no-Redis mode.
#         # Return True only if LOCAL_TEST is explicitly set, so that the /readyz
#         # probe passes in dev but fails in production if Redis is misconfigured.
#         return os.getenv("LOCAL_TEST", "false").lower() == "true"
#     try:
#         # PING is the lightest possible Redis operation — a round-trip that
#         # confirms the TCP connection and Redis daemon are both alive.
#         return redis_client.ping()
#     except Exception as e:
#         logger.warning(f"Redis health check failed: {e}")
#         return False

# def reset_active_calls() -> None:
#     """
#     Resets both the call counter and the SID set to zero / empty.

#     WHEN CALLED: During the FastAPI startup_event in server.py.
#     WHY NEEDED:  If the previous pod crashed mid-call, the Redis counter may
#                  still show 5, 10, or more "active" calls that are actually gone.
#                  Resetting on startup guarantees the new pod starts with an
#                  accurate baseline of zero, not a stale count that would reject
#                  legitimate calls immediately after a crash-restart.

#     Also resets the local in-process fallback variables for consistency so that
#     the local and Redis states are always in sync at startup.
#     """
#     global _local_counter, _local_sids

#     # Always reset the in-process shadow counters, regardless of Redis availability.
#     _local_counter = 0
#     _local_sids = set()

#     if redis_client:
#         try:
#             # Use SET rather than DEL so TTL behaviour stays predictable:
#             # deleting the key and then incrementing would not apply a TTL,
#             # leaving the key without expiry protection after a crash.
#             redis_client.set(COUNTER_KEY, 0)

#             # DELETE the SID set entirely — SET doesn't apply to Redis sets.
#             # On the next admission, SADD will create the key fresh with a new TTL.
#             redis_client.delete(ACTIVE_SIDS_KEY)
#             logger.info("Active call counter reset to 0 in Redis.")
#         except Exception as e:
#             logger.error(f"Redis reset error: {e}")
#     else:
#         logger.info("Active call counter reset to 0 locally.")

# def get_active_call_count() -> int:
#     """
#     Returns the current number of active (admitted) calls.

#     Reads from Redis when available; falls back to the local shadow counter.
#     The result is clamped to >= 0 as a defensive guard against transient negative
#     values that could occur from a Redis bug or manual key manipulation.

#     Called by:
#         - /api/live-context/data (dashboard data endpoint)
#         - decrement_active_calls / increment_if_under_cap (to return final count)
#     """
#     global _local_counter

#     if not redis_client:
#         # Return the local shadow counter, clamped to avoid confusing negative values.
#         return max(0, _local_counter)

#     try:
#         val = redis_client.get(COUNTER_KEY)
#         # GET returns None when the key doesn't exist yet (e.g. first call after a DEL).
#         # Treat None as 0 — no active calls.
#         return int(val) if val else 0
#     except Exception as e:
#         logger.error(f"Redis get error: {e}")
#         # If Redis read fails mid-call, return the local shadow as best-effort.
#         return max(0, _local_counter)

# def _debug_force_increment(call_sid: str = "unknown") -> int:
#     """
#     WARNING: Internal testing helper ONLY. Do not use in production admission paths.

#     Bypasses the atomic Lua admission control and directly increments the counter.
#     Used in unit tests to pre-populate the counter (e.g. to simulate a full server
#     so rejection logic can be tested) without going through the full admission flow.

#     Production code MUST use increment_if_under_cap() instead.
#     """
#     global _local_counter, _local_sids

#     if not redis_client:
#         # Idempotent local increment: only count each SID once.
#         if call_sid not in _local_sids:
#             _local_counter += 1
#             _local_sids.add(call_sid)
#         return _local_counter

#     try:
#         # Use standard INCR without the cap check — intentionally uncapped for tests.
#         current = redis_client.incr(COUNTER_KEY)
#         redis_client.sadd(ACTIVE_SIDS_KEY, call_sid)   # Track the SID so decrement works
#         redis_client.expire(COUNTER_KEY, TTL_SECONDS)   # Refresh the safety TTL
#         redis_client.expire(ACTIVE_SIDS_KEY, TTL_SECONDS)
#         return current
#     except Exception as e:
#         logger.error(f"Redis incr error: {e}")
#         # Fall back to local counter if Redis is unavailable even in test mode.
#         _local_counter += 1
#         return _local_counter

# # ---------------------------------------------------------------------------
# # Async admission-control functions (the public API called per inbound call)
# # ---------------------------------------------------------------------------

# async def increment_if_under_cap(max_cap: int, call_sid: str = "unknown") -> tuple[bool, int]:
#     """
#     Atomically check whether the server is under the concurrency cap and admit the call.

#     This is the MAIN GATE for every inbound call. Called from server.py's
#     handle_incoming_call() immediately after the Twilio signature is verified.

#     RACE CONDITION PREVENTION:
#         Without atomicity two simultaneous calls arriving at a fully-loaded server
#         (counter = max_cap - 1) could both see count < max_cap, both increment, and
#         both be admitted — pushing the system 1 slot over capacity. The Lua script
#         makes the read-and-increment indivisible, so only one of them wins.

#     IDEMPOTENCY:
#         If Twilio re-sends the same /voice webhook (e.g. on a timeout retry), the
#         same call_sid arrives twice. The Lua script detects the SID already in the
#         set and returns -2 without double-incrementing, so the call is re-admitted
#         at no extra cost.

#     Args:
#         max_cap:  The maximum simultaneous calls to allow. Usually MAX_INBOUND_CALLS.
#         call_sid: The Twilio CallSid from the /voice form data. Used as the
#                   idempotency key in the ACTIVE_SIDS_KEY Redis set.

#     Returns:
#         (is_accepted: bool, current_count: int)
#             is_accepted == True  → the call was admitted; proceed to TwiML Stream.
#             is_accepted == False → the cap was hit; return the apology TwiML.
#             current_count is the counter value AFTER this operation completes.
#     """
#     global _local_counter, _local_sids

#     if not redis_client:
#         # ── LOCAL FALLBACK PATH (no Redis) ──────────────────────────────────
#         # Use asyncio.Lock to serialise access across concurrent async tasks in
#         # the same process. This is correct only for single-worker deployments.
#         async with _get_lock():
#             if call_sid in _local_sids:
#                 # Idempotent re-admission: this SID is already counted.
#                 return True, _local_counter
#             if _local_counter >= max_cap:
#                 # At or over the cap — reject this call.
#                 return False, _local_counter
#             # Under cap — admit the call and record the SID.
#             _local_counter += 1
#             _local_sids.add(call_sid)
#             return True, _local_counter

#     try:
#         # ── REDIS PATH (production) ─────────────────────────────────────────
#         # eval() sends the Lua script to Redis for atomic execution.
#         # Arguments: script, num_keys, key1, key2, arg1, arg2, arg3
#         result = redis_client.eval(
#             LUA_INCREMENT_IF_UNDER_CAP,
#             2,                   # number of KEYS arguments
#             COUNTER_KEY,         # KEYS[1]: the integer counter key
#             ACTIVE_SIDS_KEY,     # KEYS[2]: the SID set key
#             call_sid,            # ARGV[1]: the call SID to admit
#             max_cap,             # ARGV[2]: the cap threshold
#             TTL_SECONDS          # ARGV[3]: TTL to refresh on the keys
#         )

#         if result == -1:
#             # The cap was hit — the call must be rejected.
#             # Log at WARNING level so the metrics/alerting pipeline can count
#             # capacity rejection events and trigger auto-scaling alerts.
#             logger.warning(
#                 f"[METRIC] Concurrency cap hit! Limit: {max_cap}. Rejecting SID: {call_sid}"
#             )
#             return False, get_active_call_count()

#         if result == -2:
#             # The SID was already in the active set (duplicate Twilio webhook).
#             # Re-admit without double-counting.
#             return True, get_active_call_count()

#         # Any positive result is the new counter value after successful increment.
#         return True, int(result)

#     except Exception as e:
#         logger.error(f"Redis increment_if_under_cap error: {e}")
#         # ── REDIS FAILURE FALLBACK ──────────────────────────────────────────
#         # On a transient Redis blip, degrade to the local lock rather than
#         # rejecting the call outright. This is safe for short outages.
#         async with _get_lock():
#             if _local_counter >= max_cap:
#                 return False, _local_counter
#             _local_counter += 1
#             _local_sids.add(call_sid)
#             return True, _local_counter


# async def decrement_active_calls(call_sid: str = "unknown") -> int:
#     """
#     Decrement the active call counter when a call terminates.

#     WHEN CALLED:
#         From server.py's handle_call_status() when Twilio posts a terminal
#         CallStatus ("completed", "failed", "busy", "no-answer", "canceled").

#     SAFETY GUARANTEES:
#         1. Only decrements if the given SID is in the active set — prevents the
#            counter from going negative when called for a rejected or unknown call.
#         2. Clamped to 0 — a Lua guard catches any numeric underflow.
#         3. Idempotent — duplicate status callbacks from Twilio are safe; the
#            second call returns -1 from Lua (SID already removed) and is ignored.

#     Args:
#         call_sid: The Twilio CallSid from the /api/call-status form data.

#     Returns:
#         The new active call count after decrement.
#         If the SID was not tracked, returns the current count unchanged.
#     """
#     global _local_counter, _local_sids

#     if not redis_client:
#         # ── LOCAL FALLBACK PATH ─────────────────────────────────────────────
#         async with _get_lock():
#             if call_sid in _local_sids:
#                 _local_sids.remove(call_sid)
#                 # Clamp to 0 — should never be negative, but guard defensively.
#                 _local_counter = max(0, _local_counter - 1)
#             return _local_counter

#     try:
#         # ── REDIS PATH ──────────────────────────────────────────────────────
#         # Lua: atomically SREM + DECR. Returns -1 if the SID was not in the set.
#         result = redis_client.eval(
#             LUA_DECREMENT_ONLY_IF_TRACKED,
#             2,               # number of KEYS arguments
#             COUNTER_KEY,     # KEYS[1]
#             ACTIVE_SIDS_KEY, # KEYS[2]
#             call_sid         # ARGV[1]
#         )

#         if result == -1:
#             # SID was not tracked — ignore this decrement.
#             # Common causes: (1) call was rejected at /voice before being admitted,
#             # (2) Twilio sent a duplicate "completed" event, (3) counter was reset
#             # on pod restart after the call was in-flight.
#             return get_active_call_count()

#         # Return the new counter value after the decrement.
#         return int(result)

#     except Exception as e:
#         logger.error(f"Redis decr error: {e}")
#         # ── REDIS FAILURE FALLBACK ──────────────────────────────────────────
#         async with _get_lock():
#             if call_sid in _local_sids:
#                 _local_sids.remove(call_sid)
#                 _local_counter = max(0, _local_counter - 1)
#             return _local_counter


# async def is_over_capacity_atomic(max_cap: int, call_sid: str = "unknown") -> bool:
#     """
#     Read-only atomic capacity check — does NOT change any state.

#     PURPOSE:
#         Used for mid-stream capacity verification inside the orchestrator (e.g.
#         before spinning up a costly STT connection) without the side effect of
#         incrementing the counter. Think of this as a "peek" at the gate, whereas
#         increment_if_under_cap is the actual gate.

#     ATOMICITY:
#         Uses LUA_CHECK_CAPACITY_ATOMIC so the read of the counter and the SID
#         membership check happen in a single indivisible Redis command. This
#         resolves the M1 TOCTOU race condition identified in the security review.

#     Args:
#         max_cap:  The maximum allowed concurrent calls.
#         call_sid: The SID to check. If the SID is already in the active set
#                   (previously admitted), the function returns False (safe) regardless
#                   of the current counter value — an admitted call cannot be rejected
#                   mid-stream by a capacity spike from other calls.

#     Returns:
#         True  → over capacity; the call should be rejected or warned.
#         False → safe to continue.
#     """
#     global _local_counter, _local_sids

#     if not redis_client:
#         # ── LOCAL FALLBACK PATH ─────────────────────────────────────────────
#         async with _get_lock():
#             is_member = call_sid in _local_sids
#             # Reject if: this SID is not yet admitted AND counter is at/over cap.
#             if not is_member and _local_counter >= max_cap:
#                 return True
#             # Reject if: counter has leaked beyond cap (defensive overflow guard).
#             if _local_counter > max_cap:
#                 return True
#             return False

#     try:
#         # ── REDIS PATH ──────────────────────────────────────────────────────
#         # Execute the read-only Lua script: returns 1 (over cap) or 0 (safe).
#         result = redis_client.eval(
#             LUA_CHECK_CAPACITY_ATOMIC,
#             2,               # number of KEYS arguments
#             COUNTER_KEY,     # KEYS[1]
#             ACTIVE_SIDS_KEY, # KEYS[2]
#             call_sid,        # ARGV[1]
#             max_cap          # ARGV[2]
#         )
#         # bool(1) == True (over cap), bool(0) == False (safe)
#         return bool(result)

#     except Exception as e:
#         logger.error(f"Redis is_over_capacity_atomic error: {e}")
#         # ── REDIS FAILURE FALLBACK ──────────────────────────────────────────
#         async with _get_lock():
#             is_member = call_sid in _local_sids
#             if not is_member and _local_counter >= max_cap:
#                 return True
#             if _local_counter > max_cap:
#                 return True
#             return False
