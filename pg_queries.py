"""Checkpoint 3 PostgreSQL operations.

The caller owns the transaction: commit on success and rollback on every error.
Use PostgreSQL's default READ COMMITTED isolation and one Session per request.
No function here imports FastAPI or queries MongoDB.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session


class RecordNotFound(Exception):
    """The requested PostgreSQL record does not exist."""


class ConcurrentUpdate(Exception):
    """The operation is no longer valid for the locked record's current state."""


def lock_user(db: Session, user_id: int) -> None:
    """Lock before reading fields used by a read/modify/write operation.

    Select only the primary key so this works in every CP2 migration phase.
    The lock remains held until the caller commits or rolls back.
    """
    row = db.execute(
        text("SELECT id FROM public.users WHERE id = :id FOR UPDATE"),
        {"id": user_id},
    ).first()
    if row is None:
        raise RecordNotFound("User not found")


def complete_order(db: Session, order_id: int) -> dict:
    """Complete a pending order exactly once, without committing the transaction.

    Concurrent callers wait for the row lock, then inspect the latest status.
    This is an order-state operation, not inventory reservation or payment.
    """
    row = db.execute(
        text("SELECT id, status FROM public.orders WHERE id = :id FOR UPDATE"),
        {"id": order_id},
    ).mappings().first()
    if row is None:
        raise RecordNotFound("Order not found")
    if row["status"] != "pending":
        raise ConcurrentUpdate("Only a pending order can be completed")
    updated = db.execute(
        text(
            "UPDATE public.orders SET status = 'completed' WHERE id = :id "
            "RETURNING id, user_id, total_amount, status, created_at"
        ),
        {"id": order_id},
    ).mappings().one()
    return dict(updated)


# Page the parent rows BEFORE joining items so LIMIT never truncates an order.
# Explicit JSON fields avoid ORM lazy loading and omit item.created_at/order_id.
_ORDER_WITH_ITEMS = """
SELECT o.id, o.user_id, o.total_amount, o.status, o.created_at,
       COALESCE(
           jsonb_agg(
               jsonb_build_object(
                   'id', i.id, 'product_id', i.product_id,
                   'quantity', i.quantity, 'unit_price', i.unit_price
               ) ORDER BY i.id
           ) FILTER (WHERE i.id IS NOT NULL),
           '[]'::jsonb
       ) AS items
FROM selected_orders AS o
LEFT JOIN public.order_items AS i ON i.order_id = o.id
GROUP BY o.id, o.user_id, o.total_amount, o.status, o.created_at
ORDER BY o.id
"""


def list_orders(
    db: Session, *, user_id: int | None = None, after_id: int = 0, limit: int = 50
) -> list[dict]:
    """Return a bounded page with items in ONE SQL statement (no N+1).

    Use the last returned id as after_id for the next page. O(1) describes
    database round trips, not the amount of data processed or returned.
    """
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if after_id < 0:
        raise ValueError("after_id must be nonnegative")
    # Only this fixed SQL fragment is composed; all values remain bound params.
    user_filter = " AND user_id = :user_id" if user_id is not None else ""
    statement = text(
        "WITH selected_orders AS ("
        "SELECT id, user_id, total_amount, status, created_at FROM public.orders "
        "WHERE id > :after_id" + user_filter + " ORDER BY id LIMIT :limit) "
        + _ORDER_WITH_ITEMS
    )
    rows = db.execute(
        statement, {"after_id": after_id, "limit": limit, "user_id": user_id}
    ).mappings().all()
    return [dict(row) for row in rows]


def get_order(db: Session, order_id: int) -> dict:
    """Read a single order and all its items with the same explicit projection."""
    row = db.execute(
        text(
            "WITH selected_orders AS ("
            "SELECT id, user_id, total_amount, status, created_at "
            "FROM public.orders WHERE id = :id) " + _ORDER_WITH_ITEMS
        ),
        {"id": order_id},
    ).mappings().first()
    if row is None:
        raise RecordNotFound("Order not found")
    return dict(row)
