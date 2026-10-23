"""Checkpoint 3 — คนที่ 3: HTTP status mapping and N+1 checks through the REST API.

Real PostgreSQL + MongoDB integration tests; never touch the app's main_db.

    PG_TEST_ADMIN_URL  disposable PostgreSQL with CREATEDB permission
    MONGO_TEST_URL     MongoDB server (a random database is created and dropped)
"""

import importlib
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import pytest
from pymongo import MongoClient, monitoring
from pymongo.errors import OperationFailure
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

NUM_USERS = 20


class MongoCommandCounter(monitoring.CommandListener):
    def __init__(self):
        self.commands = []

    def started(self, event):
        self.commands.append(event.command_name)

    def succeeded(self, event):
        pass

    def failed(self, event):
        pass

    def reads(self):
        return [c for c in self.commands if c in ("find", "aggregate", "getMore", "count")]


@pytest.fixture(scope="module")
def pg_engine():
    url = os.getenv("PG_TEST_ADMIN_URL")
    if not url:
        pytest.skip("Set PG_TEST_ADMIN_URL to run real PostgreSQL tests")
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    database_name = "cp3_api_test_" + uuid4().hex
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{database_name}"'))
    engine = create_engine(
        admin.url.set(database=database_name), pool_size=NUM_USERS + 5, max_overflow=0,
        connect_args={"options": "-c statement_timeout=15000"},
    )
    try:
        import models
        models.Base.metadata.create_all(engine)
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE "{database_name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="module")
def mongo():
    url = os.getenv("MONGO_TEST_URL")
    if not url:
        pytest.skip("Set MONGO_TEST_URL to run real MongoDB tests")
    counter = MongoCommandCounter()
    client = MongoClient(url, event_listeners=[counter], serverSelectionTimeoutMS=5000)
    database_name = "cp3_api_test_" + uuid4().hex
    try:
        yield client[database_name], counter
    finally:
        client.drop_database(database_name)
        client.close()


@pytest.fixture(scope="module")
def sql_log(pg_engine):
    statements = []

    @event.listens_for(pg_engine, "before_cursor_execute")
    def record(_conn, _cursor, statement, *_args):
        statements.append(statement)

    yield statements
    event.remove(pg_engine, "before_cursor_execute", record)


@pytest.fixture(scope="module")
def client(pg_engine, mongo):
    from fastapi.testclient import TestClient

    # main.py creates tables at import time; point that at the generated test DB.
    with patch("database.engine", pg_engine):
        main = importlib.import_module("main")
    mongo_db, _ = mongo
    import mongo_queries
    mongo_queries.ensure_indexes(mongo_db)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=pg_engine)

    def test_pg_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[main.get_pg_db] = test_pg_db
    main.app.dependency_overrides[main.get_mongo_db] = lambda: mongo_db
    # No `with TestClient(...)`: the lifespan would index the real main_db.
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


@pytest.fixture
def user_id(pg_engine):
    with pg_engine.begin() as conn:
        return conn.execute(text(
            "INSERT INTO public.users (name, first_name, last_name, email) "
            "VALUES ('Old Name', 'Old', 'Name', :email) RETURNING id"
        ), {"email": uuid4().hex + "@example.test"}).scalar_one()


def _create_product(client, name="Last iPhone"):
    response = client.post("/api/v1/products", json={
        "name": name, "category": "Electronics",
        "attributes": {"price": 9999, "condition": "Good", "serial": "x" * 1000},
    })
    assert response.status_code == 201
    return response.json()["product"]["_id"]


def _race(fn):
    """Run fn(i) in NUM_USERS threads released at the same instant; return status codes."""
    barrier = Barrier(NUM_USERS)

    def attempt(i):
        barrier.wait()
        return fn(i).status_code

    with ThreadPoolExecutor(max_workers=NUM_USERS) as executor:
        return list(executor.map(attempt, range(NUM_USERS)))


# ==========================================
# Concurrency -> 409 / 412
# ==========================================

def test_concurrent_product_updates_one_wins_rest_get_409(client):
    product_id = _create_product(client)

    codes = _race(lambda i: client.patch(
        f"/api/v1/products/{product_id}", json={"version": 1, "name": f"edit by user {i}"}
    ))

    assert codes.count(200) == 1, codes
    assert codes.count(409) == NUM_USERS - 1, codes
    assert client.get(f"/api/v1/products/{product_id}").json()["version"] == 2


