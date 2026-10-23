"""Real PostgreSQL integration tests; never seed or truncate an existing DB.

Set PG_TEST_ADMIN_URL to a disposable PostgreSQL instance. The account must
have CREATEDB permission. Each test run creates and drops its own database.
"""

import importlib
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from pg_queries import ConcurrentUpdate, RecordNotFound, complete_order, get_order, list_orders


@pytest.fixture(scope="module")
def pg_engine():
    url = os.getenv("PG_TEST_ADMIN_URL")
    if not url:
        pytest.skip("Set PG_TEST_ADMIN_URL to run real PostgreSQL tests")
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    database_name = "cp3_test_" + uuid4().hex
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{database_name}"'))
    engine = create_engine(
        admin.url.set(database=database_name), pool_size=20, max_overflow=0,
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
def api(pg_engine):
    # main.py creates tables at import time. Redirect that operation only to
    # the generated test DB, without connecting to the app's normal database.
    with patch("database.engine", pg_engine):
        yield importlib.import_module("main")


@pytest.fixture
def sample(pg_engine):
    with pg_engine.begin() as conn:
        user_id = conn.execute(text(
            "INSERT INTO public.users (name, first_name, last_name, email) "
            "VALUES ('Old Name', 'Old', 'Name', :email) RETURNING id"
        ), {"email": uuid4().hex + "@example.test"}).scalar_one()
        ids = []
        for index in range(3):
            order_id = conn.execute(text(
                "INSERT INTO public.orders (user_id, status, total_amount) "
                "VALUES (:user_id, 'pending', :amount) RETURNING id"
            ), {"user_id": user_id, "amount": 300 if index == 0 else 0}).scalar_one()
            ids.append(order_id)
        conn.execute(text(
            "INSERT INTO public.order_items (order_id, product_id, quantity, unit_price) "
            "VALUES (:id, 'product-a', 1, 100), (:id, 'product-b', 2, 100)"
        ), {"id": ids[0]})
    return user_id, ids


def test_twenty_concurrent_completions_have_one_winner(pg_engine, sample):
    _, ids = sample
    barrier = Barrier(20)

    def attempt(_):
        with Session(pg_engine) as db:
            # Acquire independent DB connections before synchronizing clients.
            db.execute(text("SELECT 1"))
            barrier.wait(timeout=15)
            try:
                result = complete_order(db, ids[0])
                db.commit()
                assert result["status"] == "completed"
                return "success"
            except ConcurrentUpdate:
                db.rollback()
                return "conflict"

    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(executor.map(attempt, range(20)))
    assert results.count("success") == 1
    assert results.count("conflict") == 19
    with Session(pg_engine) as db:
        assert get_order(db, ids[0])["status"] == "completed"


def test_completion_rollback_releases_state_and_lock(pg_engine, sample):
    _, ids = sample
    with Session(pg_engine) as db:
        complete_order(db, ids[0])
        db.rollback()
    with Session(pg_engine) as db:
        assert complete_order(db, ids[0])["status"] == "completed"
        db.commit()
        with pytest.raises(ConcurrentUpdate):
            complete_order(db, ids[0])
        db.rollback()
        with pytest.raises(RecordNotFound):
            complete_order(db, -1)
        db.rollback()
        with pytest.raises(RecordNotFound):
            get_order(db, -1)


def test_order_reads_are_one_query_and_preserve_whole_orders(pg_engine, sample):
    user_id, ids = sample
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(pg_engine, "before_cursor_execute", record)
    try:
        with Session(pg_engine) as db:
            for size in (1, 100):
                statements.clear()
                rows = list_orders(db, user_id=user_id, limit=size)
                assert len(statements) == 1
                assert len(rows) == (1 if size == 1 else 3)
                assert len(rows[0]["items"]) == 2
                assert set(rows[0]) == {
                    "id", "user_id", "total_amount", "status", "created_at", "items"
                }
                assert set(rows[0]["items"][0]) == {
                    "id", "product_id", "quantity", "unit_price"
                }
            assert rows[1]["items"] == []
            statements.clear()
            assert get_order(db, ids[0]) == rows[0]
            assert len(statements) == 1
            next_page = list_orders(db, user_id=user_id, after_id=ids[0])
            assert [row["id"] for row in next_page] == ids[1:]
            assert list_orders(db, user_id=-1) == []
            assert all(row["id"] > ids[0] for row in list_orders(db, after_id=ids[0]))
            for limit in (0, 101):
                with pytest.raises(ValueError):
                    list_orders(db, limit=limit)
            with pytest.raises(ValueError):
                list_orders(db, after_id=-1)
    finally:
        event.remove(pg_engine, "before_cursor_execute", record)


def test_query_count_stays_constant_for_one_ten_and_one_hundred_orders(pg_engine, sample):
    user_id, _ = sample
    with pg_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO public.orders (user_id, status, total_amount) "
            "SELECT :user_id, 'pending', 0 FROM generate_series(1, 97)"
        ), {"user_id": user_id})
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(pg_engine, "before_cursor_execute", record)
    try:
        with Session(pg_engine) as db:
            for size in (1, 10, 100):
                statements.clear()
                rows = list_orders(db, user_id=user_id, limit=size)
                assert len(rows) == size
                assert len(statements) == 1
    finally:
        event.remove(pg_engine, "before_cursor_execute", record)


