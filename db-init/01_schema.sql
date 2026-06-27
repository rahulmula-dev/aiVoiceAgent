-- db-init/01_schema.sql
-- Executed automatically by the pgvector/pgvector:pg16 container on first boot
-- (mounted to /docker-entrypoint-initdb.d/).
-- The migrate_to_pgvector.py script handles table creation too (idempotently),
-- so this file is a belt-and-suspenders init that ensures the schema exists
-- even before the first migration run.

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS btree_gin;

-- Isolate RAG tables under a dedicated schema
CREATE SCHEMA IF NOT EXISTS rag;

-- Set search_path for subsequent statements in this file
SET search_path TO rag, public;

-- documents: one row per logical document / program
CREATE TABLE IF NOT EXISTS rag.documents (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title       TEXT,
    source_uri  TEXT,
    doc_type    TEXT,
    ingested_at TIMESTAMPTZ DEFAULT NOW()
);

-- chunks: raw text with SHA-256 checksum for deduplication
CREATE TABLE IF NOT EXISTS rag.chunks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID REFERENCES rag.documents(id) ON DELETE CASCADE,
    content     TEXT,
    checksum    TEXT UNIQUE,
    source_id   TEXT,
    metadata    JSONB
);

-- embeddings: vector(1536) stored separately to keep text queries lean
CREATE TABLE IF NOT EXISTS rag.embeddings (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chunk_id      UUID REFERENCES rag.chunks(id) ON DELETE CASCADE,
    embedding     vector(1536),
    model_version TEXT DEFAULT 'titan-v2'
);

-- governance_metadata: sensitivity, hard-refusal tags, QA/compliance fields
CREATE TABLE IF NOT EXISTS rag.governance_metadata (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chunk_id                    UUID REFERENCES rag.chunks(id) ON DELETE CASCADE,
    sensitivity_level           TEXT DEFAULT 'public',
    is_sensitive_topic          BOOLEAN DEFAULT FALSE,
    topic_tags                  TEXT[],
    kb_version_id               TEXT DEFAULT 'v1.1',
    hard_refusal_category       TEXT,
    is_dynamic_field            BOOLEAN DEFAULT FALSE,
    is_policy_locked            BOOLEAN DEFAULT FALSE,
    requires_human_verification BOOLEAN DEFAULT FALSE,
    confidence_score            DOUBLE PRECISION DEFAULT 1.0,
    chunk_confidence_score      DOUBLE PRECISION DEFAULT 1.0
);

-- HNSW index built by the migration script after data is loaded
-- (pre-declaring here as a no-op if migration hasn't run yet)
-- CREATE INDEX IF NOT EXISTS idx_hnsw_embeddings
--     ON rag.embeddings USING hnsw (embedding vector_cosine_ops)
--     WITH (m = 32, ef_construction = 128);
