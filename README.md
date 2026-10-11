# 2Handy - Secondhand Marketplace API

This project is a Backend API for the "2Handy" secondhand marketplace platform, developed for the course **269340: Data Centric Application Development**. The project implements a Dual-Database architecture (PostgreSQL + MongoDB) and demonstrates a **Zero-Downtime Schema Migration (Checkpoint 2)** using the **Expand and Contract (Parallel Run)** pattern.

## 👥 Team Roster (the smachik gang)
* **1:** 670615108 Prempracha Numchai - PostgreSQL (`Users` + `Orders`) & Dual Write / Switch Read
* **2:** 670615036 Apiluk Bualuang - MongoDB (`Products` + `Reviews`)
* **3:** 670615031 Peeratchai Matunboon - REST API + DDL Migration & Verification Scripts
* **4:** 670615030 Pattanan Siriworakul - Docker + Seed Data + Backfill + Traffic Generator

---

## System Architecture

![Architecture Diagram](ArchitectureDiagram.jpg)

* **Backend API:** Built with Python (FastAPI).
* **PostgreSQL (Transactional Data):** Manages `Users`, `Orders`, and `Order Items`.
* **MongoDB (Flexible Data):** Manages `Products` and `Reviews`.

---

## 🔄 Checkpoint 2: Zero-Downtime Schema Migration (Expand & Contract)

### Migration Scenario
* **Splitting a Column:** Refactoring legacy `public.users.name` into `public.users.first_name` and `public.users.last_name` without downtime under continuous concurrent `POST` + `GET` load ($\ge 20\text{ req/sec}$).

### Step-by-Step Live Demo Runbook

#### 0. Start Databases, Seed Legacy Data & Launch API
**Terminal 1 (API Server):**
```bash
docker compose up -d
source venv/bin/activate
python seed.py
uvicorn main:app --host 127.0.0.1 --port 8000
```

**Terminal 2 (Concurrent Traffic Generator — keep running throughout all 5 phases):**
```bash
source venv/bin/activate
python traffic_gen.py
```

---

#### Phase 1: EXPAND (Add nullable `first_name` and `last_name` columns)
**Terminal 3 (Migration Executor):**
```bash
export DATABASE_URL='postgresql://dev_user:dev_password@localhost:5432/main_db'
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/001_expand_user_name.sql
```
*(Or via Docker if `psql` is not installed locally: `docker exec -i $(docker compose ps -q postgres_db) psql -U dev_user -d main_db -v ON_ERROR_STOP=1 < migrations/001_expand_user_name.sql`)*

---

#### Phase 2: DUAL WRITE (Write to both legacy `name` and new `first_name`/`last_name`)
Switch the running Web API to Dual Write mode (0ms downtime, no server restart required):
```bash
python switch_phase.py 2
```
Run pre-backfill verification query (shows legacy rows waiting for backfill):
```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/verify_user_name_migration.sql
```

---

#### Phase 3: BACKFILL (Batched background migration with cursor pagination & checkpointing)
Run the background backfill job while `traffic_gen.py` continues sending requests:
```bash
python switch_phase.py 3
python backfill.py
```
Verify 100% data consistency after Backfill (`legacy_rows_waiting_for_backfill = 0` and `mismatched_or_missing_rows = 0`):
```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/verify_user_name_migration.sql
```

---

#### Phase 4: SWITCH READ (Read & write exclusively using `first_name` and `last_name`)
Redirect the Web API to consume `first_name` and `last_name` exclusively:
```bash
python switch_phase.py 4
```

---

#### Phase 5: CONTRACT (Safely drop legacy `name` column)
Execute the Contract DDL script (validates 0 data loss before dropping `public.users.name`):
```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -v confirm_contract=true -f migrations/005_contract_drop_legacy_user_name.sql
python switch_phase.py 5
```
Run post-contract verification (`legacy_name_column_removed = t`, `users_missing_first_name = 0`):
```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/verify_user_name_post_contract.sql
```