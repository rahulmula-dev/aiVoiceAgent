# Step 9 — Concurrency gate (Redis Lua CAS)

**Status:** ✅ done  
**Risk to working pipeline:** NONE — feature-flagged OFF by default; server starts cleanly without Redis  
**Time:** ~2 hours  

## What this step does

Caps the number of simultaneous inbound calls the agent accepts. When a new
call arrives at `/voice` and the active-call counter is at capacity, Twilio
receives a "try again later" TwiML response instead of being connected to the
WebSocket pipeline.

```
Twilio dials
     │
     ▼
 /voice  HTTP
     │
     ├── gate disabled or Redis error → admit (fail-open)
     │
     ├── counter < MAX_CONCURRENT_CALLS → INCR, set per-call key → admit → TwiML with <Stream>
     │
     └── counter >= MAX_CONCURRENT_CALLS → REJECT → <Say> busy message + <Hangup/>

Call ends
     │
     ▼
 /api/call-status  (Twilio status callback)
     │
     └── terminal status (completed/failed/busy/no-answer/canceled)
           → release per-call key → DECR counter
```

## Why Redis + Lua?

Redis is already in `docker-compose.yml`. The counter must be **atomic** —
a plain `GET/INCR` sequence has a race condition where two concurrent `/voice`
hits both see count=4 (under cap) and both increment, overshooting the limit.

A **Lua script** executes as a single Redis command — no other command can
interleave between the `GET` and the `INCR`. This is the correct primitive for
a concurrency gate.

## Files added / changed

| File | Change |
|---|---|
| `utils/redis_gate.py` | **NEW** — `ConcurrencyGate` with `acquire()` / `release()` / `current_count()` |
| `config/__init__.py` | `CONCURRENCY_GATE_ENABLED`, `REDIS_URL`, `MAX_CONCURRENT_CALLS` |
| `orchestrator/factory.py` | `init_gate()` / `get_gate()` singleton; `init_gate()` added to startup sequence |
| `telephony/server.py` | `/voice` checks gate before admitting; `/api/call-status` releases slot on terminal states |
| `run_server.py` | `await init_gate()` after pool warmup; banner line when gate is active |

## How the Lua scripts work

### Acquire (on `/voice`)

```lua
local cur = tonumber(redis.call('GET', KEYS[1]) or 0)
if cur >= cap then return -1 end          -- reject
local new = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ttl)       -- rolling TTL safety net
return new
```

Returns the new counter value on success, `-1` on rejection.
The TTL on the counter key (3600 s) is a safety net: if the process crashes
without flushing the counter, it auto-resets within an hour.

### Release (on `/api/call-status`)

```lua
if redis.call('EXISTS', KEYS[2]) == 0 then return 0 end  -- not tracked → skip
redis.call('DEL', KEYS[2])
local n = redis.call('DECR', KEYS[1])
if tonumber(n) < 0 then redis.call('SET', KEYS[1], 0) end
return n
```

`KEYS[2]` is the per-call tracking key (`cila:call:<sid>`). This makes release
**idempotent** — Twilio occasionally delivers duplicate status callbacks, and
without the guard each duplicate would decrement the counter below the true value.

## Redis keys used

| Key | Type | Value | TTL |
|---|---|---|---|
| `cila:active_calls` | integer | current active call count | 3600 s (rolling) |
| `cila:call:<call_sid>` | string | `"1"` (presence flag) | 400 s |

The 400 s per-call TTL is a hard safety net: a 5-minute call (300 s) + 100 s
buffer. If Twilio never fires the status callback, the slot recovers automatically.

## Fail-open design

Both `acquire()` and `release()` catch all exceptions and log them. A Redis
network hiccup never drops a live call:

- `acquire()` error → `admitted = True` (call goes through)
- `release()` error → logged, counter may drift but recovers on next TTL expiry

## How to enable

### 1. Make sure Redis is running

```powershell
docker-compose up -d redis
docker ps   # cila-redis should show "healthy"
```

### 2. Add to `.env`

```
CONCURRENCY_GATE_ENABLED=true
REDIS_URL=redis://localhost:6379   # local dev (host port exposed by docker-compose)
MAX_CONCURRENT_CALLS=5             # adjust to taste
```

### 3. Restart the server

```powershell
uv run python run_server.py
```

Startup output when gate is active:
```
[MAIN] Starting modular voice pipeline server on port 5000
       STT -> Deepgram nova-3
       LLM -> Groq     llama-3.1-8b-instant
       TTS -> ElevenLabs eleven_flash_v2_5
       Gate -> Redis    max 5 concurrent calls
[MAIN] Warming up connection pools...
[POOL/STT] Pre-warming 2 Deepgram connections...
[POOL/TTS] ElevenLabs connection warmed (HTTP 200)
[POOL/STT] 2/2 connections ready
[GATE] Concurrency gate ready — max 5 concurrent calls
[MAIN] Waiting for Twilio calls...
```

Per-call output (admitted):
```
[TWIML] /voice hit  -> wss://abc123.ngrok.io/  call_sid=CA1234...
```

Per-call output (rejected):
```
[GATE] Rejecting CA5678... — at capacity (5 active)
```

On call end:
```
[GATE] Released slot for CA1234... (completed), active=4
```

## What `MAX_CONCURRENT_CALLS=5` means in practice

| Call rate | Avg call duration | Calls/hour served |
|---|---|---|
| 5 concurrent | 3 min | ~100 |
| 5 concurrent | 5 min | ~60 |

For a cosmetology school with one demo line, 5 is generous. Adjust via
`MAX_CONCURRENT_CALLS` in `.env` without touching code.

## Known follow-ups (not in Step 9 scope)

- **`/healthz` Redis check** — the healthz probe currently returns 200 without
  checking Redis. Add `await gate.current_count()` there when the gate is enabled.
- **Graceful pool shutdown on SIGTERM** — `ConnectionPools.shutdown()` and the
  Redis client's `aclose()` are both implemented but not yet wired to the uvicorn
  shutdown signal. Low priority for local dev.
- **Metrics** — `gate.current_count()` could be polled and logged to the
  sealed call summary for capacity planning.
