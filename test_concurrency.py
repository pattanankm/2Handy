"""test_concurrency.py - Checkpoint 3 Concurrency Verification Script.

Tests race condition prevention under parallel writes (20 concurrent users):
1. Optimistic Concurrency Control (OCC in MongoDB):
   20 concurrent users attempt to buy the LAST remaining item simultaneously.
   Asserts: Exactly 1 succeeds (HTTP 201), 19 receive HTTP 409 Conflict.
2. Pessimistic Concurrency Control (SELECT FOR UPDATE in PostgreSQL):
   20 concurrent users attempt to complete the same pending order simultaneously.
   Asserts: Exactly 1 succeeds (HTTP 200), 19 receive HTTP 409 Conflict.

Can be executed directly:
    python test_concurrency.py
or via pytest:
    pytest test_concurrency.py -v
"""

import concurrent.futures
import os
import sys
import threading
import time
from uuid import uuid4

import requests

# Base URL for 2Handy Web API
API_URL = os.getenv("API_URL", "http://127.0.0.1:8000").rstrip("/")
NUM_CONCURRENT_USERS = 20


def ensure_server_running():
    """Ensure the FastAPI server is reachable, or start an in-process uvicorn server."""
    try:
        res = requests.get(f"{API_URL}/docs", timeout=2)
        if res.status_code == 200:
            print(f"📡 Connected to active API server at {API_URL}")
            return None
    except requests.exceptions.RequestException:
        pass

    print(f"⚠️  No API server found at {API_URL}. Starting an in-process Uvicorn server...")
    import uvicorn
    import main

    config = uvicorn.Config(main.app, host="127.0.0.1", port=8000, log_level="warning")
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    # Wait for server to start
    for _ in range(30):
        try:
            res = requests.get("http://127.0.0.1:8000/docs", timeout=1)
            if res.status_code == 200:
                print("✅ In-process API server started successfully at http://127.0.0.1:8000")
                return server
        except requests.exceptions.RequestException:
            time.sleep(0.2)
    raise RuntimeError("Failed to start background test API server.")


def setup_test_user():
    """Create a test user in PostgreSQL via API or direct session."""
    user_payload = {
        "email": f"tester_{uuid4().hex[:8]}@example.com",
        "name": "Concurrency Tester",
        "first_name": "Concurrency",
        "last_name": "Tester",
    }
    res = requests.post(f"{API_URL}/api/v1/users", json=user_payload)
    if res.status_code in (200, 201):
        data = res.json()
        if "user" in data and "id" in data["user"]:
            return data["user"]["id"]
        if "id" in data:
            return data["id"]
    # Fallback to direct DB query if needed
    from database import get_pg_db
    import models
    db = next(get_pg_db())
    user = db.query(models.User).first()
    if user:
        return user.id
    new_user = models.User(email=user_payload["email"], name=user_payload["name"])
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return new_user.id


def setup_test_product(name="Limited Stock Item"):
    """Create an available product in MongoDB with version=1 (stock=1)."""
    product_payload = {
        "name": name,
        "category": "Electronics",
        "attributes": {
            "price": 1500.0,
            "condition": "New",
            "stock": 1,
            "sku": uuid4().hex[:8],
        },
    }
    res = requests.post(f"{API_URL}/api/v1/products", json=product_payload)
    assert res.status_code == 201, f"Failed to create product: {res.text}"
    product_data = res.json()["product"]
    return product_data["_id"]


def try_buy_item(user_id: int, product_id: str, barrier: threading.Barrier):
    """Attempt to purchase the item; barrier synchronizes all threads to hit API simultaneously."""
    payload = {
        "user_id": user_id,
        "items": [
            {
                "product_id": product_id,
                "quantity": 1,
            }
        ],
    }
    # Wait for all threads to be ready before sending request
    barrier.wait()
    response = requests.post(f"{API_URL}/api/v1/orders", json=payload)
    return response


