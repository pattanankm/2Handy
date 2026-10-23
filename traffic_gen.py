# traffic_gen.py
import random
import threading
import time
import uuid
from collections import deque
import requests

BASE_URL = "http://127.0.0.1:8000/api/v1"

FIRST_NAMES = ["Somchai", "Somsri", "Ananda", "Nattapong", "Pattanan", "Prempracha", "Peeratchai", "Apiluk"]
LAST_NAMES = ["Jaidee", "Siriworakul", "Numchai", "Matunboon", "Bualuang", "Sukkasem", "Charoen"]

stats_lock = threading.Lock()
stats = {
    "get_ok": 0,
    "post_ok": 0,
    "http_5xx": 0,
    "data_err": 0,
    "other_err": 0,
}
latencies_ms = deque(maxlen=1000)
request_timestamps = deque(maxlen=1000)
known_max_user_id = 1000


def _validate_user_payload(payload: dict) -> bool:
    """Verify 0 data corruption / 0 missing fields on user response."""
    if not isinstance(payload, dict):
        return False
    user_obj = payload.get("user", payload)
    if not isinstance(user_obj, dict):
        return False
    if "id" not in user_obj or "email" not in user_obj:
        return False
    # At least one of legacy name, full_name, or first_name must be a non-empty string
    has_name = any(
        isinstance(user_obj.get(field), str) and user_obj.get(field).strip() != ""
        for field in ("name", "full_name", "first_name")
    )
    return has_name


def send_continuous_traffic(worker_id: int):
    global known_max_user_id
    session = requests.Session()

    while True:
        start_t = time.perf_counter()
        is_post = random.random() < 0.25  # 75% GET, 25% POST (continuous read + write traffic)

        try:
            if is_post:
                first = random.choice(FIRST_NAMES)
                last = random.choice(LAST_NAMES)
                full_name = f"{first} {last}"
                unique_suffix = uuid.uuid4().hex[:10]
                body = {
                    "name": full_name,
                    "full_name": full_name,
                    "first_name": first,
                    "last_name": last,
                    "email": f"load_{worker_id}_{unique_suffix}@2handy.test",
                }
                res = session.post(f"{BASE_URL}/users", json=body, timeout=0.5)
            else:
                user_id = random.randint(1, max(100, known_max_user_id))
                res = session.get(f"{BASE_URL}/users/{user_id}", timeout=0.5)

            elapsed_ms = (time.perf_counter() - start_t) * 1000.0
            now = time.time()

            with stats_lock:
                latencies_ms.append(elapsed_ms)
                request_timestamps.append(now)

                if res.status_code in (200, 201):
                    data = res.json()
                    if _validate_user_payload(data):
                        if is_post:
                            stats["post_ok"] += 1
                            created_id = data.get("user", {}).get("id")
                            if isinstance(created_id, int) and created_id > known_max_user_id:
                                known_max_user_id = created_id
                        else:
                            stats["get_ok"] += 1
                    else:
                        stats["data_err"] += 1
                elif res.status_code >= 500:
                    stats["http_5xx"] += 1
                else:
                    stats["other_err"] += 1

        except Exception:
            elapsed_ms = (time.perf_counter() - start_t) * 1000.0
            with stats_lock:
                latencies_ms.append(elapsed_ms)
                request_timestamps.append(time.time())
                stats["other_err"] += 1

        # 3 threads * ~10 req/s each = ~30 req/s (>20 req/sec requirement)
        time.sleep(0.08)


def compute_metrics():
    with stats_lock:
        now = time.time()
        recent = [t for t in request_timestamps if now - t <= 5.0]
        rps = len(recent) / 5.0 if recent else 0.0

        if latencies_ms:
            sorted_lat = sorted(latencies_ms)
            p99_idx = min(len(sorted_lat) - 1, int(len(sorted_lat) * 0.99))
            p99_ms = sorted_lat[p99_idx]
        else:
            p99_ms = 0.0

        snapshot = dict(stats)
    return rps, p99_ms, snapshot


if __name__ == "__main__":
    print("🚀 Starting Concurrent Traffic Generator (Target: >20 req/sec, POST + GET)...")
    print("🎯 Criteria: 0 HTTP 5xx | 0 Data Corruption/Missing Field | p99 < 500ms")
    print("Press Ctrl+C to stop.\n")

    threads = []
    for wid in range(3):
        t = threading.Thread(target=send_continuous_traffic, args=(wid,), daemon=True)
        t.start()
        threads.append(t)

    try:
        while True:
            time.sleep(0.25)
            rps, p99_ms, s = compute_metrics()
            total_ok = s["get_ok"] + s["post_ok"]
            print(
                f"Traffic -> Rate: {rps:5.1f} req/s | "
                f"Success: {total_ok} (GET:{s['get_ok']} POST:{s['post_ok']}) | "
                f"5xx: {s['http_5xx']} | DataErr: {s['data_err']} | OtherErr: {s['other_err']} | "
                f"p99: {p99_ms:5.1f}ms   ",
                end="\r",
            )
    except KeyboardInterrupt:
        rps, p99_ms, s = compute_metrics()
        total_ok = s["get_ok"] + s["post_ok"]
        print(
            f"\n\n🛑 Traffic Generator stopped.\n"
            f"Final Summary -> Success: {total_ok} (GET:{s['get_ok']}, POST:{s['post_ok']}) | "
            f"HTTP 5xx: {s['http_5xx']} | Data Corruption/Missing: {s['data_err']} | "
            f"p99 Latency: {p99_ms:.1f}ms"
        )