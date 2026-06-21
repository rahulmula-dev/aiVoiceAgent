# KB Architecture - GD College AI Voice Agent
**Version:** 1.2 (Post-Migration)
**Last Updated:** 2026-03-13

## 1. Overview
This document outlines the Knowledge Base (KB) architecture for the GD College AI Voice Agent. As of v1.2, the system has migrated from Pinecone to **PGVector (PostgreSQL 16)** to comply with Canadian Data Residency laws and improve latency control.

## 2. Infrastructure
- **Vector Database**: PGVector on RDS (PostgreSQL 16)
- **Deployment Region**: `ca-central-1` (Calgary/Montreal) - **STRICT REQUIREMENT**
- **Connection Pool**: `asyncpg`
  - `min_size`: 10
  - `max_size`: 40 (Sized for 30 concurrent calls)

## 3. Embedding Pipeline
- **Model**: Amazon Titan Text Embeddings v2
- **Dimensions**: 1536
- **Normalization**: L2 Normalized (Cosine Similarity optimized)
- **Consistency**: Shared embedding utility in `retrieval/embeddings.py` used by both Ingestion and Search.

## 4. Search & Scoring Logic
The retrieval engine uses a **Weighted Ensemble Score** to prioritize accuracy:
`Final Score = (0.7 * Cosine Similarity) + (0.3 * Semantic Relevance)`

- **Cosine Similarity**: Range [0, 1], computed via `<=>` operator.
- **Semantic Relevance**: Trigram similarity computed via `pg_trgm`.

## 5. Metadata Schema (H2 Compliance)
Each chunk is associated with the following 13 metadata fields:
1. `chunk_id`: Primary key (UUID)
2. `document_id`: Parent document link
3. `content`: Raw text chunk
4. `source`: Data source identifier
5. `category`: Organizational category
6. `sensitivity_level`: Governance level (public/restricted)
7. `kb_version_id`: Current architecture version (v1.2)
8. `hard_refusal_category`: Pre-check safety mapping
9. `is_dynamic_field`: Toggle for real-time data
10. `is_policy_locked`: Toggle for compliance lock
11. `requires_human_verification`: Audit flag
12. `confidence_score`: Post-retrieval metric
13. `is_sensitive_topic`: Boolean flag

### 5.1 Legacy Audit Trail (M3 Compliance)
- `source_id`: Stores legacy Pinecone Chunk IDs to maintain audit continuity from Sprint 4.

## 6. Migration Trace (v1.1 -> v1.2)
- **Source**: Pinecone (US-hosted)
- **Target**: PGVector (ca-central-1)
- **Ingestion Method**: Golden Source local migration via `migrate_to_pgvector.py`.
- **Validation**: Strict dimension checking (1536) and non-zero vector enforcement.

## 7. Performance Budget
- **Target Retrieval Latency**: <350ms
- **Timeout (Failsafe)**: 5.0 seconds
- **Connection Buffer**: Re-ranking performed in-DB on Top-50 candidates.
