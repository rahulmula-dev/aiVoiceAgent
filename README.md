# 🎙️ CILA: AI Voice Agent.

A production-ready AI Voice Agent for GD College, designed for high-performance telephony and automated admission support.

## � Simple Flow

```mermaid
sequenceDiagram
    participant User
    participant Twilio
    participant Orchestrator
    participant Brain
    participant TTS
  
    User->>Twilio: Makes Phone Call
    Twilio->>Orchestrator: Streams Audio (WebSocket)
    Orchestrator->>Brain: Transcribes & Processes Query
    Brain-->>Orchestrator: Generates Response (RAG)
    Orchestrator->>TTS: Streams Audio Chunks
    TTS-->>Twilio: Plays Audio back to User
```

---

# Setup Guide — AI Voice Agent (GD College)

## Prerequisites — install these first on any new machine

| Tool           | Version | Install                                    |
| -------------- | ------- | ------------------------------------------ |
| Python         | 3.11+   | https://python.org/downloads               |
| uv             | latest  | `pip install uv`                         |
| Docker Desktop | latest  | https://docker.com/products/docker-desktop |
| Twilio CLI     | latest  | `npm install -g twilio-cli`              |
| ngrok          | latest  | https://ngrok.com/download                 |

Verify everything is installed:

```powershell
python --version       # should print 3.11.x or higher
uv --version
docker --version
twilio --version
ngrok version
```

---

## Step 1 — Clone the repository

```powershell
git clone <your-github-repo-url>
cd "Ai VOICE agent"
```

---

## Step 2 — Create the virtual environment and install dependencies

```powershell
uv venv
uv pip install -r requirements.txt
```

> **Note:** The venv is created at `.venv/` in the project folder. Never commit `.venv/` to git.

---

## Step 3 — Create your `.env` file

