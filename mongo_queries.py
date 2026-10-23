# mongo_queries.py
"""
MongoDB query layer for Products + Reviews (Checkpoint 3).

- Optimistic Concurrency Control (OCC): every product document carries a `version`
  field. Writes only match when the caller's version is still current, and each
  successful write increments it, so concurrent writers cannot overwrite each other.
- Query Projection: read queries return only the fields the API needs.
- No N+1: list summaries that need review data use a single aggregation ($lookup)
  instead of one query per product.
"""
from typing import Any, Dict, List, Optional

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, ReturnDocument

PRODUCTS = "products"
REVIEWS = "reviews"

# ==========================================
# Projections (ดึงเฉพาะฟิลด์ที่ API ใช้จริง)
# ==========================================
PRODUCT_LIST_PROJECTION = {
    "name": 1,
    "category": 1,
    "status": 1,
    "attributes.price": 1,
    "attributes.condition": 1,
    "version": 1,
}

# Detail view: full attributes, but never internal fields such as order_id.
PRODUCT_DETAIL_PROJECTION = {
    "name": 1,
    "category": 1,
    "status": 1,
    "attributes": 1,
    "version": 1,
}

# Minimal state needed by the order flow before it marks a product as sold.
PRODUCT_STATE_PROJECTION = {
    "status": 1,
    "order_id": 1,
    "version": 1,
}

REVIEW_LIST_PROJECTION = {
    "user_id": 1,
    "rating": 1,
    "comment": 1,
    "created_at": 1,
}

UPDATABLE_PRODUCT_FIELDS = {"name", "category", "attributes"}


class InvalidProductIdError(ValueError):
    pass


class ProductNotFoundError(LookupError):
    pass


class VersionConflictError(Exception):
    """The document changed since the caller read it (stale `version`)."""

    def __init__(self, product_id: str, expected_version: int, current_version: Optional[int]):
        self.product_id = product_id
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__(
            f"Product {product_id} version conflict: expected {expected_version}, "
            f"current {current_version}"
        )


class ProductUnavailableError(Exception):
    pass


def to_object_id(product_id: str) -> ObjectId:
    if not ObjectId.is_valid(product_id):
        raise InvalidProductIdError(f"Invalid product id: {product_id}")
    return ObjectId(product_id)


def _serialize(doc: Dict[str, Any]) -> Dict[str, Any]:
    doc["_id"] = str(doc["_id"])
    return doc


def _serialize_product(doc: Dict[str, Any]) -> Dict[str, Any]:
    # Legacy products are treated as version 0 until the migration runs.
    doc.setdefault("version", 0)
    return _serialize(doc)


def version_filter(expected_version: int) -> Any:
    # version 0 also matches legacy documents that have no `version` field yet
    return {"$in": [0, None]} if expected_version == 0 else expected_version


def ensure_indexes(mongo_db) -> None:
    mongo_db[PRODUCTS].create_index([("category", ASCENDING), ("status", ASCENDING)])
    mongo_db[REVIEWS].create_index([("product_id", ASCENDING), ("created_at", DESCENDING)])


def backfill_product_versions(mongo_db) -> int:
    result = mongo_db[PRODUCTS].update_many(
        {"version": {"$exists": False}},
        {"$set": {"version": 0}},
    )
    return result.modified_count


# ==========================================
# Read queries (Projection)
# ==========================================

def _product_filter(category: Optional[str], status: Optional[str]) -> Dict[str, Any]:
    query: Dict[str, Any] = {}
    if category:
        query["category"] = category
    if status:
        query["status"] = status
    return query


