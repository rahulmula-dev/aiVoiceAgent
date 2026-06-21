# Week 1 — Doc 3: Local Stack Runbook
**Repo:** `ai-voice-agent-dev` | **Date:** 2026-06-17 | **Author:** Cowork Audit

> **Read-only audit.** No code has been modified. All commands below are proposed for human review before execution.

---

## 1. Required Services

| Service | How Provided | Port | Notes |
|---|---|---|---|
| PostgreSQL 16 + pgvector | docker-compose (`pgvector/pgvector:pg16`) | 5432 | Schema auto-applied from `db-init/01_schema.sql` |
| Redis 7 | docker-compose (`redis:7-alpine`) | 6379 | Concurrency counter + session state |
| CILA app server | docker-compose (built from `Dockerfile`) | 8000 | Python 3.11, fastapi + uvicorn |
| ngrok | **Manual** (install separately) | tunnels 8000 | Required for Twilio webhooks |
| Deepgram | External API | wss:// | `DEEPGRAM_API_KEY` required |
| Google Gemini | External API | https:// | `GEMINI_API_KEY` required |
| AWS Bedrock | External API | https:// (prod only) | In `LOCAL_TEST=true` mode, embeddings are mocked — no AWS needed |
| AWS S3 | External API | https:// | Used for log archiving + CRM DLQ. In local dev, S3 failures are logged but non-fatal |
| LeadSquared CRM | External API | https:// | In local dev, CRM failures route to S3 DLQ — non-fatal |

---

## 2. Required Environment Variables

All must be set in `.env` at repo root before starting the stack.

### 2a. Already in `.env` (confirmed present)
| Variable | Used By | Notes |
|---|---|---|
| `GEMINI_API_KEY` | `orchestrator/brain.py` | Google Gemini — primary + fallback LLM |
| `DEEPGRAM_API_KEY` | `stt/transcriber.py`, `tts/synthesizer.py` | Both STT and TTS use the same key |
| `APP_ENV` | `contracts/config.py` | Set to `development` for local dev (enables relaxed timeouts) |
| `LOCAL_TEST` | `retrieval/vector_store.py`, `telephony/concurrency.py` | Set to `true` to skip AWS Bedrock for embeddings and allow Redis fallback |
| `TTS_PROVIDER` | `telephony/server.py` | `deepgram` (default) or `elevenlabs` |
| `PORT` | `run_server.py`, docker-compose | Set to `8000` in docker-compose; set to `8000` for bare Python too (README says 8085 — ignore that) |
| `CRM_API_KEY` | `crm/client.py` | Any non-empty value works locally; real key for live CRM |
| `CRM_BASE_URL` | `crm/client.py` | Needs `https://` prefix. Use `https://api.leadsquared.com` or a mock URL for offline dev |
| `AWS_ACCESS_KEY_ID` | `utils/s3_storage.py` | Needed for S3 log archiving. Can be left as dummy for pure local dev if you tolerate S3 errors |
| `AWS_SECRET_ACCESS_KEY` | `utils/s3_storage.py` | Same as above |

### 2b. Currently **COMMENTED OUT** in `.env` — must uncomment and populate

| Variable | Used By | Required Value Example |
|---|---|---|
| `PG_DATABASE_URL` | `retrieval/vector_store.py` | `postgresql://postgres:YOUR_PG_PASSWORD@localhost:5432/postgres` |
| `REDIS_URL` | `telephony/concurrency.py` | `redis://localhost:6379` |

