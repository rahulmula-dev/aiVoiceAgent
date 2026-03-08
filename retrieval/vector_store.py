import os
import logging
import concurrent.futures
from pinecone import Pinecone
import google.generativeai as genai
from dotenv import load_dotenv

# Configure logging
logger = logging.getLogger("Retrieval")

load_dotenv()

from contracts.interfaces import KnowledgeBaseEngine

import json

# --- CONFIGURATION (Sync with loader.py) ---
def _load_thresholds():
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "rag_thresholds.json")
    try:
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
        self.pc_key = os.getenv("PINECONE_API_KEY")
        self.gm_key = os.getenv("GEMINI_API_KEY")
        self.index_name = os.getenv("PINECONE_INDEX_NAME", "gd-college")
        
        if not self.pc_key or not self.gm_key:
            logger.warning("KnowledgeBase: Missing API keys")
            self.index = None
            return

        try:
            self.pc = Pinecone(api_key=self.pc_key)
            self.index = self.pc.Index(self.index_name)
            genai.configure(api_key=self.gm_key)
            logger.info(f"KnowledgeBase Connected to Index: {self.index_name}")
        except Exception as e:
            logger.warning(f"KnowledgeBase Init Failed: {e}")
            self.index = None

    async def check_health(self) -> bool:
        """Verifies KnowledgeBase connectivity for readiness probe."""
        if not self.index:
            return False
        try:
            # Pinecone check: describe_index_stats is lightweight
            stats = self.index.describe_index_stats()
            return stats is not None
        except Exception as e:
            logger.warning(f"KnowledgeBase health check failed: {e}")
            return False

    def search(self, query, call_logger=None, top_k=3, trace_id=None):
        """
        Search with strict Safety (Task 2.2) and Confidence (Task 2.3) gates.
        Returns: (context_text, top_confidence_score, topic, kb_version, chunk_ids)
        """
        if not self.index:
            return "", 0.0, "General", "unknown", []

        # PRD §5 RETRY LOOP: 2 attempts, ≤300ms each
        MAX_ATTEMPTS = 2
        ATTEMPT_TIMEOUT = 0.3  # 300ms budget per PRD §5
        last_error = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    # Run the entire embed+query block in a thread so we can enforce a timeout
                    def _run_search():
                        # 1. Embed Query
                        response = genai.embed_content(
                            model="models/gemini-embedding-001",
                            content=query,
                            task_type="retrieval_query"
                        )
                        query_embedding = response['embedding']

                        # 2. Query Pinecone with Dynamic Metadata Filter (Story S4-1: Deterministic Fees)
                        search_filter = {}
                        if "fee" in query.lower() or "cost" in query.lower() or "price" in query.lower():
                            search_filter = {"category": "Fees"}
                            logger.debug(f"Applying Deterministic Fee Filter for query: '{query}'")

                        results = self.index.query(
                            vector=query_embedding,
                            top_k=top_k,
                            include_metadata=True,
                            filter=search_filter if search_filter else None
                        )
                        return results

                    future = executor.submit(_run_search)
                    try:
                        results = future.result(timeout=ATTEMPT_TIMEOUT)
                    except concurrent.futures.TimeoutError:
                        raise TimeoutError(f"RAG attempt {attempt} timed out after {ATTEMPT_TIMEOUT*1000:.0f}ms")

                # Search succeeded — process results below
                break

            except Exception as e:
                last_error = e
                logger.warning(f"[RAG] Search attempt {attempt}/{MAX_ATTEMPTS} failed: {e}")
                if attempt < MAX_ATTEMPTS:
                    pass  # No sleep: search() runs in a sync thread, retry is immediate
                else:
                    logger.error(f"[RAG] All {MAX_ATTEMPTS} search attempts failed. Last error: {last_error}")
                    return "", 0.0, "General", "unknown", []

        # 3. The "Double-Filter" Loop
        valid_chunks = []
        scores = []
        chunk_ids = []
        kb_versions = set()
        matches = results.get('matches', [])

        for match in matches:
            score = match.get('score', 0.0)
            metadata = match.get('metadata', {})
            text = metadata.get('text', '')
            is_sensitive = metadata.get('is_sensitive_topic', False)
            hard_refusal_category = metadata.get('hard_refusal_category', "")
            general_category = metadata.get('category', "")

            # GATE 1: Safety (Refined to avoid collateral over-blocking)
            if is_sensitive:
                if score >= 0.70:
                    refusal_cat = hard_refusal_category or "GENERAL_POLICY_VIOLATION"
                    logger.warning(f"RAG-BLOCK: High-confidence sensitive content detected (Score: {score:.2f})")
                    if call_logger:
                        call_logger.log_event("retrieval", "rag_search_blocked", 
                                             meta={"reason": refusal_cat, "score": round(score, 2)},
                                             trace_id=trace_id)
                    return "BLOCKED_BY_SAFETY_GUARDRAIL", score
                else:
                    logger.debug(f"RAG-SKIP: Sensitive neighbor detected but score {score:.2f} < 0.70 (Likely collateral)")
                    continue

            # GATE 2: Confidence
            threshold = get_threshold(hard_refusal_category, general_category)
            if score < threshold:
                logger.debug(f"RAG-DROP: Score {score:.2f} < Threshold {threshold}")
                continue

            if text:
                valid_chunks.append(text)
                scores.append(score)
                chunk_ids.append(metadata.get('chunk_id', 'unknown'))
                if "kb_version_id" in metadata:
                    kb_versions.add(metadata.get('kb_version_id'))

        # 4. Final Logs & Return
        top_score = 0.0
        final_kb_version = "unknown"
        if kb_versions:
            final_kb_version = list(kb_versions)[0] # Take first version found in chunks
            
        if scores:
            top_score = max(scores)
            
            top_match = next((m for m in matches if m.get('score') == top_score), {})
            top_meta = top_match.get('metadata', {})
            kb_version = top_meta.get('kb_version_id', 'unknown')
            top_chunk_id = top_meta.get('chunk_id', 'unknown')
            rag_topic = top_meta.get('category', 'General')

            logger.info(f"RAG Search: Found {len(valid_chunks)} verified chunks (Top Score: {top_score:.2f})")
            
            if call_logger:
                call_logger.log_event("retrieval", "rag_search_complete",
                                     meta={
                                         "matches": len(valid_chunks), 
                                         "top_score": round(top_score, 2),
                                         "kb_version_id": kb_version,
                                         "top_chunk_id": top_chunk_id
                                     },
                                     trace_id=trace_id)
        else:
            logger.info("RAG Search: 0 chunks passed confidence gates.")
            if call_logger:
                call_logger.log_event("retrieval", "rag_search_complete",
                                     meta={"matches": 0, "top_score": 0},
                                     trace_id=trace_id)
            return "LOW_CONFIDENCE_FALLBACK", 0.0, "General", "unknown", []

        return "\n\n".join(valid_chunks), top_score, rag_topic, final_kb_version, chunk_ids
