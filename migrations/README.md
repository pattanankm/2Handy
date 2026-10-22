# Database Admin: zero-downtime user-name migration

This is the Database Admin scope for CP2.  It implements the selected migration scenario:

```text
public.users.name  ->  public.users.first_name + public.users.last_name
```

`name` is the existing field in [models.py](../models.py). The API also
dual-writes `full_name` as a compatibility alias while the split is rolled
out. `last_name` deliberately remains nullable: a one-word legacy name is
valid when the future backfill stores it in `first_name` and leaves
`last_name` empty. The verification scripts compare the reconstructed full
name, so this does not weaken the data-loss check.

## Phase 1 — Expand

Run this while the existing API is still using `users.name`:

```powershell
$env:DATABASE_URL = 'postgresql://dev_user:dev_password@localhost:5432/main_db'
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/001_expand_user_name.sql
```

The new columns are nullable and have no default value. PostgreSQL therefore does not rewrite existing user records. A short `lock_timeout` prevents the migration command from waiting long enough to interrupt live traffic; if it times out, no change is made and it can be retried.

## Verification before and after Backfill

Run the queries before backfill to capture the source count, and again after backfill:

```powershell
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/verify_user_name_migration.sql
```

For the post-backfill result to pass:

- `legacy_rows_waiting_for_backfill` must be `0`;
- `mismatched_or_missing_rows` must be `0`; and
- the detailed exception query must return no rows.

The Data Engineer owns the batched backfill. The Backend Engineer has
implemented dual-write and switch-read in the API. Before using the switched
read endpoints in production, run the backfill and confirm the verification
queries pass; the API no longer falls back to `name` or `full_name` when
building user responses.

## Phase 5 — Contract

First save a passing post-backfill verification result and ensure the switched API no longer reads or writes `users.name`. Then run:

```powershell
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -v confirm_contract=true -f migrations/005_contract_drop_legacy_user_name.sql
```

The script refuses to run without the explicit flag, re-checks every legacy name can be rebuilt from the new fields, and uses a short DDL lock timeout. If the validation or lock acquisition fails, PostgreSQL rolls back the transaction and retains `users.name`.

After it succeeds, run `verify_user_name_post_contract.sql`. It verifies that the old field was removed and the target fields remain populated; keep the pre-contract verification output as the exact legacy-to-new data-loss proof.

## Checkpoint 3 — MongoDB product versions

The MongoDB migration is separate from the PostgreSQL name migration. Run it
against the configured MongoDB database to backfill legacy product documents and
create the indexes used by the CP3 query layer:

```powershell
python migrations/006_mongo_add_version.py
```

Documents without a `version` receive version `0`; newly created products start
at version `1`. The product update and sale queries match the expected version
and increment it atomically.
