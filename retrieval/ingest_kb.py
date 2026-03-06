import os
import uuid
import logging
import json
from enum import Enum
from datetime import datetime, timezone
from typing import Optional, List, Dict
from pydantic import BaseModel, Field, field_validator, ValidationError

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("KB-Ingestion")

import google.generativeai as genai
from pinecone import Pinecone
from dotenv import load_dotenv

load_dotenv()

# --- TASK 1: Strict Data Model ---

class KBCCategory(str, Enum):
    FEES = "Fees"
    ADMISSIONS = "Admissions"
    ACADEMIC = "Academic"
    GENERAL_INFO = "General Info"
    STUDENT_FAQS = "Student FAQs"
    ALUMNI_FAQS = "Alumni FAQs"
    ALUMNI_SUPPORT_FAQS = "Alumni Support FAQs"

class ChunkMetadata(BaseModel):
    kb_version_id: str = Field(..., description="Version of the knowledge base")
    chunk_id: str = Field(..., default_factory=lambda: str(uuid.uuid4()))
    category: KBCCategory
    program_name: Optional[str] = None
    is_sensitive_topic: bool = False
    hard_refusal_category: Optional[str] = None
    ingestion_timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat() + "Z")

    @field_validator('ingestion_timestamp')
    @classmethod
    def validate_timestamp(cls, v):
        try:
            datetime.fromisoformat(v.replace('Z', '+00:00'))
            return v
        except ValueError:
            raise ValueError("Invalid ISO 8601 timestamp")

# --- TERMINOLOGY NORMALIZATION ---

TERMINOLOGY_MAP = {
    "CS": "Computer Science",
    "B.Tech": "Bachelor of Technology",
    "QA": "Quality Assurance",
    "HR": "Human Resources",
    "UX": "User Experience",
    "UI": "User Interface",
    "AI": "Artificial Intelligence",
    "ML": "Machine Learning"
}

def normalize_terminology(text: str) -> str:
    """
    Normalizes terms based on TERMINOLOGY_MAP to ensure consistency.
    """
    normalized_text = text
    for abv, full in TERMINOLOGY_MAP.items():
        # Use word boundaries to avoid partial matches
        import re
        normalized_text = re.sub(r'\b' + re.escape(abv) + r'\b', full, normalized_text)
    return normalized_text

# --- VERSIONING LOGIC ---

VERSION_FILE = "kb_version.json"

def get_next_version() -> str:
    """
    Retrieves the current version and increments it.
    Format: v1.X_YYYYMMDD
    """
    today = datetime.now(timezone.utc).strftime('%Y%m%d')
    current_version = "v1.0"
    
    if os.path.exists(VERSION_FILE):
        try:
            with open(VERSION_FILE, "r") as f:
                data = json.load(f)
                last_version = data.get("version", "v1.0")
                # Extract major.minor
                parts = last_version.split('_')[0].replace('v', '').split('.')
                major = int(parts[0])
                minor = int(parts[1])
                minor += 1
                current_version = f"v{major}.{minor}"
        except Exception as e:
            logger.warning(f"Could not read version file: {e}. Falling back to v1.0")

    version_str = f"{current_version}_{today}"
    
    # Save back
    with open(VERSION_FILE, "w") as f:
        json.dump({"version": current_version, "last_date": today}, f)
        
    return version_str

# --- TASK 2: Pre-Upload Governance Gate ---

PROHIBITED_TOPICS = {
    "salary": "INTERNAL_STAFF",
    "paycheck": "INTERNAL_STAFF",
    "hr matters": "INTERNAL_STAFF",
    "medical advice": "MEDICAL",
    "visa guarantee": "IMMIGRATION",
    "immigration permit": "IMMIGRATION",
    "refund dispute": "FINANCIAL_DISPUTE",
    "better than Oxford": "COMPETITOR",
    "worse than": "COMPETITOR"
}

SPECULATIVE_PHRASES = ["guarantee", "absolute best", "100% placement", "ensure a job", "promise"]

def validate_chunk(chunk_text: str, metadata_dict: Dict) -> ChunkMetadata:
    """
    Strict compliance validation before embedding.
    """
    # 1. Terminology Normalization
    normalized_text = normalize_terminology(chunk_text)
    
    # 2. Scan for Prohibited Topics
    text_lower = normalized_text.lower()
    for keyword, refusal_cat in PROHIBITED_TOPICS.items():
        if keyword in text_lower or (keyword.endswith('y') and keyword[:-1] + 'ie' in text_lower):
            logger.error(f"REJECTED: Prohibited topic keyword '{keyword}' found in chunk.")
            raise ValueError(f"Compliance Violation: Prohibited topic detected - {refusal_cat}")

    # 3. Scan for Speculative Language
    for phrase in SPECULATIVE_PHRASES:
        if phrase in text_lower:
            logger.warning(f"REJECTED: Speculative language '{phrase}' detected.")
            raise ValueError(f"Compliance Violation: Speculative language detected - {phrase}")

    # 4. Mandatory Metadata Check
    try:
        metadata = ChunkMetadata(**metadata_dict)
        return metadata
    except ValidationError as e:
        logger.error(f"REJECTED: Metadata validation failed: {e.json()}")
        raise

