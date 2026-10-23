-- Database verification queries for users.name -> first_name + last_name.
-- Run this file before Backfill, immediately after Backfill, and before
-- Contract. Copy the result sets into the demo evidence.
--
-- psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
--   -f migrations/verify_user_name_migration.sql

\pset pager off

-- A. Schema proof (Phase 1): all three fields must coexist before Contract.
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'users'
  AND column_name IN ('name', 'first_name', 'last_name')
  ORDER BY column_name;

-- B. Baseline to record BEFORE Backfill. `legacy_non_empty_names` is the
-- source-record count. It must equal `fully_migrated_legacy_names` afterwards.
SELECT
    count(*) AS total_users,
    count(*) FILTER (WHERE NULLIF(btrim(name), '') IS NOT NULL) AS legacy_non_empty_names,
    count(*) FILTER (
        WHERE NULLIF(btrim(name), '') IS NOT NULL
          AND NULLIF(btrim(first_name), '') IS NOT NULL
    ) AS fully_migrated_legacy_names,
    count(*) FILTER (
        WHERE NULLIF(btrim(name), '') IS NOT NULL
          AND NULLIF(btrim(first_name), '') IS NULL
    ) AS legacy_rows_waiting_for_backfill
FROM public.users;

-- C. Run AFTER Backfill. PASS only when `mismatched_or_missing_rows` is 0.
-- This compares every legacy value with a reconstructed new value rather than
-- merely comparing row counts, so reordered, truncated, or missing names fail.
WITH normalised_names AS (
    SELECT
        id,
        regexp_replace(btrim(name), '\s+', ' ', 'g') AS legacy_name,
        regexp_replace(
            btrim(concat_ws(' ', NULLIF(btrim(first_name), ''), NULLIF(btrim(last_name), ''))),
            '\s+', ' ',
            'g'
        ) AS reconstructed_name
    FROM public.users
    WHERE NULLIF(btrim(name), '') IS NOT NULL
)
SELECT
    count(*) AS source_rows,
    count(*) FILTER (WHERE legacy_name = reconstructed_name) AS exact_matches,
    count(*) FILTER (WHERE legacy_name IS DISTINCT FROM reconstructed_name) AS mismatched_or_missing_rows
FROM normalised_names;

-- D. Detailed exception list. It must return zero rows before Phase 5.
WITH normalised_names AS (
    SELECT
        id,
        name AS legacy_name,
        first_name,
        last_name,
        regexp_replace(btrim(name), '\s+', ' ', 'g') AS normalised_legacy_name,
        regexp_replace(
            btrim(concat_ws(' ', NULLIF(btrim(first_name), ''), NULLIF(btrim(last_name), ''))),
            '\s+', ' ',
            'g'
        ) AS reconstructed_name
    FROM public.users
    WHERE NULLIF(btrim(name), '') IS NOT NULL
)
SELECT id, legacy_name, first_name, last_name, reconstructed_name
FROM normalised_names
WHERE normalised_legacy_name IS DISTINCT FROM reconstructed_name
ORDER BY id;
