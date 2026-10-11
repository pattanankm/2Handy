# Database Admin: Zero-Downtime User-Name Migration

This directory contains the SQL scripts for Checkpoint 2 (CP2), implementing the **Splitting a Column** scenario:

```text
public.users.name  ->  public.users.first_name + public.users.last_name
```

`name` is the legacy field in [`models.py`](../models.py). `last_name` deliberately remains nullable: a one-word legacy name is valid when the backfill stores it in `first_name` and leaves `last_name` empty (`""`). The verification scripts compare the reconstructed full name (`first_name` + `last_name`), ensuring zero data loss.

---

## Connection Setup

**macOS / Linux (zsh/bash):**
```bash
export DATABASE_URL='postgresql://dev_user:dev_password@localhost:5432/main_db'
```

**Windows (PowerShell):**
```powershell
$env:DATABASE_URL = 'postgresql://dev_user:dev_password@localhost:5432/main_db'
```

*(Or run via Docker directly without local `psql`: `docker exec -i $(docker compose ps -q postgres_db) psql -U dev_user -d main_db -v ON_ERROR_STOP=1 < migrations/001_expand_user_name.sql`)*

---

## Phase 1 — Expand

Run this while the API is running in Phase 1 (Legacy mode):

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/001_expand_user_name.sql
```

The new columns (`first_name`, `last_name`) are nullable and have no default value, so PostgreSQL does not rewrite existing rows. A short `lock_timeout = '2s'` prevents the DDL command from queuing behind long queries.

---

## Verification Before and After Backfill (Phase 3)

Run the verification queries before Backfill to capture the baseline counts, and again after `python backfill.py` completes:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/verify_user_name_migration.sql
```

For the post-backfill result to pass:
- `legacy_rows_waiting_for_backfill` must be `0`
- `mismatched_or_missing_rows` must be `0`
- The detailed exception query must return `0 rows`

---

## Phase 5 — Contract

After switching the API to Phase 4 (`python switch_phase.py 4`) so that the API reads and writes exclusively using `first_name` and `last_name`, run the Contract script:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -v confirm_contract=true -f migrations/005_contract_drop_legacy_user_name.sql
```

The script refuses to run without `-v confirm_contract=true`, verifies inside the transaction that every legacy `name` matches `first_name` + `last_name`, and drops `public.users.name` with a `2s` lock timeout.

After Phase 5 succeeds, switch the phase indicator (`python switch_phase.py 5`) and run post-contract verification:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/verify_user_name_post_contract.sql
```