def test_stale_if_match_returns_412_with_current_etag(client):
    product_id = _create_product(client)
    read = client.get(f"/api/v1/products/{product_id}")
    assert read.headers["ETag"] == '"1"'

    first = client.patch(f"/api/v1/products/{product_id}", json={"name": "A"},
                         headers={"If-Match": read.headers["ETag"]})
    assert first.status_code == 200 and first.headers["ETag"] == '"2"'

    stale = client.patch(f"/api/v1/products/{product_id}", json={"name": "B"},
                         headers={"If-Match": read.headers["ETag"]})
    assert stale.status_code == 412
    assert stale.headers["ETag"] == '"2"'
    assert stale.json()["detail"]["current_version"] == 2
    assert client.get(f"/api/v1/products/{product_id}").json()["name"] == "A"


def test_update_without_version_is_rejected(client):
    product_id = _create_product(client)
    assert client.patch(f"/api/v1/products/{product_id}", json={"name": "A"}).status_code == 428
    assert client.patch(f"/api/v1/products/{product_id}", json={"version": 1, "name": "A"},
                        headers={"If-Match": '"2"'}).status_code == 422
    assert client.patch(f"/api/v1/products/{product_id}", json={"name": "A"},
                        headers={"If-Match": "*"}).status_code == 400


def test_twenty_buyers_last_item_one_order_rest_409(client, pg_engine, mongo, user_id):
    mongo_db, _ = mongo
    product_id = _create_product(client)

    codes = _race(lambda _i: client.post("/api/v1/orders", json={
        "user_id": user_id, "items": [{"product_id": product_id}],
    }))

    assert codes.count(201) == 1, codes
    assert codes.count(409) == NUM_USERS - 1, codes
    with pg_engine.connect() as conn:
        orders = conn.execute(text(
            "SELECT count(*) FROM public.order_items WHERE product_id = :pid"
        ), {"pid": product_id}).scalar_one()
    assert orders == 1, "losing buyers must be rolled back in PostgreSQL"
    from bson import ObjectId
    product = mongo_db["products"].find_one({"_id": ObjectId(product_id)})
    assert product["status"] == "sold" and product["version"] == 2


def test_twenty_concurrent_order_completions_one_200_rest_409(client, pg_engine, user_id):
    with pg_engine.begin() as conn:
        order_id = conn.execute(text(
            "INSERT INTO public.orders (user_id, status) VALUES (:u, 'pending') RETURNING id"
        ), {"u": user_id}).scalar_one()

    codes = _race(lambda _i: client.post(f"/api/v1/orders/{order_id}/complete"))

    assert codes.count(200) == 1, codes
    assert codes.count(409) == NUM_USERS - 1, codes
    assert client.post("/api/v1/orders/999999999/complete").status_code == 404


def test_postgres_lock_timeout_maps_to_409_and_releases(client, pg_engine, user_id):
    import main

    TimeoutSession = sessionmaker(bind=pg_engine)

    def impatient_pg_db():
        db = TimeoutSession()
        db.execute(text("SET lock_timeout = '200ms'"))
        try:
            yield db
        finally:
            db.close()

    previous = main.app.dependency_overrides[main.get_pg_db]
    main.app.dependency_overrides[main.get_pg_db] = impatient_pg_db
    try:
        with pg_engine.connect() as holder:
            holder.execute(text("BEGIN"))
            holder.execute(text("SELECT id FROM public.users WHERE id = :id FOR UPDATE"),
                           {"id": user_id})
            blocked = client.put(f"/api/v1/users/{user_id}", json={"first_name": "Blocked"})
            holder.execute(text("ROLLBACK"))
        assert blocked.status_code == 409, blocked.text
        # 55P03 must not leave the row locked or the session dirty
        retry = client.put(f"/api/v1/users/{user_id}", json={"first_name": "After"})
        assert retry.status_code == 200, retry.text
    finally:
        main.app.dependency_overrides[main.get_pg_db] = previous


