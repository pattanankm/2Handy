# migrations/003_backfill.py
"""
Phase 3 (BACKFILL) Migration Script
Runs the batched background backfill job with Cursor Pagination and Checkpointing
to migrate legacy `users.name` records into `users.first_name` and `users.last_name`.
"""
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from main import set_current_phase  # noqa: E402
from backfill import run_backfill  # noqa: E402


def main():
    reset_flag = "--reset" in sys.argv
    set_current_phase(3)
    print("🚀 Phase 3 (BACKFILL): Starting batched migration with cursor pagination & checkpointing...")
    run_backfill(batch_size=100, reset_checkpoint=reset_flag)


if __name__ == "__main__":
    main()
