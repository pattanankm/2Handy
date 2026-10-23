# test_mongo_concurrency.py
"""
CP3 verification for the MongoDB layer (mongo_queries.py).
Runs against a throwaway database, so it never touches main_db.

    python test_mongo_concurrency.py      (or: pytest test_mongo_concurrency.py)
"""
import concurrent.futures
import threading

from pymongo import MongoClient, monitoring

import mongo_queries
from database import MONGO_URL

TEST_DB_NAME = "cp3_mongo_test_db"
NUM_USERS = 20


class CommandCounter(monitoring.CommandListener):
    def __init__(self):
        self.commands = []

    def started(self, event):
        self.commands.append(event.command_name)

    def succeeded(self, event):
        pass

    def failed(self, event):
        pass


counter = CommandCounter()
client = MongoClient(MONGO_URL, event_listeners=[counter], maxPoolSize=NUM_USERS)
db = client[TEST_DB_NAME]


def _reset():
    db["products"].drop()
    db["reviews"].drop()
    mongo_queries.ensure_indexes(db)


def _insert_product(**fields):
    doc = {
        "name": "Last iPhone",
        "category": "Electronics",
        "status": "available",
        "version": 1,
        "attributes": {"price": 9999.0, "condition": "Good", "brand": "Apple"},
    }
    doc.update(fields)
    return str(db["products"].insert_one(doc).inserted_id)


def _race(fn):
    """Run fn(i) for NUM_USERS threads released at the same instant."""
    barrier = threading.Barrier(NUM_USERS)

    def attempt(i):
        barrier.wait()
        try:
            fn(i)
            return "success"
        except mongo_queries.VersionConflictError:
            return "conflict"
        except mongo_queries.ProductUnavailableError:
            return "unavailable"

    with concurrent.futures.ThreadPoolExecutor(max_workers=NUM_USERS) as executor:
        return list(executor.map(attempt, range(NUM_USERS)))


def test_concurrent_updates_only_one_wins():
    _reset()
    product_id = _insert_product()

    # every user read version 1 and tries to write with it
    results = _race(lambda i: mongo_queries.update_product(db, product_id, 1, {"name": f"edit by user {i}"}))

    print(f"update_product  -> success: {results.count('success')} | conflict: {results.count('conflict')}")
    assert results.count("success") == 1, "Lost update: more than one writer succeeded"
    assert results.count("conflict") == NUM_USERS - 1
    assert mongo_queries.get_product(db, product_id)["version"] == 2


def test_concurrent_purchase_of_last_item():
    _reset()
    product_id = _insert_product()

    results = _race(lambda i: mongo_queries.mark_product_sold(db, product_id, 1, order_id=1000 + i))

    failures = results.count("conflict") + results.count("unavailable")
    print(f"mark_product_sold -> success: {results.count('success')} | rejected: {failures}")
    assert results.count("success") == 1, "Race condition: product sold more than once"
    assert failures == NUM_USERS - 1
    product = db["products"].find_one({"_id": mongo_queries.to_object_id(product_id)})
    assert product["status"] == "sold" and product["version"] == 2


def test_stale_version_is_rejected_after_sale():
    _reset()
    product_id = _insert_product()
    mongo_queries.mark_product_sold(db, product_id, 1, order_id=1)
    try:
        mongo_queries.update_product(db, product_id, 1, {"name": "stale"})
        assert False, "stale version should be rejected"
    except mongo_queries.VersionConflictError as e:
        assert e.current_version == 2


def test_legacy_document_without_version():
    _reset()
    product_id = _insert_product()
    db["products"].update_one({"_id": mongo_queries.to_object_id(product_id)}, {"$unset": {"version": ""}})

    assert mongo_queries.get_product(db, product_id)["version"] == 0
    updated = mongo_queries.update_product(db, product_id, 0, {"category": "Phones"})
    assert updated["version"] == 1 and updated["category"] == "Phones"


def test_list_projection_returns_only_needed_fields():
    _reset()
    _insert_product(order_id=42, internal_notes="x" * 10_000)

    product = mongo_queries.list_products(db)[0]
    assert set(product) == {"_id", "name", "category", "status", "attributes", "version"}
    assert set(product["attributes"]) == {"price", "condition"}

    detail = mongo_queries.get_product(db, product["_id"])
    assert "order_id" not in detail and "internal_notes" not in detail


def test_summary_uses_a_single_query():
    _reset()
    product_ids = [_insert_product(name=f"Item {i}") for i in range(10)]
    db["reviews"].insert_many(
        [{"product_id": pid, "user_id": 1, "rating": r, "comment": "ok"} for pid in product_ids for r in (4, 5)]
    )

    counter.commands.clear()
    summaries = mongo_queries.list_product_summaries(db, limit=10)
    reads = [c for c in counter.commands if c in ("find", "aggregate", "getMore")]

    print(f"list_product_summaries -> {len(summaries)} products in {len(reads)} query: {reads}")
    assert reads == ["aggregate"], f"N+1 detected: {reads}"
    assert all(s["review_count"] == 2 and s["avg_rating"] == 4.5 for s in summaries)


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_")]
    try:
        for test in tests:
            test()
            print(f"PASS {test.__name__}")
    finally:
        client.drop_database(TEST_DB_NAME)
