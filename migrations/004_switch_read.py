# migrations/004_switch_read.py
"""
Phase 4 (SWITCH READ) Migration Script
Switches the live 2Handy Web API into Phase 4 (Switch Read) mode without downtime,
so GET/POST/PUT /api/v1/users endpoints read and write EXCLUSIVELY using the new
`first_name` and `last_name` columns, completely decoupling the API from `users.name`.
"""
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from main import set_current_phase  # noqa: E402


def main():
    set_current_phase(4)
    print("✅ Phase 4 (SWITCH READ) enabled!")
    print("   -> Web API GET /api/v1/users now reads EXCLUSIVELY from `first_name` and `last_name`.")
    print("   -> Web API is now 100% decoupled from legacy `users.name` (ready for Phase 5 Contract).")


if __name__ == "__main__":
    main()
