# main.py
import os
import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from contextlib import asynccontextmanager

from bson import ObjectId
from fastapi import FastAPI, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pymongo.errors import OperationFailure
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

import models
import mongo_queries  # from prem's branch
import schemas  # from polly's branch
from pg_queries import (
    ConcurrentUpdate,
    RecordNotFound,
    complete_order,
    get_order,
    list_orders,
    lock_user,
)
from database import engine, get_pg_db, get_mongo_db

# สั่งให้สร้างตารางใน PostgreSQL (ถ้ายังไม่มี)
models.Base.metadata.create_all(bind=engine)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Indexes used by the CP3 MongoDB read queries (idempotent)
    mongo_queries.ensure_indexes(get_mongo_db())
    yield


app = FastAPI(title="2Handy API", lifespan=lifespan)


# ==========================================
# CP3: Concurrency errors -> HTTP status
# ==========================================
# PostgreSQL SQLSTATEs that mean "another transaction got there first":
# serialization_failure, deadlock_detected, lock_not_available.
PG_CONCURRENCY_SQLSTATES = {"40001", "40P01", "55P03"}
# MongoDB WriteConflict
MONGO_WRITE_CONFLICT = 112


def is_pg_concurrency_error(exc: BaseException) -> bool:
    orig = getattr(exc, "orig", None)
    return getattr(orig, "pgcode", None) in PG_CONCURRENCY_SQLSTATES


def is_mongo_concurrency_error(exc: OperationFailure) -> bool:
    return exc.code == MONGO_WRITE_CONFLICT or exc.has_error_label("TransientTransactionError")


def _conflict(detail: Any) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": detail})


@app.exception_handler(ConcurrentUpdate)
def handle_concurrent_update(_request: Request, exc: ConcurrentUpdate):
    return _conflict(str(exc))


@app.exception_handler(mongo_queries.VersionConflictError)
def handle_version_conflict(_request: Request, exc: mongo_queries.VersionConflictError):
    return _conflict({
        "message": "Product was modified by another request; reload and retry",
        "expected_version": exc.expected_version,
        "current_version": exc.current_version,
    })


@app.exception_handler(mongo_queries.ProductUnavailableError)
def handle_product_unavailable(_request: Request, exc: mongo_queries.ProductUnavailableError):
    return _conflict(str(exc))


@app.exception_handler(RecordNotFound)
@app.exception_handler(mongo_queries.ProductNotFoundError)
def handle_not_found(_request: Request, exc: Exception):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(mongo_queries.InvalidProductIdError)
def handle_invalid_product_id(_request: Request, exc: mongo_queries.InvalidProductIdError):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(DBAPIError)
def handle_db_error(_request: Request, exc: DBAPIError):
    # Only real concurrency failures are 409; broken SQL or a lost connection is still 500.
    # The request's Session is closed (and so rolled back) by get_pg_db.
    if is_pg_concurrency_error(exc):
        return _conflict("Concurrent update detected; retry the request")
    return JSONResponse(status_code=500, content={"detail": "Database error"})


@app.exception_handler(OperationFailure)
def handle_mongo_error(_request: Request, exc: OperationFailure):
    if is_mongo_concurrency_error(exc):
        return _conflict("Concurrent update detected; retry the request")
    return JSONResponse(status_code=500, content={"detail": "Database error"})


def raise_db_error(exc: SQLAlchemyError, detail: str) -> None:
    """For handlers that catch SQLAlchemyError themselves: keep 409 distinct from 500."""
    if is_pg_concurrency_error(exc):
        raise HTTPException(status_code=409, detail="Concurrent update detected; retry the request") from exc
    raise HTTPException(status_code=500, detail=detail) from exc


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


class ProductUpdate(BaseModel):
    # version ที่ client อ่านมา (OCC) — ส่งใน body หรือส่งเป็น header If-Match ก็ได้
    version: Optional[int] = None
    name: Optional[str] = None
    category: Optional[str] = None
    attributes: Optional[Dict[str, Any]] = None


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
    except SQLAlchemyError as exc:
        db.rollback()
        raise_db_error(exc, "Could not create user")

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
    # Keep the row lock through the read/modify/write transaction, including
    # validation. The wrapper releases it on HTTP errors as well as DB errors.
    try:
        lock_user(db, user_id)
        return _update_locked_user(user_id, user_update, db)
    except RecordNotFound as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception:
        db.rollback()
        raise


