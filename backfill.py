# backfill.py
import time
from database import SessionLocal
import models

def run_backfill(batch_size=100):
    db = SessionLocal()
    try:
        total_updated = 0
        while True:
            # ดึงข้อมูลที่ยังไม่ได้ migrate
            users = db.query(models.User).filter(
                models.User.first_name == None
            ).limit(batch_size).all()

            if not users:
                print("\n✅ Backfill completed 100%!")
                break

            for user in users:
                # แยกชื่อและนามสกุลจากคอลัมน์ name เดิม
                if user.name:
                    parts = user.name.split(" ", 1)
                    user.first_name = parts[0]
                    user.last_name = parts[1] if len(parts) > 1 else ""
                else:
                    user.first_name = "Unknown"
                    user.last_name = "Unknown"

            # บันทึกข้อมูลทีละ Batch
            db.commit()
            total_updated += len(users)
            print(f"🔄 Successfully updated {total_updated} records...", end="\r")
            
            # หน่วงเวลาเล็กน้อยเพื่อไม่ให้ Database ทำงานหนักเกินไป
            time.sleep(0.1)
            
    except Exception as e:
        print(f"\n❌ Error occurred: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    print("🚀 Starting Backfill process (migrating data to new columns)...")
    run_backfill()