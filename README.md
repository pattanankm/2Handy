<div align="center">

# 2Handy — Secondhand Marketplace API

**A dual-database backend (PostgreSQL + MongoDB) with a zero-downtime schema migration**

![Python](https://img.shields.io/badge/Python-3.9+-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.128-009688?logo=fastapi&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-15-4169E1?logo=postgresql&logoColor=white)
![MongoDB](https://img.shields.io/badge/MongoDB-6.0-47A248?logo=mongodb&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)

Course project for **269340: Data Centric Application Development**

</div>

---

## Table of Contents

- [Overview](#overview)
- [Team Roster](#team-roster-the-smachik-gang)
- [System Architecture](#system-architecture)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Getting Started](#getting-started)
- [API Reference](#api-reference)
- [Data Model](#data-model)
- [Checkpoint 2: Zero-Downtime Schema Migration](#checkpoint-2-zero-downtime-schema-migration-expand--contract)
- [Checkpoint 3: Concurrency Control & Performance Audit](#checkpoint-3-concurrency-control--performance-audit)
- [Troubleshooting](#troubleshooting)

---

## Overview

**2Handy** is the backend API of a secondhand marketplace platform. It is built around two ideas:

| | What it shows |
| :--- | :--- |
| **Checkpoint 1 — Dual-Database Architecture** | Transactional data (`Users`, `Orders`, `Order Items`) lives in **PostgreSQL**, while flexible, semi-structured data (`Products`, `Reviews`) lives in **MongoDB**. One endpoint (`POST /api/v1/orders`) writes to both and keeps them consistent. |
| **Checkpoint 2 — Zero-Downtime Schema Migration** | The legacy `users.name` column is split into `first_name` + `last_name` using the **Expand and Contract (Parallel Run)** pattern, while the API keeps serving concurrent `POST` + `GET` traffic at ≥ 20 req/sec. |
| **Checkpoint 3 — Concurrency Control & Performance Audit** | Concurrent writes can no longer overwrite each other: PostgreSQL uses row locks, MongoDB uses a `version` token (optimistic concurrency), and the API answers a lost race with **409 Conflict** / **412 Precondition Failed**. Read endpoints use projections and run a fixed number of queries per request (no N+1). |

---

## Team Roster (the smachik gang)

| # | Student ID | Name | Responsibility |
| :-: | :--- | :--- | :--- |
| 1 | 670615108 | Prempracha Numchai | PostgreSQL (`Users` + `Orders`), Dual Write / Switch Read |
| 2 | 670615036 | Apiluk Bualuang | MongoDB (`Products` + `Reviews`) |
| 3 | 670615031 | Peeratchai Matunboon | REST API, DDL Migration & Verification Scripts |
| 4 | 670615030 | Pattanan Siriworakul | Docker, Seed Data, Backfill, Traffic Generator |

---

## System Architecture

<div align="center">

![2Handy Architecture Diagram](ArchitectureDiagram.jpg)

</div>

| Component | Role |
| :--- | :--- |
| **Web Frontend** | The "2Handy" user interface, talking to the backend over REST. |
| **Backend API** | Python + FastAPI. Validates requests and routes each one to the right database. |
| **PostgreSQL** | Transactional data: `users`, `orders`, `order_items`. |
| **MongoDB** | Flexible document data: `products`, `reviews`. |

> **Note:** `docker-compose.yml` starts the two databases. The FastAPI server itself is run on the host with `uvicorn` (see [Getting Started](#getting-started)).

### How an order touches both databases

```mermaid
sequenceDiagram
    participant C as Client
    participant A as FastAPI
    participant P as PostgreSQL
    participant M as MongoDB

    C->>A: POST /api/v1/orders
    A->>P: Check user exists
    A->>M: Read status + version of ALL products in one $in query
    A->>P: Insert order + order_items (not committed yet)
    A->>M: Mark each product "sold" only if its version is unchanged (OCC)
    alt everything succeeded
        A->>P: COMMIT
        A-->>C: 201 Order created
    else another buyer got there first
        A->>P: ROLLBACK
        A->>M: Restore previous product state
        A-->>C: 409 Conflict
    else any other step failed
        A->>P: ROLLBACK
        A->>M: Restore previous product state
        A-->>C: 4xx / 5xx error
    end
```

---

## Tech Stack

| Layer | Technology |
| :--- | :--- |
| Language | Python 3.9+ |
| Web framework | FastAPI + Uvicorn |
| Relational DB | PostgreSQL 15 (via SQLAlchemy 2.0 + `psycopg2-binary`) |
| Document DB | MongoDB 6.0 (via PyMongo) |
| Validation | Pydantic v2 |
| Seed data | Faker (`th_TH` + `en_US` locales) |
| Load testing | `requests` + threads (`traffic_gen.py`) |
| Infrastructure | Docker Compose with persistent volumes |

---

## Project Structure

```text
2Handy/
├── main.py                 # FastAPI app: all endpoints, migration-phase logic, 409/412 error mapping
├── database.py             # PostgreSQL engine/session and MongoDB client
├── models.py               # SQLAlchemy models: User, Order, OrderItem
├── schemas.py              # Pydantic schemas: Product, Review
├── pg_queries.py           # CP3: PostgreSQL row locks + single-query order reads
├── mongo_queries.py        # CP3: MongoDB OCC (version), projections, $lookup summaries
├── test_mongo_concurrency.py  # CP3: MongoDB concurrency / projection / N+1 checks
├── requirements-test.txt   # Test dependencies (pytest, httpx, ...)
├── seed.py                 # Resets both DBs and seeds 1,000 users / orders / products
├── traffic_gen.py          # Concurrent POST + GET load generator (~30 req/sec)
├── switch_phase.py         # Switches the live API between migration phases 1–5
├── backfill.py             # Phase 3 batched backfill with checkpointing
├── docker-compose.yml      # PostgreSQL 15 + MongoDB 6.0
├── ArchitectureDiagram.jpg
├── migrations/
│   ├── 001_expand_user_name.sql
│   ├── 002_dual_write.py
│   ├── 003_backfill.py
│   ├── 004_switch_read.py
│   ├── 005_contract_drop_legacy_user_name.sql
│   ├── 006_cp3_order_read_index.sql  # CP3: orders(user_id, id) index
│   ├── 006_mongo_add_version.py      # CP3: add version=0 to old products + Mongo indexes
│   ├── verify_user_name_migration.sql
│   ├── verify_user_name_post_contract.sql
│   └── README.md           # Per-file notes for the migration scripts
├── tests/
│   ├── test_pg_checkpoint3.py  # CP3: PostgreSQL locking + query-count tests
│   └── test_api_cp3.py         # CP3: HTTP 409/412 under 20 concurrent requests + N+1 checks
└── docs/
    ├── checkpoint3-postgresql.md  # CP3 PostgreSQL notes (Member 1)
    └── checkpoint3-rest-api.md    # CP3 REST API notes (Member 3)
```

---

## Getting Started

### Prerequisites

- [Docker](https://www.docker.com/products/docker-desktop/) with Docker Compose
- [Python 3.9+](https://www.python.org/downloads/)
- Git
- *(Optional)* `psql` — only needed to run the migration SQL from the host; a Docker alternative is shown below

### 1. Clone the repository

```bash
git clone https://github.com/pattanankm/2Handy.git
cd 2Handy
```

### 2. Start the databases

```bash
docker compose up -d
```

| Service | Image | Port | Database | Credentials |
| :--- | :--- | :-: | :--- | :--- |
| `postgres_db` | `postgres:15-alpine` | `5432` | `main_db` | `dev_user` / `dev_password` |
| `mongo_db` | `mongo:6.0` | `27017` | `main_db` | none |

Data is kept in the `pg_data` and `mongo_data` volumes, so it survives container restarts.

### 3. Create a virtual environment and install dependencies

**macOS / Linux**
```bash
python3 -m venv venv
source venv/bin/activate
pip install fastapi uvicorn sqlalchemy psycopg2-binary pymongo faker requests
```

**Windows (PowerShell)**
```powershell
python -m venv venv
venv\Scripts\Activate.ps1
pip install fastapi uvicorn sqlalchemy psycopg2-binary pymongo faker requests
```

### 4. Seed the databases

```bash
python seed.py
```

This **drops and recreates** the PostgreSQL tables, then inserts:

| Database | Data |
| :--- | :--- |
| PostgreSQL | 1,000 users, 1,000 orders (1–3 items each) |
| MongoDB | 1,000 products across 5 categories |

It also resets the migration state to **Phase 1** and clears the backfill checkpoint.

### 5. Run the API

```bash
uvicorn main:app --host 127.0.0.1 --port 8000
```

| URL | What you get |
| :--- | :--- |
| http://127.0.0.1:8000/docs | Interactive Swagger UI |
| http://127.0.0.1:8000/redoc | ReDoc documentation |

---

## API Reference

Base URL: `http://127.0.0.1:8000`

### Users — PostgreSQL

| Method | Endpoint | Description |
| :-: | :--- | :--- |
| `POST` | `/api/v1/users` | Create a user |
| `GET` | `/api/v1/users` | List users |
| `GET` | `/api/v1/users/{user_id}` | Get one user |
| `PUT` | `/api/v1/users/{user_id}` | Update a user |

The same routes are also available without the `/api/v1` prefix (`/users`, `/users/{user_id}`).

A user's name can be sent as `name`, `full_name`, or `first_name` (+ `last_name`). The response always has the same shape in every migration phase, so clients never notice the schema change:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/users \
  -H "Content-Type: application/json" \
  -d '{"first_name": "Somchai", "last_name": "Jaidee", "email": "somchai@example.com"}'
```

```json
{
  "id": 1001,
  "name": "Somchai Jaidee",
  "full_name": "Somchai Jaidee",
  "first_name": "Somchai",
  "last_name": "Jaidee",
  "email": "somchai@example.com",
  "created_at": "2026-01-01T00:00:00"
}
```

### Products & Reviews — MongoDB

| Method | Endpoint | Description |
| :-: | :--- | :--- |
| `POST` | `/api/v1/products` | Create a product (starts with `status: "available"`, `version: 1`) |
| `GET` | `/api/v1/products?category=&status=&skip=&limit=` | List products (projection, paginated, max 200) |
| `GET` | `/api/v1/products/summary` | Products with `review_count` / `avg_rating` in one aggregation |
| `GET` | `/api/v1/products/{product_id}` | Get one product; the `ETag` header holds its version |
| `PATCH` | `/api/v1/products/{product_id}` | Update a product with optimistic concurrency (see below) |
| `GET` | `/api/v1/products/{product_id}/reviews` | List a product's reviews (newest first, paginated) |
| `POST` | `/api/v1/reviews` | Create a review (`created_at` is filled in if missing) |

`attributes` is a free-form object, so every product category can carry its own fields:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/products \
  -H "Content-Type: application/json" \
  -d '{"name": "iPhone 12", "category": "Electronics", "attributes": {"condition": "Like New", "price": 8900, "brand": "Apple"}}'
```

```bash
curl -X POST http://127.0.0.1:8000/api/v1/reviews \
  -H "Content-Type: application/json" \
  -d '{"product_id": "<product _id>", "user_id": 1, "rating": 5, "comment": "Great condition!"}'
```

`PATCH` needs the version the client last read. Send it in **either** place:

```bash
# 1) In the If-Match header (value from the ETag of GET /api/v1/products/{id}) -> stale = 412
curl -X PATCH http://127.0.0.1:8000/api/v1/products/<product _id> \
  -H 'Content-Type: application/json' -H 'If-Match: "1"' \
  -d '{"attributes": {"price": 8500, "condition": "Like New"}}'

# 2) In the body -> stale = 409
curl -X PATCH http://127.0.0.1:8000/api/v1/products/<product _id> \
  -H "Content-Type: application/json" \
  -d '{"version": 1, "name": "iPhone 12 (128GB)"}'
```

On success the response has the new `version` and `ETag`. If someone else changed the product first, reload it and retry.

### Orders — PostgreSQL + MongoDB

| Method | Endpoint | Description |
| :-: | :--- | :--- |
| `POST` | `/api/v1/orders` | Create an order and mark its products as sold |
| `GET` | `/api/v1/orders?user_id=&after_id=&limit=` | List orders with their items in one SQL query (keyset pagination, max 100) |
| `GET` | `/api/v1/orders/{order_id}` | Get one order with its items |
| `POST` | `/api/v1/orders/{order_id}/complete` | Change a `pending` order to `completed` (only once) |

```bash
curl -X POST http://127.0.0.1:8000/api/v1/orders \
  -H "Content-Type: application/json" \
  -d '{"user_id": 1, "items": [{"product_id": "<product _id>", "quantity": 1}]}'
```

If any step fails, the PostgreSQL transaction is rolled back and the MongoDB products are restored to their previous state.

To get the next page of orders, pass the last `id` you received as `after_id`:

```bash
curl "http://127.0.0.1:8000/api/v1/orders?user_id=1&limit=50"
curl "http://127.0.0.1:8000/api/v1/orders?user_id=1&limit=50&after_id=<last id>"
```

### Migration Phase Control

| Method | Endpoint | Description |
| :-: | :--- | :--- |
| `GET` | `/api/v1/migration/phase` | Show the active migration phase |
| `POST` | `/api/v1/migration/phase/{phase}` | Switch to phase `1`–`5` |

### Status codes

| Code | Meaning |
| :-: | :--- |
| `200` | OK |
| `201` | Resource created |
| `400` | Malformed `If-Match` header |
| `404` | User, product or order not found |
| `409` | Email already exists; product already sold or changed by another request; order is no longer `pending`; or a PostgreSQL deadlock / lock timeout / serialization failure or MongoDB write conflict |
| `412` | `If-Match` version is out of date (the response `ETag` holds the current version) |
| `422` | Invalid input (empty name, conflicting name fields, bad product id, quantity < 1, duplicate product in one order, `If-Match` and body `version` disagree) |
| `428` | `PATCH` sent without a version (no `If-Match` and no body `version`) |
| `500` | Other database errors (bad SQL, lost connection) — these are never reported as `409` |

---

## Data Model

### PostgreSQL (transactional)

```mermaid
erDiagram
    USERS ||--o{ ORDERS : places
    ORDERS ||--|{ ORDER_ITEMS : contains

    USERS {
        int id PK
        string name "legacy, dropped in Phase 5"
        string first_name "added in Phase 1"
        string last_name "added in Phase 1"
        string email UK
        datetime created_at
    }
    ORDERS {
        int id PK
        int user_id FK
        int total_amount
        string status
        datetime created_at
    }
    ORDER_ITEMS {
        int id PK
        int order_id FK
        string product_id "MongoDB ObjectId"
        int quantity
        int unit_price
        datetime created_at
    }
```

### MongoDB (flexible)

**`products`**
```json
{
  "_id": "ObjectId",
  "name": "Ergonomic leather chair",
  "category": "Furniture",
  "status": "available",
  "attributes": { "condition": "Good", "price": 1250.0 }
}
```
When a product is ordered, `status` becomes `"sold"` and an `order_id` pointing at the PostgreSQL order is added.

**`reviews`**
```json
{
  "_id": "ObjectId",
  "product_id": "<product _id>",
  "user_id": 1,
  "rating": 5,
  "comment": "Great condition!",
  "created_at": null
}
```

---

## Checkpoint 2: Zero-Downtime Schema Migration (Expand & Contract)

### Migration Scenario

**Splitting a column** — refactor the legacy `public.users.name` into `public.users.first_name` and `public.users.last_name` with no downtime, under continuous concurrent `POST` + `GET` load (≥ 20 req/sec).

### The five phases

```mermaid
flowchart LR
    P1["1 · EXPAND<br/>add nullable columns"] --> P2["2 · DUAL WRITE<br/>write old + new"]
    P2 --> P3["3 · BACKFILL<br/>migrate old rows"]
    P3 --> P4["4 · SWITCH READ<br/>read new only"]
    P4 --> P5["5 · CONTRACT<br/>drop legacy column"]
```

| Phase | API writes to | API reads from | What changes |
| :--- | :--- | :--- | :--- |
| **1 · Expand** | `name` | `name` | Nullable `first_name` / `last_name` columns are added |
| **2 · Dual Write** | `name` **and** `first_name` + `last_name` | `name` | New rows are populated in both shapes |
| **3 · Backfill** | both | `name` | Old rows are copied in batches of 100 |
| **4 · Switch Read** | `first_name` + `last_name` | `first_name` + `last_name` | Legacy column is no longer used |
| **5 · Contract** | `first_name` + `last_name` | `first_name` + `last_name` | Legacy `name` column is dropped |

The API re-reads the active phase on every request, so switching phases needs **no server restart**.

### Success criteria

`traffic_gen.py` prints live metrics and must hold these throughout all five phases:

| Metric | Target |
| :--- | :--- |
| Request rate | > 20 req/sec (3 threads, 75% GET / 25% POST) |
| HTTP 5xx errors | 0 |
| Corrupted or missing fields | 0 |
| p99 latency | < 500 ms |

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

<details>
<summary>No <code>psql</code> installed? Run it through Docker instead</summary>

```bash
docker exec -i $(docker compose ps -q postgres_db) psql -U dev_user -d main_db -v ON_ERROR_STOP=1 < migrations/001_expand_user_name.sql
```

The same pattern works for every `.sql` file below.

</details>

---

#### Phase 2: DUAL WRITE (Write to both legacy `name` and new `first_name`/`last_name`)

Switch the running Web API to Dual Write mode (0 ms downtime, no server restart required):
```bash
python switch_phase.py 2
```
Run the pre-backfill verification query (shows legacy rows waiting for backfill):
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
The job saves its progress to `backfill_checkpoint.json` and resumes from there if interrupted. Use `python backfill.py --reset` to start again from the first row.

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

> More detail on each migration file is in [migrations/README.md](migrations/README.md).

---

## Checkpoint 3: Concurrency Control & Performance Audit

Goal: two requests that change the same data at the same time must not corrupt it, and read endpoints must not slow down as the data grows.

### What was added

| Member | Area | Added |
| :-: | :--- | :--- |
| 1 | PostgreSQL | [`pg_queries.py`](pg_queries.py): `SELECT … FOR UPDATE` row lock for `PUT /users/{id}` and for completing an order (`pending → completed` only once); orders + items read in **one** SQL query (`JOIN` + `jsonb_agg`); index `orders(user_id, id)`. Details: [docs/checkpoint3-postgresql.md](docs/checkpoint3-postgresql.md) |
| 2 | MongoDB | [`mongo_queries.py`](mongo_queries.py): each product has a `version`; updates and sales only succeed if the version is unchanged, then increment it (optimistic concurrency). Projections return only the fields the API needs; product summaries get review stats from one `$lookup` aggregation. |
| 3 | REST API | [`main.py`](main.py): maps concurrency errors to **409** / **412**; new read-only endpoints for orders and products; `PATCH /products/{id}` with `If-Match` / `ETag`; `POST /orders` reads all products in one query. Details: [docs/checkpoint3-rest-api.md](docs/checkpoint3-rest-api.md) |
| 4 | QA & Load Test | [`test_concurrency.py`](test_concurrency.py): 20 concurrent users verification (1 success, 19 conflicts); [`locustfile.py`](locustfile.py): 50 VUs automated load testing over 3 minutes; [`Performance_Audit_Report.pdf`](Performance_Audit_Report.pdf): comprehensive technical report. Details: [docs/checkpoint3-qa-loadtest.md](docs/checkpoint3-qa-loadtest.md) |

### Concurrency errors → HTTP status

All conversions happen in one place (`@app.exception_handler` in `main.py`):

| Situation | HTTP |
| :--- | :-: |
| `PATCH` a product with an old body `version` | `409` |
| `PATCH` a product with an old `If-Match` header | `412` |
| Two buyers race for the same product | winner `201`, others `409`; the losers' orders are rolled back |
| Completing an order that is no longer `pending` | `409` |
| PostgreSQL deadlock (`40P01`), lock timeout (`55P03`), serialization failure (`40001`) | `409` |
| MongoDB `WriteConflict` | `409` |
| Any other database error | `500` |

`PUT /users/{id}` uses a row lock, so two edits of the same user wait in line and both succeed instead of returning `409`. It returns `409` only if waiting for the lock times out.

### No N+1 queries

| Endpoint | Database round trips |
| :--- | :--- |
| `GET /api/v1/orders` | 1 SQL query, whether the page holds 1, 10 or 50 orders |
| `GET /api/v1/orders/{id}` | 1 SQL query |
| `GET /api/v1/products`, `/products/{id}`, `/products/{id}/reviews` | 1 MongoDB `find` |
| `GET /api/v1/products/summary` | 1 MongoDB `aggregate` |
| `POST /api/v1/orders` | 1 MongoDB `find` for all products (was one `find_one` per item), then one conditional write per product |

### Setup for an existing database

Run once on databases created before Checkpoint 3:

```powershell
# Add version=0 to old products and create the MongoDB indexes
python migrations/006_mongo_add_version.py
# Add the orders(user_id, id) index (do not use --single-transaction)
Get-Content migrations/006_cp3_order_read_index.sql | docker compose exec -T postgres_db psql -U dev_user -d main_db -v ON_ERROR_STOP=1
```

A fresh database gets both automatically: `models.py` creates the PostgreSQL index and the API creates the MongoDB indexes when it starts.

### Tests

The tests use throwaway containers, so they never touch `main_db`:

```powershell
pip install -r requirements-test.txt
docker run --detach --rm --name cp3-pg --publish 127.0.0.1::5432 --env POSTGRES_USER=cp3_test --env POSTGRES_PASSWORD=cp3_test --env POSTGRES_DB=postgres postgres:15-alpine
docker run --detach --rm --name cp3-mongo --publish 127.0.0.1::27017 mongo:6.0
docker port cp3-pg 5432; docker port cp3-mongo 27017
# Replace PG_PORT / MONGO_PORT with the ports printed above
$env:PG_TEST_ADMIN_URL = 'postgresql://cp3_test:cp3_test@127.0.0.1:PG_PORT/postgres'
$env:MONGO_TEST_URL = 'mongodb://127.0.0.1:MONGO_PORT/'
python -m pytest tests/ -q -p no:cacheprovider
docker stop cp3-pg cp3-mongo
```

### Automated Load Testing & Concurrency Verification (Member 4)

#### 1. Concurrency Verification Script (`test_concurrency.py`)
Simulates 20 concurrent users competing for shared resources simultaneously:
```bash
python test_concurrency.py
# or: pytest test_concurrency.py -v
```
* **Test 1 (MongoDB OCC):** 20 users buy the last remaining stock item simultaneously via `POST /api/v1/orders`.
  * **Result:** Exactly 1 succeeded (HTTP 201), 19 received HTTP 409 Conflict. Exactly 1 order_item persisted in PostgreSQL.
* **Test 2 (PostgreSQL Row Lock):** 20 users attempt to complete the same order simultaneously via `POST /api/v1/orders/{id}/complete`.
  * **Result:** Exactly 1 succeeded (HTTP 200), 19 received HTTP 409 Conflict. Order state transitioned to `completed`.

#### 2. Automated Load Testing Benchmark (`locustfile.py`)
Executed an automated load test under **50 Virtual Users (VUs)** continuous over **3 minutes**:
```bash
locust -f locustfile.py --headless -u 50 -r 10 --run-time 3m --host http://127.0.0.1:8000 \
       --html reports/Performance_Audit_Report.html --csv reports/locust_cp3
```

| Metric | Target Specification | Actual Result | Status |
|---|---|---|:---:|
| **Virtual Users (VUs)** | Minimum 50 VUs | **50 VUs** | **PASS** |
| **Duration** | 3 Minutes | **180 Seconds** | **PASS** |
| **Throughput (RPS)** | Stable Requests Per Second | **186.8 req/s** (33,587 total requests) | **PASS** |
| **p95 Latency** | **&lt; 200 ms** | **37 ms** | **PASS** (81.5% faster) |
| **p99 Latency** | **&lt; 500 ms** | **100 ms** | **PASS** (80.0% faster) |
| **HTTP Error Rate** | **&lt; 1.00%** | **0.00%** (0 errors) | **PASS** |

Full audit details, SVG charts, and per-endpoint breakdowns are available in [`Performance_Audit_Report.pdf`](Performance_Audit_Report.pdf) and [`docs/checkpoint3-qa-loadtest.md`](docs/checkpoint3-qa-loadtest.md).

---

## Troubleshooting

| Problem | Fix |
| :--- | :--- |
| `connection refused` on port `5432` or `27017` | Check the containers are up with `docker compose ps`, then `docker compose up -d`. |
| Port `5432` / `27017` already in use | Stop the local PostgreSQL / MongoDB service, or change the port mapping in `docker-compose.yml`. |
| `ModuleNotFoundError` | Activate the virtual environment and re-run the `pip install` command from [Getting Started](#getting-started). |
| Want to rerun the migration demo from the start | Run `python seed.py` — it resets the tables, the phase (back to 1), and the backfill checkpoint. |
| Want a completely clean database | `docker compose down -v` removes the volumes, then start again from step 2. |
| Check which phase the API is in | `python switch_phase.py` (no argument) or `GET /api/v1/migration/phase`. |
