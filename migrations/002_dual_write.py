# migrations/002_dual_write.py
"""
Phase 2 (DUAL WRITE) Migration Script
Switches the live 2Handy Web API into Phase 2 (Dual Write) mode without downtime,
so all POST /api/v1/users and PUT /api/v1/users/{id} requests write to BOTH:
  - Legacy column: `name`
  - New columns: `first_name`, `last_name`
"""
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from main import set_current_phase  # noqa: E402


def main():
    set_current_phase(2)
    print("✅ Phase 2 (DUAL WRITE) enabled!")
    print("   -> Web API POST/PUT /api/v1/users now writes to BOTH `name` and (`first_name`, `last_name`).")
    print("   -> Web API GET /api/v1/users continues reading from legacy `name`.")


if __name__ == "__main__":
    main()
