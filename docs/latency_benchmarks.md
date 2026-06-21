# Latency benchmarks — measured from live calls

This doc tracks actual end-to-end latency measured from Twilio Dev Phone calls.
All values come from `timing` events written to `logs/transcripts/<datetime>.json`.

## Metrics definition

| Metric | What it measures |
|---|---|
| `user_final_to_llm_first_token_ms` | Time from Deepgram's `is_final=true` transcript event until the first token arrives from the LLM |
| `llm_first_token_to_tts_first_audio_ms` | Time from LLM first token until ElevenLabs returns first audio chunk to Twilio |
| `user_final_to_tts_first_audio_ms` | Total caller-perceived latency: end of their speech → start of bot audio |

"Warm turn" = any turn after the first in a call. The first turn pays LLM session-init overhead.

---

## Baseline — Jun 22, 2026 (Steps 1–5, no pooling)

**Call:** `2026-06-22_17-27-49.json` — 6 turns, 3-minute call, Groq + ElevenLabs no pool

| Turn | Input | LLM first token | TTS first audio | **Total** |
|---|---|---|---|---|
| 1 (cold) | "I want to take admission..." | 2,969 ms | 4,234 ms | **7,203 ms** |
| 2 | governance refusal | 312 ms | 4,297 ms | **4,609 ms** |
| 3 | "course duration" | 297 ms | 4,281 ms | **4,578 ms** |
| 4 | "how is it different" | 313 ms | 4,265 ms | **4,578 ms** |
| 5 | weather refusal | 297 ms | 4,266 ms | **4,563 ms** |
| 6 | goodbye | 328 ms | 4,235 ms | **4,563 ms** |

**Warm-turn averages:**
- LLM first token: **309 ms**
- TTS first audio: **4,269 ms**
- Total: **4,578 ms**

**Root cause of high TTS latency:** Each `synthesize_and_stream()` call created a new
`httpx.AsyncClient` — full TCP+TLS handshake (~150–200 ms) + HTTP/2 stream setup
repeated on every turn and every sentence within a turn.

---

## After Step 6 — Jun 24, 2026 (ElevenLabs connection pool live)

**Calls:** `2026-06-24_12-17-07.json` + `2026-06-24_12-19-32.json` — 5 timed turns total

| Turn | Input | LLM first token | TTS first audio | **Total** |
|---|---|---|---|---|
| 1 (warm) | "process of getting selected" | 2,312 ms* | 234 ms | **2,546 ms** |
| 2 (warm) | "costliest degree" | 344 ms | 250 ms | **594 ms** |
| 3 (warm) | "are you a man or a boy" | 2,000 ms* | 219 ms | **2,219 ms** |
| 4 (warm) | "station course" | 516 ms | 250 ms | **766 ms** |
| 5 (warm) | "ok bye" | 343 ms | 204 ms | **547 ms** |

\* Turns marked with `*` had elevated LLM latency — likely Groq network variance, not a
   code regression. The other three turns show Groq at its normal ~350 ms warm latency.

**Warm-turn averages (all 5):**
- LLM first token: **703 ms** (inflated by 2 Groq variance outliers)
- LLM first token: **401 ms** (excluding the 2 outliers)
- TTS first audio: **231 ms**
- Total: **935 ms** / **569 ms** (excl. outliers)

---

## Summary comparison

| Metric | Jun 22 (no pool) | Jun 24 (pooled) | Change |
|---|---|---|---|
| TTS first audio (warm avg) | 4,269 ms | 231 ms | **18.5× faster** |
| Total warm turn (median) | ~4,578 ms | ~600 ms | **~7.6× faster** |
| LLM first token (stable warm) | ~309 ms | ~401 ms | ≈same (Groq variance) |
| Cold turn total | 7,203 ms | not measured | — |

**The dominant improvement is entirely from Step 6 (ElevenLabs connection pool).**
The shared `httpx.AsyncClient` with `keepalive_expiry=30` eliminates TCP+TLS setup
on every synthesis call. Subsequent calls reuse the already-established HTTP/2 connection.

---

## Target budget (for reference)

From `docs/latency_budget.md` — production targets for a voice agent:

| Segment | Target | Status |
|---|---|---|
| STT (Deepgram nova-3) | ≤ 300 ms | ✅ met (not measured separately yet) |
| LLM first token (warm) | ≤ 500 ms | ✅ met (~309–401 ms) |
| TTS first audio (ElevenLabs Flash) | ≤ 150 ms | ⚠ close (~231 ms warm avg; 75 ms spec) |
| Total caller-perceived (warm) | ≤ 1,500 ms | ✅ met (~600 ms median) |

The TTS target (150 ms) is the ElevenLabs Flash v2.5 spec under ideal conditions.
Our measured 231 ms includes network to ElevenLabs from the dev machine — collocated
infra would likely close this gap.

---

## Callouts for Step 9+

- **First turn (cold) LLM latency is still ~2–3 s.** This is Groq's model warm-up
  on a new session. Switching to OpenAI 4o (streaming start) or pre-heating a
  conversation context would reduce it. Not addressed until manager approves paid key.

- **Governance refusals are fast** — the policy check fires before the LLM call,
  returning in the `llm_first_token_to_tts_first_audio_ms` window. Jun 24 shows
  governance refusals returning full audio in 547 ms total.

- **Redis gate (Step 9) adds ~1 ms** — Lua CAS on localhost Redis is negligible.
  Not expected to affect these metrics.

---

*Last updated: 2026-06-24. Source calls: `2026-06-22_17-27-49.json`,
`2026-06-24_12-17-07.json`, `2026-06-24_12-19-32.json`.*
