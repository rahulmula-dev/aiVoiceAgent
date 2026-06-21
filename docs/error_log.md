# Error log — issues encountered and fixes

Chronological record of errors hit during the parity port (Steps 6–9).
Each entry has the error, root cause, fix applied, and a reference to where the fix lives.

---

## E-01 — asyncpg.InvalidPasswordError (Step 7, Jun 23)

**Error:**
```
asyncpg.exceptions.InvalidPasswordError: password authentication failed for user "postgres"
```

**Root cause:**
Docker initialized the Postgres container without substituting `${POSTGRES_PASSWORD}` from `.env`. The container started with an empty password, but `asyncpg` was connecting with the password string `${POSTGRES_PASSWORD}` literally.

**Fix:**
1. Stopped the container and removed the volume so Postgres would re-initialize:
   ```powershell
   docker-compose down
   docker volume rm aivoiceagent_pgdata
   docker-compose up -d postgres
   ```
2. After restart, explicitly set the password via `docker exec`:
   ```powershell
   docker exec cila-postgres psql -U postgres -c "ALTER USER postgres WITH PASSWORD 'cila_dev';"
   ```
3. Confirmed `PG_DATABASE_URL` in `.env` used the correct password.

**Reference:** `docker-compose.yml` (POSTGRES_PASSWORD env var), `.env` (PG_DATABASE_URL)

---

## E-02 — Wrong Docker volume name (Step 7, Jun 23)

**Error:**
```
Error response from daemon: no such volume: ai-voice-agent_pgdata
```

**Root cause:**
Docker named the volume based on the compose project name (derived from the folder name), which was `aivoiceagent_pgdata` not `ai-voice-agent_pgdata`.

**Fix:**
```powershell
docker volume ls   # find the actual name
docker volume rm aivoiceagent_pgdata
```

**Reference:** `docker-compose.yml` (volumes section at bottom)

---

## E-03 — Port 5432 conflict with local Windows Postgres (Step 7, Jun 23)

**Error:**
```
asyncpg.exceptions.InvalidPasswordError  (persisted even after volume reset)
```

**Root cause:**
The local machine had `postgresql-x64-18` Windows service running on port 5432. When asyncpg connected to `localhost:5432`, it was hitting the local Windows Postgres (different credentials) instead of the Docker container.

```powershell
netstat -ano | findstr ":5432"   # showed two processes on 5432
Get-Service postgresql*           # confirmed service Running
```

**Fix:**
Changed `docker-compose.yml` port mapping from `5432:5432` → `5433:5432` (host:container). Updated `PG_DATABASE_URL` in `.env` to use port 5433. No code changes needed.

**Reference:** `docker-compose.yml` line 51 (`"5433:5432"`), `.env` (PG_DATABASE_URL)

---

## E-04 — asyncpg ModuleNotFoundError (Step 7, Jun 23)

**Error:**
```
ModuleNotFoundError: No module named 'asyncpg'
```

**Root cause:**
`asyncpg` was imported at the top of `retrieval/vector_store.py` and `retrieval/migrate_to_pgvector.py`. When `RAG_ENABLED=false`, those modules still get imported by Python even if RAG is unused — causing a crash at startup.

**Fix:**
Moved `import asyncpg` (and `from pgvector.asyncpg import register_vector`) inside the functions that actually use them (`_ensure_pool()` and `run()`). The modules now compile and import cleanly without the packages installed.

**Reference:** `retrieval/vector_store.py` (`_ensure_pool()` function), `retrieval/migrate_to_pgvector.py` (`run()` function)

---

## E-05 — ElevenLabs 401 quota_exceeded (Step 7, Jun 23)

**Error:**
```
[TTS] ElevenLabs 401 — check ELEVENLABS_API_KEY and account quota
```

**Root cause:**
Free-tier ElevenLabs account had only 7 characters of quota remaining. Not a code bug.

**Fix:**
Added a new ElevenLabs API key from a fresh personal account. Updated `ELEVENLABS_API_KEY` in `.env`. (Done 2026-06-24.)

**Reference:** `.env` (ELEVENLABS_API_KEY)

---

## E-06 — Redis ModuleNotFoundError (Step 9, Jun 24)

**Error:**
```
[GATE] Redis init failed (ModuleNotFoundError: No module named 'redis') — gate disabled, all calls admitted
```

**Root cause:**
The `redis` Python package was not installed in the venv. The gate imports `redis.asyncio` lazily inside `init_gate()` — so server startup didn't crash, but the gate silently disabled itself.

**Fix:**
```powershell
uv pip install "redis>=5.0"
```

**Reference:** `orchestrator/factory.py` (`init_gate()` — lazy import inside try/except), `utils/redis_gate.py`

---

## E-07 — Redis ConnectionError (Step 9, Jun 24)

**Error:**
```
[GATE] Redis init failed (ConnectionError: Error 22 connecting to localhost:6379. The remote computer refused the network connection.) — gate disabled, all calls admitted
```

**Root cause:**
The `redis` Python package was installed, but the Redis server (Docker container) was not running.

**Fix:**
```powershell
docker-compose up -d redis
docker ps --filter name=cila-redis   # verify (healthy)
```

**Reference:** `docker-compose.yml` (redis service, port 6379), cold-start runbook Terminal 1 step

---

## E-08 — Gate showed `active=1` instead of `active=0` after call end (Step 9, Jun 24)

**Error (not a crash — unexpected counter value):**
```
[GATE] Released slot for CAd95f... (completed), active=1
```

**Root cause:**
The previous test call (`CA1464...`) completed before the Twilio status callback URL was configured on the number. That call incremented the gate counter to 1 but the decrement callback never fired. The stale slot sat in Redis.

When the next call ran (with the callback now wired), the counter went 1 → 2 on acquire, then 2 → 1 on release — hence `active=1`.

**Fix:**
No code fix needed. The gate has a 400-second TTL on each per-call key — the stale slot self-clears automatically. The next call after the TTL expired showed `active=0`.

To prevent this in future: always run the full Twilio update command (including `--status-callback`) before the first test call. This is now in the cold-start runbook.

**Reference:** `telephony/server.py` (`/api/call-status` — release logic), `utils/redis_gate.py` (`_CALL_TTL = 400`), cold-start runbook Terminal 3 step

---

## E-09 — ElevenLabs warmup returns HTTP 401 on every startup (ongoing, non-blocking)

**Error (cosmetic — synthesis still works):**
```
[POOL/TTS] ElevenLabs connection warmed (HTTP 401)
```

**Root cause:**
The TTS warmup fires a `GET /v1/models` request to pre-establish the HTTP connection. The `/v1/models` endpoint requires a paid ElevenLabs account — the free-tier key gets a 401. Synthesis itself (`POST /v1/text-to-speech/...`) works fine on the free tier.

**Fix (not yet applied):**
Switch the warmup endpoint from `GET /v1/models` to `GET /v1/user/subscription` — accessible on all account tiers.

**Reference:** `utils/connection_pool.py` (`ElevenLabsPool.warmup()` method)

---

*Last updated: 2026-06-24*
