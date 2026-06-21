"""
retrieval/vector_store.py — PGVector-backed knowledge retrieval for the voice agent.

KnowledgeBase.search(query) pipeline:
  1. Embed the query (Bedrock Titan v2 or LOCAL_TEST mock).
  2. Fetch top candidates via pgvector cosine-distance ANN (HNSW index).
  3. Re-rank with weighted ensemble: 0.7 * cosine + 0.3 * trigram similarity.
  4. Filter below category-specific confidence threshold.
  5. Return top-k passing chunks + audit metadata.

Returns "LOW_CONFIDENCE_FALLBACK" when no chunk passes the threshold — the
orchestrator falls back to the inline SYSTEM_PROMPT corpus in that case.

Feature flag: RAG_ENABLED=true (env) + PG_DATABASE_URL set. When disabled
(default) or when Postgres is unreachable, the inline corpus handles queries.
"""

import os
import json
import logging
import asyncio
from dotenv import load_dotenv
from typing import List, Tuple, Optional

# asyncpg and pgvector are only needed at runtime when RAG_ENABLED=true and
# Postgres is reachable. Lazy-import inside _ensure_pool() so this module
# compiles cleanly in environments where the packages aren't installed yet.
# Install when ready:  uv pip install asyncpg pgvector
from contracts.interfaces import KnowledgeBaseEngine

load_dotenv()
logger = logging.getLogger("Retrieval")


# ─────────────────────────────────────────────────────────────────────────────
# Confidence threshold helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_thresholds() -> dict:
    config_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "config", "rag_thresholds.json",
    )
    try:
        if os.path.exists(config_path):
            with open(config_path) as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load rag_thresholds.json: {e}")
    return {}


_THRESHOLDS = _load_thresholds()


def get_default_threshold() -> float:
    return _THRESHOLDS.get("DEFAULT_THRESHOLD", 0.58)


def get_threshold(hard_refusal_category: Optional[str], general_category: Optional[str] = None) -> float:
    hard_refusals = _THRESHOLDS.get("HARD_REFUSAL_CATEGORIES", {})
    if hard_refusal_category and hard_refusal_category in hard_refusals:
        return hard_refusals[hard_refusal_category]

    op_cats = _THRESHOLDS.get("OPERATIONAL_CATEGORIES", {})
    if general_category:
        cat = general_category.lower()
        if "program" in cat or "academic" in cat:
            return op_cats.get("PROGRAM_STRUCTURE", 0.58)
        if "fee" in cat or "financial" in cat:
            return op_cats.get("FEE_DETAILS", 0.60)
        if "eligibility" in cat or "general info" in cat:
            return op_cats.get("ELIGIBILITY", 0.62)
        if "admission" in cat or "deadline" in cat:
            return op_cats.get("DEADLINES", 0.65)

    return get_default_threshold()


# ─────────────────────────────────────────────────────────────────────────────
# KnowledgeBase
# ─────────────────────────────────────────────────────────────────────────────

