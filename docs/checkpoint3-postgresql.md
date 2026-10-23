# Checkpoint 3 — คนที่ 1: PostgreSQL

## ขอบเขตและสิ่งที่พบ

PostgreSQL เก็บ `users`, `orders`, `order_items`; สินค้าและสถานะสินค้าอยู่ MongoDB
งานนี้จึงป้องกันการแก้ไขผู้ใช้และการเปลี่ยนสถานะ Order ใน PostgreSQL
ไม่ได้เพิ่ม stock ซ้ำอีกชุดใน SQL หรือเปลี่ยน protocol การจองสินค้าของ MongoDB

| จุดตรวจ | ก่อนแก้ | ผลที่ส่งมอบ |
| --- | --- | --- |
| `PUT /api/v1/users/{id}` | อ่านข้อมูลแล้วเขียนชื่อ/email กลับทั้งชุด อาจทับการแก้ไขพร้อมกัน | ล็อกแถวก่อนอ่าน เก็บ lock จน commit/rollback และอ่าน response ก่อนปล่อย lock |
| CP2 backfill | อ่านชื่อเก่าแล้วอัปเดตภายหลัง อาจทับชื่อใหม่ | ล็อกแถวใน batch ตั้งแต่ SELECT จน commit |
| การ complete Order | ยังไม่มี operation นี้ | `complete_order()` ยอมให้เปลี่ยน `pending → completed` ครั้งเดียว |
| การอ่าน Orders พร้อม items | ยังไม่มี read endpoint/query จึงไม่มี baseline N+1 ของ endpoint นี้ | `list_orders()` / `get_order()` ใช้ JOIN + JSON aggregation ใน SQL เดียว ป้องกัน lazy-load N+1 ตั้งแต่ต้น |
| Projection | Users ใช้ explicit columns อยู่แล้ว | คง projection ที่รองรับ CP2 และเพิ่ม explicit columns/JSON fields ให้ Orders |

`create_order()` ยังมี MongoDB `find_one` ใน loop เป็นงานที่ต้องส่งต่อคนที่ 2/3
ไม่ควรนำผลการทดสอบ SQL ไปสรุปว่า endpoint นี้ไม่มี N+1 ทั้งระบบ

## Concurrency: Pessimistic row locking

