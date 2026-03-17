import os
import logging
import asyncio
import asyncpg
import json
# import boto3  # <--- Commented for local-only dev
from dotenv import load_dotenv
from typing import List, Tuple, Dict, Any

# Configure logging
logger = logging.getLogger("Retrieval")

load_dotenv()

from contracts.interfaces import KnowledgeBaseEngine

# --- CONFIGURATION (Sync with loader.py) ---
def _load_thresholds():
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "rag_thresholds.json")
    try:
        if os.path.exists(config_path):
            with open(config_path, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load rag_thresholds.json: {e}")
    return {}

_THRESHOLDS_CACHE = _load_thresholds()

def get_default_threshold():
    return _THRESHOLDS_CACHE.get("DEFAULT_THRESHOLD", 0.58)

def get_threshold(hard_refusal_category, general_category=None):
    """
    Returns the appropriate config-driven confidence threshold from the JSON configuration.
    Prioritizes hard refusal category overrides over general category overrides.
    """
    hard_refusals = _THRESHOLDS_CACHE.get("HARD_REFUSAL_CATEGORIES", {})
    if hard_refusal_category and hard_refusal_category in hard_refusals:
        return hard_refusals[hard_refusal_category]

    op_cats = _THRESHOLDS_CACHE.get("OPERATIONAL_CATEGORIES", {})
    if general_category:
        cat_lower = general_category.lower()
        if "program" in cat_lower or "academic" in cat_lower:
            return op_cats.get("PROGRAM_STRUCTURE", 0.58)
        if "fee" in cat_lower or "financial" in cat_lower:
            return op_cats.get("FEE_DETAILS", 0.60)
        if "eligibility" in cat_lower or "general info" in cat_lower:
            return op_cats.get("ELIGIBILITY", 0.62)
        if "admission" in cat_lower or "deadline" in cat_lower:
            return op_cats.get("DEADLINES", 0.65)

    return get_default_threshold()

class KnowledgeBase(KnowledgeBaseEngine):
    def __init__(self):
        self.db_url = os.getenv("PG_DATABASE_URL")
        self.local_test = os.getenv("LOCAL_TEST", "true").lower() == "true"
        
        if not self.db_url:
            logger.warning("KnowledgeBase: Missing PG_DATABASE_URL")
            self.pool = None
            return
        
        # Connection Pool (Initialized asynchronously)
        self.pool = None
        
        # M2 Fix: Assert Canadian Data Residency at startup
        self._validate_residency()

        if self.local_test:
            logger.warning("!!! CRITICAL: KnowledgeBase is running in LOCAL_TEST mode (Bypassing Bedrock) !!!")
            logger.warning("!!! Metadata thresholds will still be STRICTLY enforced (M1 Fix) !!!")
        logger.info("KnowledgeBase: Initialized (PGVector)")

    def _validate_residency(self):
        """
        M2 Fix: Assert that RDS instances are located in ca-central-1.
        Strict Canadian data residency requirement.
        """
        if self.local_test:
            return # Skip residency check for local development
            
        if not self.db_url:
            return

        # Check for AWS RDS host patterns
        if "rds.amazonaws.com" in self.db_url:
            if "ca-central-1" not in self.db_url:
                error_msg = f"DATA RESIDENCY VIOLATION: RDS host detected outside ca-central-1 region. URL: {self.db_url}"
                logger.critical(error_msg)
                raise RuntimeError(error_msg)
            else:
                logger.info("Data Residency Verified: RDS host is in ca-central-1.")
        else:
             logger.warning("Non-RDS database host detected. Manual residency verification required for production.")

    async def _ensure_pool(self):
        if self.pool is None:
            try:
                async def init(conn):
                    from pgvector.asyncpg import register_vector
                    await register_vector(conn)

                self.pool = await asyncpg.create_pool(
                    self.db_url,
                    min_size=10,
                    max_size=40,
                    command_timeout=5.0,
                    init=init
                )
                logger.info("KnowledgeBase: Connected to PGVector Pool (with Vector support)")
            except Exception as e:
                logger.error(f"KnowledgeBase Pool Init Failed: {e}")
                raise

    async def check_health(self) -> bool:
        """Verifies KnowledgeBase connectivity."""
        try:
            await self._ensure_pool()
            async with self.pool.acquire() as conn:
                res = await conn.fetchval("SELECT 1;")
                return res == 1
        except Exception as e:
            logger.warning(f"KnowledgeBase health check failed: {e}")
            return False

    async def get_query_embedding(self, query: str) -> List[float]:
        """Generate 1536-dimensional embedding for the query."""
        from retrieval.embeddings import get_bedrock_embeddings
        try:
            return await get_bedrock_embeddings(query, local_test=self.local_test)
        except Exception as e:
            logger.error(f"Search embedding failed: {e}. Falling back to zero-vector.")
            return [0.0] * 1536

    async def search(self, query: str, call_logger=None, top_k=3, trace_id=None):
        """
        Search PGVector with Safety and Confidence gates.
        """
        # [AUDIT] L1: Explicit log line to verify that RAG search happens AFTER Policy check.
        logger.info(f"RAG Search: Starting PGVector lookup for: '{query[:50]}...'")
        
        try:
            await self._ensure_pool()
            
            # 1. Embed Query
            query_embedding = await self.get_query_embedding(query)
            
            async with self.pool.acquire() as conn:
                # Set search_path for the session
                await conn.execute("SET search_path TO rag, public;")
                
                # 2. Query PGVector with Ensemble Scoring (H1 Compliance)
                # PRD spec requires 0.7 * cosine + 0.3 * semantic_relevance (trigram similarity)
                # We fetch more candidates than top_k to allow re-ranking via the ensemble score.
                re_rank_limit = 50 if self.local_test else 20
                
                logger.info(f"RAG-TRACE: Querying with ensemble re-ranking (Limit: {re_rank_limit})")
                
                rows = await conn.fetch(
                    """
                    SELECT 
                        c.content, 
                        c.metadata,
                        1 - (e.embedding <=> $1) as cosine_val,
                        similarity(c.content, $3) as semantic_val,
                        c.id as chunk_id,
                        d.doc_type as category
                    FROM chunks c
                    JOIN embeddings e ON c.id = e.chunk_id
                    JOIN documents d ON c.document_id = d.id
                    -- Initial broad candidate fetch via vector distance
                    ORDER BY e.embedding <=> $1 ASC
                    LIMIT $2;
                    """,
                    query_embedding, re_rank_limit, query
                )
                
                logger.info(f"RAG-TRACE: Found {len(rows)} candidates. Computing ensemble scores...")
                
                logger.info(f"RAG-TRACE: Found {len(rows)} raw rows from PGVector.")
                # 3. Apply Ensemble Scoring & Filtering
                scored_results = []
                for row in rows:
                    content = row['content']
                    metadata = json.loads(row['metadata']) if isinstance(row['metadata'], str) else row['metadata']
                    category = row['category']
                    
                    # Compute Weighted Ensemble Score
                    cos_score = row['cosine_val'] or 0.0
                    sem_score = row['semantic_val'] or 0.0
                    final_score = (0.7 * cos_score) + (0.3 * sem_score)
                    
                    # Confidence Gate
                    # Confidence Gate (H1 Weighting + M1 Strict Enforcement)
                    threshold = get_threshold(None, category)
                    
                    if final_score < threshold:
                        logger.debug(f"RAG-DROP: Confidence gate failed ({final_score:.4f} < {threshold}).")
                        continue
                    
                    scored_results.append({
                        "content": content,
                        "score": final_score,
                        "id": str(row['chunk_id']),
                        "category": category
                    })

                # 4. Final Sort & Top-K Slicing
                scored_results.sort(key=lambda x: x['score'], reverse=True)
                top_results = scored_results[:top_k]

                if not top_results:
                    logger.info("RAG Search: 0 chunks passed confidence gates after ensemble scoring.")
                    if call_logger:
                        call_logger.log_event("retrieval", "rag_search_complete", meta={"matches": 0, "top_score": 0}, trace_id=trace_id)
                    return "LOW_CONFIDENCE_FALLBACK", 0.0, "General", "unknown", []

                # Format return values
                final_chunks = [r['content'] for r in top_results]
                best_score = top_results[0]['score']
                best_category = top_results[0]['category']
                chunk_ids = [r['id'] for r in top_results]

                logger.info(f"RAG Search: Found {len(top_results)} ensemble-verified chunks (Top Score: {best_score:.2f})")
                
                if call_logger:
                    call_logger.log_event("retrieval", "rag_search_complete",
                                         meta={"matches": len(top_results), "top_score": round(best_score, 2)},
                                         trace_id=trace_id)
                
                return "\n\n".join(final_chunks), best_score, best_category, "pgvector-ensemble-v1", chunk_ids

        except Exception as e:
            logger.error(f"KnowledgeBase Search Failed: {e}", exc_info=True)
            return "No specific documents found due to an internal knowledge base error.", 0.0, "General", "unknown", []
