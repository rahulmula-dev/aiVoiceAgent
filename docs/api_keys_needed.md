# API keys to request from manager

This list covers every paid credential the voice agent needs to reach full
production capability. Current dev setup uses free-tier or mock alternatives
where possible — each row notes what breaks without the key and what we have
in the meantime.

---

## Priority 1 — Core LLM (blocking production quality)

### OpenAI API key
| Field | Detail |
|---|---|
| Purpose | Primary LLM: GPT-4o (main) + GPT-4o-mini (fallback race) |
| Why needed | Groq llama-3.1-8b-instant is the dev placeholder. GPT-4o has better reasoning, tool-calling, and instruction-following for a voice agent context. GPT-4o-mini as a 1.5 s fallback prevents cold-start delays. |
| Env var | `OPENAI_API_KEY` |
| Estimated cost | GPT-4o: ~$0.005/1K input tokens, ~$0.015/1K output. A 3-min call uses ~800 tokens total → ~$0.015/call |
| Current fallback | Groq llama-3.1-8b-instant (free tier, working) |
| Code ready? | Partially — `llm/gemini_llm.py` shows the pattern. `llm/openai_llm.py` to be created once key arrives. |

---

## Priority 2 — TTS fallback (blocking resilience)

### Azure Speech key
| Field | Detail |
|---|---|
| Purpose | Azure Neural TTS as backup when ElevenLabs returns 401/402/5xx |
| Why needed | ElevenLabs free tier has quota limits. Azure Neural TTS at ~$0.014/min is cheaper and has SLA uptime. Target: EL Flash primary (~75–150 ms TTFA), Azure Neural backup (~1–1.2 s TTFA). |
| Env vars | `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION` |
| Estimated cost | ~$0.014/min. 100 calls/day × 3 min avg = ~$4.20/day |
| Current fallback | ElevenLabs free tier only (no redundancy) |
| Code ready? | No — `tts/azure_tts.py` to be created. Background task chip already spawned. |

---

## Priority 3 — Semantic RAG (blocking knowledge quality)

### AWS credentials (Bedrock Titan Embeddings v2)
| Field | Detail |
|---|---|
| Purpose | Generate real 1536-dim semantic embeddings for the knowledge base. Without this, RAG uses mock `[1.0]*1536` vectors — all documents score equally and retrieval is meaningless. |
| Why needed | Semantic search understands that "how much does the esthetics course cost" matches "Esthetician Diploma fee: $12,000" even though the words differ. Mock embeddings cannot do this. |
| Env vars | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION` (ca-central-1) |
| Estimated cost | Bedrock Titan v2: ~$0.00002/1K tokens for embeddings. One-time migration of 16 documents ≈ negligible. Runtime: ~$0.0001/query. |
| Current fallback | `LOCAL_TEST=true` — mock embeddings, trigram similarity only |
| Code ready? | Yes — `retrieval/embeddings.py` has the Bedrock call ready. Just set `LOCAL_TEST=false` + AWS creds + re-run migration. |

---

## Priority 4 — TTS quality (ElevenLabs paid)

### ElevenLabs paid account API key
| Field | Detail |
|---|---|
| Purpose | Access library/shared voices and higher monthly quota |
| Why needed | Current free tier: premade voices only (Rachel, Adam, etc.), ~10K characters/month. Paid Starter ($5/mo): 30K chars, library voices. Paid Creator ($22/mo): 100K chars, all voices. |
| Env var | `ELEVENLABS_API_KEY` (already the right var — just swap the key) |
| Estimated cost | Starter: $5/mo. Creator: $22/mo. |
| Current fallback | Free tier with Rachel voice (21m00Tcm4TlvDq8ikWAM) — working but limited quota |
| Code ready? | Yes — just replace the key in `.env` |

---

## Summary table

| Key | Priority | Unblocks | Cost estimate |
|---|---|---|---|
| OpenAI API key | 🔴 High | Production LLM quality | ~$0.015/call |
| Azure Speech key | 🟠 Medium | TTS redundancy/fallback | ~$0.014/min |
| AWS credentials | 🟠 Medium | Real semantic RAG | ~$0.0001/query |
| ElevenLabs paid | 🟡 Low | Voice choice + quota | $5–22/mo |

---

*Last updated: 2026-06-24*