ใช้ `SELECT … FOR UPDATE` ภายใน transaction เดียวกับการอ่านและเขียน
ผู้เขียนที่ชนแถวเดียวกันต้องรอจน transaction แรกจบ แล้วอ่านสถานะล่าสุด
ตามพฤติกรรม [PostgreSQL row-level locks](https://www.postgresql.org/docs/15/explicit-locking.html#LOCKING-ROWS)
ที่ isolation `READ COMMITTED` ซึ่งเป็นค่าปกติของโปรเจกต์

```sql
BEGIN;
SELECT id, status FROM public.orders WHERE id = :id FOR UPDATE;
-- ถ้าไม่พบ: RecordNotFound; ถ้า status != 'pending': ConcurrentUpdate
UPDATE public.orders SET status = 'completed' WHERE id = :id
RETURNING id, user_id, total_amount, status, created_at;
COMMIT;
```

- `complete_order(db, order_id)` ไม่ commit เอง ผู้เรียกต้อง commit เมื่อสำเร็จและ rollback ทุกกรณีที่ผิดพลาด
- ต้องใช้ Session แยกต่อ request/thread และอย่า commit ระหว่าง lock กับ update
- `ConcurrentUpdate` เป็น domain error เพื่อให้คนที่ 3 map เป็น HTTP 409
- `RecordNotFound` ให้ map เป็น HTTP 404
- การแก้ผู้ใช้คนละฟิลด์พร้อมกันจะเรียงกันทำงานและสำเร็จทั้งคู่ โดยไม่ทำฟิลด์อีกคนหาย ไม่ได้บังคับให้เกิด 409
- การส่งค่าเก่ามาแก้ฟิลด์เดียวกันจากหน้าเว็บยังเป็น last-writer-wins; ถ้าต้องการปฏิเสธ stale client ต้องเพิ่ม version/If-Match เป็นอีก requirement
- `complete_order()` เปลี่ยนสถานะ Order เท่านั้น ไม่ใช่การชำระเงินจริงหรือจองสินค้าชิ้นสุดท้าย

ไม่ต้องเพิ่มคอลัมน์ version สำหรับวิธี pessimistic นี้ และการล็อก Users ใช้แค่ `id`
จึงทำงานได้ทั้งก่อนเพิ่ม `first_name/last_name` และหลังลบ `name` ใน CP2

## N+1, projection และ index

```python
from pg_queries import get_order, list_orders

orders = list_orders(db, user_id=42, after_id=0, limit=50)
order = get_order(db, order_id=123)
```

ทั้งสองฟังก์ชันส่งกลับ dictionary พร้อม `items` โดยไม่ต้องเรียก ORM relationship ใน loop
ใช้ `LEFT JOIN` เพื่อคง Order ที่ไม่มี items (`items: []`) และ
[`jsonb_agg`/`COALESCE`](https://www.postgresql.org/docs/15/functions-aggregate.html)
เพื่อรวมรายการสินค้า มีลำดับตาม item id ที่แน่นอน

Projection ที่ส่งกลับ:

- Order: `id`, `user_id`, `total_amount`, `status`, `created_at`, `items`
- Item: `id`, `product_id`, `quantity`, `unit_price`
- ไม่โหลดข้อมูลผู้ใช้ เอกสารสินค้า หรือ item timestamps ที่ response นี้ไม่ใช้

จำนวน SQL round trips เป็น **O(1)** ต่อหน้า ไม่ใช่เวลา/หน่วยความจำ O(1)
การประมวลผลยังเพิ่มตามจำนวน Orders และ items ที่ส่งกลับ
แบ่งหน้าที่ parent Orders ก่อน JOIN เพื่อไม่ตัด items กลาง Order
ใช้ `after_id` เท่ากับ id สุดท้ายของหน้าก่อน และจำกัด `limit` เป็น 1–100 (ค่าเริ่มต้น 50)

ฐานข้อมูลใหม่สร้าง index ผ่าน `models.py` อัตโนมัติ ส่วนฐานข้อมูลเดิมให้รัน:

```powershell
# ใช้ database URL ของเครื่อง demo; ต้องมีตาราง orders แล้ว
psql $env:DATABASE_URL -v ON_ERROR_STOP=1 -f migrations/006_cp3_order_read_index.sql
```

หรือใช้ Docker Compose จาก PowerShell:

```powershell
Get-Content migrations/006_cp3_order_read_index.sql | docker compose exec -T postgres_db psql -U dev_user -d main_db -v ON_ERROR_STOP=1
```

ห้ามใช้ `--single-transaction` เพราะ migration ใช้ `CREATE INDEX CONCURRENTLY`
index `(user_id, id)` รองรับการกรองผู้ใช้พร้อม cursor; index `order_items(order_id)` มีอยู่ใน model เดิมแล้ว
การมี index ไม่ใช่หลักฐานว่า latency ผ่านเกณฑ์ ต้องวัดด้วยข้อมูลและโหลดจริง

## ส่งต่อคนที่ 3: ตัวอย่าง wiring API

ฟังก์ชัน Orders อยู่ใน `pg_queries.py` และยังไม่ได้เพิ่ม route ใน `main.py`
ตัวอย่างสำหรับรวมเข้ากับ endpoint ของทีม:

```python
from fastapi import Depends, HTTPException, Query
from sqlalchemy.orm import Session
from database import get_pg_db
from pg_queries import ConcurrentUpdate, RecordNotFound, complete_order, list_orders

@app.get("/api/v1/orders")
def read_orders(
    user_id: int = Query(..., gt=0),
    after_id: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_pg_db),
):
    return list_orders(db, user_id=user_id, after_id=after_id, limit=limit)

@app.post("/api/v1/orders/{order_id}/complete")
def finish_order(order_id: int, db: Session = Depends(get_pg_db)):
    try:
        result = complete_order(db, order_id)
        db.commit()
        return result
    except RecordNotFound as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConcurrentUpdate as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        db.rollback()
        raise
```

ไม่ควร map database exceptions ทุกชนิดเป็น 409 เพราะ connection error และ SQL ผิดไม่ใช่ concurrent conflict
หากจะนำ Order completion ไปใช้เป็น concurrency scenario ให้ทีมตกลง endpoint นี้ก่อน
ถ้า rubric กำหนดแย่งซื้อสินค้าชิ้นสุดท้าย ต้องทดสอบ `POST /api/v1/orders` กับ MongoDB OCC ของคนที่ 2 เพิ่ม
การ rollback PostgreSQL ไม่สามารถ rollback MongoDB อัตโนมัติได้

## รันทดสอบ PostgreSQL แยกจากข้อมูลเดิม

ต้องมี Python 3.9+ และ Docker ที่ทำงานอยู่ ใช้ container ชั่วคราวไม่มี volume:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-test.txt
docker run --detach --rm --name 2handy-cp3-postgres-test --publish 127.0.0.1::5432 --env POSTGRES_USER=cp3_test --env POSTGRES_PASSWORD=cp3_test --env POSTGRES_DB=postgres postgres:15-alpine
docker port 2handy-cp3-postgres-test 5432
docker exec 2handy-cp3-postgres-test pg_isready -U cp3_test
# แทน PORT ด้วย port ที่ docker port แสดง และรอ pg_isready พร้อมก่อนรัน
$env:PG_TEST_ADMIN_URL = 'postgresql://cp3_test:cp3_test@127.0.0.1:PORT/postgres'
.\.venv\Scripts\python.exe -m pytest tests/test_pg_checkpoint3.py -q -p no:cacheprovider
docker stop 2handy-cp3-postgres-test
```

Tests สร้าง database ชื่อสุ่ม `cp3_test_<uuid>` และลบทิ้งเฉพาะ database นั้นเมื่อจบ
ต้องใช้ account ที่มีสิทธิ์ CREATEDB; ไม่ใช้ `seed.py` และไม่ล้างฐานข้อมูลของแอป
ถ้าไม่ตั้ง `PG_TEST_ADMIN_URL` tests จะ skip ซึ่ง **ไม่นับว่าผ่านการทดสอบ PostgreSQL**

## หลักฐานและงานที่ต้องวัดต่อ

ผลทดสอบจริงวันที่ 23 ตุลาคม 2026 บน Windows / Python 3.13.14,
SQLAlchemy 2.0.55 และ PostgreSQL 15.19 ใน Docker:

```text
14 passed, 2 warnings in 1.68s
```

Warnings ทั้งสองมาจาก `datetime.utcnow()` ใน CP2 backfill เดิม
เวลารัน pytest นี้ไม่ใช่ response latency ของ API

- 20 independent connections complete Order เดียวกัน: ได้ 1 success / 19 `ConcurrentUpdate`
- อ่าน 1, 10, 100 Orders: ได้ 1 SQL statement ต่อหน้าทุกขนาด
- ตรวจ rollback, missing records, pagination, empty items, projection และ index migration ซ้ำ
- ตรวจการแก้ผู้ใช้พร้อมกันใน CP2 ทั้ง 5 phases และ schema ที่ลบคอลัมน์จริง
- ตรวจว่า backfill ถือ row lock จนจบ batch

ส่วนของคนที่ 4 ที่ยังต้องทำหลังรวม API: 20 HTTP requests ได้ 1 success / 19 HTTP 409,
load test อย่างน้อย 50 VUs เป็นเวลา 3 นาที และรายงาน p95 < 200 ms, p99 < 500 ms,
error rate < 1% โดยแยก expected 409 จากการทดสอบแย่งรายการเดียวกันออกจาก load test ปกติ
ยังไม่มีผล latency/error-rate ของระบบรวมจากงานส่วนนี้
