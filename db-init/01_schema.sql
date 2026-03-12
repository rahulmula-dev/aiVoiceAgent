-- Sprint 4 | Schema Version: 2.0
-- Initializes the PGVector database for the CILA Voice Agent

-- Enable extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS btree_gin;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Create dedicated RAG schema
CREATE SCHEMA IF NOT EXISTS rag;
SET search_path TO rag, public;

-- Documents table: tracks the source of each knowledge chunk
CREATE TABLE IF NOT EXISTS documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT,
    source_uri TEXT,
    doc_type TEXT,
    ingested_at TIMESTAMPTZ DEFAULT NOW()
);

-- Chunks table: stores the actual text content
CREATE TABLE IF NOT EXISTS chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID REFERENCES documents(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    checksum TEXT UNIQUE,
    metadata JSONB
);

-- Embeddings table: stores 1536-dim vectors (AWS Bedrock Titan v2 / local mock)
-- CRITICAL: vector(1536) — NOT 3072 (which was Pinecone's legacy dimension)
CREATE TABLE IF NOT EXISTS embeddings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chunk_id UUID REFERENCES chunks(id) ON DELETE CASCADE,
    embedding vector(1536) NOT NULL,
    model_version TEXT DEFAULT 'titan-v2'
);

-- Governance metadata: tracks sensitivity and topic tags per chunk
CREATE TABLE IF NOT EXISTS governance_metadata (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chunk_id UUID REFERENCES chunks(id) ON DELETE CASCADE,
    sensitivity_level TEXT DEFAULT 'public',
    topic_tags TEXT[]
);

-- HNSW Index for fast cosine similarity search
-- m=32 and ef_construction=128 are tuned for the current data size
CREATE INDEX IF NOT EXISTS idx_hnsw_embeddings
    ON embeddings USING hnsw (embedding vector_cosine_ops)
    WITH (m = 32, ef_construction = 128);