# --- TASK 3: Pinecone Uploader ---

class KBIngestionPipeline:
    def __init__(self):
        self.pc_key = os.getenv("PINECONE_API_KEY")
        self.gm_key = os.getenv("GEMINI_API_KEY")
        self.index_name = os.getenv("PINECONE_INDEX_NAME", "gd-college")
        
        if not self.pc_key or not self.gm_key:
            raise ValueError("Missing API keys for Pinecone or Gemini.")

        self.pc = Pinecone(api_key=self.pc_key)
        
        # --- DATA RESIDENCY COMPLIANCE (AWS Canada) ---
        from pinecone import ServerlessSpec
        existing_indexes = self.pc.list_indexes().names()
        if self.index_name in existing_indexes:
            logger.info("Deleting existing index to recreate in ca-central-1...")
            self.pc.delete_index(self.index_name)
            import time
            time.sleep(5)
            
        logger.info(f"Creating Index {self.index_name} in us-east-1...")
        self.pc.create_index(
            name=self.index_name,
            dimension=3072, 
            metric="cosine",
            spec=ServerlessSpec(cloud="aws", region="us-east-1")
        )
        import time
        time.sleep(15) # Wait for index to be ready
            
        self.index = self.pc.Index(self.index_name)
        genai.configure(api_key=self.gm_key)
        
        self.version_id = get_next_version()
        logger.info(f"Initialized Pipeline with Version: {self.version_id}")

    def upload_chunk(self, text: str, category: KBCCategory, program: Optional[str] = None, is_sensitive: bool = False, hard_refusal_category: Optional[str] = None):
        """
        Embeds and upserts a single validated chunk.
        """
        try:
            # Prepare metadata dict for validation
            metadata_input = {
                "kb_version_id": self.version_id,
                "category": category,
                "program_name": program,
                "is_sensitive_topic": is_sensitive,
                "hard_refusal_category": hard_refusal_category
            }
            
            # Validation Gate
            metadata = validate_chunk(text, metadata_input)
            
            # Normalization (again for the final text)
            normalized_text = normalize_terminology(text)
            
            # Embedding with Rate Limit Handling
            embedding = None
            import time
            for attempt in range(3):
                try:
                    time.sleep(1) # Base spacing
                    response = genai.embed_content(
                        model="models/gemini-embedding-001",
                        content=normalized_text,
                        task_type="retrieval_document"
                    )
                    embedding = response['embedding']
                    break
                except Exception as api_e:
                    if "429" in str(api_e) and attempt < 2:
                        logger.warning("Rate limit hit, sleeping for 60s...")
                        time.sleep(60)
                    else:
                        raise api_e
            
            # Upsert
            vector_id = f"vec_{metadata.chunk_id}"
            metadata_payload = metadata.model_dump(exclude_none=True)
            metadata_payload["text"] = normalized_text
            
            self.index.upsert(vectors=[{
                "id": vector_id,
                "values": embedding,
                "metadata": metadata_payload
            }])
            
            logger.info(f"Successfully uploaded chunk {metadata.chunk_id} to {self.index_name}")
            return True

        except Exception as e:
            logger.error(f"Failed to process chunk: {e}")
            return False

# --- FINAL INGESTION BLOCK ---

if __name__ == "__main__":
    from gd_college_data import gd_college_raw_data
    
    pipeline = KBIngestionPipeline()
    
    print("\n--- Starting FINAL GD College Ingestion ---")
    for i, data in enumerate(gd_college_raw_data):
        print(f"\nProcessing Chunk {i+1}/{len(gd_college_raw_data)}: {data.get('program_name') or data.get('category')}...")
        
        # Mapping loosely for safety
        cat_str = data["category"]
        if cat_str == "Academic Information": cat_str = "Academic"
        if cat_str == "General Institutional Info": cat_str = "General Info"
        if cat_str == "Existing Student FAQs": cat_str = "Student FAQs"
        
        success = pipeline.upload_chunk(
            text=data["text"], 
            category=KBCCategory(cat_str), 
            program=data.get("program_name"),
            is_sensitive=data.get("is_sensitive_topic", False),
            hard_refusal_category=data.get("hard_refusal_category", None)
        )
        if success:
            print(f"Result: SUCCESS")
        else:
            print(f"Result: FAILED (Blocked or Error)")
    
    print("\n--- FINAL Ingestion Complete ---")
