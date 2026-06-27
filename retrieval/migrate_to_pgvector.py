"""
retrieval/migrate_to_pgvector.py — One-shot knowledge ingestion into pgvector.

Reads every record from retrieval/gd_college_data.py (the "Golden Source"),
generates a 1536-dim embedding per chunk, and writes into four tables:

    rag.documents          — one row per logical document / program
    rag.chunks             — raw text + SHA-256 checksum (dedup guard)
    rag.embeddings         — vector(1536) for each chunk
    rag.governance_metadata — sensitivity, hard-refusal tags, QA fields

After ingestion, builds an HNSW index for fast approximate-nearest-neighbour
search. Idempotent: chunks with matching checksums are silently skipped.

Usage:
    uv run python -m retrieval.migrate_to_pgvector

Prerequisites:
    1. Postgres with pgvector running:  docker-compose up -d postgres
    2. PG_DATABASE_URL set in .env      (e.g. postgresql://postgres:pass@localhost:5432/postgres)
    3. LOCAL_TEST=true  (default) for mock embeddings — no AWS credentials needed.
       LOCAL_TEST=false for real Bedrock Titan v2 embeddings (requires AWS creds).
"""

import os
import asyncio
import logging
import json
import hashlib
from dotenv import load_dotenv
# asyncpg imported lazily inside run() — not needed at import time
# Install when ready:  uv pip install asyncpg pgvector

try:
    from retrieval.gd_college_data import gd_college_raw_data
except ImportError:
    from gd_college_data import gd_college_raw_data

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Migration")
load_dotenv()

LOCAL_TEST = os.getenv("LOCAL_TEST", "true").lower() in ("1", "true", "yes", "on")
DB_URL = os.getenv("PG_DATABASE_URL", "postgresql://postgres:password@localhost:5432/postgres")
AWS_REGION = os.getenv("AWS_REGION", "ca-central-1")

SENSITIVITY_BLOCKLIST = ["student_mental_health", "medical_records", "financial_aid_secrets"]
TOPIC_BLOCKLIST_PATTERNS = ["social security", "credit card number", "password"]