> ⚠️ **Important:** `PG_DATABASE_URL` must use `localhost:5432` when running the app **outside** Docker, but `cila-postgres:5432` when running **inside** docker-compose (the container's service name). docker-compose sets this correctly only if you use the network; for bare-Python local, use localhost.

### 2c. Variables with defaults that need overriding for local dev

| Variable | Default in Code | Recommended Local Value | Why |
|---|---|---|---|
| `PORT` | 8001 (run_server.py) / 8000 (server.py __main__) | `8000` | Avoid confusion; match docker-compose |
| `POSTGRES_PASSWORD` | None (required by docker-compose) | Any password | Set in `.env`, referenced by docker-compose |
| `NGROK_URL` | None (falls back to `request.host`) | Your ngrok URL e.g. `https://abc123.ngrok.io` | Must be set so `/voice` builds correct WS URL for Twilio |
| `BYPASS_TWILIO_AUTH` | `false` | `true` for local testing without Twilio | Skips Twilio signature check |
| `DEEPGRAM_ENDPOINTING_MS` | `800` | `800` | Default is fine |
| `DEEPGRAM_LANGUAGE` | `multi` | `multi` | Default is fine |

### 2d. Optional variables (not required to start, but affect behavior)

| Variable | Default | Notes |
|---|---|---|
| `INTAKE_ENABLED` | `true` | Set `false` to reject all incoming calls (kill switch) |
| `OV_DISABLE_RETRIEVAL` | `false` | Set `true` to skip RAG (pure LLM) — only works if `APP_ENV=development` |
| `FASTTEXT_MODEL_PATH` | `/app/models/lid.176.ftz` | Only needed when fasttext-wheel is installed |
| `DEEPGRAM_POOL_SIZE` | `5` (dev) | Pool of pre-warmed Deepgram WebSockets |
| `DEEPGRAM_MIN_CONNECTIONS` | `2` (dev) | Minimum warm connections at startup |
| `DPA_CANADA_ACTIVE` | `false` | Set `true` only for Canadian data residency enforcement |
| `PRIMARY_LLM_MODEL` | `gemini-2.0-flash` | Override Gemini model |
| `SILENCE_SOFT_PROMPT_S` | `10` | Seconds before "Are you still there?" |
| `SILENCE_TERMINATION_S` | `20` | Seconds before hanging up on silence |

---

## 3. Startup Order and Dependencies

```
1. docker-compose up postgres redis   ← wait for healthchecks to pass
2. docker-compose up cila-ai-agent    ← starts only after postgres + redis healthy
```

Or all at once: `docker-compose up --build`

The app server's `startup_event` (in `telephony/server.py`) blocks startup until:
- `stt_pool.initialize()` completes (opens `DEEPGRAM_MIN_CONNECTIONS` WebSockets to Deepgram)
- `elevenlabs_pool.initialize()` if `TTS_PROVIDER=elevenlabs`
- `reset_active_calls()` resets Redis call counter

If Deepgram WebSocket connections fail at startup, the server crashes and does not accept calls. This is by design (`raise e` on line 68 in server.py).

---

## 4. Local Stack Step-by-Step

### Step 1 — Prepare `.env`

Minimum working `.env` for local docker-compose:
```env
# Core APIs
GEMINI_API_KEY=your_gemini_key
DEEPGRAM_API_KEY=your_deepgram_key

# App env
APP_ENV=development
LOCAL_TEST=true
PORT=8000

# Database (for docker-compose, app connects to service name)
POSTGRES_PASSWORD=localpassword
PG_DATABASE_URL=postgresql://postgres:localpassword@cila-postgres:5432/postgres

# Redis (for docker-compose, app connects to service name)
REDIS_URL=redis://cila-redis:6379

# TTS
TTS_PROVIDER=deepgram

# CRM (mock for local)
CRM_API_KEY=crm_test_key_123
CRM_BASE_URL=https://api.leadsquared.com

# AWS (can be dummy if S3 errors are acceptable)
AWS_ACCESS_KEY_ID=dummy
AWS_SECRET_ACCESS_KEY=dummy

# Twilio auth bypass for local testing
BYPASS_TWILIO_AUTH=true

# ngrok URL (fill in after step 4)
# NGROK_URL=https://abc123.ngrok.io
```

> ⚠️ **Note on `PG_DATABASE_URL` with `LOCAL_TEST=true`:** Even though embeddings are mocked (all `[1.0]*1536`), the app still connects to Postgres for RAG searches. The connection must succeed. The downside is that all vector searches will return the same candidates (constant embedding) — RAG won't rank meaningfully. This is expected for local dev without real embeddings.

### Step 2 — Build and Start

```bash
docker-compose up --build
```

Expected healthy output:
```
cila-postgres  | database system is ready to accept connections
cila-redis     | Ready to accept connections
cila-ai-agent  | >>> Starting AI Voice Agent Server at http://localhost:8000
cila-ai-agent  | INFO: STT Pool initialized
cila-ai-agent  | INFO: Application startup complete.
```

### Step 3 — Verify Services

```bash
curl http://localhost:8000/healthz      # → {"status":"alive",...}
curl http://localhost:8000/readyz       # → {"status":"ready",...}
```

`/readyz` checks: CRM, KnowledgeBase (Postgres), Redis, and Deepgram API key presence.
- If `LOCAL_TEST=true`, Redis is optional.
- CRM failure is non-fatal ("degraded mode").

### Step 4 — Start ngrok

```bash
ngrok http 8000
```

Copy the HTTPS URL (e.g. `https://abc123.ngrok.io`) and:
1. Set `NGROK_URL=https://abc123.ngrok.io` in `.env`
2. Restart the cila-ai-agent container: `docker-compose restart cila-ai-agent`
3. In Twilio Console → Phone Numbers → your number → Voice & Fax → A Call Comes In → set Webhook to `https://abc123.ngrok.io/voice` (HTTP POST)
4. Set Status Callback URL to `https://abc123.ngrok.io/api/call-status` (HTTP POST) — required for accurate call counter decrement

### Step 5 — Test Chat (no Twilio needed)

Visit `http://localhost:8000/chat-ui` in a browser. Uses MockSTT/MockTTS — text only, no audio. Requires a valid token (default token `local-dev-token` works if `BYPASS_TWILIO_AUTH=true`).

---

## 5. Service Port Map

| Service | Container Name | Host Port | Container Port |
|---|---|---|---|
| PostgreSQL | `cila-postgres` | 5432 | 5432 |
| Redis | `cila-redis` | 6379 | 6379 |
| CILA app | `cila-ai-agent` | 8000 | 8000 |

---

## 6. Known Issues to Resolve Before Testing

| Issue | Severity | Details |
|---|---|---|
| `PG_DATABASE_URL` commented out | P0 | App will fail to connect to Postgres. Uncomment and set. |
| `REDIS_URL` commented out | P1 | Falls back to RAM counter silently — fine for solo dev, misleading for team |
| README ngrok port 8085 wrong | P1 | Should be 8000 (Docker) or 8001 (bare Python). Fix README before sharing |
| `NGROK_URL` not set | P1 | Without it, `/voice` uses `request.host` which may be `localhost` — Twilio cannot reach it |
| Constant embeddings with `LOCAL_TEST=true` | P2 | All RAG searches return same candidates — cannot test RAG ranking accuracy locally without real embeddings |
| `APP_ENV` not set to `development` | P2 | If `APP_ENV=production`, timeouts are PRD-strict (0.5s STT, 0.3s TTFA) which will fail on home internet |

---

## 7. File Notes (Local Stack)

### `docker-compose.yml`
```
File: docker-compose.yml
Importance: P0
Purpose: Defines all local services and their dependencies.
Key services: postgres (pgvector/pg16), redis (7-alpine), cila-ai-agent (builds from Dockerfile)
Inputs: .env (via env_file), db-init/01_schema.sql (volume mount)
Outputs: Three running containers on cila-network bridge
Runtime role: Full local stack replacement for EC2
Risks: POSTGRES_PASSWORD not validated; PG_DATABASE_URL must use service name inside Docker but localhost outside
Questions for human: Should there be a docker-compose.override.yml for dev-specific settings?
```

### `run_server.py`
```
File: run_server.py
Importance: P0
Purpose: Entry point for bare Python and Docker CMD. Loads .env and starts uvicorn.
Key functions: Loads dotenv, imports telephony.server.app, starts uvicorn on PORT (default 8001)
Inputs: .env, telephony/server.py
Outputs: Running uvicorn server
Runtime role: Process start — Docker CMD and local python run_server.py
Risks: Default port 8001 conflicts with Dockerfile EXPOSE 8000; README is wrong
Questions for human: Should we standardize to 8000 everywhere?
```