def _update_locked_user(user_id: int, user_update: UserUpdate, db: Session):
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
        # Read our own result while still holding the lock, before another
        # writer can change it. Return a plain payload, not an expired ORM row.
        result = get_user(user_id, db)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Email already exists")
    except SQLAlchemyError as exc:
        db.rollback()
        raise_db_error(exc, "Could not update user")

    return result


# ==========================================
# Endpoints สำหรับ MongoDB (Products)
# ==========================================

@app.post("/api/v1/products", status_code=201)
def create_product(product: ProductCreate, mongo_db=Depends(get_mongo_db)):
    # บันทึกข้อมูลลง Collection "products" ใน MongoDB
    product_dict = product.model_dump() if hasattr(product, "model_dump") else product.dict()
    product_dict["status"] = "available"
    product_dict["version"] = 1  # OCC version token
    result = mongo_db["products"].insert_one(product_dict)

    # ดึง ID ที่ MongoDB สร้างให้แปลงเป็น String
    product_dict["_id"] = str(result.inserted_id)
    return {"message": "Product created", "product": product_dict}


def _etag(version: int) -> str:
    return f'"{version}"'


def _version_from_if_match(if_match: str) -> int:
    value = if_match.strip()
    if value.startswith("W/"):
        value = value[2:]
    value = value.strip('"')
    if not value.isdigit():
        raise HTTPException(status_code=400, detail='If-Match must be a product version ETag such as "3"')
    return int(value)


# --- Read-only endpoints: projection + pagination, one MongoDB query each (no N+1) ---

@app.get("/api/v1/products")
def get_products(
    category: Optional[str] = None,
    status: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    mongo_db=Depends(get_mongo_db),
):
    return mongo_queries.list_products(mongo_db, category, status, skip, limit)


# Declared before /{product_id} so "summary" is not parsed as a product id
@app.get("/api/v1/products/summary")
def get_product_summaries(
    category: Optional[str] = None,
    status: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    mongo_db=Depends(get_mongo_db),
):
    # review_count / avg_rating come from one $lookup aggregation, not a query per product
    return mongo_queries.list_product_summaries(mongo_db, category, status, skip, limit)


