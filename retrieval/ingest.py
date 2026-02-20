import os
import time
import uuid
from datetime import datetime
from pinecone import Pinecone, ServerlessSpec
import google.generativeai as genai
from dotenv import load_dotenv

# Optional: Try to import Langchain for chunking, fallback to simple split
try:
    from langchain.text_splitter import RecursiveCharacterTextSplitter
    HAS_LANGCHAIN = True
except ImportError:
    HAS_LANGCHAIN = False

load_dotenv()

# --- CONFIGURATION ---
PINECONE_KEY = os.getenv("PINECONE_API_KEY")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
INDEX_NAME = os.getenv("PINECONE_INDEX_NAME", "gd-college")
KB_VERSION = f"v1.0_{datetime.now().strftime('%Y%m%d')}"

# restricted_topics logic (Task 3 Requirement)
RESTRICTED_KEYWORDS = {
    "immigration": "HARD_REFUSAL_IMMIGRATION",
    "visa": "HARD_REFUSAL_IMMIGRATION",
    "visa status": "HARD_REFUSAL_IMMIGRATION",
    "immigration status": "HARD_REFUSAL_IMMIGRATION",
    "legal action": "HARD_REFUSAL_LEGAL",
    "sue": "HARD_REFUSAL_LEGAL",
    "lawsuit": "HARD_REFUSAL_LEGAL",
    "harassment": "HARD_REFUSAL_HARASSMENT"
}

def get_safety_metadata(text):
    """
    Scans text for restricted keywords to populate schema.
    Returns: (is_sensitive, refusal_category)
    """
    text_lower = text.lower()
    for keyword, category in RESTRICTED_KEYWORDS.items():
        if keyword in text_lower:
            return True, category
    return False, ""

# --- 1. THE TRUTH SOURCE (Calgary, Canada Context) ---
knowledge_chunks_raw = [
    # --- GENERAL COLLEGE INFO ---
    "GD College Mission: To empower students of all genders with skills for financial independence in beauty and cosmetology. We focus on business marketing, portfolio building, and job interview preparation.",
    "GD College is located in Calgary, Alberta, Canada. The specific address is #108, 1935-27 Ave NE, Calgary, AB T2E 7E4.",
    "General Admissions: A high school diploma or equivalent is required. You can apply online via the GD College website or visit the campus.",
    "Financial Aid: GD College offers various financial aid and payment options to help manage tuition costs. Contact the admissions office for details.",
    "Class Schedules: We offer flexible schedules including morning, afternoon, evening, and weekend options.",

    # --- PROGRAMS & DATES (2026) ---
    "Advanced Esthetics Diploma: A 10-month on-site program (8-month intensive). Covers practical training with real clients. Next Batch: February 24, 2026.",
    "Clinical Esthetician Diploma: A 5-month on-site professional certification. Includes job placement assistance. Next Batch: May 18, 2026.",
    "Esthetician Diploma: A 5-month on-site program with state-of-the-art facilities. Next Batch: February 24, 2026.",
    "Makeup Artist & Hairstylist Diploma: A 4-month on-site program. Requires high school diploma; no prior experience needed. Includes career counselling. Next Batch: February 24, 2026.",
    "Massage Therapy Diploma: A 2-year on-site professional program. Next Batch: May 18, 2026.",
    "Nail Technician Diploma: A 4-month (14-week) on-site course. Suitable for beginners. Next Batch: February 24, 2026.",
    
    # --- POLICIES & SAFETY (Critical Guardrails) ---
    "CALL DURATION LIMIT: To ensure all students can be served, each automated session is restricted to a maximum of 5 minutes.",
    "SENSITIVE: GD College does not provide immigration or visa advice. Students must contact IRCC directly.",
    "SENSITIVE: We have a zero-tolerance policy for harassment. Any legal action or lawsuit should be directed to our legal department."
]

def ingest_data():
    if not PINECONE_KEY or not GEMINI_KEY:
        print("Error: Missing API keys in .env")
        return

    print("Connecting to Pinecone...")
    pc = Pinecone(api_key=PINECONE_KEY)
    
    # Recreate Index for new model dimensions
    existing_indexes = pc.list_indexes().names()
    if INDEX_NAME in existing_indexes:
        print(f"Deleting existing index '{INDEX_NAME}' to update data with refined safety tags...")
        pc.delete_index(INDEX_NAME)
        time.sleep(5)
    
    print(f"Creating Index '{INDEX_NAME}' (3072 dimensions)...")
    pc.create_index(
        name=INDEX_NAME,
        dimension=3072, 
        metric="cosine",
        spec=ServerlessSpec(cloud="aws", region="us-east-1")
    )
    time.sleep(15) # Wait for index to be ready
    
    index = pc.Index(INDEX_NAME)
    genai.configure(api_key=GEMINI_KEY)

    print("Processing knowledge into chunks...")
    final_chunks = []
    if HAS_LANGCHAIN:
        splitter = RecursiveCharacterTextSplitter(chunk_size=400, chunk_overlap=50)
        final_chunks = splitter.split_text("\n\n".join(knowledge_chunks_raw))
    else:
        # Simple fallback if Langchain is missing
        final_chunks = knowledge_chunks_raw

    print(f"Embedding & Uploading {len(final_chunks)} chunks with SMART METADATA...")
    vectors = []
    
    for i, text in enumerate(final_chunks):
        try:
            # A. Generate Safety Tags
            is_sensitive, refusal_cat = get_safety_metadata(text)
            
            # B. Generate Unique ID
            chunk_id = str(uuid.uuid4())

            # C. Generate Embedding (Gemini Embedding 001)
            time.sleep(15)  # ADDED: Rate Limit buffer (Increased for Free Tier)
            response = genai.embed_content(
                model="models/gemini-embedding-001",
                content=text,
                task_type="retrieval_document"
            )
            
            # D. Construct Full Metadata (STRICT SCHEMA)
            metadata = {
                "text": text,
                "kb_version_id": KB_VERSION,          # Requirement 1
                "chunk_id": chunk_id,                 # Requirement 2
                "chunk_confidence_score": 1.0,        # Requirement 3 (Default)
                "is_sensitive_topic": is_sensitive,   # Requirement 4
                "hard_refusal_category": refusal_cat  # Requirement 5
            }

            # E. Immediate Upsert (Resilience)
            vector_data = [{
                "id": f"vec_{i}",
                "values": response['embedding'],
                "metadata": metadata
            }]
            index.upsert(vectors=vector_data)
            print(f"   [SUCCESS] Uploaded chunk {i+1}/{len(final_chunks)} (Sensitive: {is_sensitive})")
            
        except Exception as e:
            print(f"   [FAILED] Chunk {i+1}: {e}")
            if "429" in str(e):
                print("      -> Waiting 60s for Rate Limit cooldown...")
                time.sleep(60)

    print("\nIngestion Process Complete.")

if __name__ == "__main__":
    ingest_data()
