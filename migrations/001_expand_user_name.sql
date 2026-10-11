-- Phase 1 (EXPAND)
-- Migration scenario: users.name -> users.first_name + users.last_name
--
-- Safe for the existing API: both new columns are nullable and no default is
-- added, so PostgreSQL does not need to rewrite existing rows.
--
-- Run with:
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/001_expand_user_name.sql

\pset pager off

BEGIN;

-- Do not wait indefinitely for a DDL lock while the API is serving traffic.
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '10s';

ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS first_name VARCHAR,
    ADD COLUMN IF NOT EXISTS last_name VARCHAR;

COMMIT;

-- Evidence for the live demo: existing rows remain readable and the new
-- columns exist without changing the legacy name value.
SELECT id, name, first_name, last_name
FROM public.users
ORDER BY id
LIMIT 20;
