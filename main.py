# main.py
import os
import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from bson import ObjectId
from fastapi import FastAPI, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

import models
import schemas  # from polly's branch
from database import engine, get_pg_db, get_mongo_db

# สั่งให้สร้างตารางใน PostgreSQL (ถ้ายังไม่มี)
models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="2Handy API")

PHASE_FILE = Path(__file__).resolve().parent / ".migration_phase"


def get_current_phase() -> int:
    """
    Determine the active Expand-Contract migration phase (1 to 5):
      1 = Phase 1 (Expand / Legacy Read & Write on `name`)
      2 = Phase 2 (Dual Write to `name` + `first_name`/`last_name`, Legacy Read)
      3 = Phase 3 (Backfill running under Dual Write)
      4 = Phase 4 (Switch Read to `first_name` + `last_name` exclusively)
      5 = Phase 5 (Contract - legacy `name` column dropped)
    """
    env_phase = os.getenv("MIGRATION_PHASE")
    if env_phase and env_phase.isdigit():
        return int(env_phase)
    if PHASE_FILE.exists():
        try:
            content = PHASE_FILE.read_text(encoding="utf-8").strip()
            if content.isdigit():
                return int(content)
        except OSError:
            pass
    return 2


def set_current_phase(phase: int) -> int:
    if phase not in (1, 2, 3, 4, 5):
        raise ValueError("Phase must be between 1 and 5")
    PHASE_FILE.write_text(str(phase), encoding="utf-8")
    return phase


# --- Pydantic Schemas สำหรับรับข้อมูลจาก Request ---
class UserCreate(BaseModel):
    name: Optional[str] = None
    full_name: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: str


class UserUpdate(BaseModel):
    name: Optional[str] = None
    full_name: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None


def _split_full_name(full_name: str) -> Tuple[str, str]:
    """Split at the first whitespace; names with more parts keep them in last_name."""
    normalized = " ".join(full_name.split())
    if not normalized:
        raise HTTPException(status_code=422, detail="Name must not be empty")
    first_name, separator, last_name = normalized.partition(" ")
    return first_name, last_name if separator else ""


def _resolve_name_parts(
    name: Optional[str],
    full_name: Optional[str],
    first_name: Optional[str],
    last_name: Optional[str],
) -> Tuple[str, str, str]:
    legacy_input = full_name if full_name is not None else name
    if full_name is not None and name is not None:
        if " ".join(full_name.split()) != " ".join(name.split()):
            raise HTTPException(status_code=422, detail="name and full_name must match")

    if legacy_input is not None:
        split_first, split_last = _split_full_name(legacy_input)
        normalized_legacy = " ".join(part for part in (split_first, split_last) if part)
        if first_name is not None and " ".join(first_name.split()) != split_first:
            raise HTTPException(status_code=422, detail="first_name conflicts with full_name")
        if last_name is not None and " ".join(last_name.split()) != split_last:
            raise HTTPException(status_code=422, detail="last_name conflicts with full_name")
        return normalized_legacy, split_first, split_last

    if first_name is None:
        raise HTTPException(status_code=422, detail="Provide name, full_name, or first_name")
    normalized_first = " ".join(first_name.split())
    normalized_last = " ".join((last_name or "").split())
    if not normalized_first:
        raise HTTPException(status_code=422, detail="first_name must not be empty")
    combined = " ".join(part for part in (normalized_first, normalized_last) if part)
    return combined, normalized_first, normalized_last


