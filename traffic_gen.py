# traffic_gen.py
import requests
import time
import threading
import random

BASE_URL = "http://127.0.0.1:8000/api/v1"

# เก็บสถิติ
stats = {"success": 0, "errors": 0}

def send_continuous_traffic():
    while True:
        try:
            # จำลองการยิง GET (สุ่มดึงข้อมูล User)
            user_id = random.randint(1, 100)
            res = requests.get(f"{BASE_URL}/users/{user_id}", timeout=0.5) # กำหนด timeout 500ms
            
            if res.status_code in (200, 201):
                stats["success"] += 1
            else:
                stats["errors"] += 1
                
        except Exception as e:
            stats["errors"] += 1
            
        # พิมพ์สถิติแสดงผลบนหน้าจอ
        print(f"Traffic Stats -> Success: {stats['success']} | Errors: {stats['errors']}", end="\r")
        time.sleep(0.05) # หน่วงเวลาเล็กน้อยเพื่อให้ได้เรทประมาณ 20 req/sec ต่อ 1 Thread

if __name__ == "__main__":
    print("🚀 Starting Traffic Generator (Target: >20 req/sec)...")
    print("Press Ctrl+C to stop.")
    
    # รันหลาย Thread เพื่อให้ได้ Load มากพอ
    threads = []
    for _ in range(3): # ใช้ 3 Threads ทำงานพร้อมกัน
        t = threading.Thread(target=send_continuous_traffic, daemon=True)
        t.start()
        threads.append(t)
        
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n🛑 Traffic Generator stopped.")