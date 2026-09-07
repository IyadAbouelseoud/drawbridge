-- Bootstrap: n8n gets its own database inside the same instance.
-- Application schema lands in week 2 via Alembic; nothing DDL-ish belongs here.
CREATE DATABASE n8n OWNER drawbridge;

\connect drawbridge
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pg_trgm;
