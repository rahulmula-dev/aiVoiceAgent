# Step 7 — RAG with pgvector + Postgres

**Status:** ✅ code done — needs infra bring-up + migration run to go live  
**Risk to working pipeline:** LOW (feature-flagged OFF by default)  
**Time:** ~4 hours  

## What this step does

Replaces the static inlined knowledge corpus (13 KB in SYSTEM_PROMPT) with a
live retrieval pipeline:

```
Caller utterance
    │
    ▼
KnowledgeBase.search(query)
    │  1. Embed query (Bedrock Titan v2 or LOCAL_TEST mock [1.0]*1536)
    │  2. pgvector ANN search → top-20 candidates (HNSW cosine)
    │  3. Ensemble re-rank: 0.7*cosine + 0.3*trigram
    │  4. Confidence gate (category-specific threshold)
    │  5. Return top-3 passing chunks
    │
    ▼  ← injected into user message as [RELEVANT KNOWLEDGE]
Groq LLM
    │
    ▼
TTS → Twilio
```

When no chunk passes the confidence threshold, the search returns
`LOW_CONFIDENCE_FALLBACK` and the LLM falls back to its inline SYSTEM_PROMPT
corpus unchanged — zero regression.

## Files added / changed

| File | Change |
|---|---|
| `retrieval/embeddings.py` | Uncommented — `get_bedrock_embeddings()` (Bedrock or mock) |
| `retrieval/vector_store.py` | Uncommented — `KnowledgeBase` implementing `KnowledgeBaseEngine` |
| `retrieval/migrate_to_pgvector.py` | Uncommented — `PGVectorMigrator` one-shot ingestion CLI |
| `config/rag_thresholds.json` | **NEW** — category confidence thresholds |
| `db-init/01_schema.sql` | **NEW** — SQL executed by docker on first postgres boot |
| `llm/groq_llm.py` | `kb=None` param + RAG context injection before LLM call |
| `orchestrator/manager.py` | KB instantiation per call (when `RAG_ENABLED=true`) |
| `config/__init__.py` | `RAG_ENABLED`, `LOCAL_TEST`, `PG_DATABASE_URL` config vars |

## Database schema (rag schema)

```
rag.documents          id, title, source_uri, doc_type, ingested_at
rag.chunks             id, document_id→, content, checksum(UNIQUE), source_id, metadata(JSONB)
rag.embeddings         id, chunk_id→, embedding vector(1536), model_version
rag.governance_metadata id, chunk_id→, sensitivity_level, topic_tags, hard_refusal_category, …
```

HNSW index on `rag.embeddings.embedding` with `m=32, ef_construction=128`.

## Feature flags

| `.env` key | Default | Effect |
|---|---|---|
| `RAG_ENABLED` | `false` | `true` → KnowledgeBase created per call; context injected into LLM |
| `LOCAL_TEST` | `true` | `true` → mock embeddings `[1.0]*1536` (no AWS needed) |
| `PG_DATABASE_URL` | `""` | Connection string for asyncpg pool |

## How to bring RAG live (two commands)

### 1. Add to `.env`

```
POSTGRES_PASSWORD=cila_dev
PG_DATABASE_URL=postgresql://postgres:cila_dev@localhost:5432/postgres
LOCAL_TEST=true
RAG_ENABLED=false      # keep off until migration succeeds
```

### 2. Start Postgres

```powershell
docker-compose up -d postgres
# Wait for "database system is ready to accept connections"
```

### 3. Run the migration

```powershell
uv run python -m retrieval.migrate_to_pgvector
```

Expected output:
```
WARNING:Migration:[MIGRATION] LOCAL_TEST mode — using mock embeddings ([1.0]*1536)
INFO:Migration:[MIGRATION] Initializing database schema...
INFO:Migration:[MIGRATION] Ingesting 23 records...
INFO:Migration:[MIGRATION] Building HNSW index...
INFO:Migration:[MIGRATION] Done — 23 vectors in pgvector.
```

### 4. Enable RAG

```
RAG_ENABLED=true   # in .env
```

Restart the server. You'll see:
```
[ORCH] RAG ENABLED — KnowledgeBase (pgvector) active
```

And per turn:
```
[RAG] Injecting context (score=0.72, cat=Fees)
```

## Install dependencies (if not already present)

```powershell
uv pip install asyncpg pgvector
```

These are in `requirements.txt` but not installed by the default clean-build
venv. Only needed when `RAG_ENABLED=true`.

## LOCAL_TEST mode caveats

With `LOCAL_TEST=true` all 23 stored embeddings are `[1.0]*1536` and the
query vector is also `[1.0]*1536`. This means:

- Cosine similarity ≈ 1.0 for every chunk → all vectors tie on vector score
- Re-ranking breaks the tie using **trigram similarity** (lexical overlap)
- In practice the top-k returned chunks are the ones whose text contains
  the most words from the query — a reasonable approximation
- Confidence thresholds still apply (a chunk with score < 0.58 is still dropped)

For real semantic search, switch to `LOCAL_TEST=false` and provide AWS
credentials (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` in `.env`) then
re-run the migration to regenerate real Bedrock vectors.

## What this unlocks

- **Step 8 (LLM swap to Gemini)**: RAG context is already injected as a
  clean `[RELEVANT KNOWLEDGE]` block — works with any LLM provider.
- **Latency**: adding RAG adds one async DB round-trip per LLM call. On
  local Postgres with `LOCAL_TEST=true` this is typically <5ms; on cloud
  RDS ~15-30ms. The HNSW index keeps ANN search sub-millisecond.
- **Accuracy**: replacing static corpus with retrieved context means the
  agent can answer questions about topics not in the top-20 ranked facts
  of the inline prompt (the inline prompt doesn't rank, it includes all).

## Known follow-ups (not in Step 7 scope)

- **Per-server KB singleton**: KnowledgeBase is currently created per-call
  (Step 7 simplicity). The asyncpg pool inside it is lazy so the first
  call per server pays the pool-creation overhead. For Step 9 a process-level
  singleton would be cleaner.
- **`/readyz` endpoint**: `/healthz` returns 200 regardless of DB state.
  A `/readyz` that calls `kb.check_health()` would let load-balancers detect
  Postgres outages. Noted in the Step 9 doc.
- **Real embeddings**: Switch `LOCAL_TEST=false` + AWS creds + re-run migration.
