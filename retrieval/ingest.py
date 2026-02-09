import os
import time
from pinecone import Pinecone, ServerlessSpec
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

# --- CONFIGURATION ---
PINECONE_KEY = os.getenv("PINECONE_API_KEY")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
INDEX_NAME = "gd-college"

# --- 1. THE TRUTH SOURCE (Calgary, Canada Context) ---
knowledge_chunks = [
    "GD College is located in Calgary, Alberta, Canada. The specific address is #108, 1935-27 Ave NE, Calgary, AB T2E 7E4.",
    "It is a recognized cosmetology school offering diploma programs in Esthetics, Makeup Artistry, Hairstyling, and Massage Therapy.",
    "The GD College AI Voice Agent handles inbound calls from prospective students, existing students, and alumni. It operates 24/7 replacing the need for a human receptionist.",
    "RESTRICTED TOPICS: The AI must never discuss internal staff issues, salary, HR matters, political opinions, medical advice, or immigration guarantees. If asked, politely refuse.",
    "For vendors, partners, or internal staff inquiries, please contact the college office via email. The AI only assists with student-related queries.",
    "The system targets a 1-2 second response latency. It operates strictly within defined knowledge boundaries and never hallucinates.",
    "If the user interrupts (barge-in), the AI stops speaking immediately. It then asks 'Should I continue from where I left off?' before proceeding.",
    "Admissions for the 2026 Batch are currently open. Please visit the GD College website for specific fee structures and application deadlines.",
    "CALL DURATION LIMIT: To ensure all students can be served, each automated session is restricted to a maximum of 5 minutes. The AI will provide a warning 30 seconds before the limit is reached."
]

def ingest_data():
    if not PINECONE_KEY or not GEMINI_KEY:
        print("Error: Missing API keys in .env")
        return

    print(f"Connecting to Pinecone...")
    pc = Pinecone(api_key=PINECONE_KEY)
    
    # Recreate Index for new model dimensions
    existing_indexes = pc.list_indexes().names()
    if INDEX_NAME in existing_indexes:
        print(f"Deleting existing index '{INDEX_NAME}' to update dimensions...")
        pc.delete_index(INDEX_NAME)
        time.sleep(5)
    
    print(f"Creating Index '{INDEX_NAME}' (3072 dimensions)...")
    pc.create_index(
        name=INDEX_NAME,
        dimension=3072, 
        metric="cosine",
        spec=ServerlessSpec(cloud="aws", region="us-east-1")
    )
    time.sleep(10)
    
    index = pc.Index(INDEX_NAME)
    genai.configure(api_key=GEMINI_KEY)

    print("Embedding & Uploading Knowledge...")
    vectors = []
    
    for i, text in enumerate(knowledge_chunks):
        try:
            # Use text-embedding-004
            response = genai.embed_content(
                model="models/gemini-embedding-001",
                content=text,
                task_type="retrieval_document"
            )
            vectors.append({
                "id": f"vec_{i}",
                "values": response['embedding'],
                "metadata": {"text": text}
            })
            print(f"   - Processed chunk {i+1}/{len(knowledge_chunks)}")
        except Exception as e:
            print(f"Error embedding chunk {i}: {e}")

    # Upsert to Pinecone
    if vectors:
        index.upsert(vectors=vectors)
        print(f"\nSUCCESS: Uploaded {len(vectors)} facts to the Brain!")
    else:
        print("\nFAILED: No vectors created.")

if __name__ == "__main__":
    ingest_data()
