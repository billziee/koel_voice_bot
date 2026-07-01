-- KOEL — PostgreSQL bootstrap (PRD §10 action item: Database Schema Design)
-- Run once against a fresh database before the SQLAlchemy models create tables.
-- The pgvector extension must exist before KnowledgeChunk.embedding is created.

CREATE EXTENSION IF NOT EXISTS vector;

-- The application creates tables from koel.db.models via metadata.create_all.
-- scripts/bootstrap_db.py then creates an HNSW index on the embedding column for
-- sub-100ms cosine ANN retrieval at scale (pgvector >= 0.5; preferred over IVFFlat
-- for recall and because it needs no post-seed rebuild):
--
--   CREATE INDEX IF NOT EXISTS knowledge_chunks_embedding_hnsw
--     ON knowledge_chunks USING hnsw (embedding vector_cosine_ops);
--
-- (IVFFlat alternative for pgvector < 0.5, built AFTER seeding so lists reflect data:
--   CREATE INDEX ... USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);)
