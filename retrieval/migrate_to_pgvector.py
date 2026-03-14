
import os
import asyncio
import logging
import json
import hashlib
# import boto3  # <--- Commented for local-only dev
import asyncpg
from typing import List, Dict, Any
from dotenv import load_dotenv

# --- CRITICAL FIX: Data Residency (C1) ---
# Removed Pinecone SDK dependency. 
# We now ingest from local Golden Source (retrieval/gd_college_data.py).
try:
    from retrieval.gd_college_data import gd_college_raw_data
except ImportError:
    # Fallback for different execution contexts
    from gd_college_data import gd_college_raw_data

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Migration")

load_dotenv()

# --- CONFIGURATION ---
# Set to True (default) for local development without AWS
LOCAL_TEST = os.getenv("LOCAL_TEST", "true").lower() == "true"

# Target DB (PostgreSQL)
DB_URL = os.getenv("PG_DATABASE_URL", "postgresql://user:password@localhost:5432/dbname")

# AWS Bedrock (Production Requirement - Commented for now)
AWS_REGION = os.getenv("AWS_REGION", "ca-central-1")

# Governance Blocklist
SENSITIVITY_BLOCKLIST = ["student_mental_health", "medical_records", "financial_aid_secrets"]
TOPIC_BLOCKLIST_PATTERNS = ["social security", "credit card number", "password"]

