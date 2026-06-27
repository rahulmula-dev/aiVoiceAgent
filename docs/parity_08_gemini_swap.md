# Step 8 — LLM swap: Groq → Gemini

**Status:** ✅ code done — needs `GEMINI_API_KEY` + `uv pip install google-generativeai` to go live  
**Risk to working pipeline:** LOW (feature-flagged, defaults to Groq)  
**Time:** ~2 hours  

## What this step does

Adds Google Gemini as an alternative LLM provider behind a config flag.
Both providers share the **exact same `run_llm()` signature**, so the
orchestrator picks one at boot and the rest of the pipeline (STT, RAG,
TTS, governance) is provider-agnostic.

```
LLM_PROVIDER=groq    (default)  →  llm/groq_llm.py   →  Groq llama-3.1-8b-instant
LLM_PROVIDER=gemini             →  llm/gemini_llm.py →  Google gemini-2.0-flash
```

The Groq path is **unchanged**. Nothing about your current setup breaks.

## Files added / changed

| File | Change |
|---|---|
| `llm/gemini_llm.py` | **NEW** — full Gemini implementation mirroring `groq_llm.py` |
| `config/__init__.py` | `LLM_PROVIDER`, `GEMINI_API_KEY`, `GEMINI_MODEL` config vars |
| `orchestrator/manager.py` | Import `run_llm` from the right module based on `LLM_PROVIDER` |
| `run_server.py` | Startup banner shows which LLM provider is active |

## What the Gemini implementation supports

Everything Groq does, with one structural difference: history is in Gemini's native format throughout.

| Feature | Groq | Gemini |
|---|---|---|
| Governance interceptor (language strikes) | ✅ | ✅ |
| Restricted-topic detector | ✅ | ✅ |
| RAG context injection (Step 7) | ✅ | ✅ |
| Streaming token-by-token to TTS | ✅ | ✅ |
| First-token timing marker (logger.mark_llm_first_token) | ✅ | ✅ |
| Quota-exhaustion graceful fallback | ✅ (APIError) | ✅ (ResourceExhausted) |
| Tool calls | ✅ (none registered) | ❌ (not needed — corpus inlined) |
| History format | OpenAI (`role/content`) | Gemini (`role/parts`) |
| System prompt | first history entry | `system_instruction` constructor arg |

## How to switch to Gemini

### 1. Install the SDK

```powershell
uv pip install google-generativeai
```

(Already in `requirements.txt` but not in the clean-build venv yet.)

### 2. Get a Gemini API key

Visit https://aistudio.google.com/apikey → "Get API key" → free tier.
Limits: 15 requests/min, 1M tokens/day on `gemini-2.0-flash`.

### 3. Add to `.env`

```
LLM_PROVIDER=gemini
GEMINI_API_KEY=AIza...
GEMINI_MODEL=gemini-2.0-flash    # optional — this is the default
```

### 4. Restart the server

```powershell
uv run python run_server.py
```

You should see:
```
[MAIN] Starting modular voice pipeline server on port 5000
       STT -> Deepgram nova-3
       LLM -> Gemini   gemini-2.0-flash
       TTS -> ElevenLabs eleven_flash_v2_5
[BOOT] LLM provider = Gemini (gemini-2.0-flash)
```

Per call:
```
[LLM/Gemini] Model ready: gemini-2.0-flash
[LLM/Gemini] User said: 'how much is the esthetics program'
[RAG] Injecting context (score=0.75, cat=Fees)
```

### 5. To switch back to Groq

```
LLM_PROVIDER=groq
```
(or just delete the `LLM_PROVIDER` line — Groq is the default.)

## Differences worth knowing

### History format

Groq history (OpenAI-style):
```python
[{"role": "system", "content": "You are..."},
 {"role": "user",   "content": "How much is..."},
 {"role": "assistant", "content": "It's $12,000."}]
```

Gemini history (native):
```python
# system_instruction is set on the model, not in history
[{"role": "user",  "parts": ["How much is..."]},
 {"role": "model", "parts": ["It's $12,000."]}]
```

`gemini_llm.py` stores history in Gemini format from the start — no
conversion at request time.

### Safety filters

Gemini's default safety filter trips on benign GD-College questions that
mention chemicals, sharp tools, or body topics (cosmetology). The
implementation sets all four safety categories to `BLOCK_NONE`. Governance
happens upstream in the language interceptor + restricted-topic detector,
not in the LLM safety filter.

### Streaming

Gemini's `generate_content_async(..., stream=True)` returns text chunks
(not single tokens like Groq). The TTS layer doesn't care — it buffers
into sentences either way.

### Markdown stripping

Gemini occasionally emits markdown asterisks for emphasis. We strip them
before pushing to TTS so the voice doesn't read "asterisk" aloud.

## Why no dual-model race?

The company project runs Gemini Flash as primary with `gemini-1.5-flash` as
a 1.5-second fallback if the primary stalls. We skipped that for Step 8 to
keep the change minimal and testable. If you want it later it's a clean
addition inside `_stream_turn` — race two `generate_content_async` tasks
with `asyncio.wait(..., return_when=FIRST_COMPLETED)`.

## Known follow-ups (not in Step 8 scope)

- **Dual-model race** — add a fast-fallback model when primary stalls past 1.5s.
- **Per-provider TTFT metrics** — `logger.mark_llm_first_token()` already
  fires for both providers, but the sealed summary doesn't break it down
  by provider. Useful for A/B comparisons.
- **Cost tracking** — Gemini charges per million tokens; a per-call token
  count in the sealed summary would help budget planning.