def test_only_real_concurrency_errors_become_409():
    import main

    assert main.is_mongo_concurrency_error(OperationFailure("WriteConflict", code=112))
    assert not main.is_mongo_concurrency_error(OperationFailure("bad query", code=2))

    class Orig:
        def __init__(self, pgcode):
            self.pgcode = pgcode

    class Wrapped(Exception):
        def __init__(self, pgcode):
            self.orig = Orig(pgcode)

    for code in ("40001", "40P01", "55P03"):
        assert main.is_pg_concurrency_error(Wrapped(code))
    for code in ("42601", "23505", None):  # syntax error, unique violation, no SQLSTATE
        assert not main.is_pg_concurrency_error(Wrapped(code))


# ==========================================
# Read-only endpoints: no N+1, projection
# ==========================================

def _insert_orders(pg_engine, user_id, count, items_each=3):
    with pg_engine.begin() as conn:
        for _ in range(count):
            order_id = conn.execute(text(
                "INSERT INTO public.orders (user_id, status) VALUES (:u, 'pending') RETURNING id"
            ), {"u": user_id}).scalar_one()
            for n in range(items_each):
                conn.execute(text(
                    "INSERT INTO public.order_items (order_id, product_id, quantity, unit_price) "
                    "VALUES (:o, :p, 1, 100)"
                ), {"o": order_id, "p": f"product-{n}"})


@pytest.mark.parametrize("count", [1, 10, 50])
def test_order_list_is_one_sql_statement_for_any_page_size(client, pg_engine, sql_log, user_id, count):
    _insert_orders(pg_engine, user_id, count)

    sql_log.clear()
    response = client.get("/api/v1/orders", params={"user_id": user_id, "limit": 100})

    assert response.status_code == 200
    body = response.json()
    assert len(body) == count and all(len(order["items"]) == 3 for order in body)
    selects = [s for s in sql_log if s.lstrip().upper().startswith(("WITH", "SELECT"))]
    assert len(selects) == 1, f"N+1 detected: {len(selects)} SELECTs for {count} orders"
    assert set(body[0]["items"][0]) == {"id", "product_id", "quantity", "unit_price"}


def test_single_order_is_one_sql_statement_and_404(client, pg_engine, sql_log, user_id):
    _insert_orders(pg_engine, user_id, 1)
    order_id = client.get("/api/v1/orders", params={"user_id": user_id}).json()[0]["id"]

    sql_log.clear()
    response = client.get(f"/api/v1/orders/{order_id}")
    assert response.status_code == 200 and len(response.json()["items"]) == 3
    assert len([s for s in sql_log if "order_items" in s]) == 1
    assert client.get("/api/v1/orders/999999999").status_code == 404


def test_product_reads_are_one_mongo_query_with_projection(client, mongo):
    mongo_db, counter = mongo
    mongo_db["products"].delete_many({})
    mongo_db["reviews"].delete_many({})
    product_ids = [_create_product(client, f"Item {i}") for i in range(10)]
    mongo_db["reviews"].insert_many([
        {"product_id": pid, "user_id": 1, "rating": r, "comment": "ok"}
        for pid in product_ids for r in (4, 5)
    ])

    counter.commands.clear()
    products = client.get("/api/v1/products").json()
    assert counter.reads() == ["find"]
    assert len(products) == 10
    assert set(products[0]) == {"_id", "name", "category", "status", "attributes", "version"}
    assert set(products[0]["attributes"]) == {"price", "condition"}

    counter.commands.clear()
    summaries = client.get("/api/v1/products/summary").json()
    assert counter.reads() == ["aggregate"], f"N+1 detected: {counter.reads()}"
    assert all(s["review_count"] == 2 and s["avg_rating"] == 4.5 for s in summaries)

    counter.commands.clear()
    reviews = client.get(f"/api/v1/products/{product_ids[0]}/reviews").json()
    assert counter.reads() == ["find"] and len(reviews) == 2

    assert client.get("/api/v1/products/not-an-id").status_code == 422
    assert client.get("/api/v1/products/0123456789abcdef01234567").status_code == 404


def test_create_order_reads_all_products_in_one_query(client, mongo, user_id):
    _, counter = mongo
    product_ids = [_create_product(client, f"Bundle {i}") for i in range(5)]

    counter.commands.clear()
    response = client.post("/api/v1/orders", json={
        "user_id": user_id, "items": [{"product_id": pid} for pid in product_ids],
    })

    assert response.status_code == 201, response.text
    assert counter.reads() == ["find"], f"per-item product lookup: {counter.reads()}"
    assert counter.commands.count("findAndModify") == 5  # one OCC write per sold product