def test_backfill_holds_row_lock_until_batch_commit(pg_engine, sample, monkeypatch, tmp_path):
    import backfill

    user_id, _ = sample
    with pg_engine.begin() as conn:
        conn.execute(text(
            "UPDATE public.users SET first_name = NULL, last_name = NULL WHERE id = :id"
        ), {"id": user_id})
    monkeypatch.setattr(backfill, "SessionLocal", sessionmaker(bind=pg_engine))
    monkeypatch.setattr(backfill, "CHECKPOINT_FILE", tmp_path / "checkpoint.json")
    selected = Event()
    release = Event()

    def pause_after_read(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT id, name FROM public.users"):
            selected.set()
            assert release.wait(timeout=15)

    event.listen(pg_engine, "after_cursor_execute", pause_after_read)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(backfill.run_backfill, batch_size=100)
            try:
                assert selected.wait(timeout=15)
                with pg_engine.connect() as conn:
                    with pytest.raises(OperationalError) as error:
                        conn.execute(text(
                            "SELECT id FROM public.users WHERE id = :id FOR UPDATE NOWAIT"
                        ), {"id": user_id})
                    assert error.value.orig.pgcode == "55P03"
                    conn.rollback()
            finally:
                release.set()
            future.result(timeout=15)
        with pg_engine.connect() as conn:
            row = conn.execute(text(
                "SELECT first_name, last_name FROM public.users WHERE id = :id"
            ), {"id": user_id}).one()
            assert tuple(row) == ("Old", "Name")
    finally:
        release.set()
        event.remove(pg_engine, "after_cursor_execute", pause_after_read)


def test_index_migration_on_existing_tables_is_repeatable(pg_engine):
    migration = (Path(__file__).resolve().parents[1] /
                 "migrations/006_cp3_order_read_index.sql").read_text(encoding="utf-8")
    with pg_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("DROP INDEX public.ix_orders_user_id_id"))
        conn.execute(text(migration))
        conn.execute(text(migration))
        definition = conn.execute(text(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' "
            "AND indexname = 'ix_orders_user_id_id'"
        )).scalar_one()
        assert "(user_id, id)" in definition


@pytest.mark.parametrize("phase", [1, 2, 3, 4, 5])
def test_user_updates_preserve_other_concurrent_fields(pg_engine, api, sample, monkeypatch, phase):
    user_id, _ = sample
    monkeypatch.setattr(api, "get_current_phase", lambda: phase)
    barrier = Barrier(2)
    email = uuid4().hex + "@example.test"

    def update(payload):
        with Session(pg_engine) as db:
            db.execute(text("SELECT 1"))
            barrier.wait(timeout=15)
            return api.update_user(user_id, api.UserUpdate(**payload), db)

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(update, [{"name": "New Person"}, {"email": email}]))
    with Session(pg_engine) as db:
        result = api.get_user(user_id, db)
        assert result["name"] == "New Person"
        assert result["email"] == email


def test_user_validation_rolls_back_and_releases_lock(pg_engine, api, sample, monkeypatch):
    user_id, _ = sample
    monkeypatch.setattr(api, "get_current_phase", lambda: 2)
    with Session(pg_engine) as db:
        with pytest.raises(api.HTTPException) as error:
            api.update_user(user_id, api.UserUpdate(name=" "), db)
        assert error.value.status_code == 422
        assert not db.in_transaction()
    with Session(pg_engine) as db:
        assert api.update_user(user_id, api.UserUpdate(name="Valid Person"), db)["name"] == "Valid Person"
        with pytest.raises(api.HTTPException) as error:
            api.update_user(-1, api.UserUpdate(name="Missing Person"), db)
        assert error.value.status_code == 404
        assert not db.in_transaction()


@pytest.mark.parametrize("phase, columns", [(1, "first_name, last_name"), (5, "name")])
def test_user_lock_works_with_actual_legacy_and_contract_schemas(pg_engine, api, sample, monkeypatch, phase, columns):
    user_id, _ = sample
    monkeypatch.setattr(api, "get_current_phase", lambda: phase)
    # PostgreSQL DDL is transactional. The endpoint's commit releases a
    # savepoint; the outer rollback restores all dropped columns afterward.
    with pg_engine.connect() as conn:
        outer = conn.begin()
        try:
            for column in columns.split(", "):
                conn.execute(text(f"ALTER TABLE public.users DROP COLUMN {column}"))
            with Session(bind=conn, join_transaction_mode="create_savepoint") as db:
                result = api.update_user(user_id, api.UserUpdate(name="Schema Compatible"), db)
                assert result["name"] == "Schema Compatible"
        finally:
            outer.rollback()
