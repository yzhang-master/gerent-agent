-- Optional. pgvector is NOT assumed: where the extension is unavailable, semantic
-- memory and tool retrieval fall back to the full-text index on memories.tsv behind the
-- same interface. Applied only if the extension can be created.

CREATE EXTENSION IF NOT EXISTS vector;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS embedding VECTOR(1024);
