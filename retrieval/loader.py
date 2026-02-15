import os
import logging
from pinecone import Pinecone
import google.generativeai as genai
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logger = logging.getLogger("RAG-Loader")

def retrieve_context(query, top_k=3):
    """
    Search Pinecone for relevant knowledge chunks and enforce a strict 
    post-retrieval guardrail to prevent sensitive info from reaching the LLM.
    
    Returns:
        dict: {"status": "success", "context": "..."} OR
              {"status": "blocked", "reason": "HARD_REFUSAL_CATEGORY"}
    """
    pc_key = os.getenv("PINECONE_API_KEY")
    gm_key = os.getenv("GEMINI_API_KEY")
    index_name = os.getenv("PINECONE_INDEX_NAME", "gd-college")

    if not pc_key or not gm_key:
        logger.error("RAG-Loader: Missing API keys in .env")
        return {"status": "error", "message": "Missing credentials"}

    try:
        # 1. Initialize Pinecone & AI Engine
        pc = Pinecone(api_key=pc_key)
        index = pc.Index(index_name)
        genai.configure(api_key=gm_key)

        # 2. Embed Query (Using Gemini Embedding-001 for index compatibility)
        # Dimensions: 3072 (matches the gd-college index created in ingest.py)
        response = genai.embed_content(
            model="models/gemini-embedding-001",
            content=query,
            task_type="retrieval_query"
        )
        query_embedding = response['embedding']

        # 3. Search Pinecone with Metadata
        search_results = index.query(
            vector=query_embedding,
            top_k=top_k,
            include_metadata=True
        )

        matches = search_results.get('matches', [])
        valid_chunks = []

        # 4. THE SAFETY FILTER (Post-Retrieval Guardrail)
        for match in matches:
            metadata = match.get('metadata', {})
            is_sensitive = metadata.get('is_sensitive_topic', False)
            score = match.get('score', 0.0)
            
            # CRITICAL CHECK: If any match is sensitive, block the entire response
            if is_sensitive:
                # STOP immediately if metadata flags this as restricted
                refusal_category = metadata.get('hard_refusal_category', 'GENERAL_POLICY_VIOLATION')
                
                logger.warning(f"RAG-BLOCK: sensitive content detected (Score: {score:.2f}) for query '{query}'. Category: {refusal_category}")
                
                # Return the block dictionary as per Requirement
                return {
                    "status": "blocked", 
                    "reason": refusal_category
                }

            # Else, collect the safe text
            text = metadata.get('text', '')
            if text:
                valid_chunks.append(text)

        # 5. Normal Retrieval Result
        if not valid_chunks:
            return {"status": "success", "context": ""}

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
