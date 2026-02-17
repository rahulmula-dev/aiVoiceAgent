import os
import logging
from pinecone import Pinecone
import google.generativeai as genai
from dotenv import load_dotenv

# Configure logging
logger = logging.getLogger("Retrieval")

load_dotenv()

from contracts.interfaces import KnowledgeBaseEngine

# --- CONFIGURATION (Sync with loader.py) ---
DEFAULT_THRESHOLD = 0.58
CATEGORY_THRESHOLDS = {
    "HARD_REFUSAL_LEGAL": 0.75,
    "HARD_REFUSAL_IMMIGRATION": 0.75,
    "HARD_REFUSAL_HARASSMENT": 0.80
}

def get_threshold(category):
    if not category:
        return DEFAULT_THRESHOLD
    return CATEGORY_THRESHOLDS.get(category, DEFAULT_THRESHOLD)

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

    def search(self, query, call_logger=None, top_k=3, trace_id=None):
        """
        Search with strict Safety (Task 2.2) and Confidence (Task 2.3) gates.
        Returns: (context_text, top_confidence_score)
        """
        if not self.index:
            return "", 0.0

        try:
            # 1. Embed Query
            response = genai.embed_content(
                model="models/gemini-embedding-001",
                content=query,
                task_type="retrieval_query"
            )
            query_embedding = response['embedding']

            # 2. Query Pinecone
            results = self.index.query(
                vector=query_embedding,
                top_k=top_k,
                include_metadata=True
            )

            # 3. The "Double-Filter" Loop
            valid_chunks = []
            scores = []
            matches = results.get('matches', [])

            for match in matches:
                score = match.get('score', 0.0)
                metadata = match.get('metadata', {})
                text = metadata.get('text', '')
                is_sensitive = metadata.get('is_sensitive_topic', False)
                category = metadata.get('hard_refusal_category', "")

                # GATE 1: Safety (Refined to avoid collateral over-blocking)
                if is_sensitive:
                    if score >= 0.70:
                        refusal_cat = category or "GENERAL_POLICY_VIOLATION"
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
                threshold = get_threshold(category)
                if score < threshold:
                    logger.debug(f"RAG-DROP: Score {score:.2f} < Threshold {threshold}")
                    continue

                if text:
                    valid_chunks.append(text)
                    scores.append(score)

            # 4. Final Logs & Return
            top_score = 0.0
            if scores:
                top_score = max(scores)
                
                # Extract Top Match Metadata for Tracing (Sprint 2 Requirement)
                # We use the metadata from the top-scoring match
                top_match = next((m for m in matches if m.get('score') == top_score), {})
                top_meta = top_match.get('metadata', {})
                kb_version = top_meta.get('kb_version_id', 'unknown')
                top_chunk_id = top_meta.get('chunk_id', 'unknown')

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
                return "LOW_CONFIDENCE_FALLBACK", 0.0

            return "\n\n".join(valid_chunks), top_score

        except Exception as e:
            logger.error(f"ERROR: Knowledge Search Error: {e}")
            return "", 0.0
