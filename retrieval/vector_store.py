import os
import logging
from pinecone import Pinecone
import google.generativeai as genai
from dotenv import load_dotenv

# Configure logging
logger = logging.getLogger("Retrieval")

load_dotenv()

from contracts.interfaces import KnowledgeBaseEngine

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

    def search(self, query, call_logger=None, top_k=3):
        """
        Search the knowledge base for relevant documents.
        """
        if not self.index:
            return ""

        try:
            # Generate Embedding for Query
            response = genai.embed_content(
                model="models/gemini-embedding-001",
                content=query,
                task_type="retrieval_query"
            )
            query_embedding = response['embedding']

            # Query Pinecone
            results = self.index.query(
                vector=query_embedding,
                top_k=top_k,
                include_metadata=True
            )

            # Extract text from metadata
            context_chunks = []
            scores = []
            for match in results.get('matches', []):
                text = match.get('metadata', {}).get('text', '')
                score = match.get('score', 0)
                if text:
                    context_chunks.append(text)
                    scores.append(score)
            
            # Log retrieval boundary with statistics (not content)
            top_score = 0.0
            if scores:
                top_score = max(scores) # Assume index is cosine sim, 0-1
                logger.info(f"RAG Search: Found {len(context_chunks)} matches (Top Score: {top_score:.2f})")
                
                # Structured log event for RAG search completion
                if call_logger:
                    call_logger.log_event("retrieval", "rag_search_complete",
                                         meta={"matches": len(context_chunks), 
                                               "top_score": round(top_score, 2)})
            else:
                logger.info("RAG Search: Found 0 matches")
                if call_logger:
                    call_logger.log_event("retrieval", "rag_search_complete",
                                         meta={"matches": 0, "top_score": 0})

            return "\n".join(context_chunks), top_score
        except Exception as e:
            logger.error(f"ERROR: Knowledge Search Error: {e}")
            return "", 0.0