`.env` is **not in the repository** (it's gitignored — it contains API keys). You must create it manually on every new machine.

Create a file named `.env` in the project root with this content:

```env
# --- Speech-to-Text ---
DEEPGRAM_API_KEY=your_deepgram_api_key_here

# --- Text-to-Speech ---
ELEVENLABS_API_KEY=your_elevenlabs_api_key_here
ELEVENLABS_VOICE_ID=XrExE9yKIg1WjnnlVkGX

# --- LLM ---
GROQ_API_KEY=your_groq_api_key_here

# --- PostgreSQL (Docker, mapped to host port 5433) ---
PG_DATABASE_URL=postgresql://postgres:cila_dev@localhost:5433/postgres
POSTGRES_PASSWORD=cila_dev

# --- Feature flags ---
LOCAL_TEST=true
RAG_ENABLED=true
CONCURRENCY_GATE_ENABLED=true
REDIS_URL=redis://localhost:6379
MAX_CONCURRENT_CALLS=5
```

Replace the three `your_..._key_here` values with real keys.

> **Where to get keys:**
>
> * Deepgram: https://console.deepgram.com
> * ElevenLabs: https://elevenlabs.io (free tier works)
> * Groq: https://console.groq.com

---

## Step 4 — Start infrastructure (Postgres + Redis)

```powershell
docker-compose up -d postgres redis
```

Wait ~15 seconds, then verify both containers are healthy:

```powershell
docker ps --filter name=cila-postgres
docker ps --filter name=cila-redis
```

Both should show `(healthy)` in the STATUS column.

> **Note:** If your machine already has a local PostgreSQL service running on port 5432 (e.g., Windows PostgreSQL service), Docker maps to port **5433** instead. The `.env` above already accounts for this.

---

## Step 5 — Run the database migration (first time only)

This creates the RAG schema and loads the knowledge base vectors:

```powershell
uv run retrieval/migrate_to_pgvector.py
```

Expected output ends with something like:

```
[MIGRATE] Done — 16 chunks inserted
```

You only need to run this once. Data persists in the Docker volume across restarts.

---

## Step 6 — Set up ngrok

Sign in to your ngrok account and set your auth token (one-time per machine):

```powershell
ngrok authtoken <your_ngrok_authtoken>
```

Start a tunnel to port 5000:

```powershell
ngrok http 5000
```

Copy the `https://` forwarding URL — you'll need it in the next step. It changes every time ngrok restarts, so repeat this update on every session.

---

## Step 7 — Configure Twilio CLI

Log in with your Twilio profile:

```powershell
twilio login --profile calltestai
```

Update the phone number's webhook URLs (replace `<ngrok-url>` with your current ngrok URL):

```powershell
twilio api:core:incoming-phone-numbers:update `
    --sid PNe626f6d9628d06e85a8081058f1e9da5 `
    --profile calltestai `
    --voice-url "https://<ngrok-url>/voice" `
    --status-callback "https://<ngrok-url>/api/call-status" `
    --status-callback-method POST
```

> **Important:** Always update **both** `--voice-url` and `--status-callback` together. If the status callback is missing, the concurrency gate will leak slots after each call.

---

## Step 8 — Start the voice agent server

```powershell
uv run run_server.py
```

Expected startup output:

```
[MAIN] Starting modular voice pipeline server on port 5000
       STT -> Deepgram nova-3
       LLM -> Groq llama-3.1-8b-instant
       TTS -> ElevenLabs eleven_flash_v2_5
       Gate -> Redis max 5 concurrent calls
[MAIN] Warming up connection pools...
[POOL/TTS] ElevenLabs connection warmed (HTTP 401)   ← cosmetic only, synthesis works
[GATE] Concurrency gate ready — max 5 concurrent calls
[MAIN] Waiting for Twilio calls...
```

---

## Step 9 — Make a test call

Start the Twilio Dev Phone in a separate terminal:

```powershell
twilio dev-phone
```

Call **+18567165450** from the Dev Phone interface. You should hear the agent respond.

---

## Daily workflow (after first setup)

Every time you start a new session on the same machine:

1. `docker-compose up -d postgres redis` — start infra
2. Start ngrok: `ngrok http 5000`
3. Update Twilio webhook URLs (Step 7 command above, new ngrok URL)
4. `uv run run_server.py` — start agent
5. `twilio dev-phone` — test calls

Steps 1 (infra) and 5 (migration) are one-time on first setup only.

---

## Feature flags

Control features via `.env` without changing code:

| Flag                         | Default  | What it does                                                                 |
| ---------------------------- | -------- | ---------------------------------------------------------------------------- |
| `RAG_ENABLED`              | `true` | Enable knowledge base search                                                 |
| `LOCAL_TEST`               | `true` | Use mock embeddings (no AWS needed). Set`false`for real Bedrock embeddings |
| `CONCURRENCY_GATE_ENABLED` | `true` | Limit max simultaneous calls via Redis                                       |
| `MAX_CONCURRENT_CALLS`     | `5`    | Max calls allowed at once                                                    |
| `LLM_PROVIDER`             | `groq` | Set to`gemini`to use Google Gemini 2.0 Flash instead                       |

---

## Troubleshooting

| Error                                                  | Cause                                                   | Fix                                                                                              |
| ------------------------------------------------------ | ------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| `asyncpg.InvalidPasswordError`                       | Docker Postgres didn't initialize with correct password | `docker-compose down && docker volume rm aivoiceagent_pgdata && docker-compose up -d postgres` |
| `Error 22 connecting to localhost:6379`              | Redis container not running                             | `docker-compose up -d redis`                                                                   |
| `ModuleNotFoundError: No module named 'redis'`       | Package not installed                                   | `uv pip install "redis>=5.0"`                                                                  |
| `[TTS] ElevenLabs 401`                               | API key expired or quota exhausted                      | Replace`ELEVENLABS_API_KEY`in`.env`                                                          |
| `[POOL/TTS] ElevenLabs connection warmed (HTTP 401)` | Free-tier key on`/v1/models`warmup                    | Cosmetic — synthesis still works, safe to ignore                                                |
| Gate shows`active=1`after first call                 | Stale slot from a previous call with no status callback | Auto-clears after 400 seconds — reconfigure Twilio webhooks (Step 7) to prevent recurrence      |

---

## Known limitations

These are visible-to-the-caller behaviors we know about but haven't fixed yet. Document for honesty; address in future iterations.

### STT mistranscribes hangup-intent phrases

**Symptom:** Caller says *"hang up"* or *"wrap up"*. Deepgram nova-3 transcribes it as a similar-sounding word (observed: *"Hannah"*, *"hang on"*, *"hank up"*). The hangup phrase matcher in `stt/deepgram_stt.py:_HANGUP_PHRASES` doesn't fire, and the agent keeps talking.

**Workaround for callers:** Use a longer, unambiguous phrase — *"goodbye"*, *"thanks bye"*, *"I'm done"*, *"bye bye"*. These transcribe reliably and trigger immediate hangup.

**Real fix (future):** Replace literal-phrase matching with LLM-based intent classification. Adds ~200 ms latency per turn but handles arbitrary hangup phrasings.

### Strike-3 termination has a brief silence tail

**Symptom:** When the 3rd language-strike refusal terminates a call, there is up to ~2 seconds of silence after the refusal finishes before the line drops.

**Why:** The mark-event drain waits for Twilio to confirm audio playback. The strike-3 refusal is ~10 s of audio; the drain timeout is 15 s. If Twilio's mark echo is delayed, we wait the difference.

**Real fix (future):** Send the mark event earlier (before the last TTS chunk reaches the WS) or scale the timeout by audio bytes shipped.

### LLM occasionally hallucinates a "5-minute call limit"

**Symptom:** Toward the end of a long call, the agent says *"We're running low on time, our automated session is restricted to 5 minutes…"*. There is no such limit in the codebase.

**Why:** `contracts/policy.py::ResponsePolicyEngine.violates()` is defined to detect this exact hallucination but is not yet wired into the LLM/TTS output path.

**Real fix (future):** Add `ResponsePolicyEngine.violates(response_text)` check in the LLM module after each completed turn; replace the hallucinated turn with `PRDScripts.LOW_CONFIDENCE_FALLBACK`.

### Deepgram occasionally mis-recognizes named entities

**Symptom:** Program names like *"Nail Technician"* sometimes transcribe as *"name sequential"*, *"nail tech"* as *"mail tech"*, etc. The agent then says it doesn't have information on the (incorrectly transcribed) topic.

**Why:** Generic STT model. No domain-specific vocabulary boost.

**Real fix (future):** Use Deepgram's `keywords` parameter to boost program names (`nail technician`, `esthetician`, `cosmetology`, etc.) in the URL query string.

---

## Architecture overview

```
Phone call (Twilio)
    │
    ▼
/voice webhook (FastAPI, port 5000)
    │  concurrency gate check (Redis)
    ▼
WebSocket — Twilio Media Streams
    │
    ├── STT: Deepgram nova-3 (multilingual)
    ├── LLM: Groq llama-3.1-8b-instant (or Gemini 2.0 Flash)
    └── TTS: ElevenLabs eleven_flash_v2_5 (pooled HTTP/2)
  
Infrastructure (Docker):
    - PostgreSQL 16 + pgvector  → knowledge base (RAG)
    - Redis 7                   → concurrency gate
```

---

> **Measured latency (warm turns):** LLM ~401 ms · TTS ~231 ms · Total ~600 ms
