# switch_phase.py
import sys
from pathlib import Path

PHASE_FILE = Path(__file__).resolve().parent / ".migration_phase"

PHASE_DESCRIPTIONS = {
    1: "Phase 1: EXPAND (Legacy API reads/writes `name` only)",
    2: "Phase 2: DUAL WRITE (API writes to BOTH `name` and `first_name`+`last_name`, reads `name`)",
    3: "Phase 3: BACKFILL (Dual Write active while background backfill runs)",
    4: "Phase 4: SWITCH READ (API reads/writes EXCLUSIVELY using `first_name`+`last_name`)",
    5: "Phase 5: CONTRACT (Legacy `name` column dropped safely)",
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("1", "2", "3", "4", "5"):
        current = PHASE_FILE.read_text(encoding="utf-8").strip() if PHASE_FILE.exists() else "2"
        print(f"Current Phase: {current} -> {PHASE_DESCRIPTIONS.get(int(current), 'Unknown')}")
        print("Usage: python switch_phase.py <1|2|3|4|5>")
        sys.exit(1)

    phase = int(sys.argv[1])
    PHASE_FILE.write_text(str(phase), encoding="utf-8")
    print(f"✅ Switched API to {PHASE_DESCRIPTIONS[phase]}")


if __name__ == "__main__":
    main()