@app.get("/api/v1/products/{product_id}")
def get_product(product_id: str, response: Response, mongo_db=Depends(get_mongo_db)):
    product = mongo_queries.get_product(mongo_db, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    response.headers["ETag"] = _etag(product["version"])
    return product


@app.get("/api/v1/products/{product_id}/reviews")
def get_product_reviews(
    product_id: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    mongo_db=Depends(get_mongo_db),
):
    mongo_queries.to_object_id(product_id)  # 422 for a malformed id
    return mongo_queries.list_reviews(mongo_db, product_id, skip, limit)


@app.patch("/api/v1/products/{product_id}")
def update_product(
    product_id: str,
    product_update: ProductUpdate,
    response: Response,
    if_match: Optional[str] = Header(None),
    mongo_db=Depends(get_mongo_db),
):
    """OCC update. A stale If-Match header -> 412; a stale body `version` -> 409."""
    header_version = _version_from_if_match(if_match) if if_match is not None else None
    body_version = product_update.version
    if header_version is None and body_version is None:
        raise HTTPException(
            status_code=428,
            detail="Send the product version in the If-Match header or the body",
        )
    if header_version is not None and body_version is not None and header_version != body_version:
        raise HTTPException(status_code=422, detail="If-Match and body version disagree")
    expected_version = header_version if header_version is not None else body_version

    changes = (
        product_update.model_dump(exclude_unset=True, exclude={"version"})
        if hasattr(product_update, "model_dump")
        else product_update.dict(exclude_unset=True, exclude={"version"})
    )
    try:
        product = mongo_queries.update_product(mongo_db, product_id, expected_version, changes)
    except mongo_queries.VersionConflictError as exc:
        if header_version is None:
            raise  # -> 409 Conflict via handle_version_conflict
        raise HTTPException(
            status_code=412,
            detail={
                "message": "If-Match does not match the current product version",
                "expected_version": exc.expected_version,
                "current_version": exc.current_version,
            },
            headers={"ETag": _etag(exc.current_version)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    response.headers["ETag"] = _etag(product["version"])
    return product


# from polly's branch
@app.post("/api/v1/reviews", status_code=201)
def create_review(review: schemas.Review, mongo_db=Depends(get_mongo_db)):
    # เพิ่ม Endpoint ให้รองรับ Review ของคนที่ 2
    review_dict = review.model_dump() if hasattr(review, "model_dump") else review.dict()
    if review_dict.get("created_at") is None:
        # list_reviews sorts by created_at
        review_dict["created_at"] = datetime.datetime.utcnow()
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

    requested_product_ids = set()

    for item in order.items:
        if item.quantity < 1:
            raise HTTPException(status_code=422, detail="Quantity must be at least 1")

        if item.product_id in requested_product_ids:
            raise HTTPException(status_code=422, detail="A product can appear only once in an order")
        requested_product_ids.add(item.product_id)

        if not ObjectId.is_valid(item.product_id):
            raise HTTPException(status_code=422, detail=f"Invalid product id: {item.product_id}")

    # One $in query for every item instead of one find_one per item (no N+1).
    # Projection: only the state needed to sell the product (status/order_id/version)
    product_states = mongo_queries.get_product_states(
        mongo_db, [item.product_id for item in order.items]
    )

    products_to_order = []
    for item in order.items:
        product = product_states.get(item.product_id)
        if not product:
            raise HTTPException(status_code=404, detail=f"Product not found: {item.product_id}")

        if product.get("status") not in (None, "available"):
            raise HTTPException(status_code=409, detail=f"Product is not available: {item.product_id}")

        products_to_order.append({
            "item": item,
            "object_id": ObjectId(item.product_id),
            "previous_status": product.get("status"),
            "had_status": "status" in product,
            "previous_order_id": product.get("order_id"),
            "had_order_id": "order_id" in product,
            "version": product["version"],
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

            # Restoring is also a write, so bump the version for anyone holding the "sold" one
            restore_update = {"$inc": {"version": 1}}
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
            # OCC: only succeeds if nobody changed the product since we read its version
            try:
                mongo_queries.mark_product_sold(
                    mongo_db, product_data["item"].product_id, product_data["version"], db_order.id
                )
            except (mongo_queries.VersionConflictError, mongo_queries.ProductUnavailableError) as exc:
                raise HTTPException(
                    status_code=409,
                    detail=f"Product is no longer available: {product_data['item'].product_id}",
                ) from exc
            except mongo_queries.ProductNotFoundError as exc:
                raise HTTPException(
                    status_code=404,
                    detail=f"Product not found: {product_data['item'].product_id}",
                ) from exc
            updated_products.append(product_data)

        db.commit()
    except HTTPException:
        db.rollback()
        restore_products()
        raise
    except Exception as exc:
        db.rollback()
        restore_products()
        if is_pg_concurrency_error(exc):
            raise HTTPException(status_code=409, detail="Concurrent update detected; retry the request") from exc
        raise HTTPException(status_code=500, detail="Could not create order") from exc

    return {
        "message": "Order created successfully",
        "order_id": db_order.id,
        "user_id": order.user_id,
        "items": [
            item.model_dump() if hasattr(item, "model_dump") else item.dict()
            for item in order.items
        ],
    }


# --- Read-only endpoints: orders + items in ONE SQL statement (JOIN + jsonb_agg, no N+1) ---

@app.get("/api/v1/orders")
def read_orders(
    user_id: Optional[int] = Query(None, gt=0),
    after_id: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_pg_db),
):
    # Keyset pagination: pass the last returned id as after_id for the next page
    return list_orders(db, user_id=user_id, after_id=after_id, limit=limit)


@app.get("/api/v1/orders/{order_id}")
def read_order(order_id: int, db: Session = Depends(get_pg_db)):
    return get_order(db, order_id)  # RecordNotFound -> 404


@app.post("/api/v1/orders/{order_id}/complete")
def finish_order(order_id: int, db: Session = Depends(get_pg_db)):
    # SELECT ... FOR UPDATE: concurrent callers wait, then see the order is no longer
    # pending -> ConcurrentUpdate -> 409 Conflict (RecordNotFound -> 404)
    try:
        result = complete_order(db, order_id)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