class PGVectorMigrator:
    """Orchestrates the full knowledge ingestion pipeline into pgvector."""

    def __init__(self) -> None:
        if LOCAL_TEST:
            logger.warning("[MIGRATION] LOCAL_TEST mode — using mock embeddings ([1.0]*1536)")
            self.bedrock = None
        else:
            logger.info("[MIGRATION] Production mode — using AWS Bedrock Titan v2")
            try:
                import boto3
                self.bedrock = boto3.client(
                    service_name="bedrock-runtime", region_name=AWS_REGION
                )
            except Exception as e:
                logger.error(f"Failed to init Bedrock client: {e}")
                self.bedrock = None

    # ── Schema init ───────────────────────────────────────────────────────────

    async def initialize_db(self, conn) -> None:
        """Create extensions, schema, tables. Truncates existing data for a clean run."""
        logger.info("[MIGRATION] Initializing database schema...")

        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
        await conn.execute("CREATE EXTENSION IF NOT EXISTS btree_gin;")

        await conn.execute("CREATE SCHEMA IF NOT EXISTS rag;")
        await conn.execute("SET search_path TO rag, public;")

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
                source_id TEXT,
                metadata JSONB
            );
        """)
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

        logger.info("[MIGRATION] Truncating existing RAG data for clean migration...")
        await conn.execute(
            "TRUNCATE TABLE governance_metadata, embeddings, chunks, documents CASCADE;"
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def generate_checksum(self, text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()

    def is_safe_chunk(self, text: str) -> bool:
        t = text.lower()
        return not any(p in t for p in TOPIC_BLOCKLIST_PATTERNS)

    def get_sensitivity(self, text: str) -> str:
        t = text.lower()
        return "restricted" if any(k in t for k in SENSITIVITY_BLOCKLIST) else "public"

    async def get_embedding(self, text: str):
        from retrieval.embeddings import get_bedrock_embeddings
        return await get_bedrock_embeddings(text, region=AWS_REGION, local_test=LOCAL_TEST)

    # ── Ingestion ─────────────────────────────────────────────────────────────

    async def migrate_local_data(self, conn) -> None:
        logger.info(f"[MIGRATION] Ingesting {len(gd_college_raw_data)} records...")

        for i, item in enumerate(gd_college_raw_data):
            try:
                source_id = item.get("id")
                text = item.get("text", "")
                category = item.get("category", "General")
                program = item.get("program_name") or "General Information"
                is_sensitive = item.get("is_sensitive_topic", False)
                hard_refusal = item.get("hard_refusal_category")

                if not text or not self.is_safe_chunk(text):
                    logger.warning(f"  Skipping unsafe/empty record #{i}")
                    continue

                checksum = self.generate_checksum(text)

                # Idempotency check — skip if already ingested.
                if await conn.fetchval("SELECT id FROM chunks WHERE checksum = $1", checksum):
                    continue

                embedding = await self.get_embedding(text)
                title = f"{program} - {category}"
                metadata = {
                    "category": category,
                    "program_name": program,
                    "is_sensitive": is_sensitive,
                    "hard_refusal": hard_refusal,
                    "source": "Golden Source",
                }

                doc_id = await conn.fetchval(
                    "INSERT INTO documents (title, source_uri, doc_type) "
                    "VALUES ($1, $2, $3) RETURNING id;",
                    title,
                    "local://retrieval/gd_college_data.py",
                    category,
                )

                chunk_id = await conn.fetchval(
                    "INSERT INTO chunks (document_id, content, checksum, source_id, metadata) "
                    "VALUES ($1, $2, $3, $4, $5) RETURNING id;",
                    doc_id, text, checksum,
                    str(source_id) if source_id else None,
                    json.dumps(metadata),
                )

                await conn.execute(
                    "INSERT INTO embeddings (chunk_id, embedding) VALUES ($1, $2);",
                    chunk_id, embedding,
                )

                await conn.execute(
                    """
                    INSERT INTO governance_metadata (
                        chunk_id, sensitivity_level, is_sensitive_topic, topic_tags,
                        kb_version_id, hard_refusal_category, is_dynamic_field,
                        is_policy_locked, requires_human_verification,
                        confidence_score, chunk_confidence_score
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11);
                    """,
                    chunk_id,
                    "restricted" if is_sensitive else "public",
                    is_sensitive,
                    [str(category)],
                    "v1.1",
                    hard_refusal,
                    False, False, False, 1.0, 1.0,
                )

            except Exception as e:
                logger.error(f"  Failed on record #{i}: {e}")
                continue

    # ── Main entry point ──────────────────────────────────────────────────────

    async def run(self) -> None:
        if LOCAL_TEST:
            logger.warning("[MIGRATION] WARNING: using mock vectors — not for production!")

        if "rds.amazonaws.com" in DB_URL and "ca-central-1" not in DB_URL:
            raise RuntimeError(
                f"Data residency violation: RDS host not in ca-central-1. URL: {DB_URL}"
            )

        import asyncpg
        conn = await asyncpg.connect(DB_URL)
        try:
            await self.initialize_db(conn)

            from pgvector.asyncpg import register_vector
            await register_vector(conn)

            await self.migrate_local_data(conn)

            logger.info("[MIGRATION] Building HNSW index...")
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_hnsw_embeddings
                ON embeddings USING hnsw (embedding vector_cosine_ops)
                WITH (m = 32, ef_construction = 128);
            """)

            count = await conn.fetchval("SELECT COUNT(*) FROM embeddings;")
            logger.info(f"[MIGRATION] Done — {count} vectors in pgvector.")

        finally:
            await conn.close()


if __name__ == "__main__":
    asyncio.run(PGVectorMigrator().run())