class KnowledgeBase(KnowledgeBaseEngine):
    """
    PGVector-backed knowledge retrieval engine.

    Implements KnowledgeBaseEngine so the orchestrator can swap out the
    underlying storage without changing its own code.

    Usage:
        kb = KnowledgeBase()
        context, score, category, retriever_id, chunk_ids = await kb.search(query)
    """

    def __init__(self) -> None:
        self.db_url = os.getenv("PG_DATABASE_URL")
        self.local_test = os.getenv("LOCAL_TEST", "true").lower() in ("1", "true", "yes", "on")
        self.pool = None

        if not self.db_url:
            logger.warning("KnowledgeBase: PG_DATABASE_URL not set — search will fail")
            return

        self._validate_residency()

        if self.local_test:
            logger.warning("[RAG] LOCAL_TEST mode — using mock embeddings ([1.0]*1536)")
        logger.info("KnowledgeBase initialised (pgvector)")

    def _validate_residency(self) -> None:
        if self.local_test or not self.db_url:
            return
        if "rds.amazonaws.com" in self.db_url and "ca-central-1" not in self.db_url:
            raise RuntimeError(
                f"Data residency violation: RDS host not in ca-central-1. URL: {self.db_url}"
            )

    async def _ensure_pool(self) -> None:
        if self.pool is not None:
            return
        try:
            import asyncpg as _asyncpg
            from pgvector.asyncpg import register_vector

            async def _init(conn):
                await register_vector(conn)

            self.pool = await _asyncpg.create_pool(
                self.db_url,
                min_size=2,
                max_size=10,
                command_timeout=5.0,
                init=_init,
            )
            logger.info("KnowledgeBase: asyncpg pool connected")
        except ImportError as e:
            raise RuntimeError(
                "asyncpg/pgvector not installed. Run: uv pip install asyncpg pgvector"
            ) from e
        except Exception as e:
            logger.error(f"KnowledgeBase pool init failed: {e}")
            raise

    async def check_health(self) -> bool:
        try:
            await self._ensure_pool()
            async with self.pool.acquire() as conn:
                return await conn.fetchval("SELECT 1;") == 1
        except Exception as e:
            logger.warning(f"KnowledgeBase health check failed: {e}")
            return False

    async def _get_query_embedding(self, query: str) -> List[float]:
        from retrieval.embeddings import get_bedrock_embeddings
        try:
            return await get_bedrock_embeddings(query, local_test=self.local_test)
        except Exception as e:
            logger.error(f"Query embedding failed: {e} — falling back to zero vector")
            return [0.0] * 1536

    async def search(
        self,
        query: str,
        call_logger=None,
        top_k: int = 3,
        trace_id: Optional[str] = None,
    ) -> Tuple[str, float, str, str, List[str]]:
        """
        Search the pgvector knowledge base for chunks relevant to `query`.

        Returns:
            (context_str, best_score, best_category, retriever_id, chunk_ids)

        context_str is "LOW_CONFIDENCE_FALLBACK" when no chunk passes the
        confidence threshold — the orchestrator falls back to inline corpus.
        """
        logger.info(f"[RAG] Searching: '{query[:60]}'")

        try:
            await self._ensure_pool()
            query_embedding = await self._get_query_embedding(query)

            async with self.pool.acquire() as conn:
                await conn.execute("SET search_path TO rag, public;")

                # Over-fetch so ensemble re-ranking has enough candidates.
                # LOCAL_TEST uses a higher limit since all mock vectors are identical.
                re_rank_limit = 50 if self.local_test else 20

                rows = await conn.fetch(
                    """
                    SELECT
                        c.content,
                        c.metadata,
                        1 - (e.embedding <=> $1)   AS cosine_val,
                        similarity(c.content, $3)  AS semantic_val,
                        c.id                       AS chunk_id,
                        d.doc_type                 AS category
                    FROM chunks c
                    JOIN embeddings e ON c.id = e.chunk_id
                    JOIN documents  d ON c.document_id = d.id
                    ORDER BY e.embedding <=> $1 ASC
                    LIMIT $2;
                    """,
                    query_embedding, re_rank_limit, query,
                )

                logger.info(f"[RAG] {len(rows)} candidates; computing ensemble scores...")

                scored = []
                for row in rows:
                    category = row["category"]
                    cos_score = row["cosine_val"] or 0.0
                    sem_score = row["semantic_val"] or 0.0
                    final_score = 0.7 * cos_score + 0.3 * sem_score
                    threshold = get_threshold(None, category)

                    if final_score < threshold:
                        continue

                    scored.append({
                        "content": row["content"],
                        "score": final_score,
                        "id": str(row["chunk_id"]),
                        "category": category,
                    })

                scored.sort(key=lambda x: x["score"], reverse=True)
                top = scored[:top_k]

                if not top:
                    logger.info("[RAG] No chunks passed confidence threshold")
                    if call_logger and hasattr(call_logger, "log_event"):
                        call_logger.log_event(
                            "retrieval", "rag_search_complete",
                            meta={"matches": 0, "top_score": 0},
                            trace_id=trace_id,
                        )
                    return "LOW_CONFIDENCE_FALLBACK", 0.0, "General", "unknown", []

                context_str = "\n\n".join(r["content"] for r in top)
                best_score = top[0]["score"]
                best_category = top[0]["category"]
                chunk_ids = [r["id"] for r in top]

                logger.info(
                    f"[RAG] {len(top)} chunks returned "
                    f"(top score={best_score:.2f}, category={best_category})"
                )
                if call_logger and hasattr(call_logger, "log_event"):
                    call_logger.log_event(
                        "retrieval", "rag_search_complete",
                        meta={"matches": len(top), "top_score": round(best_score, 2)},
                        trace_id=trace_id,
                    )

                return context_str, best_score, best_category, "pgvector-ensemble-v1", chunk_ids

        except Exception as e:
            logger.error(f"KnowledgeBase search failed: {e}", exc_info=True)
            return (
                "No specific documents found due to a knowledge base error.",
                0.0, "General", "unknown", [],
            )
