"""Backfill MongoDB product versions and create the CP3 query indexes."""

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

import mongo_queries  # noqa: E402
from database import mongo_db  # noqa: E402


def main() -> None:
    updated = mongo_queries.backfill_product_versions(mongo_db)
    mongo_queries.ensure_indexes(mongo_db)
    print(f"Added version=0 to {updated} legacy product(s)")
    print("Ensured products(category, status) and reviews(product_id, created_at) indexes")


if __name__ == "__main__":
    main()
