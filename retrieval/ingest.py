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
    "status": "HARD_REFUSAL_IMMIGRATION",
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
    # Normal Facts
    "GD College is located in Calgary, Alberta, Canada. The specific address is #108, 1935-27 Ave NE, Calgary, AB T2E 7E4.",
    "It is a recognized cosmetology school offering diploma programs in Esthetics, Makeup Artistry, Hairstyling, and Massage Therapy.",
    "The GD College AI Voice Agent handles inbound calls from prospective students, existing students, and alumni. It operates 24/7 replacing the need for a human receptionist.",
    "GD College Policy: We prioritize student privacy and strictly follow Alberta's post-secondary guidelines.", # Replaced meta-talk with safe policy
    "For vendors, partners, or internal staff inquiries, please contact the college office via email. The AI only assists with student-related queries.",
    "The system targets a 1-2 second response latency. It operates strictly within defined knowledge boundaries and never hallucinates.",
    "If the user interrupts (barge-in), the AI stops speaking immediately. It then asks 'Should I continue from where I left off?' before proceeding.",
    "Admissions for the 2026 Batch are currently open. Please visit the GD College website for specific fee structures and application deadlines.",
    "CALL DURATION LIMIT: To ensure all students can be served, each automated session is restricted to a maximum of 5 minutes.",
    
    # HARD REFUSAL TARGETS (Actually Sensitive)
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

            vectors.append({
                "id": f"vec_{i}",
                "values": response['embedding'],
                "metadata": metadata
            })
            print(f"   - Processed chunk {i+1}/{len(final_chunks)} (Sensitive: {is_sensitive})")
        except Exception as e:
            print(f"Error embedding chunk {i}: {e}")

    # Upsert to Pinecone
    if vectors:
        index.upsert(vectors=vectors)
        print(f"\nSUCCESS: Uploaded {len(vectors)} facts with full metadata to the Brain!")
    else:
        print("\nFAILED: No vectors created.")

if __name__ == "__main__":
    ingest_data()
