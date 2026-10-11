-- Phase 5 (CONTRACT)
-- Permanently remove users.name only after the application has switched reads
-- and Database verification reports zero mismatches.
--
-- Required invocation (the explicit flag prevents an accidental drop):
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -v confirm_contract=true \
--     -f migrations/005_contract_drop_legacy_user_name.sql

\if :{?confirm_contract}
\else
  \echo 'Refusing to run: pass -v confirm_contract=true after Phase 4 and verification.'
  \quit
\endif
\if :confirm_contract
\else
  \echo 'Refusing to run: confirm_contract must be true.'
  \quit
\endif

BEGIN;

-- Never queue a disruptive ACCESS EXCLUSIVE lock behind live API traffic.
-- If PostgreSQL cannot acquire it quickly, this transaction fails unchanged;
-- retry during the next low-traffic window after investigating the lock.
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '10s';

DO $$
DECLARE
    legacy_column_exists BOOLEAN;
    invalid_row_count BIGINT;
BEGIN
    SELECT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'users'
          AND column_name = 'name'
    ) INTO legacy_column_exists;

    IF NOT legacy_column_exists THEN
        RAISE EXCEPTION 'Contract aborted: public.users.name does not exist';
    END IF;

    -- A non-empty legacy name must have a first name and reproduce exactly
    -- after normalising whitespace. This is the final 0-data-loss guard.
    SELECT count(*)
    INTO invalid_row_count
    FROM public.users
    WHERE NULLIF(btrim(name), '') IS NOT NULL
      AND (
          NULLIF(btrim(first_name), '') IS NULL
          OR regexp_replace(btrim(name), '\s+', ' ', 'g')
             IS DISTINCT FROM
             regexp_replace(
                 btrim(concat_ws(' ', NULLIF(btrim(first_name), ''), NULLIF(btrim(last_name), ''))),
                 '\s+', ' ',
                 'g'
             )
      );

    IF invalid_row_count <> 0 THEN
        RAISE EXCEPTION
            'Contract aborted: % user rows are missing or differ from first_name/last_name',
            invalid_row_count;
    END IF;
END $$;

ALTER TABLE public.users DROP COLUMN name;

COMMIT;

-- Post-condition: the legacy column is gone and no target data was removed.
SELECT id, first_name, last_name
FROM public.users
ORDER BY id
LIMIT 20;