def run_race_condition_test():
    """Test Option A: OCC (MongoDB) - 20 concurrent users competing for the LAST remaining item.

    Asserts that exactly 1 succeeds (HTTP 201) and 19 receive HTTP 409 Conflict.
    """
    print("\n" + "=" * 70)
    print("🚀 [TEST 1] Concurrency Test: 20 concurrent users buying LAST item")
    print("=" * 70)

    user_id = setup_test_user()
    product_id = setup_test_product(name="Final RTX 5090 GPU")
    print(f"📦 Created single-stock product: ID={product_id}")
    print(f"👤 Using test user: ID={user_id}")
    print(f"⚡ Firing {NUM_CONCURRENT_USERS} simultaneous requests to POST /api/v1/orders...")

    barrier = threading.Barrier(NUM_CONCURRENT_USERS)

    with concurrent.futures.ThreadPoolExecutor(max_workers=NUM_CONCURRENT_USERS) as executor:
        futures = [
            executor.submit(try_buy_item, user_id, product_id, barrier)
            for _ in range(NUM_CONCURRENT_USERS)
        ]
        responses = [f.result() for f in futures]
        results = [r.status_code for r in responses]

    successes = results.count(201) + results.count(200)
    conflicts = results.count(409)
    other_errors = len(results) - successes - conflicts

    print(f"📊 Results -> Success (201): {successes} | Conflicts (409): {conflicts} | Others: {other_errors}")

    # Rubric Assertions
    assert successes == 1, (
        f"❌ Race Condition Detected! Expected exactly 1 success, but got {successes}. "
        f"Multiple users booked the same item!"
    )
    assert conflicts == NUM_CONCURRENT_USERS - 1, (
        f"❌ Expected {NUM_CONCURRENT_USERS - 1} conflicts (HTTP 409), but got {conflicts}."
    )

    # Verify Database Consistency
    from database import get_pg_db, get_mongo_db
    from bson import ObjectId
    import models

    db_pg = next(get_pg_db())
    order_items_count = db_pg.query(models.OrderItem).filter(
        models.OrderItem.product_id == product_id
    ).count()
    assert order_items_count == 1, (
        f"Database Inconsistency! PostgreSQL has {order_items_count} order_items for this product (must be 1)."
    )

    mongo_db = get_mongo_db()
    product_doc = mongo_db["products"].find_one({"_id": ObjectId(product_id)})
    assert product_doc["status"] == "sold", "Product status in MongoDB should be 'sold'"
    assert product_doc["version"] == 2, f"Product version should be 2, got {product_doc['version']}"

    print("✅ [TEST 1 PASSED] Flawless OCC Race Condition Prevention verified!")
    print(f"   • Exactly 1 user bought the product (HTTP 201)")
    print(f"   • Exactly {NUM_CONCURRENT_USERS - 1} users received HTTP 409 Conflict")
    print(f"   • PostgreSQL has exactly 1 order_item record (Zero ghost orders)")
    print(f"   • MongoDB product marked 'sold' with version bumped to 2")


def try_complete_order(order_id: int, barrier: threading.Barrier):
    """Attempt to complete an order; barrier synchronizes all threads."""
    barrier.wait()
    response = requests.post(f"{API_URL}/api/v1/orders/{order_id}/complete")
    return response


def run_pessimistic_lock_order_complete_test():
    """Test Option B: Pessimistic Lock (SELECT FOR UPDATE in PostgreSQL).

    20 concurrent users attempting to complete the SAME pending order simultaneously.
    Asserts that exactly 1 succeeds (HTTP 200) and 19 receive HTTP 409 Conflict.
    """
    print("\n" + "=" * 70)
    print("🚀 [TEST 2] Concurrency Test: 20 concurrent users completing SAME order")
    print("=" * 70)

    user_id = setup_test_user()
    from database import get_pg_db
    import models

    db_pg = next(get_pg_db())
    order = models.Order(user_id=user_id, status="pending")
    db_pg.add(order)
    db_pg.commit()
    db_pg.refresh(order)
    order_id = order.id

    print(f"📋 Created pending order: ID={order_id}")
    print(f"⚡ Firing {NUM_CONCURRENT_USERS} simultaneous requests to POST /api/v1/orders/{order_id}/complete...")

    barrier = threading.Barrier(NUM_CONCURRENT_USERS)

    with concurrent.futures.ThreadPoolExecutor(max_workers=NUM_CONCURRENT_USERS) as executor:
        futures = [
            executor.submit(try_complete_order, order_id, barrier)
            for _ in range(NUM_CONCURRENT_USERS)
        ]
        responses = [f.result() for f in futures]
        results = [r.status_code for r in responses]

    successes = results.count(200)
    conflicts = results.count(409)
    other_errors = len(results) - successes - conflicts

    print(f"📊 Results -> Success (200): {successes} | Conflicts (409): {conflicts} | Others: {other_errors}")

    assert successes == 1, (
        f"❌ Race Condition Detected! Multiple users completed the same order! Successes={successes}"
    )
    assert conflicts == NUM_CONCURRENT_USERS - 1, (
        f"❌ Expected {NUM_CONCURRENT_USERS - 1} conflicts (HTTP 409), but got {conflicts}."
    )

    db_pg.refresh(order)
    assert order.status == "completed", "Order status must be 'completed'"

    print("✅ [TEST 2 PASSED] Flawless Pessimistic Row Lock (SELECT FOR UPDATE) verified!")
    print(f"   • Exactly 1 request completed the order (HTTP 200)")
    print(f"   • Exactly {NUM_CONCURRENT_USERS - 1} requests received HTTP 409 Conflict")
    print(f"   • Order status in PostgreSQL is 'completed'")


try:
    import pytest

    @pytest.fixture(scope="session", autouse=True)
    def auto_server():
        server = ensure_server_running()
        yield
        if server:
            server.should_exit = True
except ImportError:
    pass


def test_concurrency_race_condition():
    """Pytest entrypoint for Test 1."""
    run_race_condition_test()


def test_concurrency_pessimistic_lock():
    """Pytest entrypoint for Test 2."""
    run_pessimistic_lock_order_complete_test()


if __name__ == "__main__":
    server_instance = ensure_server_running()
    try:
        run_race_condition_test()
        run_pessimistic_lock_order_complete_test()
        print("\n" + "=" * 70)
        print("🎉 ALL CONCURRENCY VERIFICATION TESTS PASSED SUCCESSFULLY! (100% PASS)")
        print("=" * 70)
    finally:
        if server_instance:
            server_instance.should_exit = True