def _format_user_response(
    user_id: int,
    email: str,
    created_at: Any,
    phase: int,
    legacy_name: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build response payload according to the current migration phase:
      - Phase 1, 2, 3: Legacy reader (reads `name` first, falls back to `first_name` + `last_name`)
      - Phase 4, 5: Switched reader (reads exclusively from `first_name` and `last_name`)
    """
    if phase >= 4:
        # Phase 4 (Switch Read) & Phase 5 (Contract): Read EXCLUSIVELY from new columns
        fn = (first_name or "").strip()
        ln = (last_name or "").strip()
        reconstructed = " ".join(part for part in (fn, ln) if part)
        return {
            "id": user_id,
            "name": reconstructed,
            "full_name": reconstructed,
            "first_name": fn,
            "last_name": ln,
            "email": email,
            "created_at": created_at,
        }
    else:
        # Phase 1, 2, 3: Legacy reader consumes `name`, populates split fields for compatibility
        fn = (first_name or "").strip()
        ln = (last_name or "").strip()
        raw_legacy = (legacy_name or "").strip()
        if not fn and not ln and raw_legacy:
            fn, ln = _split_full_name(raw_legacy)
        full = raw_legacy or " ".join(part for part in (fn, ln) if part)
        return {
            "id": user_id,
            "name": full,
            "full_name": full,
            "first_name": fn,
            "last_name": ln,
            "email": email,
            "created_at": created_at,
        }


class ProductCreate(BaseModel):
    name: str
    category: str
    attributes: Dict[str, Any]  # รับค่า Dynamic attributes แบบยืดหยุ่นสำหรับ MongoDB


class OrderItemCreate(BaseModel):
    product_id: str
    quantity: int = 1


class OrderCreate(BaseModel):
    user_id: int
    items: List[OrderItemCreate]


# from polly's branch
class ReviewCreate(BaseModel):
    product_id: str
    user_id: int
    rating: int
    comment: str


# ==========================================
# Migration Phase Control Endpoints (CP2)
# ==========================================

@app.get("/api/v1/migration/phase")
def read_migration_phase():
    phase = get_current_phase()
    phase_names = {
        1: "Phase 1: EXPAND (Legacy Read/Write)",
        2: "Phase 2: DUAL WRITE (Write both old & new, Read old)",
        3: "Phase 3: BACKFILL (Dual Write active while backfilling)",
        4: "Phase 4: SWITCH READ (Read/Write exclusively on new fields)",
        5: "Phase 5: CONTRACT (Legacy column dropped)",
    }
    return {"phase": phase, "description": phase_names.get(phase, "Unknown")}


@app.post("/api/v1/migration/phase/{phase}")
def update_migration_phase(phase: int):
    if phase not in (1, 2, 3, 4, 5):
        raise HTTPException(status_code=422, detail="Phase must be 1, 2, 3, 4, or 5")
    set_current_phase(phase)
    return read_migration_phase()


# ==========================================
# Endpoints สำหรับ PostgreSQL (Users)
# ==========================================

@app.post("/api/v1/users", status_code=201)
@app.post("/users", status_code=201, include_in_schema=False)
def create_user(user: UserCreate, db: Session = Depends(get_pg_db)):
    phase = get_current_phase()
    legacy_name, first_name, last_name = _resolve_name_parts(
        user.name, user.full_name, user.first_name, user.last_name
    )
    now = datetime.datetime.utcnow()

    try:
        if phase == 1:
            # Phase 1 (Expand / Legacy): Write ONLY to legacy `name` column
            row = db.execute(
                text(
                    "INSERT INTO public.users (name, email, created_at) "
                    "VALUES (:name, :email, :created_at) "
                    "RETURNING id, name, email, created_at"
                ),
                {"name": legacy_name, "email": user.email, "created_at": now},
            ).mappings().first()
            db.commit()
            user_payload = _format_user_response(
                user_id=row["id"],
                email=row["email"],
                created_at=row["created_at"],
                phase=phase,
                legacy_name=row["name"],
            )
        elif phase in (2, 3):
            # Phase 2 & 3 (Dual Write): Write to BOTH legacy `name` AND new `first_name`, `last_name`
            row = db.execute(
                text(
                    "INSERT INTO public.users (name, first_name, last_name, email, created_at) "
                    "VALUES (:name, :first_name, :last_name, :email, :created_at) "
                    "RETURNING id, name, first_name, last_name, email, created_at"
                ),
                {
                    "name": legacy_name,
                    "first_name": first_name,
                    "last_name": last_name,
                    "email": user.email,
                    "created_at": now,
                },
            ).mappings().first()
            db.commit()
            user_payload = _format_user_response(
                user_id=row["id"],
                email=row["email"],
                created_at=row["created_at"],
                phase=phase,
                legacy_name=row["name"],
                first_name=row["first_name"],
                last_name=row["last_name"],
            )
        else:
            # Phase 4 (Switch Read) & Phase 5 (Contract): Write ONLY to new `first_name`, `last_name`
            # Decoupled from legacy `name` column so Phase 5 DROP COLUMN name runs with zero errors
            row = db.execute(
                text(
                    "INSERT INTO public.users (first_name, last_name, email, created_at) "
                    "VALUES (:first_name, :last_name, :email, :created_at) "
                    "RETURNING id, first_name, last_name, email, created_at"
                ),
                {
                    "first_name": first_name,
                    "last_name": last_name,
                    "email": user.email,
                    "created_at": now,
                },
            ).mappings().first()
            db.commit()
            user_payload = _format_user_response(
                user_id=row["id"],
                email=row["email"],
                created_at=row["created_at"],
                phase=phase,
                first_name=row["first_name"],
                last_name=row["last_name"],
            )
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Email already exists")
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=500, detail="Could not create user")

    return {"message": "User created", "user": user_payload}


@app.get("/api/v1/users/{user_id}")
@app.get("/users/{user_id}", include_in_schema=False)
def get_user(user_id: int, db: Session = Depends(get_pg_db)):
    phase = get_current_phase()

    if phase == 1:
        row = db.execute(
            text("SELECT id, name, email, created_at FROM public.users WHERE id = :id"),
            {"id": user_id},
        ).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        return _format_user_response(
            user_id=row["id"],
            email=row["email"],
            created_at=row["created_at"],
            phase=phase,
            legacy_name=row["name"],
        )
    elif phase in (2, 3):
        row = db.execute(
            text(
                "SELECT id, name, first_name, last_name, email, created_at "
                "FROM public.users WHERE id = :id"
            ),
            {"id": user_id},
        ).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        return _format_user_response(
            user_id=row["id"],
            email=row["email"],
            created_at=row["created_at"],
            phase=phase,
            legacy_name=row["name"],
            first_name=row["first_name"],
            last_name=row["last_name"],
        )
    else:
        # Phase 4 (Switch Read) & Phase 5 (Contract): Read EXCLUSIVELY from `first_name`, `last_name`
        row = db.execute(
            text(
                "SELECT id, first_name, last_name, email, created_at "
                "FROM public.users WHERE id = :id"
            ),
            {"id": user_id},
        ).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        return _format_user_response(
            user_id=row["id"],
            email=row["email"],
            created_at=row["created_at"],
            phase=phase,
            first_name=row["first_name"],
            last_name=row["last_name"],
        )


@app.get("/api/v1/users")
@app.get("/users", include_in_schema=False)
def get_users(db: Session = Depends(get_pg_db)):
    phase = get_current_phase()
    if phase == 1:
        rows = db.execute(
            text("SELECT id, name, email, created_at FROM public.users ORDER BY id")
        ).mappings().all()
        return [
            _format_user_response(
                user_id=r["id"],
                email=r["email"],
                created_at=r["created_at"],
                phase=phase,
                legacy_name=r["name"],
            )
            for r in rows
        ]
    elif phase in (2, 3):
        rows = db.execute(
            text(
                "SELECT id, name, first_name, last_name, email, created_at "
                "FROM public.users ORDER BY id"
            )
        ).mappings().all()
        return [
            _format_user_response(
                user_id=r["id"],
                email=r["email"],
                created_at=r["created_at"],
                phase=phase,
                legacy_name=r["name"],
                first_name=r["first_name"],
                last_name=r["last_name"],
            )
            for r in rows
        ]
    else:
        rows = db.execute(
            text(
                "SELECT id, first_name, last_name, email, created_at "
                "FROM public.users ORDER BY id"
            )
        ).mappings().all()
        return [
            _format_user_response(
                user_id=r["id"],
                email=r["email"],
                created_at=r["created_at"],
                phase=phase,
                first_name=r["first_name"],
                last_name=r["last_name"],
            )
            for r in rows
        ]


@app.put("/api/v1/users/{user_id}")
@app.put("/users/{user_id}", include_in_schema=False)
def update_user(user_id: int, user_update: UserUpdate, db: Session = Depends(get_pg_db)):
    phase = get_current_phase()
    existing = get_user(user_id, db)

    has_name_update = any(
        value is not None
        for value in (
            user_update.name,
            user_update.full_name,
            user_update.first_name,
            user_update.last_name,
        )
    )
    if has_name_update:
        if user_update.name is not None or user_update.full_name is not None:
            legacy_name, first_name, last_name = _resolve_name_parts(
                user_update.name,
                user_update.full_name,
                user_update.first_name,
                user_update.last_name,
            )
        else:
            first_name = (
                user_update.first_name
                if user_update.first_name is not None
                else existing["first_name"]
            )
            last_name = (
                user_update.last_name
                if user_update.last_name is not None
                else existing["last_name"]
            )
            legacy_name, first_name, last_name = _resolve_name_parts(
                None, None, first_name, last_name
            )
    else:
        legacy_name = existing["name"]
        first_name = existing["first_name"]
        last_name = existing["last_name"]

    new_email = user_update.email if user_update.email is not None else existing["email"]

    try:
        if phase == 1:
            db.execute(
                text("UPDATE public.users SET name = :name, email = :email WHERE id = :id"),
                {"name": legacy_name, "email": new_email, "id": user_id},
            )
        elif phase in (2, 3):
            # Phase 2 & 3 (Dual Write): Update BOTH legacy and new fields
            db.execute(
                text(
                    "UPDATE public.users "
                    "SET name = :name, first_name = :first_name, last_name = :last_name, email = :email "
                    "WHERE id = :id"
                ),
                {
                    "name": legacy_name,
                    "first_name": first_name,
                    "last_name": last_name,
                    "email": new_email,
                    "id": user_id,
                },
            )
        else:
            # Phase 4 & 5: Update ONLY new fields
            db.execute(
                text(
                    "UPDATE public.users "
                    "SET first_name = :first_name, last_name = :last_name, email = :email "
                    "WHERE id = :id"
                ),
                {
                    "first_name": first_name,
                    "last_name": last_name,
                    "email": new_email,
                    "id": user_id,
                },
            )
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Email already exists")
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=500, detail="Could not update user")

    return get_user(user_id, db)


# ==========================================
# Endpoints สำหรับ MongoDB (Products)
# ==========================================

@app.post("/api/v1/products", status_code=201)
def create_product(product: ProductCreate, mongo_db=Depends(get_mongo_db)):
    # บันทึกข้อมูลลง Collection "products" ใน MongoDB
    product_dict = product.model_dump() if hasattr(product, "model_dump") else product.dict()
    result = mongo_db["products"].insert_one(product_dict)

    # ดึง ID ที่ MongoDB สร้างให้แปลงเป็น String
    product_dict["_id"] = str(result.inserted_id)
    return {"message": "Product created", "product": product_dict}


@app.get("/api/v1/products")
def get_products(mongo_db=Depends(get_mongo_db)):
    # ดึงข้อมูลทั้งหมดจาก MongoDB (ในงานจริงควรทำ Pagination)
    products = list(mongo_db["products"].find())
    for prod in products:
        prod["_id"] = str(prod["_id"])  # แปลง ObjectId ให้เป็น String
    return products


# from polly's branch
@app.post("/api/v1/reviews", status_code=201)
def create_review(review: schemas.Review, mongo_db=Depends(get_mongo_db)):
    # เพิ่ม Endpoint ให้รองรับ Review ของคนที่ 2
    review_dict = review.model_dump() if hasattr(review, "model_dump") else review.dict()
    result = mongo_db["reviews"].insert_one(review_dict)
    review_dict["_id"] = str(result.inserted_id)
    return {"message": "Review created", "review": review_dict}


# ==========================================
# Endpoint แบบ Dual-DB (Orders)
# ==========================================
@app.post("/api/v1/orders", status_code=201)
def create_order(order: OrderCreate, db: Session = Depends(get_pg_db), mongo_db=Depends(get_mongo_db)):
    # 1. ตรวจสอบข้อมูล Product จาก MongoDB (ดึงราคา, สต็อก)
    # 2. บันทึก Transaction การสั่งซื้อลง PostgreSQL (ตาราง orders, order_items)
    # 3. อัปเดตข้อมูลหรือทำ Audit log กลับไปที่ MongoDB

    # from ice's branch
    if not hasattr(models, "Order") or not hasattr(models, "OrderItem"):
        raise HTTPException(
            status_code=503,
            detail="Order and OrderItem models are not available yet",
        )

    if not order.items:
        raise HTTPException(status_code=422, detail="Order must contain at least one item")

    user_exists = db.query(models.User.id).filter(models.User.id == order.user_id).first()
    if not user_exists:
        raise HTTPException(status_code=404, detail="User not found")

    products_to_order = []
    requested_product_ids = set()

    for item in order.items:
        if item.quantity < 1:
            raise HTTPException(status_code=422, detail="Quantity must be at least 1")

        if item.product_id in requested_product_ids:
            raise HTTPException(status_code=422, detail="A product can appear only once in an order")
        requested_product_ids.add(item.product_id)

        if not ObjectId.is_valid(item.product_id):
            raise HTTPException(status_code=422, detail=f"Invalid product id: {item.product_id}")

        product_object_id = ObjectId(item.product_id)
        product = mongo_db["products"].find_one({"_id": product_object_id})
        if not product:
            raise HTTPException(status_code=404, detail=f"Product not found: {item.product_id}")

        if product.get("status") not in (None, "available"):
            raise HTTPException(status_code=409, detail=f"Product is not available: {item.product_id}")

        products_to_order.append({
            "item": item,
            "object_id": product_object_id,
            "previous_status": product.get("status"),
            "had_status": "status" in product,
            "previous_order_id": product.get("order_id"),
            "had_order_id": "order_id" in product,
        })

    updated_products = []
    db_order = None

    def restore_products():
        if not db_order:
            return

        for product_data in updated_products:
            restore_set = {}
            restore_unset = {}

            if product_data["had_status"]:
                restore_set["status"] = product_data["previous_status"]
            else:
                restore_unset["status"] = ""

            if product_data["had_order_id"]:
                restore_set["order_id"] = product_data["previous_order_id"]
            else:
                restore_unset["order_id"] = ""

            restore_update = {}
            if restore_set:
                restore_update["$set"] = restore_set
            if restore_unset:
                restore_update["$unset"] = restore_unset

            mongo_db["products"].update_one(
                {"_id": product_data["object_id"], "order_id": db_order.id},
                restore_update,
            )

    try:
        db_order = models.Order(user_id=order.user_id)
        db.add(db_order)
        db.flush()

        for product_data in products_to_order:
            item = product_data["item"]
            if item.quantity != 1 and not hasattr(models.OrderItem, "quantity"):
                raise HTTPException(
                    status_code=422,
                    detail="OrderItem model does not support quantities greater than 1",
                )
            order_item_data = {
                "order_id": db_order.id,
                "product_id": item.product_id,
            }
            if hasattr(models.OrderItem, "quantity"):
                order_item_data["quantity"] = item.quantity
            db.add(models.OrderItem(**order_item_data))

        for product_data in products_to_order:
            product_filter = {"_id": product_data["object_id"]}
            previous_status = product_data["previous_status"]
            if product_data["had_status"]:
                product_filter["status"] = previous_status
            else:
                product_filter["status"] = {"$exists": False}

            result = mongo_db["products"].update_one(
                product_filter,
                {"$set": {"status": "sold", "order_id": db_order.id}},
            )
            if result.modified_count != 1:
                raise HTTPException(
                    status_code=409,
                    detail=f"Product is no longer available: {product_data['item'].product_id}",
                )
            updated_products.append(product_data)

        db.commit()
    except HTTPException:
        db.rollback()
        restore_products()
        raise
    except Exception:
        db.rollback()
        restore_products()
        raise HTTPException(status_code=500, detail="Could not create order")

    return {
        "message": "Order created successfully",
        "order_id": db_order.id,
        "user_id": order.user_id,
        "items": [
            item.model_dump() if hasattr(item, "model_dump") else item.dict()
            for item in order.items
        ],
    }
