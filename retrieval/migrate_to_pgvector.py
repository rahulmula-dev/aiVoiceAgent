import os
import asyncio
import logging
import json
import hashlib
# import boto3  # <--- Commented for local-only dev
import asyncpg
from typing import List, Dict, Any
from pinecone import Pinecone
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Migration")

load_dotenv()

# --- CONFIGURATION ---
PINECONE_INDEX_NAME = os.getenv("PINECONE_INDEX_NAME", "gd-college")
PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")

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
        self.pc = Pinecone(api_key=PINECONE_API_KEY)
        self.index = self.pc.Index(PINECONE_INDEX_NAME)
        
        # Bedrock client (Commented for Local Dev)
        # if not LOCAL_TEST:
        #     self.bedrock = boto3.client(
        #         service_name="bedrock-runtime",
        #         region_name=AWS_REGION
        #     )
        # else:
        #     logger.warning("Running in LOCAL_TEST mode. Bedrock embeddings will be MOCKED.")
        #     self.bedrock = None
        
        logger.info("Migrator initialized. Using mock 1536-dim vectors if LOCAL_TEST=true.")
        self.bedrock = None
        
        self.batch_size = 100

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
        # Ensure search_path is set for the database user permanently for production
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
                document_id UUID REFERENCES documents(id),
                content TEXT,
                checksum TEXT UNIQUE,
                metadata JSONB
            );
        """)
        # Specific upgrade for existing local tables missing the checksum column
        await conn.execute("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS checksum TEXT UNIQUE;")
        
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
                chunk_id UUID REFERENCES chunks(id),
                sensitivity_level TEXT,
                topic_tags TEXT[]
            );
        """)

    def generate_checksum(self, text: str) -> str:
        """Generate SHA-256 checksum for content."""
        return hashlib.sha256(text.encode()).hexdigest()

    async def get_embeddings_bedrock(self, text: str) -> List[float]:
        """Generate 1536-dimensional embeddings (Mocked for Local Dev)."""
        # LOCAL MOCK (Consistent Non-Zero Vector)
        return [1.0] * 1536
            
        # PRODUCTION CODE (Uncomment when AWS is set up):
        # body = json.dumps({
        #     "inputText": text,
        #     "dimensions": 1536,
        #     "normalize": True
        # })
        # response = self.bedrock.invoke_model(
        #     body=body,
        #     modelId="amazon.titan-embed-text-v2:0",
        #     accept="application/json",
        #     contentType="application/json"
        # )
        # response_body = json.loads(response.get("body").read())
        # return response_body.get("embedding")

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

    async def migrate_batch(self, conn, vector_ids: List[str]):
        """Fetch from Pinecone, re-embed, and upsert to PGVector."""
        logger.info(f"Processing batch of {len(vector_ids)} records...")
        
        fetch_response = self.index.fetch(vector_ids)
        vectors = fetch_response.get("vectors", {})
        
        for vid, data in vectors.items():
            try:
                metadata = data.get("metadata", {})
                text = metadata.get("text", "")
                source_uri = metadata.get("source_uri", metadata.get("url", "migration://pinecone"))
                
                if not text or not self.is_safe_chunk(text):
                    logger.warning(f"Skipping unsafe or empty chunk: {vid}")
                    continue
                
                # Checksum for duplicate prevention
                checksum = self.generate_checksum(text)
                
                # Check if already ingested
                exists = await conn.fetchval("SELECT id FROM chunks WHERE checksum = $1", checksum)
                if exists:
                    logger.info(f"Skipping duplicate chunk (checksum match): {vid}")
                    continue

                # 1. Generate New Embedding
                try:
                    embedding = await self.get_embeddings_bedrock(text)
                except Exception as e:
                    logger.error(f"Failed to generate embedding for {vid}: {e}")
                    continue
                
                # 2. Map Metadata (Ensure strings, handle potential lists from Pinecone)
                category = metadata.get("category", "General")
                if isinstance(category, list):
                    category = category[0] if category else "General"
                
                program = metadata.get("program_name", "N/A")
                if isinstance(program, list):
                    program = program[0] if program else "N/A"
                    
                title = f"{program} - {category}"
                
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
                    INSERT INTO chunks (document_id, content, checksum, metadata)
                    VALUES ($1, $2, $3, $4)
                    RETURNING id;
                    """,
                    doc_id, str(text), checksum, json.dumps(metadata)
                )
                
                # 5. Insert Embedding (Casting list to string for PGVector ::vector)
                await conn.execute(
                    """
                    INSERT INTO embeddings (chunk_id, embedding)
                    VALUES ($1, $2::vector);
                    """,
                    chunk_id, str(embedding)
                )
                
                # 6. Governance Metadata
                sensitivity = self.get_sensitivity(text)
                await conn.execute(
                    """
                    INSERT INTO governance_metadata (chunk_id, sensitivity_level, topic_tags)
                    VALUES ($1, $2, $3);
                    """,
                    chunk_id, str(sensitivity), [str(category)]
                )
            except Exception as item_e:
                logger.error(f"Failed to migrate record {vid}: {item_e}")
                continue

    async def run(self):
        """Main migration loop."""
        conn = await asyncpg.connect(DB_URL)
        try:
            await self.initialize_db(conn)
            
            # Fetch all IDs from Pinecone
            logger.info("Scanning Pinecone index for IDs...")
            stats = self.index.describe_index_stats()
            total_vectors = stats.get("total_vector_count", 0)
            logger.info(f"Total vectors to migrate: {total_vectors}")
            
            all_ids = []
            for ids in self.index.list():
                all_ids.extend(ids)
            
            # Process in batches
            for i in range(0, len(all_ids), self.batch_size):
                batch_ids = all_ids[i:i + self.batch_size]
                await self.migrate_batch(conn, batch_ids)
            
            # Post-migration: Create HNSW Index
            logger.info("Creating HNSW Index...")
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_hnsw_embeddings 
                ON embeddings USING hnsw (embedding vector_cosine_ops) 
                WITH (m = 32, ef_construction = 128);
            """)
            
            logger.info("Migration Complete.")
            
            # Validation
            pg_count = await conn.fetchval("SELECT COUNT(*) FROM embeddings;")
            logger.info(f"Migration Validation: Pinecone ({len(all_ids)}) members, PGVector ({pg_count}) members.")

        finally:
            await conn.close()

if __name__ == "__main__":
    migrator = PGVectorMigrator()
    asyncio.run(migrator.run())
