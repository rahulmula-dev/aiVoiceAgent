import os
import logging
from pinecone import Pinecone
import google.generativeai as genai
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logger = logging.getLogger("RAG-Loader")

# --- CONFIGURATION ---
DEFAULT_THRESHOLD = 0.58
CATEGORY_THRESHOLDS = {
    "HARD_REFUSAL_LEGAL": 0.75,       # Higher bar for legal
    "HARD_REFUSAL_IMMIGRATION": 0.75, # Higher bar for immigration
    "HARD_REFUSAL_HARASSMENT": 0.80   # Highest bar for harassment
}

def get_threshold(category):
    """Returns the specific threshold for a category, or the default."""
    if not category:
        return DEFAULT_THRESHOLD
    return CATEGORY_THRESHOLDS.get(category, DEFAULT_THRESHOLD)

def retrieve_context(query, top_k=3):
    """
    Retrieves context with strict Safety (Task 2.2) and Confidence (Task 2.3) gates.
    Returns:
        dict: {"status": "success", "context": "..."} OR
              {"status": "blocked", "reason": "CATEGORY"} OR
              {"status": "low_confidence", "context": ""}
    """
    pc_key = os.getenv("PINECONE_API_KEY")
    gm_key = os.getenv("GEMINI_API_KEY")
    index_name = os.getenv("PINECONE_INDEX_NAME", "gd-college")

    if not pc_key or not gm_key:
        logger.error("RAG-Loader: Missing API keys in .env")
        return {"status": "error", "message": "Missing credentials"}

    try:
        # 1. Initialize & Embed
        pc = Pinecone(api_key=pc_key)
        index = pc.Index(index_name)
        genai.configure(api_key=gm_key)

        response = genai.embed_content(
            model="models/gemini-embedding-001",
            content=query,
            task_type="retrieval_query"
        )
        query_embedding = response['embedding']

        # 2. Query Pinecone
        search_results = index.query(
            vector=query_embedding,
            top_k=top_k,
            include_metadata=True
        )

        matches = search_results.get('matches', [])
        valid_chunks = []

        logger.info(f"RAG-Search: '{query}' ({len(matches)} potential matches)")

        # 3. The "Double-Filter" Loop
        for match in matches:
            score = match.get('score', 0.0)
            metadata = match.get('metadata', {})
            text = metadata.get('text', '')
            is_sensitive = metadata.get('is_sensitive_topic', False)
            category = metadata.get('hard_refusal_category', "")

            # --- GATE 1: SAFETY (Task 2.2) ---
            if is_sensitive:
                # If it's sensitive, we STOP immediately as per PRD
                refusal_cat = category or "GENERAL_POLICY_VIOLATION"
                logger.warning(f"RAG-BLOCK: Sensitive topic detected (Score: {score:.2f}) | Category: {refusal_cat}")
                return {
                    "status": "blocked", 
                    "reason": refusal_cat
                }

            # --- GATE 2: CONFIDENCE (Task 2.3) ---
            threshold = get_threshold(category)
            
            if score < threshold:
                logger.debug(f"RAG-DROP: Score {score:.2f} < Threshold {threshold} for chunk")
                continue # Discard low quality chunk
            
            # Passed both gates
            if text:
                valid_chunks.append(text)

        # 4. Final Decision
        if not valid_chunks:
            logger.info(f"RAG-FALLBACK: Low Confidence / No Valid Chunks for '{query}'")
            return {
                "status": "low_confidence", 
                "context": ""
            }

        logger.info(f"RAG-SUCCESS: Retrieved {len(valid_chunks)} verified chunks.")
        combined_text = "\n\n".join(valid_chunks)
        return {
            "status": "success",
            "context": combined_text
        }

    except Exception as e:
        logger.error(f"RAG-Loader Error: {e}")
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    # Test safe query
    print("Testing Safe Query...")
    print(retrieve_context("What programs do you offer?"))
    
    # Test sensitive query
    print("\nTesting Sensitive Query (Should Block)...")
    print(retrieve_context("I want to talk about visa and immigration"))

    # Test nonsense/low-confidence query
    print("\nTesting Nonsense Query (Should Fallback)...")
    print(retrieve_context("xyzabc123 non-existent college facility"))
