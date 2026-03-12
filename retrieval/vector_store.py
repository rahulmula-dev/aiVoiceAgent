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
        logger.info("KnowledgeBase: Initialized (PGVector)")

    async def _ensure_pool(self):
        if self.pool is None:
            try:
                self.pool = await asyncpg.create_pool(
                    self.db_url,
                    min_size=1,
                    max_size=5,
                    command_timeout=5.0
                )
                logger.info("KnowledgeBase: Connected to PGVector Pool")
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
        if self.local_test:
            # Mock 1536-dim vector (all 1s to match newly migrated data)
            return [1.0] * 1536
            
        # PRODUCTION CODE (Commented for now):
        # bedrock = boto3.client(service_name="bedrock-runtime", region_name="ca-central-1")
        # body = json.dumps({"inputText": query, "dimensions": 1536, "normalize": True})
        # response = bedrock.invoke_model(body=body, modelId="amazon.titan-embed-text-v2:0")
        # return json.loads(response.get("body").read()).get("embedding")
        return [0.0] * 1536

    async def search(self, query: str, call_logger=None, top_k=3, trace_id=None):
        """
        Search PGVector with Safety and Confidence gates.
        """
        try:
            await self._ensure_pool()
            
            # 1. Embed Query
            query_embedding = await self.get_query_embedding(query)
            
            async with self.pool.acquire() as conn:
                # Set search_path for the session
                await conn.execute("SET search_path TO rag, public;")
                
                # 2. Query PGVector using <=> (cosine distance)
                # Results sorted by distance ascending
                logger.info(f"RAG-TRACE: Querying with embedding prefix: {query_embedding[:5]}...")
                
                # SPREAD BUG FIX: In local_test mode, embeddings are mocked (all 1s).
                # To avoid missing the relevant chunk because it didn't random-seed into the top_k,
                # we fetch 100 rows (the whole DB currently) for keyword re-ranking.
                fetch_limit = 100 if self.local_test else top_k
                
                rows = await conn.fetch(
                    """
                    SELECT 
                        c.content, 
                        c.metadata,
                        1 - (e.embedding <=> $1::vector) as similarity_score,
                        e.embedding <=> $1::vector as distance,
                        c.id as chunk_id,
                        d.doc_type as category
                    FROM chunks c
                    JOIN embeddings e ON c.id = e.chunk_id
                    JOIN documents d ON c.document_id = d.id
                    ORDER BY e.embedding <=> $1::vector ASC
                    LIMIT $2;
                    """,
                    str(query_embedding), fetch_limit
                )
                
                logger.info(f"RAG-TRACE: Found {len(rows)} raw rows from PGVector.")
                for i, row in enumerate(rows):
                    logger.info(f"ROW {i}: Score={row['similarity_score']}, Distance={row['distance']}, Content={row['content'][:50]}...")

                valid_chunks = []
                scores = []
                chunk_ids = []
                final_category = "General"
                
                for row in rows:
                    score = row['similarity_score']
                    content = row['content']
                    metadata = json.loads(row['metadata']) if isinstance(row['metadata'], str) else row['metadata']
                    category = row['category']
                    
                    # Confidence Gate
                    threshold = get_threshold(None, category)
                    
                    # For local testing with mock vectors, we might want to bypass the threshold if scores are weird
                    if self.local_test:
                        # In local test mode, we accept anything since vectors are mocked
                        pass
                    elif score < threshold:
                        logger.debug(f"RAG-DROP: Score {score:.2f} < Threshold {threshold}")
                        continue
                    
                    valid_chunks.append(content)
                    scores.append(score)
                    chunk_ids.append(str(row['chunk_id']))
                    final_category = category

                if not valid_chunks:
                    logger.info("RAG Search: 0 chunks passed confidence gates.")
                    if call_logger:
                        call_logger.log_event("retrieval", "rag_search_complete", meta={"matches": 0, "top_score": 0}, trace_id=trace_id)
                    return "LOW_CONFIDENCE_FALLBACK", 0.0, "General", "unknown", []

                # --- 🟢 LOCAL SEARCH OPTIMIZATION 🟢 ---
                # If we are in local test mode, embeddings are mocked (all 1s).
                # To ensure the user gets relevant answers for "fee", "location", etc.,
                # we perform a simple keyword-based filter/ranking on the top candidate chunks.
                if self.local_test:
                    logger.info("[LOCAL-RAG] Performing keyword-boost for relevance...")
                    query_words = set(query.lower().replace("?", "").replace(".", "").split())
                    
                    # Score each chunk by intersection count
                    ranked_chunks = []
                    for content in valid_chunks:
                        content_words = set(content.lower().split())
                        overlap = len(query_words.intersection(content_words))
                        if overlap > 0:
                            ranked_chunks.append((overlap, content))
                    
                    # Sort by overlap descending
                    ranked_chunks.sort(key=lambda x: x[0], reverse=True)
                    
                    if ranked_chunks:
                        # Take top 5 boosters for context
                        selected = [c[1] for c in ranked_chunks[:5]]
                        logger.info(f"[LOCAL-RAG] Returning {len(selected)} chunks based on keyword overlap.")
                        return "\n\n".join(selected), 1.0, final_category, "pgvector-local-boost", chunk_ids

                top_score = max(scores) if scores else 0.0
                logger.info(f"RAG Search: Found {len(valid_chunks)} verified chunks (Top Score: {top_score:.2f})")
                
                if call_logger:
                    call_logger.log_event("retrieval", "rag_search_complete",
                                         meta={"matches": len(valid_chunks), "top_score": round(top_score, 2)},
                                         trace_id=trace_id)
                
                return "\n\n".join(valid_chunks), top_score, final_category, "pgvector-v1", chunk_ids

        except Exception as e:
            logger.error(f"KnowledgeBase Search Failed: {e}", exc_info=True)
            return "No specific documents found due to an internal knowledge base error.", 0.0, "General", "unknown", []
