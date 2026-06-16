-- KOEL — PostgreSQL bootstrap (PRD §10 action item: Database Schema Design)
-- Run once against a fresh database before the SQLAlchemy models create tables.
-- The pgvector extension must exist before KnowledgeChunk.embedding is created.

CREATE EXTENSION IF NOT EXISTS vector;

-- The application creates tables from koel.db.models via metadata.create_all.
-- An IVFFlat index on the embedding column accelerates RAG retrieval once the
-- knowledge base is seeded (PRD §10 step 2). Create it after seeding so the
-- index lists reflect real data distribution:
--
--   CREATE INDEX IF NOT EXISTS knowledge_chunks_embedding_idx
--     ON knowledge_chunks USING ivfflat (embedding vector_cosine_ops)
--     WITH (lists = 100);
