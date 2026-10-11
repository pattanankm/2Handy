-- Run only AFTER Phase 5 has committed successfully.
-- The pre-contract full legacy-to-new comparison must be retained as evidence,
-- because users.name intentionally no longer exists at this point.

\pset pager off

-- The legacy field must be absent.
SELECT NOT EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'users'
      AND column_name = 'name'
) AS legacy_name_column_removed;

-- Target data survives Contract. `users_missing_first_name` should be zero for
-- every user that had a non-empty legacy name before migration.
SELECT
    count(*) AS total_users,
    count(*) FILTER (WHERE NULLIF(btrim(first_name), '') IS NOT NULL) AS users_with_first_name,
    count(*) FILTER (WHERE NULLIF(btrim(first_name), '') IS NULL) AS users_missing_first_name
FROM public.users;
