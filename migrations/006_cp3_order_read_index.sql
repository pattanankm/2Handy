-- Run outside a transaction (psql without --single-transaction).
-- Existing databases need this; metadata.create_all does not add indexes to
-- existing tables. Fresh databases get the same index from models.py.
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_orders_user_id_id
    ON public.orders (user_id, id);

-- The existing ix_order_items_order_id index supports the items join.