def list_products(
    mongo_db,
    category: Optional[str] = None,
    status: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    cursor = (
        mongo_db[PRODUCTS]
        .find(_product_filter(category, status), PRODUCT_LIST_PROJECTION)
        .sort("_id", ASCENDING)
        .skip(skip)
        .limit(limit)
    )
    return [_serialize_product(doc) for doc in cursor]


def get_product(mongo_db, product_id: str) -> Optional[Dict[str, Any]]:
    doc = mongo_db[PRODUCTS].find_one({"_id": to_object_id(product_id)}, PRODUCT_DETAIL_PROJECTION)
    return _serialize_product(doc) if doc else None


def get_product_state(mongo_db, product_id: str) -> Optional[Dict[str, Any]]:
    doc = mongo_db[PRODUCTS].find_one({"_id": to_object_id(product_id)}, PRODUCT_STATE_PROJECTION)
    return _serialize_product(doc) if doc else None


def get_product_states(mongo_db, product_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """State of many products in ONE query, keyed by product id; missing ids are absent."""
    object_ids = [to_object_id(product_id) for product_id in product_ids]
    cursor = mongo_db[PRODUCTS].find({"_id": {"$in": object_ids}}, PRODUCT_STATE_PROJECTION)
    return {doc["_id"]: doc for doc in map(_serialize_product, cursor)}


def list_reviews(mongo_db, product_id: str, skip: int = 0, limit: int = 50) -> List[Dict[str, Any]]:
    cursor = (
        mongo_db[REVIEWS]
        .find({"product_id": product_id}, REVIEW_LIST_PROJECTION)
        .sort("created_at", DESCENDING)
        .skip(skip)
        .limit(limit)
    )
    return [_serialize(doc) for doc in cursor]


def list_product_summaries(
    mongo_db,
    category: Optional[str] = None,
    status: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Products with review_count / avg_rating in ONE aggregation (no per-product query)."""
    pipeline = [
        {"$match": _product_filter(category, status)},
        {"$sort": {"_id": 1}},
        {"$skip": skip},
        {"$limit": limit},
        {
            "$project": {
                "name": 1,
                "category": 1,
                "status": 1,
                "attributes.price": 1,
                "attributes.condition": 1,
                "version": {"$ifNull": ["$version", 0]},
                "pid": {"$toString": "$_id"},
            }
        },
        {
            # reviews.product_id is stored as the string form of the product _id
            "$lookup": {
                "from": REVIEWS,
                "localField": "pid",
                "foreignField": "product_id",
                "pipeline": [
                    {"$group": {"_id": None, "count": {"$sum": 1}, "avg": {"$avg": "$rating"}}}
                ],
                "as": "review_stats",
            }
        },
        {"$set": {"review_stats": {"$first": "$review_stats"}}},
        {
            "$set": {
                "review_count": {"$ifNull": ["$review_stats.count", 0]},
                "avg_rating": {"$round": [{"$ifNull": ["$review_stats.avg", 0]}, 2]},
            }
        },
        {"$unset": ["pid", "review_stats"]},
    ]
    return [_serialize(doc) for doc in mongo_db[PRODUCTS].aggregate(pipeline)]


# ==========================================
# Write queries (Optimistic Concurrency Control)
# ==========================================

def _raise_write_failure(mongo_db, object_id: ObjectId, expected_version: int) -> None:
    current = mongo_db[PRODUCTS].find_one(
        {"_id": object_id},
        {"version": 1, "status": 1},
    )
    if current is None:
        raise ProductNotFoundError(f"Product not found: {object_id}")
    current_version = current.get("version", 0)
    if current_version != expected_version:
        raise VersionConflictError(str(object_id), expected_version, current_version)
    raise ProductUnavailableError(f"Product is not available: {object_id}")


def update_product(
    mongo_db, product_id: str, expected_version: int, changes: Dict[str, Any]
) -> Dict[str, Any]:
    """Apply `changes` only if the product is still at `expected_version`."""
    unknown = set(changes) - UPDATABLE_PRODUCT_FIELDS
    if unknown:
        raise ValueError(f"Fields cannot be updated: {', '.join(sorted(unknown))}")

    object_id = to_object_id(product_id)
    update: Dict[str, Any] = {"$inc": {"version": 1}}
    if changes:
        update["$set"] = changes

    doc = mongo_db[PRODUCTS].find_one_and_update(
        {"_id": object_id, "version": version_filter(expected_version)},
        update,
        projection=PRODUCT_DETAIL_PROJECTION,
        return_document=ReturnDocument.AFTER,
    )
    if doc is None:
        _raise_write_failure(mongo_db, object_id, expected_version)
    return _serialize_product(doc)


def mark_product_sold(mongo_db, product_id: str, expected_version: int, order_id: int) -> int:
    """Atomically sell an available product at `expected_version`; returns the new version."""
    object_id = to_object_id(product_id)
    doc = mongo_db[PRODUCTS].find_one_and_update(
        {
            "_id": object_id,
            "version": version_filter(expected_version),
            "status": {"$in": ["available", None]},
        },
        {"$set": {"status": "sold", "order_id": order_id}, "$inc": {"version": 1}},
        projection={"version": 1},
        return_document=ReturnDocument.AFTER,
    )
    if doc is None:
        _raise_write_failure(mongo_db, object_id, expected_version)
    return doc["version"]
