# backfill.py
import datetime
import json
import sys
import time
from pathlib import Path
from typing import Tuple
from sqlalchemy import text
from database import SessionLocal

CHECKPOINT_FILE = Path(__file__).resolve().parent / "backfill_checkpoint.json"


def split_legacy_name(raw_name: str) -> Tuple[str, str]:
    """
    Normalize whitespace and split at the first space so that:
      concat_ws(' ', first_name, NULLIF(last_name, ''))
    matches regexp_replace(btrim(name), '\\s+', ' ', 'g') in verification SQL.
    """
    normalized = " ".join((raw_name or "").split())
    if not normalized:
        return "Unknown", "Unknown"
    first_name, separator, last_name = normalized.partition(" ")
    return first_name, last_name if separator else ""


def load_checkpoint(reset: bool = False) -> dict:
    if not reset and CHECKPOINT_FILE.exists():
        try:
            data = json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "last_id" in data:
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return {"last_id": 0, "total_updated": 0, "completed": False}


def save_checkpoint(last_id: int, total_updated: int, completed: bool = False) -> None:
    payload = {
        "last_id": last_id,
        "total_updated": total_updated,
        "completed": completed,
        "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    CHECKPOINT_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def run_backfill(batch_size: int = 100, reset_checkpoint: bool = False) -> None:
    db = SessionLocal()
    try:
        checkpoint = load_checkpoint(reset=reset_checkpoint)
        last_id = int(checkpoint.get("last_id", 0))
        total_updated = int(checkpoint.get("total_updated", 0))

        # Check if there are unmigrated rows with id <= last_id (e.g. after re-seeding)
        min_unmigrated_id = db.execute(
            text(
                "SELECT MIN(id) FROM public.users "
                "WHERE NULLIF(btrim(first_name), '') IS NULL"
            )
        ).scalar()

        if min_unmigrated_id is not None and min_unmigrated_id <= last_id:
            print(
                f"ℹ️ Detected un-migrated records starting at id={min_unmigrated_id} "
                f"(checkpoint was last_id={last_id}). Resetting cursor to 0."
            )
            last_id = 0
            total_updated = 0

        print(
            f"📌 Resuming from checkpoint: last_id={last_id}, "
            f"batch_size={batch_size} (Cursor Pagination enabled)"
        )

        while True:
            # Cursor pagination using primary key index (id > :last_id)
            # Avoids full-table locks and OFFSET scan overhead on large tables
            rows = db.execute(
                text(
                    "SELECT id, name "
                    "FROM public.users "
                    "WHERE id > :last_id "
                    "  AND NULLIF(btrim(first_name), '') IS NULL "
                    "ORDER BY id ASC "
                    "LIMIT :batch_size"
                ),
                {"last_id": last_id, "batch_size": batch_size},
            ).mappings().all()

            if not rows:
                # Final safety check for any remaining un-migrated rows
                remaining = db.execute(
                    text(
                        "SELECT COUNT(*) FROM public.users "
                        "WHERE NULLIF(btrim(first_name), '') IS NULL"
                    )
                ).scalar()
                if remaining and remaining > 0:
                    last_id = 0
                    continue

                save_checkpoint(last_id=last_id, total_updated=total_updated, completed=True)
                print(
                    f"\n✅ Backfill completed 100%! "
                    f"(Total migrated: {total_updated} records, last_id={last_id})"
                )
                break

            update_params = []
            for row in rows:
                first_name, last_name = split_legacy_name(row["name"])
                update_params.append(
                    {
                        "id": row["id"],
                        "first_name": first_name,
                        "last_name": last_name,
                    }
                )
                last_id = row["id"]

            # Commit each small batch and persist checkpoint
            db.execute(
                text(
                    "UPDATE public.users "
                    "SET first_name = :first_name, last_name = :last_name "
                    "WHERE id = :id"
                ),
                update_params,
            )
            db.commit()

            total_updated += len(rows)
            save_checkpoint(last_id=last_id, total_updated=total_updated, completed=False)
            print(
                f"🔄 Backfill progress -> Updated: {total_updated} records | "
                f"Checkpoint cursor id: {last_id}",
                end="\r",
            )

            # Throttle slightly between batches to keep DB latency low for live API traffic
            time.sleep(0.1)

    except Exception as e:
        print(f"\n❌ Error occurred during backfill: {e}")
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    reset_flag = "--reset" in sys.argv
    print("🚀 Starting Phase 3 Backfill process (Cursor Pagination + Checkpointing)...")
    run_backfill(batch_size=100, reset_checkpoint=reset_flag)