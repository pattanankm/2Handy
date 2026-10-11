# Zero-Downtime User-Name Migration (All 5 Phases)

This directory contains all 5 sequential migration scripts for Checkpoint 2 (CP2), implementing the **Splitting a Column** scenario:

```text
public.users.name  ->  public.users.first_name + public.users.last_name
```

## Files in `migrations/` (Phases 1 to 5)

| Phase | File | Type | Role |
| :--- | :--- | :--- | :--- |
| **Phase 1: Expand** | `001_expand_user_name.sql` | SQL DDL | Adds nullable `first_name` and `last_name` columns with `lock_timeout = '2s'` |
| **Phase 2: Dual Write** | `002_dual_write.py` | Python / API | Switches Web API (`main.py`) to write to BOTH `name` and `first_name`+`last_name` |
| **Phase 3: Backfill** | `003_backfill.py` (calls `../backfill.py`) | Python Job | Batched background migration using cursor pagination (`id > last_id`) & checkpointing |
| **Phase 4: Switch Read** | `004_switch_read.py` | Python / API | Switches Web API (`main.py`) to read/write exclusively from `first_name`+`last_name` |
| **Phase 5: Contract** | `005_contract_drop_legacy_user_name.sql` | SQL DDL | Verifies 0 data loss and drops legacy `public.users.name` column safely |
| **Verification (Pre/Post Backfill)** | `verify_user_name_migration.sql` | SQL Query | Proves 0 un-migrated rows and 0 mismatched names before & after Phase 3 |
| **Verification (Post Contract)** | `verify_user_name_post_contract.sql` | SQL Query | Proves `name` column is removed and all target rows remain populated after Phase 5 |

---

## Step-by-Step Execution

```bash
export DATABASE_URL='postgresql://dev_user:dev_password@localhost:5432/main_db'

# Phase 1 — Expand
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/001_expand_user_name.sql

# Phase 2 — Dual Write
python migrations/002_dual_write.py
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/verify_user_name_migration.sql

# Phase 3 — Backfill
python migrations/003_backfill.py
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/verify_user_name_migration.sql

# Phase 4 — Switch Read
python migrations/004_switch_read.py

# Phase 5 — Contract
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -v confirm_contract=true -f migrations/005_contract_drop_legacy_user_name.sql
python switch_phase.py 5
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/verify_user_name_post_contract.sql
```