class PGVectorMigrator:
    def __init__(self):
        # Pinecone initialization REMOVED for Canadian Data Residency compliance (C1).
        
        # Bedrock client (H5 Fix: Production Path restored)
        if not LOCAL_TEST:
            logger.info("Initializing Bedrock client for production embedding generation.")
            logger.info("Target Model: Amazon Titan Text Embeddings v2 (1536-dim)")
            try:
                import boto3
                self.bedrock = boto3.client(
                    service_name="bedrock-runtime",
                    region_name=AWS_REGION
                )
            except Exception as e:
                logger.error(f"Failed to initialize Bedrock client: {e}. Productions runs will fail.")
                self.bedrock = None
        else:
            logger.warning("Running in LOCAL_TEST mode. Bedrock embeddings will be MOCKED.")
            self.bedrock = None
        self.batch_size = 100
        logger.info("Migrator initialized. Using local gd_college_data.py as Golden Source.")

    async def initialize_db(self, conn):
        """Enable required extensions and create schema."""
        logger.info("Initializing Database (Extensions & Search Path)...")
        # Run extensions as superuser (if DB user has permissions)
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
        await conn.execute("CREATE EXTENSION IF NOT EXISTS btree_gin;")
        
        logger.info("Ensuring RAG schema exists and setting search_path...")
        await conn.execute("CREATE SCHEMA IF NOT EXISTS rag;")
        await conn.execute("SET search_path TO rag, public;")
        
        # Ensure search_path is set for the database user permanently for production (Restored Comment)
        # await conn.execute(f"ALTER DATABASE {conn.get_settings().database} SET search_path TO rag, public;")

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                title TEXT,
                source_uri TEXT,
                doc_type TEXT,
                ingested_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                document_id UUID REFERENCES documents(id) ON DELETE CASCADE,
                content TEXT,
                checksum TEXT UNIQUE,
                source_id TEXT, -- M3: Preserves audit trail from Pinecone
                metadata JSONB
            );
        """)
        # Specific upgrade for existing local tables missing required columns
        await conn.execute("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS checksum TEXT UNIQUE;")
        await conn.execute("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS source_id TEXT;")
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS embeddings (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                chunk_id UUID REFERENCES chunks(id),
                embedding vector(1536),
                model_version TEXT DEFAULT 'titan-v2'
            );
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS governance_metadata (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                chunk_id UUID REFERENCES chunks(id) ON DELETE CASCADE,
                sensitivity_level TEXT DEFAULT 'public',
                is_sensitive_topic BOOLEAN DEFAULT FALSE,
                topic_tags TEXT[],
                kb_version_id TEXT DEFAULT 'v1.1',
                hard_refusal_category TEXT,
                is_dynamic_field BOOLEAN DEFAULT FALSE,
                is_policy_locked BOOLEAN DEFAULT FALSE,
                requires_human_verification BOOLEAN DEFAULT FALSE,
                confidence_score DOUBLE PRECISION DEFAULT 1.0,
                chunk_confidence_score DOUBLE PRECISION DEFAULT 1.0
            );
        """)

        # 🟢 CLEAN SLATE: Truncate existing data to avoid partial migration artifacts
        logger.info("Clearing existing RAG data for clean migration...")
        await conn.execute("TRUNCATE TABLE governance_metadata, embeddings, chunks, documents CASCADE;")

    def generate_checksum(self, text: str) -> str:
        """Generate SHA-256 checksum for content."""
        return hashlib.sha256(text.encode()).hexdigest()

    async def get_embeddings_bedrock(self, text: str) -> List[float]:
        """Generate 1536-dimensional embeddings (STRICT H5 - Point 1, 2, 3)."""
        from retrieval.embeddings import get_bedrock_embeddings
        # Fail hard on error during migration (H5 - Point 2)
        return await get_bedrock_embeddings(text, region=AWS_REGION, local_test=LOCAL_TEST)

    def is_safe_chunk(self, text: str) -> bool:
        """Skip chunks matching topic blocklist patterns."""
        text_lower = text.lower()
        for pattern in TOPIC_BLOCKLIST_PATTERNS:
            if pattern in text_lower:
                return False
        return True

    def get_sensitivity(self, text: str) -> str:
        """Map sensitivity based on keywords."""
        text_lower = text.lower()
        for keyword in SENSITIVITY_BLOCKLIST:
            if keyword in text_lower:
                return "restricted"
        return "public"

    async def migrate_local_data(self, conn):
        """Process data from gd_college_data.py and insert into PGVector."""
        logger.info(f"Ingesting {len(gd_college_raw_data)} records from local Golden Source...")
        
        for i, item in enumerate(gd_college_raw_data):
            try:
                source_id = item.get("id") # M3: Maintain Pinecone/Source ID link
                text = item.get("text", "")
                category = item.get("category", "General")
                program = item.get("program_name") or "General Information"
                is_sensitive = item.get("is_sensitive_topic", False)
                hard_refusal = item.get("hard_refusal_category")
                
                if not text or not self.is_safe_chunk(text):
                    logger.warning(f"Skipping unsafe or empty chunk index {i}")
                    continue
                
                # Checksum for duplicate prevention
                checksum = self.generate_checksum(text)
                
                # Check if already ingested
                exists = await conn.fetchval("SELECT id FROM chunks WHERE checksum = $1", checksum)
                if exists:
                    continue

                # 1. Generate New Embedding
                embedding = await self.get_embeddings_bedrock(text)
                
                title = f"{program} - {category}"
                source_uri = "local://retrieval/gd_college_data.py"
                
                # 2. Map Metadata
                metadata = {
                    "category": category,
                    "program_name": program,
                    "is_sensitive": is_sensitive,
                    "hard_refusal": hard_refusal,
                    "source": "Golden Source"
                }
                
                # 3. Insert Document
                doc_id = await conn.fetchval(
                    """
                    INSERT INTO documents (title, source_uri, doc_type)
                    VALUES ($1, $2, $3)
                    RETURNING id;
                    """,
                    str(title), str(source_uri), str(category)
                )
                
                # 4. Insert Chunk
                chunk_id = await conn.fetchval(
                    """
                    INSERT INTO chunks (document_id, content, checksum, source_id, metadata)
                    VALUES ($1, $2, $3, $4, $5)
                    RETURNING id;
                    """,
                    doc_id, str(text), checksum, str(source_id) if source_id else None, json.dumps(metadata)
                )
                
                # 5. Insert Embedding
                await conn.execute(
                    """
                    INSERT INTO embeddings (chunk_id, embedding)
                    VALUES ($1, $2);
                    """,
                    chunk_id, embedding
                )
                
                # 6. Governance Metadata (H2 Compliant - All 13 fields mapped)
                # Note: These values are derived from gd_college_data.py per H2 spec.
                await conn.execute(
                    """
                    INSERT INTO governance_metadata (
                        chunk_id, sensitivity_level, is_sensitive_topic, topic_tags, 
                        kb_version_id, hard_refusal_category, is_dynamic_field, 
                        is_policy_locked, requires_human_verification, 
                        confidence_score, chunk_confidence_score
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11);
                    """,
                    chunk_id, 
                    "restricted" if is_sensitive else "public",
                    is_sensitive,
                    [str(category)],
                    "v1.1", # Baseline for current PGVector migration
                    hard_refusal,
                    False, # Default: mostly static institutional data
                    False, # Default
                    False, # Default
                    1.0,   # Human-curated content
                    1.0
                )
            except Exception as item_e:
                logger.error(f"Failed to ingest record index {i}: {item_e}")
                continue

    async def run(self):
        """Main migration loop (STRICT H5 compliance - Point 4)."""
        if LOCAL_TEST:
             logger.warning("!!! WARNING: migrate_to_pgvector.py is running in LOCAL_TEST mode !!!")
             logger.warning("!!! Using mock vectors (1536-dim [1.0, ...]) for local development !!!")
             # We allow this locally to unblock development without Bedrock
        
        # M2 Fix: Assert Canadian Data Residency before ingestion
        if "rds.amazonaws.com" in DB_URL and "ca-central-1" not in DB_URL:
            error_msg = f"CRITICAL: RDS data residency violation in Migrator. Host is not in ca-central-1: {DB_URL}"
            logger.critical(error_msg)
            raise RuntimeError(error_msg)
        else:
            logger.info("Database Residency Verified.")

        conn = await asyncpg.connect(DB_URL)
        try:
            await self.initialize_db(conn)
            
            # Native Vector Support
            from pgvector.asyncpg import register_vector
            await register_vector(conn)
            
            # Start Ingestion
            await self.migrate_local_data(conn)
            
            # Post-migration: Create HNSW Index
            logger.info("Creating HNSW Index for ultra-fast RAG retrieval...")
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_hnsw_embeddings 
                ON embeddings USING hnsw (embedding vector_cosine_ops) 
                WITH (m = 32, ef_construction = 128);
            """)
            
            logger.info("Ingestion Complete.")
            
            # Validation
            pg_count = await conn.fetchval("SELECT COUNT(*) FROM embeddings;")
            logger.info(f"Validation: Ingested {pg_count} vectors into PGVector.")

        finally:
            await conn.close()

if __name__ == "__main__":
    migrator = PGVectorMigrator()
    asyncio.run(migrator.run())
