# Checkpoint 3 — คนที่ 3: REST API (HTTP Status + Read-only endpoints)

รวม Query ของคนที่ 1 (`pg_queries.py`, PostgreSQL pessimistic lock) และคนที่ 2
(`mongo_queries.py` จาก branch `prem`, MongoDB optimistic concurrency) เข้ากับ `main.py`

## Error ที่เกิดจากการชนกันของข้อมูล → HTTP Status

แปลงที่จุดเดียวด้วย `@app.exception_handler` ใน `main.py`

| สาเหตุ | Exception | HTTP |
| --- | --- | --- |
| PATCH product ด้วย `version` ใน body ที่เก่าแล้ว | `VersionConflictError` | **409 Conflict** (มี `expected_version`, `current_version`) |
| PATCH product ด้วย header `If-Match` ที่เก่าแล้ว | `VersionConflictError` | **412 Precondition Failed** + `ETag` ของเวอร์ชันปัจจุบัน |
| แย่งซื้อสินค้าชิ้นสุดท้าย (`POST /api/v1/orders`) | `VersionConflictError` / `ProductUnavailableError` | **409** และ rollback PostgreSQL |
| complete order ที่ไม่ใช่ `pending` แล้ว | `ConcurrentUpdate` | **409** |
| PostgreSQL `40001` serialization, `40P01` deadlock, `55P03` lock timeout | `DBAPIError` | **409** |
| MongoDB `WriteConflict` (code 112) / `TransientTransactionError` | `OperationFailure` | **409** |
| PATCH ไม่ส่ง version ทั้ง body และ If-Match | — | 428 Precondition Required |
| ไม่พบข้อมูล | `RecordNotFound` / `ProductNotFoundError` | 404 |
| Database error อื่น ๆ (SQL ผิด, connection หลุด) | `DBAPIError` / `OperationFailure` | 500 — **ไม่** นับเป็น 409 |

ขั้นตอนฝั่ง client สำหรับ 412: `GET /api/v1/products/{id}` → อ่าน header `ETag` (เช่น `"3"`)
→ `PATCH` พร้อม `If-Match: "3"` → ถ้าได้ 412 ให้โหลดใหม่ด้วย ETag ที่ส่งกลับมาแล้วลองอีกครั้ง

## Read-only endpoints (ไม่มี N+1)

| Endpoint | Query | จำนวน round trip |
| --- | --- | --- |
| `GET /api/v1/orders?user_id=&after_id=&limit=` | `list_orders` — JOIN + `jsonb_agg` | 1 SQL ทุกขนาดหน้า |
| `GET /api/v1/orders/{id}` | `get_order` | 1 SQL |
| `GET /api/v1/products?category=&status=&skip=&limit=` | `list_products` (projection) | 1 `find` |
| `GET /api/v1/products/summary` | `list_product_summaries` — `$lookup` reviews | 1 `aggregate` |
| `GET /api/v1/products/{id}` | `get_product` (+ `ETag`) | 1 `find` |
| `GET /api/v1/products/{id}/reviews` | `list_reviews` (projection) | 1 `find` |

Endpoint เขียนที่เกี่ยวข้อง:

- `PATCH /api/v1/products/{id}` — OCC update
- `POST /api/v1/orders/{id}/complete` — `complete_order` (row lock)
- `POST /api/v1/orders` — เดิมเรียก `find_one` ทีละสินค้า ตอนนี้อ่านสถานะสินค้าทั้งหมดด้วย `$in`
  ครั้งเดียว (`get_product_states` ที่เพิ่มใน `mongo_queries.py`) แล้วขายด้วย `mark_product_sold`
  ทีละชิ้น (OCC write ต้องแยกตามเอกสาร)
- `POST /api/v1/products` ตั้ง `status: "available"`, `version: 1`
- `POST /api/v1/reviews` เติม `created_at` ถ้าไม่ส่งมา (ใช้เรียง review)

ตอน start แอปจะเรียก `mongo_queries.ensure_indexes()` (idempotent) ส่วนสินค้าเก่าที่ยังไม่มี `version`
ให้รัน `python migrations/006_mongo_add_version.py` หนึ่งครั้ง

## รันทดสอบ

ใช้ container ชั่วคราวไม่มี volume (ไม่แตะ `main_db`):

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-test.txt
docker run --detach --rm --name cp3-pg --publish 127.0.0.1::5432 --env POSTGRES_USER=cp3_test --env POSTGRES_PASSWORD=cp3_test --env POSTGRES_DB=postgres postgres:15-alpine
docker run --detach --rm --name cp3-mongo --publish 127.0.0.1::27017 mongo:6.0
docker port cp3-pg 5432; docker port cp3-mongo 27017
# แทน PG_PORT / MONGO_PORT ด้วย port ที่ docker port แสดง
$env:PG_TEST_ADMIN_URL = 'postgresql://cp3_test:cp3_test@127.0.0.1:PG_PORT/postgres'
$env:MONGO_TEST_URL = 'mongodb://127.0.0.1:MONGO_PORT/'
.\.venv\Scripts\python.exe -m pytest tests/ -q -p no:cacheprovider
docker stop cp3-pg cp3-mongo
```

ผลวันที่ 23 ตุลาคม 2026 (Windows, Python 3.12, PostgreSQL 15, MongoDB 6.0): `27 passed`
(`tests/test_api_cp3.py` 13 + `tests/test_pg_checkpoint3.py` 14) และ `test_mongo_concurrency.py` ผ่านทั้ง 6

`tests/test_api_cp3.py` ยิงผ่าน HTTP จริง (FastAPI TestClient) โดย 20 threads พร้อมกัน:

- PATCH product เวอร์ชันเดียวกัน → 1 × 200 / 19 × 409
- ซื้อสินค้าชิ้นสุดท้าย → 1 × 201 / 19 × 409 และมี order_item เพียง 1 แถวใน PostgreSQL
- complete order เดียวกัน → 1 × 200 / 19 × 409
- If-Match เก่า → 412 พร้อม ETag ใหม่; lock timeout ของ PostgreSQL → 409 และ retry สำเร็จ
- นับ SQL / MongoDB commands: orders 1, 10, 50 รายการ = 1 SELECT; products / summary / reviews = 1 คำสั่ง;
  `POST /orders` 5 สินค้า = 1 `find` + 5 `findAndModify`

ถ้าไม่ตั้ง env ทั้งสองตัว tests จะ skip ซึ่ง **ไม่นับว่าผ่าน**
Load test (p95/p99/error rate) ยังเป็นงานของคนที่ 4
